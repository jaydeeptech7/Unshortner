
import ipaddress
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse, urldefrag

import streamlit as st
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


st.set_page_config(
    page_title="Smart Shortlink Resolver",
    page_icon="🔗",
    layout="wide",
)

st.title("🔗 Smart Shortlink Resolver")
st.caption(
    "Inspect redirects, JavaScript navigation, timed pages, intermediate URLs, "
    "and possible destination links."
)

MAX_URL_LENGTH = 4096
MAX_HISTORY = 100
MAX_LINKS = 150
MAX_NAVIGATIONS = 30


def normalize_url(raw_url):
    raw_url = (raw_url or "").strip()

    if not raw_url or len(raw_url) > MAX_URL_LENGTH:
        raise ValueError("Enter a valid URL under 4096 characters.")

    if "://" not in raw_url:
        raw_url = "https://" + raw_url

    parsed = urlparse(raw_url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only HTTP and HTTPS URLs are supported.")

    if not parsed.hostname:
        raise ValueError("The URL must contain a hostname.")

    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded usernames or passwords are not supported.")

    return urldefrag(raw_url).url


def check_public_url(url):
    """
    Basic SSRF protection. This is not a complete security boundary:
    public deployments should additionally enforce network-level egress rules.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only public HTTP/HTTPS destinations are permitted.")

    host = parsed.hostname.lower().rstrip(".")

    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("Local hostnames are blocked.")

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            results = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = list({
                ipaddress.ip_address(item[4][0])
                for item in results
            })
        except (socket.gaierror, ValueError, OSError):
            raise ValueError(f"Could not resolve hostname: {host}")

    if not addresses:
        raise ValueError("No IP address found for hostname.")

    for address in addresses:
        if not address.is_global:
            raise ValueError(f"Blocked a non-public network address: {host}")


def detect_cloudflare_block(title, body_text, current_url=""):
    """
    Identify common Cloudflare block/challenge indicators.
    This detects a block; it does not bypass the challenge.
    """
    text = f"{title}\n{body_text}\n{current_url}".lower()

    indicators = [
        "sorry, you have been blocked",
        "you are unable to access",
        "attention required! | cloudflare",
        "error 1010",
        "error 1020",
        "cf-error-details",
        "cloudflare ray id",
        "checking your browser before accessing",
        "verify you are human",
        "enable javascript and cookies to continue",
    ]

    return any(indicator in text for indicator in indicators)


def ensure_browser_installed():
    from playwright.sync_api import sync_playwright as playwright

    with playwright() as p:
        if os.path.exists(p.chromium.executable_path):
            return

    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
        timeout=240,
    )

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "Unknown error")[-2000:]
        raise RuntimeError(
            "Chromium installation failed. Check the deployment logs.\n" + details
        )


def add_history(history, seen, url, source, details=""):
    if not url or len(history) >= MAX_HISTORY:
        return

    try:
        url = normalize_url(url)
    except ValueError:
        return

    if url in seen:
        return

    seen.add(url)
    history.append({
        "Step": len(history) + 1,
        "URL": url,
        "Detected by": source,
        "Details": details,
    })


def resolve_link(start_url, wait_seconds, scroll_count, timeout_seconds):
    history = []
    seen_urls = set()
    candidates = {}
    controls = []
    warnings = []
    blocked_requests = []
    navigation_count = 0

    ensure_browser_installed()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        context = browser.new_context(
            accept_downloads=False,
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        page.set_default_timeout(5000)

        # Apply a basic public-address check to every requested URL.
        def route_handler(route):
            request_url = route.request.url

            # Ignore browser-internal requests.
            if request_url.startswith(("data:", "blob:", "about:")):
                route.continue_()
                return

            try:
                check_public_url(request_url)
                route.continue_()
            except Exception as exc:
                if len(blocked_requests) < 50:
                    blocked_requests.append({
                        "URL": request_url,
                        "Reason": str(exc)[:300],
                    })
                route.abort("blockedbyclient")

        page.route("**/*", route_handler)

        def on_navigation(frame):
            nonlocal navigation_count

            if frame != page.main_frame:
                return

            navigation_count += 1
            add_history(
                history,
                seen_urls,
                frame.url,
                "Browser navigation",
                "Main page URL changed",
            )

        page.on("framenavigated", on_navigation)

        title = ""
        body_text = ""
        final_url = start_url

        try:
            check_public_url(start_url)
            add_history(
                history,
                seen_urls,
                start_url,
                "Input",
                "Original submitted URL",
            )

            try:
                page.goto(
                    start_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_seconds * 1000,
                )
            except PlaywrightTimeoutError:
                warnings.append(
                    "The initial page load timed out. The page may still have loaded partially."
                )
            except Exception as exc:
                warnings.append(f"Navigation error: {str(exc)[:400]}")

            # Observe JavaScript redirects and timer-based URL changes.
            deadline = time.monotonic() + wait_seconds

            while time.monotonic() < deadline:
                if navigation_count >= MAX_NAVIGATIONS:
                    warnings.append("Navigation limit reached.")
                    break

                add_history(
                    history,
                    seen_urls,
                    page.url,
                    "URL monitor",
                    "Observed during waiting period",
                )

                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break

            # Gradually scroll to reveal lazy-loaded links.
            for _ in range(scroll_count):
                try:
                    page.evaluate(
                        "() => window.scrollBy(0, Math.max(500, window.innerHeight * 0.8))"
                    )
                    page.wait_for_timeout(700)

                    add_history(
                        history,
                        seen_urls,
                        page.url,
                        "Scroll monitor",
                        "URL observed after scrolling",
                    )
                except Exception as exc:
                    warnings.append(f"Scrolling stopped: {str(exc)[:200]}")
                    break

            final_url = page.url

            try:
                title = page.title()
            except Exception:
                pass

            try:
                body_text = page.locator("body").inner_text(timeout=2000)[:6000]
            except Exception:
                body_text = ""

            # Detect common block pages before presenting links as useful results.
            cloudflare_blocked = detect_cloudflare_block(
                title,
                body_text,
                final_url,
            )

            if cloudflare_blocked:
                warnings.append(
                    "Cloudflare block or verification page detected. "
                    "Open the URL in your regular browser and complete any "
                    "legitimate verification manually. This app does not bypass "
                    "Cloudflare protections."
                )

            # Extract rendered links and common redirect hints.
            try:
                extracted = page.evaluate(
                    """() => {
                        const results = [];

                        const add = (url, text, type) => {
                            if (url) {
                                results.push({
                                    url: String(url),
                                    text: String(text || '').trim().slice(0, 250),
                                    type
                                });
                            }
                        };

                        for (const a of document.querySelectorAll('a[href]')) {
                            add(
                                a.href,
                                a.innerText || a.getAttribute('aria-label') || '',
                                'Link'
                            );
                        }

                        for (const el of document.querySelectorAll(
                            'button, input[type="button"], input[type="submit"], [role="button"]'
                        )) {
                            const text = (
                                el.innerText ||
                                el.value ||
                                el.getAttribute('aria-label') ||
                                ''
                            ).trim();

                            if (/continue|next|proceed|download|skip|unlock|go to link/i.test(text)) {
                                results.push({
                                    url: '',
                                    text: text.slice(0, 250),
                                    type: 'Possible control'
                                });
                            }
                        }

                        const refresh = document.querySelector(
                            'meta[http-equiv="refresh" i]'
                        );

                        if (refresh) {
                            const match = (refresh.content || '').match(
                                /url\\s*=\\s*['"]?([^'"]+)/i
                            );

                            if (match) {
                                add(match[1], 'Meta refresh', 'Meta refresh');
                            }
                        }

                        const canonical = document.querySelector(
                            'link[rel="canonical"]'
                        );

                        if (canonical) {
                            add(canonical.href, 'Canonical URL', 'Canonical');
                        }

                        return results.slice(0, 500);
                    }"""
                )
            except Exception as exc:
                extracted = []
                warnings.append(f"Could not extract page links: {str(exc)[:250]}")

            for item in extracted:
                kind = item.get("type", "Link")
                text = item.get("text", "")
                raw_url = item.get("url", "")

                if kind == "Possible control":
                    if text and text not in controls and len(controls) < 50:
                        controls.append(text)
                    continue

                try:
                    candidate_url = normalize_url(raw_url)
                    check_public_url(candidate_url)
                except ValueError:
                    continue

                if candidate_url not in candidates and len(candidates) < MAX_LINKS:
                    candidates[candidate_url] = {
                        "URL": candidate_url,
                        "Link text": text or "(no text)",
                        "Type": kind,
                    }

            add_history(
                history,
                seen_urls,
                final_url,
                "Final observation",
                "Last observed URL",
            )

        finally:
            context.close()
            browser.close()

    page_indicators = []

    if detect_cloudflare_block(title, body_text, final_url):
        page_indicators.append("Cloudflare block or verification detected")

    lowered_text = body_text.lower()

    if "captcha" in lowered_text or "verify you are human" in lowered_text:
        page_indicators.append("Possible CAPTCHA or human-verification step")

    if "javascript is required" in lowered_text:
        page_indicators.append("Page may require JavaScript")

    if controls:
        page_indicators.append("Possible Continue/Next controls detected")

    if not candidates:
        page_indicators.append("No candidate destination links extracted")

    return {
        "final_url": final_url,
        "title": title,
        "history": history,
        "links": list(candidates.values()),
        "controls": controls,
        "warnings": warnings,
        "blocked_requests": blocked_requests,
        "page_indicators": page_indicators,
        "body_text": body_text,
    }


with st.sidebar:
    st.header("Resolution settings")

    wait_seconds = st.slider(
        "Wait for JavaScript / timers",
        min_value=0,
        max_value=30,
        value=15,
    )

    scroll_count = st.slider(
        "Scroll passes",
        min_value=0,
        max_value=8,
        value=3,
    )

    timeout_seconds = st.slider(
        "Page-load timeout (seconds)",
        min_value=10,
        max_value=60,
        value=30,
    )

with st.form("resolver_form"):
    submitted_url = st.text_input(
        "Short URL",
        placeholder="https://arolinks.com/flzphw",
    )
    submitted = st.form_submit_button("Resolve link", type="primary")

if submitted:
    try:
        start_url = normalize_url(submitted_url)

        with st.spinner("Opening link and observing navigation..."):
            result = resolve_link(
                start_url,
                wait_seconds,
                scroll_count,
                timeout_seconds,
            )

        st.session_state["resolver_result"] = result

    except Exception as exc:
        st.error(f"Could not resolve URL: {exc}")

result = st.session_state.get("resolver_result")

if result:
    st.subheader("Resolution result")

    st.write("**Page title:**", result["title"] or "(not available)")
    st.write("**Last observed URL:**")
    st.code(result["final_url"], language=None)

    if result["final_url"]:
        st.link_button("Open last observed page", result["final_url"])

    for indicator in result["page_indicators"]:
        if "Cloudflare" in indicator or "CAPTCHA" in indicator:
            st.warning(indicator)
        else:
            st.info(indicator)

    if result["controls"]:
        st.subheader("Possible manual steps")
        st.write(
            "These controls were identified by their visible text. "
            "Review the page manually; this app does not click them."
        )

        for control in result["controls"]:
            st.write(f"- {control}")

    st.subheader("Intermediate URL history")

    if result["history"]:
        st.dataframe(
            result["history"],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No navigation history was captured.")

    st.subheader("Possible destination links")

    if result["links"]:
        st.dataframe(
            result["links"],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No candidate links were extracted.")

    if result["warnings"]:
        with st.expander("Warnings and navigation errors"):
            for warning in result["warnings"]:
                st.write(f"- {warning}")

    if result["blocked_requests"]:
        with st.expander("Blocked network requests"):
            st.dataframe(
                result["blocked_requests"],
                use_container_width=True,
                hide_index=True,
            )

    with st.expander("Page text preview"):
        st.text(result["body_text"] or "(No page text available)")

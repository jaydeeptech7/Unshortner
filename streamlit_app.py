
import ipaddress
import socket
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse, urldefrag

import streamlit as st
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


st.set_page_config(
    page_title="Smart Shortlink Resolver",
    page_icon="🔗",
    layout="wide",
)

st.title("🔗 Smart Shortlink Resolver")
st.caption(
    "Resolve redirects, observe JavaScript navigation, inspect intermediate "
    "pages, and find likely destination links."
)

# Keep the browser and network behavior conservative because this app may be
# deployed publicly and users can submit arbitrary URLs.
MAX_URL_LENGTH = 4096
MAX_NAVIGATIONS = 30
MAX_LINKS = 150
MAX_HISTORY = 100


def normalize_url(raw_url: str) -> str:
    """Normalize a user-supplied URL and require HTTP or HTTPS."""
    raw_url = (raw_url or "").strip()

    if not raw_url or len(raw_url) > MAX_URL_LENGTH:
        raise ValueError("Enter a valid URL under 4096 characters.")

    if "://" not in raw_url:
        raw_url = "https://" + raw_url

    parsed = urlparse(raw_url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only HTTP and HTTPS links are supported.")

    if not parsed.hostname:
        raise ValueError("The URL must contain a hostname.")

    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded usernames or passwords are not supported.")

    return urldefrag(raw_url).url


def check_public_url(url: str) -> None:
    """
    Reject local/private IPs and hostnames resolving to non-public IPs.
    This is a defensive check, not a complete production-grade SSRF boundary.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Blocked a non-HTTP destination.")

    host = parsed.hostname.lower().rstrip(".")

    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Blocked a local hostname.")

    try:
        literal_ip = ipaddress.ip_address(host)
        addresses = [literal_ip]
    except ValueError:
        try:
            results = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = [ipaddress.ip_address(item[4][0]) for item in results]
        except (socket.gaierror, ValueError, OSError):
            raise ValueError(f"Could not safely resolve hostname: {host}")

    if not addresses:
        raise ValueError(f"No address found for hostname: {host}")

    for address in addresses:
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError(f"Blocked a non-public network destination: {host}")


def ensure_browser_installed() -> None:
    """Install Playwright's Chromium browser on first run if necessary."""
    try:
        from playwright.sync_api import sync_playwright as _sp

        with _sp() as p:
            executable = p.chromium.executable_path
            import os

            if os.path.exists(executable):
                return
    except Exception:
        pass

    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
        timeout=240,
    )

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Unknown browser installation error")
        raise RuntimeError(
            "Chromium could not be installed. Check the app dependencies and "
            f"deployment logs.\n{message[-2000:]}"
        )


def safe_add(history, seen, url, source, note=""):
    """Add a URL to history once, with a short explanation."""
    if not url or len(history) >= MAX_HISTORY:
        return

    try:
        url = normalize_url(url)
    except ValueError:
        return

    if url not in seen:
        seen.add(url)
        history.append(
            {
                "Step": len(history) + 1,
                "URL": url,
                "Detected by": source,
                "Details": note,
            }
        )


def resolve_link(start_url, wait_seconds, scroll_count, timeout_seconds):
    history = []
    seen_urls = set()
    found_links = {}
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
            ignore_https_errors=False,
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

        # Inspect every outgoing request, including redirect destinations.
        def guard_route(route):
            request_url = route.request.url
            try:
                check_public_url(request_url)
                route.continue_()
            except Exception as exc:
                if len(blocked_requests) < 50:
                    blocked_requests.append(
                        {"URL": request_url, "Reason": str(exc)[:300]}
                    )
                route.abort("blockedbyclient")

        page.route("**/*", guard_route)

        def record_navigation(frame):
            nonlocal navigation_count
            if frame != page.main_frame:
                return
            navigation_count += 1
            safe_add(
                history,
                seen_urls,
                frame.url,
                "Browser navigation",
                "Main frame URL changed",
            )

        page.on("framenavigated", record_navigation)

        try:
            check_public_url(start_url)
            safe_add(history, seen_urls, start_url, "Input", "Submitted URL")

            try:
                page.goto(
                    start_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_seconds * 1000,
                )
            except PlaywrightTimeoutError:
                warnings.append(
                    "Initial page load timed out; the page may still be usable."
                )
            except Exception as exc:
                warnings.append(f"Initial navigation: {str(exc)[:400]}")

            # Wait in short intervals so URL changes can be recorded while
            # JavaScript timers or client-side navigation run.
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                try:
                    current_url = page.url
                    safe_add(
                        history,
                        seen_urls,
                        current_url,
                        "URL monitor",
                        "Observed during wait",
                    )
                except Exception:
                    pass

                if navigation_count >= MAX_NAVIGATIONS:
                    warnings.append("Navigation limit reached; monitoring stopped.")
                    break

                page.wait_for_timeout(500)

            # Scroll gradually to reveal lazy-loaded content. This does not
            # click buttons, submit forms, solve CAPTCHAs, or bypass gates.
            for _ in range(scroll_count):
                try:
                    page.evaluate(
                        "() => window.scrollBy(0, Math.max(500, window.innerHeight * 0.8))"
                    )
                    page.wait_for_timeout(700)
                    safe_add(
                        history,
                        seen_urls,
                        page.url,
                        "Scroll monitor",
                        "Observed after scrolling",
                    )
                except Exception as exc:
                    warnings.append(f"Scrolling stopped: {str(exc)[:200]}")
                    break

            # Collect links and likely destination URLs from the rendered DOM.
            try:
                extracted = page.evaluate(
                    """() => {
                        const out = [];
                        const add = (url, text, kind) => {
                            if (!url) return;
                            out.push({
                                url: String(url),
                                text: String(text || '').trim().slice(0, 250),
                                kind
                            });
                        };

                        for (const a of document.querySelectorAll('a[href]')) {
                            add(a.href, a.innerText || a.getAttribute('aria-label'), 'Link');
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
                                add('', text, 'Possible control');
                                out.push({
                                    url: '',
                                    text: text.slice(0, 250),
                                    kind: 'Possible control'
                                });
                            }
                        }

                        // Inspect common meta-refresh and canonical URL hints.
                        const refresh = document.querySelector('meta[http-equiv="refresh" i]');
                        if (refresh) {
                            const match = (refresh.content || '').match(/url\\s*=\\s*['"]?([^'"]+)/i);
                            if (match) add(match[1], 'Meta refresh', 'Meta refresh');
                        }

                        const canonical = document.querySelector('link[rel="canonical"]');
                        if (canonical) add(canonical.href, 'Canonical URL', 'Canonical');

                        return out.slice(0, 500);
                    }"""
                )
            except Exception as exc:
                extracted = []
                warnings.append(f"Could not inspect page links: {str(exc)[:250]}")

            for item in extracted:
                kind = item.get("kind", "Link")
                text = item.get("text", "")
                raw_url = item.get("url", "")

                if kind == "Possible control":
                    if text and text not in controls and len(controls) < 50:
                        controls.append(text)
                    continue

                if not raw_url:
                    continue

                candidate = urljoin(page.url, raw_url)
                try:
                    candidate = normalize_url(candidate)
                except ValueError:
                    continue

                # Avoid exposing obvious local/private URLs as candidates.
                try:
                    check_public_url(candidate)
                except Exception:
                    continue

                if candidate not in found_links and len(found_links) < MAX_LINKS:
                    found_links[candidate] = {
                        "URL": candidate,
                        "Link text": text or "(no text)",
                        "Type": kind,
                    }

            safe_add(
                history,
                seen_urls,
                page.url,
                "Final page",
                "URL at end of observation",
            )

            final_url = page.url
            try:
                title = page.title()
            except Exception:
                title = ""

            try:
                body_text = page.locator("body").inner_text(timeout=2000)
                body_text = body_text[:6000]
            except Exception:
                body_text = ""

            page_indicators = []
            if any(word in body_text.lower() for word in ["captcha", "verify you are human"]):
                page_indicators.append("Possible CAPTCHA or human-verification step")
            if any(word in body_text.lower() for word in ["enable javascript", "javascript is required"]):
                page_indicators.append("Page may require JavaScript")
            if controls:
                page_indicators.append("Possible Continue/Next controls detected")
            if not found_links:
                page_indicators.append("No public destination links extracted")

        finally:
            context.close()
            browser.close()

    return {
        "final_url": final_url,
        "title": title,
        "history": history,
        "links": list(found_links.values()),
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
        help="Waits for automatic navigation. Does not click Continue or solve CAPTCHA.",
    )
    scroll_count = st.slider(
        "Scroll passes",
        min_value=0,
        max_value=8,
        value=3,
        help="Scrolls to reveal content loaded as you move down the page.",
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
        placeholder="https://example.com/your-short-link",
    )
    submitted = st.form_submit_button("Resolve link", type="primary")

if submitted:
    try:
        start_url = normalize_url(submitted_url)
        with st.spinner("Opening link and observing redirects..."):
            result = resolve_link(
                start_url,
                wait_seconds=wait_seconds,
                scroll_count=scroll_count,
                timeout_seconds=timeout_seconds,
            )

        st.session_state["resolver_result"] = result

    except Exception as exc:
        st.error(f"Could not resolve this URL: {exc}")

result = st.session_state.get("resolver_result")

if result:
    st.subheader("Resolution result")

    st.write("**Page title:**", result.get("title") or "(not available)")
    st.write("**Last observed URL:**")
    st.code(result.get("final_url", ""), language=None)

    if result.get("final_url"):
        st.link_button("Open last observed page", result["final_url"])

    for indicator in result.get("page_indicators", []):
        st.warning(indicator)

    if result.get("controls"):
        st.subheader("Possible manual steps")
        st.write(
            "These controls were detected by their text. Review the page yourself; "
            "the resolver does not click them automatically."
        )
        for control in result["controls"]:
            st.write(f"- {control}")

    st.subheader("Intermediate URL history")
    if result.get("history"):
        st.dataframe(result["history"], use_container_width=True, hide_index=True)
    else:
        st.info("No navigation history was captured.")

    st.subheader("Possible destination links")
    if result.get("links"):
        st.dataframe(result["links"], use_container_width=True, hide_index=True)
    else:
        st.info("No candidate links were extracted from the rendered page.")

    if result.get("warnings"):
        with st.expander("Warnings"):
            for warning in result["warnings"]:
                st.write(f"- {warning}")

    if result.get("blocked_requests"):
        with st.expander("Blocked network requests"):
            st.dataframe(
                result["blocked_requests"],
                use_container_width=True,
                hide_index=True,
            )

    with st.expander("Page text preview"):
        st.text(result.get("body_text") or "(No text available)")

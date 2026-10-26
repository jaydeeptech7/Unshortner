
import ipaddress
import socket
import subprocess
import sys
import time
from urllib.parse import urldefrag, urlparse

import streamlit as st
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

st.set_page_config(page_title="Shortlink Resolver", page_icon="🔗", layout="wide")
st.title("🔗 Streamlit Shortlink Resolver")
st.caption("HTTP redirects • JavaScript navigation • Timers • Scroll • Link detection")


def normalize_url(value):
    value = (value or "").strip()
    if not value:
        raise ValueError("Enter a URL.")

    if "://" not in value:
        value = "https://" + value

    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only valid HTTP/HTTPS URLs are supported.")

    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not supported.")

    return urldefrag(value).url


def is_public_host(url):
    """Block localhost, private IPs, and hostnames resolving to non-public IPs."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname

        if not host:
            return False

        if host.lower() == "localhost" or host.lower().endswith(".localhost"):
            return False

        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            answers = socket.getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = [ipaddress.ip_address(item[4][0]) for item in answers]

        return bool(addresses) and all(ip.is_global for ip in addresses)

    except Exception:
        return False


@st.cache_resource
def ensure_browser():
    """Install Playwright Chromium on the Streamlit server if needed."""
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
        timeout=240,
        capture_output=True,
        text=True,
    )
    return True


def resolve_url(start_url, wait_seconds, scroll_passes):
    history = []
    responses = []
    seen = set()
    errors = []
    candidate_links = []
    candidate_controls = []
    final_url = start_url
    page_title = ""
    page_text = ""
    blocked = False

    def record(url, reason):
        if url and url not in seen:
            seen.add(url)
            history.append({
                "Step": len(history) + 1,
                "URL": url,
                "Observed by": reason,
            })

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            locale="en-US",
            accept_downloads=False,
        )
        page = context.new_page()
        page.set_default_timeout(8000)

        # Validate every requested URL, including redirects and subresources.
        def guard_route(route):
            request_url = route.request.url
            if urlparse(request_url).scheme not in ("http", "https"):
                return route.abort()

            if not is_public_host(request_url):
                return route.abort()

            return route.continue_()

        page.route("**/*", guard_route)

        def on_navigation(frame):
            if frame == page.main_frame:
                record(frame.url, "Main-frame navigation")

        def on_response(response):
            try:
                if response.request.is_navigation_request():
                    responses.append({
                        "Status": response.status,
                        "URL": response.url,
                        "Redirected from": (
                            response.request.redirected_from.url
                            if response.request.redirected_from else ""
                        ),
                    })
            except Exception:
                pass

        page.on("framenavigated", on_navigation)
        page.on("response", on_response)
        record(start_url, "Submitted URL")

        try:
            page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except PlaywrightTimeoutError:
            errors.append("Initial page load timed out; continuing inspection.")
        except Exception as exc:
            errors.append(f"Initial navigation: {str(exc)[:300]}")

        deadline = time.monotonic() + wait_seconds

        # Observe JavaScript redirects, meta refreshes, and timer navigation.
        # Scroll gradually to reveal lazy-loaded links.
        for index in range(scroll_passes + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                record(page.url, "URL monitor")

                if index < scroll_passes:
                    page.evaluate(
                        "() => window.scrollTo(0, document.body.scrollHeight)"
                    )

                pause = min(3, max(0.5, remaining))
                page.wait_for_timeout(int(pause * 1000))
                record(page.url, "After wait/scroll")

            except Exception as exc:
                errors.append(f"Page monitoring: {str(exc)[:250]}")
                break

        # Continue observing until the requested timer has elapsed.
        while time.monotonic() < deadline:
            try:
                record(page.url, "Timer monitor")
                page.wait_for_timeout(500)
            except Exception as exc:
                errors.append(f"Monitoring ended: {str(exc)[:250]}")
                break

        try:
            final_url = page.url
            page_title = page.title()
            page_text = page.locator("body").inner_text(timeout=5000)[:12000]

            candidate_links = page.locator("a[href]").evaluate_all(
                """els => els.slice(0, 500).map(a => ({
                    text: (a.innerText || a.getAttribute('aria-label') || '').trim(),
                    url: a.href,
                    visible: !!(a.offsetWidth || a.offsetHeight ||
                                a.getClientRects().length)
                }))"""
            )

            candidate_controls = page.locator(
                "button, input[type=button], input[type=submit], [role=button]"
            ).evaluate_all(
                """els => els.slice(0, 150).map(e => ({
                    text: (e.innerText || e.value ||
                           e.getAttribute('aria-label') || '').trim()
                })).filter(x => x.text &&
                    /continue|next|proceed|download|skip|unlock|visit|go to link/i.test(x.text))"""
            )

            lower = (page_title + "\n" + page_text).lower()
            blocked = any(term in lower for term in [
                "sorry, you have been blocked",
                "you are unable to access",
                "attention required! | cloudflare",
                "error 1020",
                "cloudflare ray id",
                "cf-error-details",
            ])

            record(final_url, "Final URL")

        except Exception as exc:
            errors.append(f"Page inspection: {str(exc)[:250]}")

        context.close()
        browser.close()

    return {
        "history": history,
        "responses": responses,
        "errors": errors,
        "final_url": final_url,
        "title": page_title,
        "text": page_text,
        "links": candidate_links,
        "controls": candidate_controls,
        "blocked": blocked,
    }


with st.sidebar:
    st.header("Resolution settings")
    wait_seconds = st.slider("Wait for page navigation (seconds)", 5, 30, 15)
    scroll_passes = st.slider("Scroll passes", 0, 8, 3)
    st.caption("The browser runs on the Streamlit server, not your phone.")

with st.form("resolve_form"):
    entered_url = st.text_input(
        "Short URL",
        placeholder="https://arolinks.com/flzphw",
    )
    submitted = st.form_submit_button("Resolve URL", type="primary")

if submitted:
    try:
        start_url = normalize_url(entered_url)

        if not is_public_host(start_url):
            st.error("This URL resolves to a non-public or inaccessible host.")
            st.stop()

        with st.spinner("Preparing the hosted browser..."):
            ensure_browser()

        with st.spinner(
            "Following redirects and observing JavaScript navigation..."
        ):
            st.session_state["resolution"] = resolve_url(
                start_url, wait_seconds, scroll_passes
            )

    except subprocess.TimeoutExpired:
        st.error("Chromium installation timed out. Check the app logs.")
    except Exception as exc:
        st.error(f"Resolution failed: {str(exc)[:500]}")

result = st.session_state.get("resolution")

if result:
    st.subheader("Resolution result")

    if result["blocked"]:
        st.warning(
            "A Cloudflare/access block appears to be present. "
            "This hosted server cannot guarantee access to the destination."
        )

    st.write("**Page title:**", result["title"] or "(unavailable)")
    st.write("**Last observed URL:**")
    st.code(result["final_url"], language=None)

    if result["final_url"]:
        st.link_button("Open last observed URL", result["final_url"])

    st.subheader("Navigation history")
    st.dataframe(result["history"], use_container_width=True, hide_index=True)

    st.subheader("HTTP navigation responses")
    if result["responses"]:
        st.dataframe(
            result["responses"], use_container_width=True, hide_index=True
        )
    else:
        st.info("No navigation responses were captured.")

    st.subheader("Possible Continue / Next controls")
    if result["controls"]:
        st.dataframe(
            result["controls"], use_container_width=True, hide_index=True
        )
        st.caption(
            "These are detected controls, not proof that clicking them is safe "
            "or necessary. Review them manually."
        )
    else:
        st.write("No likely controls detected.")

    st.subheader("Extracted links")
    if result["links"]:
        st.dataframe(result["links"], use_container_width=True, hide_index=True)
    else:
        st.write("No links extracted.")

    with st.expander("Page text"):
        st.text(result["text"] or "(No page text available)")

    with st.expander("Errors"):
        for error in result["errors"]:
            st.write(f"- {error}")

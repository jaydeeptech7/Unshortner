
import time
import streamlit as st
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

st.set_page_config(page_title="Shortlink Resolver", page_icon="🔗", layout="wide")
st.title("🔗 Shortlink Resolver")
st.caption("Streamlit UI • Local Chromium • Redirect tracking")

if "history" not in st.session_state:
    st.session_state.history = []
if "final_url" not in st.session_state:
    st.session_state.final_url = ""
if "page_title" not in st.session_state:
    st.session_state.page_title = ""
if "page_text" not in st.session_state:
    st.session_state.page_text = ""
if "links" not in st.session_state:
    st.session_state.links = []
if "browser_running" not in st.session_state:
    st.session_state.browser_running = False


def valid_url(url):
    from urllib.parse import urlparse

    url = url.strip()
    if not url:
        raise ValueError("Enter a URL.")

    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Enter a valid HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not supported.")

    return url


with st.sidebar:
    st.subheader("Browser settings")
    wait_seconds = st.slider("Observe navigation for (seconds)", 10, 120, 30)
    st.info("Chromium opens on this computer. Interact with its window directly.")


with st.form("open_link"):
    input_url = st.text_input(
        "Short URL",
        placeholder="https://arolinks.com/flzphw",
    )
    launch = st.form_submit_button("Open in local Chromium", type="primary")


if launch:
    try:
        target = valid_url(input_url)

        st.session_state.history = []
        st.session_state.links = []
        st.session_state.final_url = target
        st.session_state.page_title = ""
        st.session_state.page_text = ""
        st.session_state.browser_running = True

        history = []
        seen = set()
        captured_links = []
        responses = []

        def record(url, reason):
            if url and url not in seen:
                seen.add(url)
                history.append({
                    "Step": len(history) + 1,
                    "URL": url,
                    "Event": reason,
                })

        st.warning(
            "A separate Chromium window will open. You can interact with "
            "the page manually, including legitimate verification steps."
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                viewport={"width": 1280, "height": 850}
            )
            page = context.new_page()
            page.set_default_timeout(8000)

            page.on(
                "framenavigated",
                lambda frame: record(frame.url, "Navigation")
                if frame == page.main_frame else None,
            )

            page.on(
                "response",
                lambda response: responses.append({
                    "Status": response.status,
                    "URL": response.url,
                })
                if response.request.is_navigation_request() else None,
            )

            record(target, "Submitted URL")

            try:
                page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except PlaywrightTimeoutError:
                st.warning("Page loading timed out; browser remains available.")
            except Exception as exc:
                st.warning(f"Navigation message: {exc}")

            st.write(
                "Use the Chromium window to wait, scroll, or complete "
                "verification yourself. This app does not bypass security checks."
            )

            progress = st.progress(0)
            deadline = time.monotonic() + wait_seconds

            while time.monotonic() < deadline:
                if page.is_closed():
                    break

                try:
                    record(page.url, "Observed URL")
                    remaining = deadline - time.monotonic()
                    progress.progress(
                        min(100, int(100 * (1 - remaining / wait_seconds)))
                    )
                    page.wait_for_timeout(500)
                except Exception:
                    break

            try:
                final_url = page.url
                title = page.title()
                text = page.locator("body").inner_text(timeout=4000)[:12000]

                captured_links = page.locator("a[href]").evaluate_all(
                    """els => els.slice(0, 300).map(a => ({
                        Text: (a.innerText || a.getAttribute('aria-label') || '').trim(),
                        URL: a.href
                    }))"""
                )

                st.session_state.final_url = final_url
                st.session_state.page_title = title
                st.session_state.page_text = text
                st.session_state.links = captured_links
                record(final_url, "Final observation")

            except Exception as exc:
                st.warning(f"Could not inspect the final page: {exc}")

            # Leave the browser open briefly so the user can see the result.
            st.info("Resolution observation finished. Close Chromium when done.")
            try:
                page.wait_for_timeout(5000)
            except Exception:
                pass

            context.close()
            browser.close()

        st.session_state.history = history
        st.session_state.responses = responses
        st.session_state.browser_running = False

    except Exception as exc:
        st.session_state.browser_running = False
        st.error(f"Could not open URL: {exc}")


if st.session_state.final_url:
    st.divider()
    st.subheader("Observed result")
    st.write("**Page title:**", st.session_state.page_title or "(unavailable)")
    st.code(st.session_state.final_url, language=None)
    st.link_button("Open final URL in your default browser",
                   st.session_state.final_url)

    st.subheader("Navigation history")
    if st.session_state.history:
        st.dataframe(
            st.session_state.history,
            use_container_width=True,
            hide_index=True,
        )

    responses = st.session_state.get("responses", [])
    if responses:
        st.subheader("HTTP navigation responses")
        st.dataframe(responses, use_container_width=True, hide_index=True)

    if st.session_state.links:
        st.subheader("Links found on final page")
        st.dataframe(
            st.session_state.links,
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("Page text"):
        st.text(st.session_state.page_text or "(No text captured)")

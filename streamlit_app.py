
import re
import time
import socket
import ipaddress
from urllib.parse import urlparse, urljoin, urldefrag

import requests
import streamlit as st
import streamlit.components.v1 as components
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


st.set_page_config(
    page_title="Shortlink Resolver",
    page_icon="🔗",
    layout="wide",
)

st.title("🔗 Shortlink Resolver")
st.caption("Cloud-hosted redirects • JavaScript navigation • Link extraction")


# --------------------------------------------------
# URL helpers
# --------------------------------------------------

def normalize_url(value):
    value = (value or "").strip()

    if not value:
        raise ValueError("Enter a URL.")

    if "://" not in value:
        value = "https://" + value

    parsed = urlparse(value)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Enter a valid HTTP or HTTPS URL.")

    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not supported.")

    return urldefrag(value).url


def is_public_url(url):
    """Basic protection against requests to private/internal addresses."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname

        if parsed.scheme not in ("http", "https") or not host:
            return False

        if host == "localhost" or host.endswith(".localhost"):
            return False

        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            answers = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = [
                ipaddress.ip_address(answer[4][0])
                for answer in answers
            ]

        return bool(addresses) and all(addr.is_global for addr in addresses)

    except Exception:
        return False


def detect_block(title, body):
    text = f"{title}\n{body}".lower()

    return any(term in text for term in (
        "sorry, you have been blocked",
        "you are unable to access",
        "attention required! | cloudflare",
        "cloudflare ray id",
        "cf-error-details",
        "error 1020",
        "error 1010",
    ))


def get_ray_id(text):
    match = re.search(
        r"Cloudflare Ray ID:\s*([a-fA-F0-9]+)",
        text or "",
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


# --------------------------------------------------
# HTTP redirect resolver
# --------------------------------------------------

def resolve_http(start_url, timeout=15):
    history = []
    responses = []
    links = []
    errors = []

    current_url = start_url
    final_url = start_url
    title = ""
    body_text = ""
    blocked = False

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    })

    for step in range(10):
        if not is_public_url(current_url):
            errors.append("Stopped at a non-public or invalid destination.")
            break

        try:
            response = session.get(
                current_url,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )

            location = response.headers.get("Location", "")

            responses.append({
                "Step": step + 1,
                "Status": response.status_code,
                "URL": current_url,
                "Location": urljoin(current_url, location) if location else "",
            })

            history.append({
                "Step": len(history) + 1,
                "URL": current_url,
                "Event": f"HTTP {response.status_code}",
            })

            if response.status_code in (301, 302, 303, 307, 308) and location:
                next_url = urljoin(current_url, location)
                response.close()

                if next_url == current_url:
                    break

                current_url = next_url
                final_url = current_url
                continue

            content_type = response.headers.get("Content-Type", "").lower()

            if "text/html" in content_type:
                chunks = []
                total = 0

                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue

                    remaining = 1_000_000 - total
                    if remaining <= 0:
                        break

                    chunk = chunk[:remaining]
                    chunks.append(chunk)
                    total += len(chunk)

                    if total >= 1_000_000:
                        break

                html = b"".join(chunks).decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )

                soup = BeautifulSoup(html, "html.parser")

                title = (
                    soup.title.get_text(" ", strip=True)
                    if soup.title else ""
                )
                body_text = soup.get_text(" ", strip=True)[:12000]

                for anchor in soup.select("a[href]")[:300]:
                    links.append({
                        "Text": anchor.get_text(" ", strip=True)[:200],
                        "URL": urljoin(current_url, anchor.get("href", "")),
                    })

                blocked = detect_block(title, body_text)

                if blocked:
                    response.close()
                    break

                meta = soup.find(
                    "meta",
                    attrs={"http-equiv": re.compile("^refresh$", re.I)},
                )

                if meta:
                    match = re.search(
                        r"url\s*=\s*['\"]?([^'\"]+)",
                        meta.get("content", ""),
                        re.I,
                    )

                    if match:
                        next_url = urljoin(current_url, match.group(1).strip())

                        if is_public_url(next_url):
                            response.close()
                            current_url = next_url
                            final_url = next_url
                            continue

            response.close()
            final_url = current_url
            break

        except Exception as exc:
            errors.append(f"HTTP resolution: {str(exc)[:250]}")
            break

    session.close()

    return {
        "original_url": start_url,
        "final_url": final_url,
        "history": history,
        "responses": responses,
        "links": links,
        "errors": errors,
        "title": title,
        "text": body_text,
        "blocked": blocked,
        "ray_id": get_ray_id(body_text),
    }


# --------------------------------------------------
# Cloud-hosted headless Chromium
# --------------------------------------------------

def resolve_browser(start_url, wait_seconds, scroll_passes):
    history = []
    responses = []
    errors = []
    seen = set()

    final_url = start_url
    title = ""
    body_text = ""
    links = []
    controls = []
    blocked = False

    def record(url, event):
        if url and url not in seen:
            seen.add(url)
            history.append({
                "Step": len(history) + 1,
                "URL": url,
                "Event": event,
            })

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        page.set_default_timeout(7000)

        def guard_request(route):
            request_url = route.request.url

            if urlparse(request_url).scheme not in ("http", "https"):
                route.abort()
                return

            if not is_public_url(request_url):
                route.abort()
                return

            route.continue_()

        page.route("**/*", guard_request)

        page.on(
            "framenavigated",
            lambda frame: record(frame.url, "Navigation")
            if frame == page.main_frame else None,
        )

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

        page.on("response", on_response)

        try:
            record(start_url, "Submitted URL")

            page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )

        except PlaywrightTimeoutError:
            errors.append("Initial page load timed out.")
        except Exception as exc:
            errors.append(f"Browser navigation: {str(exc)[:250]}")

        deadline = time.monotonic() + wait_seconds

        for index in range(scroll_passes + 1):
            if time.monotonic() >= deadline:
                break

            try:
                record(page.url, "Observed URL")

                if index < scroll_passes:
                    page.evaluate(
                        "() => window.scrollTo(0, document.body.scrollHeight)"
                    )

                remaining = max(0, deadline - time.monotonic())
                page.wait_for_timeout(int(min(3, remaining) * 1000))
                record(page.url, "After wait/scroll")

            except Exception as exc:
                errors.append(f"Page observation: {str(exc)[:250]}")
                break

        while time.monotonic() < deadline:
            try:
                record(page.url, "Navigation monitor")
                page.wait_for_timeout(500)
            except Exception:
                break

        try:
            final_url = page.url
            title = page.title()
            body_text = page.locator("body").inner_text(timeout=4000)[:12000]

            blocked = detect_block(title, body_text)

            links = page.locator("a[href]").evaluate_all(
                """els => els.slice(0, 300).map(a => ({
                    Text: (a.innerText || a.getAttribute('aria-label') || '').trim(),
                    URL: a.href
                }))"""
            )

            controls = page.locator(
                "button, input[type=button], input[type=submit], [role=button]"
            ).evaluate_all(
                """els => els.slice(0, 100).map(e => ({
                    Text: (e.innerText || e.value ||
                        e.getAttribute('aria-label') || '').trim()
                })).filter(x => x.Text &&
                    /continue|next|proceed|download|skip|unlock|go to link/i.test(x.Text))"""
            )

            record(final_url, "Final observation")

        except Exception as exc:
            errors.append(f"Page inspection: {str(exc)[:250]}")

        context.close()
        browser.close()

    return {
        "original_url": start_url,
        "final_url": final_url,
        "history": history,
        "responses": responses,
        "links": links,
        "controls": controls,
        "errors": errors,
        "title": title,
        "text": body_text,
        "blocked": blocked,
        "ray_id": get_ray_id(body_text),
    }


# --------------------------------------------------
# Streamlit UI
# --------------------------------------------------

with st.sidebar:
    st.header("Settings")

    mode = st.radio(
        "Resolution method",
        ["HTTP redirects", "JavaScript browser", "Browser preview"],
    )

    wait_seconds = st.slider("Wait for navigation (seconds)", 5, 30, 15)
    scroll_passes = st.slider("Scroll passes", 0, 8, 3)

with st.form("resolve_form"):
    entered_url = st.text_input(
        "Short URL",
        placeholder="https://arolinks.com/flzphw",
    )
    submitted = st.form_submit_button("Resolve", type="primary")

if submitted:
    try:
        start_url = normalize_url(entered_url)

        if not is_public_url(start_url):
            st.error("The URL is invalid or resolves to a non-public address.")
            st.stop()

        if mode == "HTTP redirects":
            with st.spinner("Following HTTP redirects..."):
                result = resolve_http(start_url)

        elif mode == "JavaScript browser":
            with st.spinner("Starting headless Chromium..."):
                result = resolve_browser(
                    start_url,
                    wait_seconds,
                    scroll_passes,
                )

        else:
            result = {
                "original_url": start_url,
                "final_url": start_url,
                "history": [],
                "responses": [],
                "links": [],
                "controls": [],
                "errors": [],
                "title": "",
                "text": "",
                "blocked": False,
                "ray_id": "",
            }

        st.session_state["result"] = result
        st.session_state["preview_url"] = start_url

    except Exception as exc:
        st.error(f"Resolution failed: {str(exc)[:400]}")


result = st.session_state.get("result")

if result:
    st.divider()
    st.subheader("Resolution result")

    if result.get("blocked"):
        st.error("Cloudflare/access block detected.")

        if result.get("ray_id"):
            st.write("**Cloudflare Ray ID:**", result["ray_id"])

        st.info(
            "The hosted browser cannot access the destination through this "
            "blocked response. Open the original URL in your normal browser "
            "and paste the destination below if it resolves there."
        )

    st.write("**Original URL**")
    st.code(result.get("original_url", ""), language=None)

    st.write("**Last observed URL**")
    st.code(result.get("final_url", ""), language=None)

    if result.get("final_url"):
        st.link_button("Open URL in your browser", result["final_url"])

    st.subheader("Navigation history")
    if result.get("history"):
        st.dataframe(result["history"], use_container_width=True, hide_index=True)

    st.subheader("HTTP navigation responses")
    if result.get("responses"):
        st.dataframe(result["responses"], use_container_width=True, hide_index=True)

    st.subheader("Extracted links")
    if result.get("links"):
        st.dataframe(result["links"], use_container_width=True, hide_index=True)

    if result.get("controls") is not None:
        with st.expander("Possible Continue / Next controls"):
            st.dataframe(result["controls"], use_container_width=True, hide_index=True)

    with st.expander("Page text"):
        st.text(result.get("text") or "(No page text available)")

    with st.expander("Errors"):
        for error in result.get("errors", []):
            st.write("- " + error)


# Browser preview: this renders the page in the visitor's browser.
# It does not proxy requests through the Python server.
if mode == "Browser preview":
    preview_url = st.session_state.get("preview_url", "")

    if preview_url:
        st.subheader("Browser preview")
        st.caption(
            "Some sites prohibit iframe embedding. If the preview is blocked, "
            "use the open-in-browser link instead."
        )

        components.iframe(
            preview_url,
            height=650,
            scrolling=True,
        )

        st.link_button("Open page in a new browser tab", preview_url)


st.divider()
st.subheader("Manual destination fallback")

manual_url = st.text_input(
    "Paste the destination URL you reached in your browser",
    key="manual_destination",
    placeholder="https://destination.example/",
)

if manual_url:
    try:
        destination = normalize_url(manual_url)

        if is_valid_manual_destination := (
            urlparse(destination).scheme in ("http", "https")
            and bool(urlparse(destination).hostname)
        ):
            st.link_button("Open destination", destination)
        else:
            st.warning("Enter a valid HTTP/HTTPS URL.")
    except ValueError as exc:
        st.warning(str(exc))

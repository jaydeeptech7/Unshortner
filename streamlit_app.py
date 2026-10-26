
import re
import time
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

st.set_page_config(
    page_title="Telegram Shortlink Resolver",
    page_icon="🔗",
    layout="wide",
)

st.title("🔗 Shortlink Resolver")
st.caption(
    "Resolve ordinary redirects and JavaScript-based shortlinks, "
    "with step-by-step browser diagnostics."
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MAX_STEPS = 15
MAX_WAIT_SECONDS = 45

CONTINUE_WORDS = re.compile(
    r"^\s*(continue|continue to site|next|proceed|"
    r"get link|go to link|visit link|open link|"
    r"skip|continue now|click here|"
    r"download|access link)\s*$",
    re.I,
)

CLOSE_WORDS = re.compile(
    r"^\s*(close|dismiss|×|x|no thanks|not now|"
    r"maybe later|got it|cancel)\s*$",
    re.I,
)

BLOCK_WORDS = [
    "sorry, you have been blocked",
    "you have been blocked",
    "attention required! | cloudflare",
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies to continue",
]


def normalize_url(value):
    value = (value or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    return value


def public_url_check(url):
    """Reject obvious private/local destinations before navigation."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return False, "Missing hostname."

        if host.lower() in ("localhost",):
            return False, "Local destinations are not allowed."

        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            addresses = list({
                result[4][0]
                for result in socket.getaddrinfo(
                    host, parsed.port or (443 if parsed.scheme == "https" else 80)
                )
            })

        if not addresses:
            return False, "Hostname did not resolve."

        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                return False, "Private or non-public destination blocked."

        return True, ""
    except Exception as exc:
        return False, f"Could not validate destination: {exc}"


def looks_blocked(text, title=""):
    combined = f"{title}\n{text}".lower()
    return any(word in combined for word in BLOCK_WORDS)


def get_ray_id(text):
    match = re.search(
        r"(?:cloudflare ray id|ray id)\s*[:#]?\s*([a-f0-9-]+)",
        text,
        re.I,
    )
    return match.group(1) if match else None


def extract_links(html, base_url):
    soup = BeautifulSoup(html or "", "html.parser")
    links = []
    for tag in soup.select("a[href], form[action]"):
        value = tag.get("href") or tag.get("action")
        if value:
            absolute = urljoin(base_url, value)
            if absolute.startswith(("http://", "https://")):
                links.append(absolute)
    return list(dict.fromkeys(links))


def resolve_http(start_url, max_steps=MAX_STEPS):
    history = []
    errors = []
    current = start_url

    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT})

        for step in range(max_steps):
            allowed, reason = public_url_check(current)
            if not allowed:
                errors.append(reason)
                break

            try:
                response = session.get(
                    current,
                    allow_redirects=False,
                    timeout=15,
                    stream=True,
                )
                status = response.status_code
                content_type = response.headers.get("content-type", "")
                location = response.headers.get("location")

                history.append({
                    "step": step + 1,
                    "url": current,
                    "status": status,
                    "type": content_type,
                })

                if status in (301, 302, 303, 307, 308) and location:
                    next_url = urljoin(current, location)
                    response.close()
                    if next_url == current:
                        break
                    current = next_url
                    continue

                body = ""
                if "text/html" in content_type.lower():
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(16384):
                        size += len(chunk)
                        chunks.append(chunk)
                        if size >= 500_000:
                            break
                    body = b"".join(chunks).decode(
                        response.encoding or "utf-8", errors="replace"
                    )

                response.close()

                if looks_blocked(body):
                    errors.append(
                        "Access or Cloudflare block detected in HTTP mode."
                    )
                    return {
                        "destination": None,
                        "last_url": current,
                        "history": history,
                        "errors": errors,
                        "page_text": body[:3000],
                        "blocked": True,
                    }

                soup = BeautifulSoup(body, "html.parser")
                meta = soup.find(
                    "meta",
                    attrs={"http-equiv": re.compile(r"refresh", re.I)},
                )
                if meta:
                    content = meta.get("content", "")
                    match = re.search(
                        r"url\s*=\s*['\"]?([^'\"]+)",
                        content,
                        re.I,
                    )
                    if match:
                        current = urljoin(current, match.group(1).strip())
                        continue

                return {
                    "destination": current,
                    "last_url": current,
                    "history": history,
                    "errors": errors,
                    "page_text": soup.get_text(" ", strip=True)[:3000],
                    "links": extract_links(body, current),
                    "blocked": False,
                }

            except requests.RequestException as exc:
                errors.append(str(exc))
                break

    return {
        "destination": None,
        "last_url": current,
        "history": history,
        "errors": errors,
        "blocked": False,
    }


def resolve_browser(start_url):
    history = []
    responses = []
    actions = []
    errors = []
    current = start_url
    blocked = False
    final_text = ""
    final_links = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1365, "height": 900},
            ignore_https_errors=False,
        )
        page = context.new_page()
        page.set_default_timeout(2500)

        def guard_request(route):
            request_url = route.request.url
            if request_url.startswith(("data:", "blob:", "about:")):
                route.continue_()
                return

            allowed, reason = public_url_check(request_url)
            if allowed:
                route.continue_()
            else:
                actions.append(f"Blocked unsafe request: {reason}")
                route.abort()

        page.route("**/*", guard_request)

        page.on(
            "response",
            lambda response: responses.append({
                "status": response.status,
                "url": response.url,
            }) if len(responses) < 100 else None,
        )

        seen_urls = set()

        try:
            allowed, reason = public_url_check(current)
            if not allowed:
                return {
                    "destination": None,
                    "last_url": current,
                    "history": history,
                    "responses": responses,
                    "actions": actions,
                    "errors": [reason],
                    "blocked": False,
                }

            page.goto(
                current,
                wait_until="domcontentloaded",
                timeout=25000,
            )

            for step in range(MAX_STEPS):
                current = page.url

                if current not in seen_urls:
                    seen_urls.add(current)
                    history.append(current)

                try:
                    page.wait_for_load_state(
                        "domcontentloaded", timeout=3000
                    )
                except PlaywrightTimeoutError:
                    pass

                try:
                    page.wait_for_timeout(1200)
                    title = page.title()
                    final_text = page.locator("body").inner_text(
                        timeout=3000
                    )[:8000]
                except Exception:
                    title, final_text = "", ""

                if looks_blocked(final_text, title):
                    blocked = True
                    errors.append(
                        "Cloudflare/access verification detected. "
                        "The hosted browser cannot continue through this block."
                    )
                    break

                final_links = extract_links(
                    page.content(), page.url
                )

                # Close ordinary visible dialogs and overlays.
                closed = False
                for selector in [
                    'button[aria-label="Close"]',
                    'button[aria-label="close"]',
                    '[role="dialog"] button',
                    '.modal button.close',
                    '.close',
                    'button.close',
                ]:
                    try:
                        candidates = page.locator(selector)
                        for index in range(min(candidates.count(), 4)):
                            item = candidates.nth(index)
                            if item.is_visible() and item.is_enabled():
                                item.click(timeout=1000)
                                actions.append(
                                    f"Step {step + 1}: closed overlay ({selector})"
                                )
                                closed = True
                                break
                    except Exception:
                        continue
                    if closed:
                        break

                if closed:
                    page.wait_for_timeout(500)

                # Scroll in case a normal Continue button is below the fold.
                try:
                    page.evaluate(
                        "() => window.scrollTo(0, document.body.scrollHeight)"
                    )
                    page.wait_for_timeout(500)
                except Exception:
                    pass

                clicked = False

                # Find normal buttons, links, and submit controls by label.
                selectors = [
                    "button",
                    'input[type="button"]',
                    'input[type="submit"]',
                    'a',
                    '[role="button"]',
                ]

                for selector in selectors:
                    try:
                        elements = page.locator(selector)
                        count = min(elements.count(), 80)

                        for index in range(count):
                            item = elements.nth(index)

                            try:
                                if not item.is_visible() or not item.is_enabled():
                                    continue

                                label = (
                                    item.inner_text(timeout=500)
                                    or item.get_attribute("value")
                                    or item.get_attribute("aria-label")
                                    or ""
                                ).strip()

                                if not CONTINUE_WORDS.match(label):
                                    continue

                                before = page.url
                                item.click(timeout=1500)
                                actions.append(
                                    f"Step {step + 1}: clicked "
                                    f"{label[:60]!r}"
                                )
                                clicked = True

                                # Allow a timer or navigation to progress.
                                for _ in range(5):
                                    page.wait_for_timeout(1000)
                                    if page.url != before:
                                        break

                                break

                            except Exception:
                                continue

                    except Exception:
                        continue

                    if clicked:
                        break

                if clicked:
                    continue

                # Wait briefly for ordinary timer-driven buttons to enable.
                try:
                    page.wait_for_timeout(2000)
                    newly_enabled = page.locator(
                        "button:enabled, input[type=submit]:enabled"
                    ).count()
                    if newly_enabled:
                        actions.append(
                            f"Step {step + 1}: checked for enabled buttons"
                        )
                except Exception:
                    pass

                # Detect if the page has moved on automatically.
                if page.url != current:
                    continue

                break

        except Exception as exc:
            errors.append(f"Browser error: {exc}")

        try:
            current = page.url
            if current not in seen_urls:
                history.append(current)
            final_text = page.locator("body").inner_text(
                timeout=2000
            )[:8000]
            final_links = extract_links(page.content(), current)
        except Exception:
            pass

        browser.close()

    destination = None if blocked else current

    return {
        "destination": destination,
        "last_url": current,
        "history": history,
        "responses": responses,
        "actions": actions,
        "errors": errors,
        "page_text": final_text,
        "links": final_links,
        "blocked": blocked,
    }


def show_result(result, original):
    st.subheader("Resolution result")

    if result.get("blocked"):
        st.error(
            "Cloudflare/access block detected. The hosted browser "
            "cannot resolve this link through the blocked response."
        )
    elif result.get("destination"):
        st.success("Resolution process completed.")
        st.code(result["destination"])
        st.link_button(
            "Open destination",
            result["destination"],
            use_container_width=True,
        )
    else:
        st.warning(
            "No final destination could be confirmed. "
            "Review the diagnostics below."
        )

    st.markdown("**Original URL**")
    st.code(original)

    st.markdown("**Last observed URL**")
    st.code(result.get("last_url") or "Not available")

    if result.get("history"):
        with st.expander("Navigation history", expanded=True):
            for index, url in enumerate(result["history"], 1):
                st.write(f"{index}. {url}")

    if result.get("actions"):
        with st.expander("Browser actions", expanded=True):
            for action in result["actions"]:
                st.write(f"- {action}")

    if result.get("responses"):
        with st.expander("HTTP navigation responses"):
            for response in result["responses"]:
                st.write(
                    f"{response['status']} — {response['url']}"
                )

    if result.get("links"):
        with st.expander("Extracted links"):
            for link in result["links"][:60]:
                st.write(link)

    if result.get("page_text"):
        with st.expander("Page text"):
            st.text(result["page_text"])

    if result.get("errors"):
        with st.expander("Errors"):
            for error in result["errors"]:
                st.write(f"- {error}")

    st.divider()
    st.subheader("Manual destination fallback")
    manual = st.text_input(
        "Paste the final destination URL if you resolved it in your browser",
        key="manual_destination",
    )

    if st.button("Use manual destination"):
        candidate = normalize_url(manual)
        if not candidate:
            st.error("Enter a valid HTTP or HTTPS URL.")
        else:
            st.session_state["manual_result"] = candidate
            st.success("Manual destination saved for this session.")
            st.code(candidate)
            st.link_button("Open manual destination", candidate)


with st.sidebar:
    st.header("Settings")
    mode = st.radio(
        "Resolution mode",
        [
            "HTTP redirects",
            "JavaScript browser",
        ],
        index=1,
    )
    st.caption(
        "Browser mode handles ordinary buttons and multi-step pages. "
        "It does not bypass access controls or human-verification challenges."
    )

with st.form("resolver_form"):
    entered_url = st.text_input(
        "Shortlink URL",
        placeholder="https://example.com/abc123",
    )
    submitted = st.form_submit_button(
        "Resolve link",
        type="primary",
        use_container_width=True,
    )

if submitted:
    original = normalize_url(entered_url)

    if not original:
        st.error("Enter a valid HTTP or HTTPS URL.")
    else:
        with st.spinner("Resolving link and inspecting page steps..."):
            if mode == "HTTP redirects":
                result = resolve_http(original)
            else:
                result = resolve_browser(original)

        show_result(result, original)

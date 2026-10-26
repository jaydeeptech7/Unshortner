import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import requests
import streamlit as st
from bs4 import BeautifulSoup

APP_TITLE = "Telegram Shortlink Resolver"
TIMEOUT = 10
MAX_REDIRECTS = 8
MAX_HTML_BYTES = 1_000_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
SHORTENER_EXAMPLES = [
    "GPLinks", "AroLinks", "Linkvertise", "LootLabs", "Work.ink",
    "ShrinkMe", "AdFoc.us", "ouo.io", "exe.io", "Adrinolinks",
    "Gyanilinks", "Droplink", "VIPURL", "TPI.li",
]

st.set_page_config(page_title=APP_TITLE, page_icon="🔗", layout="centered")
st.title("🔗 Telegram Shortlink Resolver")
st.caption("Resolve ordinary redirects and inspect pages for likely destination/download links.")

st.info(
    "Works with normal HTTP redirects and common HTML link patterns. Some shorteners "
    "require a timer, JavaScript challenge, CAPTCHA, login, or server-generated token; "
    "those links may not resolve automatically. No universal bypass is guaranteed."
)

with st.expander("Shorteners to test first"):
    st.write(", ".join(SHORTENER_EXAMPLES))
    st.caption("These are targets to test, not a claim that every site is currently supported.")

def validate_public_url(url: str) -> str:
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Enter a complete http:// or https:// URL.")
    if p.username or p.password:
        raise ValueError("URLs containing embedded credentials are not supported.")
    host = p.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Local network addresses are blocked.")
    try:
        records = socket.getaddrinfo(
            host, p.port or (443 if p.scheme == "https" else 80),
            type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        raise ValueError("Could not resolve the hostname.")
    if not records:
        raise ValueError("Could not resolve the hostname.")
    for record in records:
        ip = ipaddress.ip_address(record[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError("Private, local, and reserved network addresses are blocked.")
    return url.strip()

def get_one_hop(session: requests.Session, url: str):
    validate_public_url(url)
    # Do not follow redirects implicitly: validate each redirect destination.
    response = session.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
        allow_redirects=False, stream=True
    )
    return response

def fetch_final_page(start_url: str):
    session = requests.Session()
    current = validate_public_url(start_url)
    for _ in range(MAX_REDIRECTS + 1):
        response = get_one_hop(session, current)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                return current, None, "", "Redirect response had no Location header."
            current = validate_public_url(urljoin(current, location))
            continue

        content_type = response.headers.get("Content-Type", "").lower()
        body = b""
        if "text/html" in content_type or "xhtml" in content_type or not content_type:
            for chunk in response.iter_content(chunk_size=16384):
                body += chunk
                if len(body) >= MAX_HTML_BYTES:
                    body = body[:MAX_HTML_BYTES]
                    break
        status = response.status_code
        final_url = response.url or current
        response.close()
        html = body.decode("utf-8", errors="replace") if body else ""
        return final_url, status, content_type, html
    raise ValueError("Too many redirects or redirect loop detected.")

def unwrap_query_target(url: str):
    query = parse_qs(urlparse(url).query)
    keys = ("url", "u", "target", "dest", "destination", "redirect",
            "redirect_url", "to", "download", "link")
    for key in keys:
        for value in query.get(key, []):
            value = unquote(value)
            if value.startswith(("http://", "https://")):
                return value
    return None

def extract_candidates(html: str, page_url: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    candidates, seen = [], set()

    def add(raw, label):
        if not raw:
            return
        raw = raw.strip().strip("'\"")
        absolute = urljoin(page_url, raw)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return
        if absolute not in seen:
            seen.add(absolute)
            candidates.append({"label": label, "url": absolute})

    query_target = unwrap_query_target(page_url)
    if query_target:
        add(query_target, "URL embedded in redirect parameters")

    for meta in soup.find_all("meta", attrs={"http-equiv": re.compile("refresh", re.I)}):
        match = re.search(r"url\s*=\s*['\"]?([^'\";]+)", meta.get("content", ""), re.I)
        if match:
            add(match.group(1), "Meta-refresh destination")

    action_words = re.compile(
        r"\b(download|continue|skip|proceed|get\s+link|direct\s+link|go\s+to\s+link)\b",
        re.I
    )
    for anchor in soup.find_all("a", href=True):
        label = " ".join(anchor.get_text(" ", strip=True).split())
        attrs = " ".join([
            str(anchor.get("id", "")),
            " ".join(anchor.get("class", [])),
            str(anchor.get("title", "")),
        ])
        if action_words.search(label) or action_words.search(attrs):
            add(anchor["href"], label or "Likely destination link")

    patterns = [
        r"""(?:window\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""",
        r"""window\.open\(\s*["']([^"']+)["']""",
        r"""document\.location\s*=\s*["']([^"']+)["']""",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, html, re.I):
            add(match, "URL found in page script")

    return candidates[:20]

def host_label(url: str):
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url

with st.form("resolve_form"):
    short_url = st.text_input(
        "Paste a shortened URL",
        placeholder="https://example-shortener.com/abc123",
    )
    submitted = st.form_submit_button("Resolve link", type="primary", use_container_width=True)

if submitted:
    if not short_url.strip():
        st.error("Paste a URL first.")
    else:
        try:
            with st.spinner("Following redirects and inspecting the landing page…"):
                final_url, status, content_type, html = fetch_final_page(short_url.strip())
                candidates = extract_candidates(html, final_url)

            st.subheader("Result")
            st.write("**Final URL after ordinary redirects**")
            st.code(final_url, language=None)
            st.link_button("Open final URL", final_url, use_container_width=True)
            if isinstance(status, int):
                st.caption(f"HTTP status: {status} · Content type: {content_type or 'unknown'}")

            if candidates:
                st.subheader("Possible destination links")
                st.caption("Candidates are extracted from page markup and are not guaranteed to be direct downloads.")
                for index, candidate in enumerate(candidates, start=1):
                    with st.container(border=True):
                        st.write(f"**{index}. {candidate['label']}**")
                        st.caption(host_label(candidate["url"]))
                        st.code(candidate["url"], language=None)
                        st.link_button("Open candidate", candidate["url"], key=f"candidate_{index}")
            else:
                st.warning(
                    "No likely destination was found in the page HTML. This shortener may need "
                    "JavaScript execution, a timer, a CAPTCHA, login, or a site-specific adapter."
                )
        except (ValueError, requests.RequestException) as exc:
            st.error(str(exc) or "Could not resolve this URL.")
        except Exception:
            st.error("Unexpected error while resolving this URL.")

st.divider()
st.caption(
    "Safety: inspect destination domains before opening them. This public demo is not intended "
    "for confidential URLs. It does not solve CAPTCHAs, complete ad tasks, or defeat access controls."
)

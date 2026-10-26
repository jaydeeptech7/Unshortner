# Telegram Shortlink Resolver — Streamlit deploy package

A Streamlit starter app for short links commonly shared in Telegram download channels.

## Included
- HTTP redirect resolution (validates every redirect destination)
- Extracts likely links from common HTML patterns, meta refresh, redirect query parameters, and simple JavaScript location assignments
- Candidate links displayed with open buttons
- Blocks private/local/reserved IP addresses as a basic SSRF mitigation
- Request timeouts and HTML size cap

## Deploy free on Streamlit Community Cloud
1. Download and extract this ZIP.
2. Create a GitHub repository (for example `telegram-shortlink-resolver`) and upload `streamlit_app.py`, `requirements.txt`, and `README.md` to the repository root.
3. Sign in at https://share.streamlit.io/ with GitHub.
4. Choose **Create app**, select your repository and branch, set the main file path to `streamlit_app.py`, then click **Deploy**.
5. Streamlit will provide a public `*.streamlit.app` URL.

Official docs: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy

## Important limitations
This is a generic resolver, not a universal bypass. GPLinks, AroLinks, Linkvertise, LootLabs, Work.ink and other services may change their markup or require JavaScript, timers, CAPTCHA, login, cookies, or server-side token generation. Site-specific support must be tested and maintained; no support is claimed just because a service appears in the app's list.

Do not use this app to evade access controls or complete prohibited ad/click fraud. Review destination domains before opening links.

## Public hosting warning
Any public URL-fetching service can be abused. The app validates host IPs and each redirect, but production-grade deployment should add outbound network egress restrictions and stronger DNS-rebinding defenses. Do not process confidential URLs on a public instance.

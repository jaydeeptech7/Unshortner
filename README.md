
# Smart Shortlink Resolver

A Streamlit application that helps inspect short links and multi-step redirect pages.

## Features

- Follows normal HTTP and HTTPS redirects.
- Observes JavaScript-driven navigation and URL changes.
- Waits for configurable periods to allow automatic timers to finish.
- Scrolls pages to reveal lazy-loaded content.
- Records intermediate URLs observed during navigation.
- Extracts candidate destination links from the rendered page.
- Detects likely Continue, Next, Download, and similar controls.
- Displays warnings for possible CAPTCHA or human-verification steps.
- Includes basic checks intended to block local and private network destinations.

## Files

- `streamlit_app.py` — application.
- `requirements.txt` — Python dependencies.
- `packages.txt` — Linux libraries for Chromium on Streamlit Cloud.

## Run locally

Use Python 3.10 or later.

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it on Windows:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Start the application:

```bash
streamlit run streamlit_app.py
```

Open the local URL displayed in your terminal.

## Deploy to Streamlit Community Cloud

1. Upload these files to the root of your GitHub repository.
2. Open https://share.streamlit.io/
3. Create a new app and select your repository.
4. Set the branch to `main`.
5. Set the main file path to `streamlit_app.py`.
6. Deploy the app.

The application attempts to install Chromium when it starts. Browser installation may take time, and some hosting environments may need additional system libraries.

## Important limitations

- The resolver observes automatic navigation; it does not automatically click through ad-gated Continue buttons.
- It does not solve CAPTCHAs or bypass authentication, paywalls, or other access controls.
- Some websites require cookies, user interaction, or browser features that a headless browser cannot reproduce.
- A discovered link is a candidate destination, not a guarantee that it is the final download.
- Public hosting can expose the app to abuse. The included network checks are basic safeguards, not a complete production-grade SSRF defense. Do not expose this app as a trusted public service without additional network isolation, request limits, and security review.
- Websites may block automated browsers, change their layouts, or use techniques that prevent reliable resolution.

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Optional headless-render fallback for JavaScript-driven websites.

The normal crawler (email_scraper.py) fetches raw server HTML — fast and polite.
But some sites (React/Next/Nuxt SPAs) draw almost all their content in the
browser, so a raw fetch comes back as a near-empty shell with no emails and no
social links. When the crawler decides a page "looks hollow", it hands the URL
to render() here, which loads the page in a real headless browser and returns
the fully-rendered DOM — so email / social / analysis extraction runs on the
content users actually see.

Kept fully OPTIONAL and OFF by default, so the pipeline still runs in lean
containers that never install a browser. Toggle with ENRICH_JS_RENDER.

To enable on a host (one-time):
    pip install playwright
    playwright install chromium

Design choices
--------------
* Hollow-only trigger: a browser render only happens for pages whose raw HTML
  has almost no text AND looks script-driven (SPA markers / script-src). Ordinary
  server-rendered sites never pay for a browser.
* One browser per process, started lazily on the first render and reused, so a
  campaign with several hollow pages doesn't relaunch Chromium each time.
* Playwright is imported lazily (inside render()), so this module loads fine
  even where Playwright isn't installed — render() just returns None.
"""

import os
import re

# The env toggle is read fresh on every call so a .env loaded later still works.
def enabled():
    return os.environ.get("ENRICH_JS_RENDER", "false").strip().lower() in (
        "1", "true", "yes", "on")


# Single browser/context/page, reused across render() calls within one process.
_browser = None


def looks_js_hollow(html_text):
    """Cheap guess that a raw-HTTP response is an empty JS shell: there is
    almost no visible text AND the page looks script-driven (SPA markers or a
    big inline/external script). Only such pages are worth a browser render."""
    if not isinstance(html_text, str) or not html_text.strip():
        return False

    # Count visible text with script/style/noscript bodies stripped out.
    text = re.sub(r"<script.*?</script>", " ", html_text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<noscript.*?</noscript>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    if len(text.split()) >= 40:
        return False                     # plenty of real server-rendered text

    low = html_text.lower()
    return bool(re.search(
        r"__NEXT_DATA__|__NUXT__|id=[\"'](root|app|__next|app-mount)[\"']"
        r"|<script\s+[^>]*\bsrc=", low, re.I))


def render_page(url, timeout_ms=15000):
    """Load url in a headless browser and return the rendered DOM, or None on
    any failure. NOT gated by an env toggle — callers decide when to use this
    (the website crawl gates via render(); social enrichment has its own
    SOCIAL_JS_RENDER switch). Starts the shared browser on first use."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    global _browser
    try:
        if _browser is None:
            _pw = sync_playwright().start()
            _browser = _pw.chromium.launch(headless=True)
            # Hold the browser/page objects on the module so they survive.
            _render_ctx["page"] = _browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"),
            ).new_page()

        page = _render_ctx["page"]
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2000)        # let client-side JS settle
        return page.content()
    except Exception:
        return None


def render(url, timeout_ms=15000):
    """ENRICH_JS_RENDER-gated wrapper used by the website-crawl path."""
    if not enabled():
        return None
    return render_page(url, timeout_ms)


_render_ctx = {}

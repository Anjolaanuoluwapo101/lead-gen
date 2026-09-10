#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Fetch a seller's portfolio website into plain visible text, so the outreach
draft can cite their real work/services.

Reuses the app's existing hardened fetch stack instead of reinventing it:
  * email_scraper.Fetcher  -> anti-bot raw-HTML fetch (curl_cffi -> cloudscraper
                              -> requests) with retries + a browser-ish UA.
  * headless_fetch         -> optional JS render for SPA/hollow pages.

Honors the seller's tri-state render_mode (see docs/multi-tenant-plan.md):
  html -> static fetch only (never a browser).
  js   -> always render with headless Chromium.
  auto -> static fetch; if the page looks like a hollow JS shell, render it.

JS rendering is best-effort: if Playwright/Chromium isn't installed the call
falls back to whatever static HTML it got, and reports that in `notes` rather
than failing — so the endpoint never 500s just because Chromium is missing.
"""

import re

from bs4 import BeautifulSoup

import email_scraper
import headless_fetch

_TAG_DROP = ("script", "style", "noscript", "nav", "footer", "header",
             "iframe", "form", "aside")
_TEXT_CAP = 12000


def _clean_text(html, cap=_TEXT_CAP):
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(_TAG_DROP):
        tag.decompose()
    for tag in soup(["svg", "path", "img"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()[:_TEXT_CAP]


def _js_available():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:
        return False


def fetch_portfolio(url, render_mode="auto", timeout_ms=15000):
    """Fetch a portfolio URL into visible text, honoring `render_mode`.

    Returns a dict: {ok, url, status, rendered, js_unavailable, page_title,
    text, notes}. `ok` is True on any fetch success (even a JS-less fallback);
    only a hard fetch failure sets ok=False with an `error`.
    """
    mode = (render_mode or "auto").strip().lower()
    if mode not in ("html", "js", "auto"):
        mode = "auto"

    notes = []
    fetcher = email_scraper.Fetcher(delay=(0.3, 0.8), verbose=False)
    html, status = fetcher.get(url)
    if html is None:
        return {"ok": False, "error": f"fetch failed (http {status})",
                "url": url, "status": status}

    rendered = False
    wants_js = mode == "js" or (mode == "auto" and headless_fetch.looks_js_hollow(html))
    if wants_js:
        if _js_available():
            r = headless_fetch.render_page(url, timeout_ms=timeout_ms)
            if r:
                html, rendered = r, True
            else:
                notes.append("headless render returned nothing; used static HTML")
        else:
            notes.append("render_mode requests JS but Playwright/Chromium is not "
                         "installed; used static HTML")

    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.title
    page_title = title_tag.get_text(strip=True) if title_tag else ""
    return {
        "ok": True,
        "url": url,
        "status": status,
        "render_mode": mode,
        "rendered": rendered,
        "page_title": page_title,
        "text": _clean_text(html),
        "notes": notes,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Fetch a portfolio site to text.")
    ap.add_argument("url")
    ap.add_argument("--render-mode", default="auto",
                    choices=["html", "js", "auto"])
    args = ap.parse_args()
    import json
    print(json.dumps(fetch_portfolio(args.url, args.render_mode), indent=2,
                     default=str))

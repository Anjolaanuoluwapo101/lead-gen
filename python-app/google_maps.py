#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Google Maps business scraper — Selenium edition.

Replaces the Playwright version. Identical logic, identical CLI, identical
output schema — only the browser driver changed. Selenium + system Chromium
works on Alpine/musl without any compilation or wheel issues.

Setup (one-time, or baked into Dockerfile)
-------------------------------------------
    apk add chromium chromium-chromedriver
    pip install selenium

Usage
-----
    python google_maps.py "plumbers" "Austin, TX" --max-results 60 -o plumbers.csv
    python google_maps.py "restaurants" "Boston, MA" --offset 60 --max-results 60
"""

import argparse
import csv
import os
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass, asdict, fields
from urllib.parse import quote_plus

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
except ImportError:
    print("Missing dependency. Run:\n  pip install selenium\n  apk add chromium chromium-chromedriver")
    sys.exit(1)

# Windows consoles default to cp1252, which crashes on '✓'/'—'. Force UTF-8 so
# any log line is safe to print regardless of the terminal's codepage.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# Row schema — identical to the Playwright version.
# --------------------------------------------------------------------------- #
@dataclass
class MapsBusiness:
    rank: str = ""
    business_name: str = ""
    telephone: str = ""
    business_page: str = ""
    category: str = ""
    rating: str = ""
    review_count: str = ""
    street: str = ""
    locality: str = ""
    region: str = ""
    zipcode: str = ""
    hours: str = ""
    price_level: str = ""
    place_id: str = ""
    plus_code: str = ""
    maps_url: str = ""
    listing_url: str = ""


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def looks_blocked(page_source):
    if not page_source:
        return True
    low = page_source.lower()
    signals = ["unusual traffic", "captcha", "before you continue",
               "our systems have detected"]
    return any(s in low for s in signals)


# --------------------------------------------------------------------------- #
# Driver factory
# --------------------------------------------------------------------------- #
def _resolve_chromium():
    """
    Locate a launchable Chromium/Chrome binary. The Alpine `chromium` apk
    package installs the real binary as /usr/bin/chromium-browser (not
    /usr/bin/chromium), so trusting one env path is fragile. We try every
    plausible location plus a PATH lookup, and return the first that exists
    and is executable.
    """
    candidates = []
    for env in ("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "CHROMIUM_PATH"):
        if os.environ.get(env):
            candidates.append(os.environ[env])
    candidates += [
        "/usr/bin/chromium-browser",   # Alpine apk 'chromium'
        "/usr/bin/chromium",           # Debian-style symlink / common path
        "/opt/chromium/chrome",
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        # Windows: normal Google Chrome / Edge installs.
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        os.path.expandvars("%LOCALAPPDATA%/Google/Chrome/Application/chrome.exe"),
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return (shutil.which("chromium-browser") or shutil.which("chromium")
            or shutil.which("chrome") or shutil.which("msedge")
            or shutil.which("google-chrome") or shutil.which("google-chrome-stable"))


# Persistent Chrome profile. Google asks for consent on the first visit from a
# fresh profile; a one-time --headed run can accept it and we reuse that choice
# on later headless runs. A throwaway temp profile would re-trigger the wall
# every run, so this must be stable across invocations.
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".browser-profile")


def make_driver(headless=True):
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--lang=en-US")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    chromium_path = _resolve_chromium()
    if chromium_path:
        opts.binary_location = chromium_path
        print(f"  chromium: {chromium_path}", file=sys.stderr)
    elif os.name != "nt":
        # On Linux/containers we expect an explicit apk/system Chromium. Fail
        # loudly with an actionable message instead of the cryptic
        # "DevToolsActivePort file doesn't exist" that Selenium throws when the
        # binary it was told to launch isn't there.
        raise RuntimeError(
            "Could not find a Chromium/Chrome executable. Checked env vars "
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH/CHROMIUM_PATH and the usual "
            "system paths (chromium-browser, chromium, google-chrome). Is "
            "chromium installed in this container?"
        )
    else:
        # Windows: no explicit path, but Selenium Manager can auto-locate a
        # normal Chrome/Edge install (and auto-fetch a matching chromedriver).
        print("  chromium: no explicit path; letting Selenium auto-detect Chrome",
              file=sys.stderr)

    chromedriver = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
    if os.path.exists(chromedriver):
        service = Service(executable_path=chromedriver)
    else:
        service = Service()

    os.makedirs(PROFILE_DIR, exist_ok=True)
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")

    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


# --------------------------------------------------------------------------- #
# Scraper
# --------------------------------------------------------------------------- #
class MapsScraper:
    FEED_CSS = 'div[role="feed"]'
    CARD_LINK_CSS = 'div[role="feed"] a[href*="/maps/place/"]'

    def __init__(self, headless=True, max_retries=3, verbose=True):
        self.headless = headless
        self.max_retries = max_retries
        self.verbose = verbose

    def log(self, *a):
        if self.verbose:
            print(*a)

    def build_url(self, keyword, place):
        query = quote_plus(f"{keyword} in {place}")
        return f"https://www.google.com/maps/search/{query}"

    def _pause(self, lo=0.6, hi=1.6):
        time.sleep(random.uniform(lo, hi))

    def _scroll_and_collect(self, driver, max_results, offset=0):
        seen_hrefs = []
        stagnant = 0
        max_stagnant = 4
        target = offset + max_results

        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.FEED_CSS))
            )
        except TimeoutException:
            self.log("  results feed never appeared")
            return []

        if offset > 0:
            self.log(f"  offset={offset}: scrolling past first {offset} results...")

        while len(seen_hrefs) < target and stagnant < max_stagnant:
            cards = driver.find_elements(By.CSS_SELECTOR, self.CARD_LINK_CSS)
            hrefs = []
            for c in cards:
                try:
                    href = c.get_attribute("href")
                    if href and href not in hrefs:
                        hrefs.append(href)
                except Exception:
                    pass

            new_count = len(set(hrefs) - set(seen_hrefs))
            seen_hrefs = list(dict.fromkeys(seen_hrefs + hrefs))
            stagnant = 0 if new_count > 0 else stagnant + 1

            self.log(f"  scrolled: {len(seen_hrefs)} unique listings "
                     f"(need {target}, offset={offset})")

            if len(seen_hrefs) >= target:
                break

            driver.execute_script(
                "const f = document.querySelector(arguments[0]); "
                "if (f) f.scrollTop = f.scrollHeight;",
                self.FEED_CSS
            )
            self._pause(0.8, 1.8)

        if offset and len(seen_hrefs) <= offset:
            self.log(f"  offset ({offset}) past end of results — nothing new.")
            return []

        if offset:
            self.log(f"  skipping first {offset} already-seen listings.")

        return seen_hrefs[offset:target]

    def _safe_text(self, driver, css):
        try:
            return driver.find_element(By.CSS_SELECTOR, css).text.strip()
        except Exception:
            return ""

    def _extract_from_detail(self, driver, href, rank, listing_url):
        for attempt in range(1, self.max_retries + 1):
            try:
                driver.get(href)
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "h1"))
                )
                self._pause(0.4, 1.0)
                return self._parse_detail(driver, rank, listing_url, href)
            except TimeoutException:
                self.log(f"    timeout (attempt {attempt}/{self.max_retries})")
                if attempt < self.max_retries:
                    time.sleep(1.5 * attempt)
            except WebDriverException as e:
                self.log(f"    driver error: {e} (attempt {attempt}/{self.max_retries})")
                if attempt < self.max_retries:
                    time.sleep(1.5 * attempt)
        return None

    def _parse_detail(self, driver, rank, listing_url, href):
        name = self._safe_text(driver, "h1")

        category = rating = review_count = ""
        try:
            category = driver.find_element(
                By.CSS_SELECTOR, 'button[jsaction*="category"]'
            ).text.strip()
        except Exception:
            pass

        try:
            label = driver.find_element(
                By.CSS_SELECTOR, 'div[role="img"][aria-label*="star"]'
            ).get_attribute("aria-label") or ""
            m = re.search(r"([\d.]+)\s*star", label)
            if m:
                rating = m.group(1)
            m2 = re.search(r"([\d,]+)\s*review", label)
            if m2:
                review_count = m2.group(1).replace(",", "")
        except Exception:
            pass

        street = locality = region = zipcode = ""
        telephone = website = hours = plus_code = ""

        try:
            for btn in driver.find_elements(
                By.CSS_SELECTOR, 'button[data-item-id], a[data-item-id]'
            ):
                try:
                    item_id = (btn.get_attribute("data-item-id") or "").lower()
                    text = btn.text.strip()
                    if not text:
                        continue
                    if item_id.startswith("address"):
                        street, locality, region, zipcode = self._split_address(text)
                    elif item_id.startswith("phone"):
                        telephone = self._clean_phone(text)
                    elif item_id.startswith("authority") or "website" in item_id:
                        website = btn.get_attribute("href") or text
                    elif "oloc" in item_id or "plus" in item_id:
                        plus_code = text
                except Exception:
                    pass
        except Exception:
            pass

        try:
            hours_el = driver.find_element(
                By.CSS_SELECTOR, 'div[aria-label*="Hours"], table'
            )
            hours = " | ".join(
                ln.strip() for ln in hours_el.text.split("\n") if ln.strip()
            )[:500]
        except Exception:
            pass

        place_id = ""
        m = re.search(r"!1s([^!]+)", href)
        if m:
            place_id = m.group(1)

        return MapsBusiness(
            rank=str(rank), business_name=name, telephone=telephone,
            business_page=website, category=category, rating=rating,
            review_count=review_count, street=street, locality=locality,
            region=region, zipcode=zipcode, hours=hours, price_level="",
            place_id=place_id, plus_code=plus_code, maps_url=href,
            listing_url=listing_url,
        )

    @staticmethod
    def _clean_phone(text):
        """
        Google sometimes prefixes a stored phone number with a private-use
        char + newline as light obfuscation ('\n+1 512-...'). Keep only
        phone-meaningful characters so the value is clean for outreach.
        """
        allowed = "+0123456789 -()."
        cleaned = "".join(ch for ch in (text or "") if ch in allowed)
        return " ".join(cleaned.split()).strip()

    def _split_address(self, text):
        text = text.strip()
        m = re.search(r"(.*?),?\s*([A-Za-z .]+),\s*([A-Z]{2})\s*(\d{5})?$", text)
        if m:
            return (
                m.group(1).strip().rstrip(","),
                m.group(2).strip(),
                m.group(3).strip(),
                (m.group(4) or "").strip(),
            )
        return text, "", "", ""

    def _await_consent(self, driver, listing_url, timeout=120):
        """
        Headed mode: give the human a chance to clear Google's consent/CAPTCHA
        in the visible window. Polls until the page is no longer blocked or the
        timeout elapses. This is what makes the one-time consent pass work —
        without it the scraper bails ~2s after opening, before anyone can click.
        """
        deadline = time.time() + timeout
        self.log("  Headed run: a Google consent/CAPTCHA prompt may be showing. "
                 "Click 'Accept all' (or solve it) in the browser window...")
        while time.time() < deadline:
            if not looks_blocked(driver.page_source):
                return True
            time.sleep(2)
        return False

    def scrape(self, keyword, place, max_results=40, offset=0):
        listing_url = self.build_url(keyword, place)
        results = []
        driver = make_driver(headless=self.headless)

        try:
            self.log(f"Opening: {listing_url}")
            driver.get(listing_url)
            self._pause(1.5, 2.5)

            if looks_blocked(driver.page_source):
                if self.headless:
                    self.log("  consent/CAPTCHA wall detected. Accept it once "
                             "with --headed; the choice is stored in the "
                             f"persistent profile at {PROFILE_DIR}.")
                    return results
                if not self._await_consent(driver, listing_url):
                    self.log("  Consent/CAPTCHA not resolved in time. Exiting.")
                    return results
                self.log("  consent cleared — continuing.")

            hrefs = self._scroll_and_collect(driver, max_results, offset=offset)
            self.log(f"Collected {len(hrefs)} listing links. Visiting each for details...")

            for i, href in enumerate(hrefs, start=offset + 1):
                self.log(f"[{i}/{offset + len(hrefs)}] {href[:90]}")
                row = self._extract_from_detail(driver, href, i, listing_url)
                if row and row.business_name:
                    results.append(row)
                self._pause(0.5, 1.2)

        finally:
            driver.quit()

        return results


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def write_csv(rows, path):
    fieldnames = [f.name for f in fields(MapsBusiness)]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))


def main():
    ap = argparse.ArgumentParser(
        description="Scrape business listings from Google Maps (Selenium/Chromium).")
    ap.add_argument("keyword", help="search keyword, e.g. 'plumbers'")
    ap.add_argument("place", help="location, e.g. 'Austin, TX'")
    ap.add_argument("-r", "--max-results", type=int, default=40,
                    help="max listings to collect (default 40)")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip this many results from top of feed "
                         "(use to continue a previous run)")
    ap.add_argument("-o", "--output", help="output CSV path")
    ap.add_argument("--retries", type=int, default=3,
                    help="max retries per detail page (default 3)")
    ap.add_argument("--headed", action="store_true",
                    help="show browser window (useful for consent wall on first run)")
    ap.add_argument("-q", "--quiet", action="store_true", help="less logging")
    args = ap.parse_args()

    scraper = MapsScraper(
        headless=not args.headed,
        max_retries=args.retries,
        verbose=not args.quiet,
    )

    rows = scraper.scrape(args.keyword, args.place,
                          max_results=args.max_results, offset=args.offset)

    if not rows:
        print("\nNo data scraped. Likely causes:")
        print("  1. Consent/CAPTCHA wall — rerun with --headed once.")
        print("  2. Google DOM changed — selectors may need updating.")
        print("  3. Network/timeout — retry or check connectivity.")
        sys.exit(1)

    out = args.output or (
        f"{args.keyword}-{args.place}-googlemaps.csv"
        .replace(" ", "_").replace(",", "")
    )
    write_csv(rows, out)
    print(f"\nWrote {len(rows)} listings to {out}")


if __name__ == "__main__":
    main()

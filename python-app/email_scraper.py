#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Website intelligence scraper — email extraction + content-intelligence, no LLM.

Two jobs, same crawl:
1. Email extraction (same as before, now with source-page tagging).
2. Content intelligence: pull every *structured* signal a page already gives
   you for free — title, meta description, Open Graph, JSON-LD schema.org
   data, headings, phone/address patterns, social links, detected CMS/platform
   — so a downstream LLM node (or a human) has real material to work with,
   instead of guessing from raw HTML. No LLM call happens here; this only
   extracts and organizes what's already on the page.

Robustness — ported from yellow_pages.py's Fetcher
----------------------------------------------------
* Same curl_cffi -> cloudscraper -> requests backend fallback chain (TLS
  fingerprint impersonation first, since that's what actually gets past
  Cloudflare/PerimeterX-style bot detection).
* Retry-with-backoff that actually retries (fresh proxy + backend combo each
  attempt), instead of dying on the first failure.
* Block/CAPTCHA page detection, so a "200 OK challenge page" doesn't get
  silently treated as real content.
* Proxy pool support (single --proxy or --proxy-file, rotated per retry).
* Randomised human-like delay between requests.

Usage
-----
    python email_scraper.py "https://example.com" --max-depth 2 --max-count 50
    python email_scraper.py "https://example.com" --json      # for the Flask wrapper
"""

import argparse
import json
import random
import re
import sys
import time
from collections import deque
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

import headless_fetch  # optional JS-render fallback for hollow/SPA pages

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

import requests as py_requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    Retry = None
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from fake_useragent import UserAgent
    HAS_FAKE_UA = True
except ImportError:
    HAS_FAKE_UA = False


# --------------------------------------------------------------------------- #
# Headers / fingerprints (same approach as yellow_pages.py)
# --------------------------------------------------------------------------- #
_FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def random_user_agent():
    if HAS_FAKE_UA:
        try:
            return UserAgent(browsers=["Chrome"], os=["Windows", "Mac OS X", "Linux"]).random
        except Exception:
            pass
    return _FALLBACK_UA


def full_headers(ua, referer=None):
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        h["Referer"] = referer
    return h


def minimal_headers(referer=None):
    h = {"Accept-Language": "en-US,en;q=0.9"}
    if referer:
        h["Referer"] = referer
    return h


BLOCK_SIGNATURES = [
    "access denied", "unusual traffic", "verify you are a human", "captcha",
    "cf-chl", "checking your browser", "px-captcha", "please enable javascript",
    "requests from your network", "temporarily blocked", "are you a robot",
    "attention required",
]


def looks_blocked(text):
    if not text or len(text.strip()) < 200:
        return True
    low = text.lower()
    return any(sig in low for sig in BLOCK_SIGNATURES)


# --------------------------------------------------------------------------- #
# Fetcher — curl_cffi -> cloudscraper -> requests, retry + proxy rotation
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self, proxies=None, delay=(1.5, 3.5), max_retries=3, verbose=True):
        self.proxy_pool = proxies or []
        self.delay = delay
        self.max_retries = max_retries
        self.verbose = verbose
        self.ua = random_user_agent()
        self._session = self._build_session()
        self._scraper = cloudscraper.create_scraper() if HAS_CLOUDSCRAPER else None

    def log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr)

    def _build_session(self):
        s = py_requests.Session()
        if Retry is not None:
            retry = Retry(total=2, backoff_factor=1,
                          status_forcelist=[429, 500, 502, 503, 504],
                          allowed_methods=frozenset(["GET"]))
            adapter = HTTPAdapter(max_retries=retry)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
        return s

    def _pick_proxy(self):
        if not self.proxy_pool:
            return None
        p = random.choice(self.proxy_pool)
        return {"http": p, "https": p}

    def polite_sleep(self):
        t = random.uniform(*self.delay)
        self.log(f"  sleeping {t:.1f}s")
        time.sleep(t)

    def _via_curl_cffi(self, url, proxy, referer):
        last = None
        for target in ("chrome", "chrome124", "chrome120", None):
            try:
                kw = dict(headers=minimal_headers(referer), proxies=proxy, timeout=20)
                if target:
                    kw["impersonate"] = target
                r = cffi_requests.get(url, **kw)
                return r.text, r.status_code
            except Exception as e:
                last = e
                continue
        raise last if last else RuntimeError("curl_cffi failed")

    def _via_cloudscraper(self, url, proxy, referer):
        r = self._scraper.get(url, headers=minimal_headers(referer),
                              proxies=proxy, timeout=20)
        return r.text, r.status_code

    def _via_requests(self, url, proxy, referer):
        headers = full_headers(self.ua, referer)
        try:
            r = self._session.get(url, headers=headers, proxies=proxy,
                                  timeout=20, verify=True)
        except py_requests.exceptions.SSLError:
            r = self._session.get(url, headers=headers, proxies=proxy,
                                  timeout=20, verify=False)
        return r.text, r.status_code

    def _try_backends(self, url, proxy, referer):
        if HAS_CURL_CFFI:
            try:
                return self._via_curl_cffi(url, proxy, referer)
            except Exception as e:
                self.log(f"    curl_cffi error: {e}")
        if self._scraper is not None:
            try:
                return self._via_cloudscraper(url, proxy, referer)
            except Exception as e:
                self.log(f"    cloudscraper error: {e}")
        try:
            return self._via_requests(url, proxy, referer)
        except Exception as e:
            self.log(f"    requests error: {e}")
        return None, None

    def get(self, url, referer=None):
        """Returns (text, status) or (None, None). Never raises."""
        last_status = None
        for attempt in range(1, self.max_retries + 1):
            proxy = self._pick_proxy()
            where = f" via {list(proxy.values())[0]}" if proxy else ""
            self.log(f"[attempt {attempt}/{self.max_retries}] GET {url}{where}")

            text, status = self._try_backends(url, proxy, referer)
            last_status = status

            if status == 200 and text and not looks_blocked(text):
                self.log(f"  OK ({len(text):,} bytes)")
                return text, status
            if status == 404:
                self.log("  404 — page not found")
                return None, 404
            if status in (403, 429) or (text and looks_blocked(text)):
                self.log(f"  blocked/challenged (status={status})")
            else:
                self.log(f"  failed (status={status})")

            if attempt < self.max_retries:
                backoff = (1.5 * attempt) + random.uniform(0, 1)
                time.sleep(backoff)

        return None, last_status


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "#")
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip",
                   ".mp4", ".mp3", ".css", ".js", ".ico", ".woff", ".woff2")


def get_base_url(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def get_page_path(url):
    parts = urlsplit(url)
    return url[:url.rfind('/') + 1] if '/' in parts.path else url


def normalize_url(url):
    parts = urlsplit(url)
    path = parts.path.rstrip('/') or '/'
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ''))


def normalize_link(link, base_url, page_path):
    if link.startswith('//'):
        return "https:" + link
    if link.startswith('/'):
        return base_url + link
    if not link.startswith('http'):
        return page_path + link
    return link


# --------------------------------------------------------------------------- #
# Email extraction
# --------------------------------------------------------------------------- #
EMAIL_PATTERN = re.compile(r'[a-z0-9\.\-+_]+@[a-z0-9\.\-]+\.[a-z]{2,}', re.I)
EMAIL_JUNK_DOMAINS = ("example.com", "sentry.io", "wixpress.com", "godaddy.com",
                      "yourdomain.com", "domain.com", "email.com", "address.com")


def extract_emails(text):
    found = set()
    for m in EMAIL_PATTERN.findall(text):
        low = m.lower()
        if any(low.endswith("@" + d) or f"@{d}" in low for d in EMAIL_JUNK_DOMAINS):
            continue
        if re.search(r'\.(png|jpe?g|gif|svg|webp)$', low):
            continue
        found.add(low)
    return found


# --------------------------------------------------------------------------- #
# Content intelligence — structured signal extraction, no LLM
# --------------------------------------------------------------------------- #
PHONE_PATTERN = re.compile(
    r'(?:\+?\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}'
)
SOCIAL_DOMAINS = {
    "facebook.com": "facebook", "instagram.com": "instagram", "twitter.com": "twitter",
    "x.com": "twitter", "linkedin.com": "linkedin", "youtube.com": "youtube",
    "tiktok.com": "tiktok", "wa.me": "whatsapp", "whatsapp.com": "whatsapp",
    "t.me": "telegram", "pinterest.com": "pinterest",
}
CMS_SIGNATURES = {
    "wp-content": "WordPress", "wp-includes": "WordPress",
    "cdn.shopify.com": "Shopify", "myshopify.com": "Shopify",
    "static.wixstatic.com": "Wix", "squarespace.com": "Squarespace",
    "sqsp.net": "Squarespace", "webflow.io": "Webflow", "webflow.com": "Webflow",
}


def _clean_text(s, limit=None):
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s[:limit] if limit else s


def extract_jsonld(soup):
    """Parse schema.org JSON-LD blocks — the single richest structured signal
    a site can offer (business name, type, address, phone, hours, socials)."""
    blocks = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = {}
            for key in ("@type", "name", "description", "telephone", "email",
                        "priceRange", "openingHours"):
                if key in item:
                    entry[key] = item[key]
            addr = item.get("address")
            if isinstance(addr, dict):
                entry["address"] = _clean_text(
                    ", ".join(str(v) for v in addr.values() if isinstance(v, str))
                )
            same_as = item.get("sameAs")
            if same_as:
                entry["sameAs"] = same_as if isinstance(same_as, list) else [same_as]
            if entry:
                blocks.append(entry)
    return blocks


def extract_open_graph(soup):
    og = {}
    for tag in soup.find_all("meta"):
        prop = tag.get("property") or tag.get("name") or ""
        if prop.startswith("og:") or prop.startswith("twitter:"):
            content = tag.get("content")
            if content:
                og[prop] = _clean_text(content, 500)
    return og


def extract_social_links(soup):
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        try:
            netloc = urlsplit(href).netloc.lower().replace("www.", "")
        except ValueError:
            continue
        for domain, label in SOCIAL_DOMAINS.items():
            if domain in netloc and label not in found:
                found[label] = href
    return found


def detect_cms(html_text):
    low = html_text.lower()
    for signature, label in CMS_SIGNATURES.items():
        if signature in low:
            return label
    return None


def extract_headings(soup, max_each=5):
    return {
        "h1": [_clean_text(h.get_text(), 150) for h in soup.find_all("h1")][:max_each],
        "h2": [_clean_text(h.get_text(), 150) for h in soup.find_all("h2")][:max_each],
    }


def extract_nav_links(soup, max_links=15):
    """Nav/menu link text is a cheap, strong signal of what a site actually
    offers (e.g. 'Services', 'Menu', 'Book Now', 'Pricing')."""
    labels = []
    containers = soup.find_all(["nav", "header"])
    for container in containers:
        for a in container.find_all("a"):
            text = _clean_text(a.get_text(), 40)
            if text and len(text) > 1:
                labels.append(text)
    seen = []
    for l in labels:
        if l not in seen:
            seen.append(l)
    return seen[:max_links]


def extract_phones(text, limit=5):
    candidates = PHONE_PATTERN.findall(text)
    cleaned = []
    for c in candidates:
        digits = re.sub(r'\D', '', c)
        if 7 <= len(digits) <= 15 and c.strip() not in cleaned:
            cleaned.append(c.strip())
    return cleaned[:limit]


def analyze_page(html_text, url):
    """
    Everything content-intelligence-related that can be pulled from ONE page
    without an LLM call. This is the payload a downstream AI/enrichment node
    should consume.
    """
    soup = BeautifulSoup(html_text, 'lxml')

    title_tag = soup.find("title")
    title = _clean_text(title_tag.get_text()) if title_tag else ""

    meta_desc = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        meta_desc = _clean_text(md["content"], 500)

    open_graph = extract_open_graph(soup)
    jsonld = extract_jsonld(soup)
    headings = extract_headings(soup)
    nav_links = extract_nav_links(soup)
    phones = extract_phones(html_text)
    cms = detect_cms(html_text)

    # Strip script/style/nav noise before pulling a body-text sample —
    # otherwise the "content" signal is 40% menu links and JS junk.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    body_text = _clean_text(soup.get_text(separator=" "), 1500)

    return {
        "url": url,
        "title": title,
        "meta_description": meta_desc,
        "open_graph": open_graph,
        "jsonld": jsonld,
        "headings": headings,
        "nav_links": nav_links,
        "phones_found": phones,
        "cms_platform": cms,
        "body_text_sample": body_text,
    }


def merge_intelligence(page_analyses, site_socials=None):
    """
    Combine per-page analyze_page() results into one site-level summary.
    Prefers the homepage's title/meta/jsonld (most authoritative), but unions
    nav links, phones, and CMS detection across every page crawled.

    site_socials: social links already accumulated across EVERY fetched page
    (see scrape_website), not just the analyzed ones — so max_count actually
    widens social coverage just like email coverage.
    """
    if not page_analyses:
        return {}

    primary = page_analyses[0]
    all_nav, all_social, all_phones, all_jsonld = [], dict(site_socials or {}), [], []
    cms = None

    for p in page_analyses:
        for link in p["nav_links"]:
            if link not in all_nav:
                all_nav.append(link)
        for ph in p["phones_found"]:
            if ph not in all_phones:
                all_phones.append(ph)
        all_jsonld.extend(p["jsonld"])
        if not cms and p["cms_platform"]:
            cms = p["cms_platform"]

    return {
        "title": primary["title"],
        "meta_description": primary["meta_description"],
        "open_graph": primary["open_graph"],
        "jsonld": all_jsonld,
        "nav_links": all_nav[:20],
        "social_links": all_social,
        "phones_found": all_phones[:8],
        "cms_platform": cms,
        "body_text_sample": primary["body_text_sample"],
        "pages_analyzed": [p["url"] for p in page_analyses],
    }


# --------------------------------------------------------------------------- #
# Orchestration — crawl once, extract both emails and intelligence
# --------------------------------------------------------------------------- #
def scrape_website(start_url, max_count=50, max_depth=2, same_domain_only=True,
                    fetcher=None, analyze_pages_limit=5):
    """
    Crawl a site once, extracting BOTH email addresses and content-intelligence
    signals from every page fetched (analysis capped at analyze_pages_limit
    pages to keep the payload reasonable — homepage + a few key pages tell
    you most of what you need).

    Returns:
        {
          "emails": {email: [source_urls]},
          "intelligence": {...merged site-level signals...},
          "pages_scraped": int,
          "errors": [...]
        }
    """
    fetcher = fetcher or Fetcher()
    start_domain = urlsplit(start_url).netloc.replace("www.", "")

    urls_to_process = deque([(start_url, 0)])
    scraped_urls = set()
    email_sources = {}
    site_socials = {}          # social links across EVERY fetched page
    page_analyses = []
    errors = []
    count = 0

    while urls_to_process and count < max_count:
        url, depth = urls_to_process.popleft()
        norm = normalize_url(url)
        if norm in scraped_urls:
            continue
        scraped_urls.add(norm)
        count += 1

        base_url = get_base_url(url)
        page_path = get_page_path(url)
        referer = start_url if url != start_url else None

        text, status = fetcher.get(url, referer=referer)
        if not text:
            errors.append(f"{url}: no content (status={status})")
            if count < max_count and urls_to_process:
                fetcher.polite_sleep()
            continue

        # Headless fallback: if this raw response is an empty JS shell, load the
        # page for real so email/social/analysis run on the rendered content.
        # Only fires when enabled (ENRICH_JS_RENDER) and the page looks hollow.
        if headless_fetch.looks_js_hollow(text) and headless_fetch.enabled():
            rendered = headless_fetch.render(url)
            if rendered:
                text = rendered

        for email in extract_emails(text):
            email_sources.setdefault(email, set()).add(url)

        # Socials are as cheap as emails, so extract them on EVERY fetched page
        # (not throttled to analyze_pages_limit) and union site-wide. Raising
        # max_count therefore widens social coverage, matching emails.
        for label, href in extract_social_links(
                BeautifulSoup(text, 'lxml')).items():
            site_socials.setdefault(label, href)

        if len(page_analyses) < analyze_pages_limit:
            try:
                page_analyses.append(analyze_page(text, url))
            except Exception as e:
                errors.append(f"{url}: analysis error: {e}")

        if max_depth < 0 or depth < max_depth:
            soup = BeautifulSoup(text, 'lxml')
            for anchor in soup.find_all('a'):
                link = anchor.get('href', '').strip()
                if not link or link.lower().startswith(SKIP_SCHEMES):
                    continue
                if any(link.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
                    continue
                full_link = normalize_link(link, base_url, page_path)
                if same_domain_only:
                    link_domain = urlsplit(full_link).netloc.replace("www.", "")
                    if link_domain != start_domain:
                        continue
                if normalize_url(full_link) not in scraped_urls:
                    urls_to_process.append((full_link, depth + 1))

        if count < max_count and urls_to_process:
            fetcher.polite_sleep()

    return {
        "emails": {email: sorted(sources) for email, sources in email_sources.items()},
        "intelligence": merge_intelligence(page_analyses, site_socials),
        "pages_scraped": count,
        "errors": errors,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_proxies(args):
    proxies = []
    if args.proxy:
        p = args.proxy
        proxies.append(p if "://" in p else f"http://{p}")
    if args.proxy_file:
        with open(args.proxy_file) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    proxies.append(line if "://" in line else f"http://{line}")
    return proxies


def main():
    ap = argparse.ArgumentParser(
        description="Crawl a site for emails + content-intelligence signals (no LLM call).")
    ap.add_argument("url", help="starting URL, e.g. https://example.com")
    ap.add_argument("-d", "--max-depth", type=int, default=2,
                     help="0 = first page only, -1 = unlimited (default 2)")
    ap.add_argument("-c", "--max-count", type=int, default=50,
                     help="max number of pages to visit (default 50)")
    ap.add_argument("--analyze-pages-limit", type=int, default=5,
                     help="max number of pages to run content-intelligence "
                          "extraction on (default 5; email crawl still covers "
                          "up to --max-count pages)")
    ap.add_argument("--all-domains", action="store_true",
                     help="follow offsite links too (off by default)")
    ap.add_argument("--proxy", help="single proxy: 'ip:port' or full URL")
    ap.add_argument("--proxy-file", help="text file, one proxy per line")
    ap.add_argument("--min-delay", type=float, default=1.5)
    ap.add_argument("--max-delay", type=float, default=3.5)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="print JSON result to stdout")
    ap.add_argument("-q", "--quiet", action="store_true", help="suppress progress logging")
    args = ap.parse_args()

    proxies = load_proxies(args)
    fetcher = Fetcher(proxies=proxies, delay=(args.min_delay, args.max_delay),
                      max_retries=args.retries, verbose=not args.quiet)

    backends = []
    if HAS_CURL_CFFI:
        backends.append("curl_cffi")
    if HAS_CLOUDSCRAPER:
        backends.append("cloudscraper")
    backends.append("requests")
    if not args.quiet:
        print("HTTP backends: " + ", ".join(backends), file=sys.stderr)

    result = scrape_website(
        args.url, max_count=args.max_count, max_depth=args.max_depth,
        same_domain_only=not args.all_domains, fetcher=fetcher,
        analyze_pages_limit=args.analyze_pages_limit,
    )

    if args.json:
        print(json.dumps(result))
        return

    print(f"\nScraped {result['pages_scraped']} pages, "
          f"found {len(result['emails'])} email(s).\n")
    for email, sources in result["emails"].items():
        print(f"  {email}  <-  {sources[0]}")

    intel = result["intelligence"]
    if intel:
        print("\n--- Content intelligence ---")
        print(f"Title: {intel.get('title')}")
        print(f"Meta description: {intel.get('meta_description')}")
        if intel.get("cms_platform"):
            print(f"Platform: {intel['cms_platform']}")
        if intel.get("nav_links"):
            print(f"Nav links: {', '.join(intel['nav_links'][:10])}")
        if intel.get("social_links"):
            print(f"Social: {intel['social_links']}")
        if intel.get("phones_found"):
            print(f"Phones found on page: {intel['phones_found']}")
        if intel.get("jsonld"):
            print(f"Structured data (schema.org) blocks found: {len(intel['jsonld'])}")

    if result["errors"]:
        print(f"\n{len(result['errors'])} error(s), e.g.: {result['errors'][0]}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print('\n[-] Interrupted.', file=sys.stderr)
        sys.exit(1)

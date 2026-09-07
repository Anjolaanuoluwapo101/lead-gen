#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Per-platform social-profile enrichment (the "grab-what-they-allow" stage).

Input:  the social profile links already found on a business's site, keyed by
        platform — intelligence["social_links"] = {facebook: url, youtube: url, ...}.
        lead_engine collects these on EVERY fetched page (see email_scraper).

Job:    for the platforms an operator selects, pull as much as that platform
        publicly allows and return it keyed the same way. Each payload carries a
        status (ok | link_only | blocked | unverified | error) plus the maximal
        fields that handler could get — we deliberately do NOT trim to a tiny
        fixed schema, so a "money shot" (a phone in a bio, a last-post date, a
        name that contradicts the listing) is never discarded before the LLM
        sees it. The caller drops the result under intelligence["socials"], which
        groq_ai.score_lead() already serialises into the prompt and lead_engine
        already persists as JSON — so scoring and storage need no changes.

Mechanism (decided with the user): public API / instant-parse FIRST, best-effort
logged-out fetch for the walled platforms. No headless browser by default —
social platforms block browsers harder than ordinary sites, so a browser is
fragile and non-hostable here.

  - whatsapp / telegram      -> instant parse / cheap Bot API (no heavy fetch)
  - youtube                  -> Data API v3 when YOUTUBE_API_KEY is set
  - twitter (x.com)          -> oEmbed JSON (public, no key)
  - facebook / instagram /
    tiktok / linkedin / pinterest -> best-effort logged-out fetch of og tags;
    expect unverified/blocked often (login/consent walls). Never a gate.

Only used where a public channel exists; every handler is time-bounded and
wrapped so one platform failing can't kill the pass. New env keys are OPTIONAL —
handlers auto-skip when a key is missing, so the no-key wins (WhatsApp, X
oEmbed, Telegram handle) still work out of the box.
"""

import json
import os
import re
import sys
from urllib.parse import parse_qs, unquote, urlparse

# Platform ids we know how to handle. Matches the labels email_scraper puts in
# intelligence["social_links"] (twitter/x.com are folded into "twitter").
PLATFORM_IDS = {
    "facebook", "instagram", "twitter", "youtube", "tiktok",
    "whatsapp", "telegram", "linkedin", "pinterest",
}

# Env keys read fresh on every call (lead_engine loads .env at import).
def _env(name):
    return os.environ.get(name, "").strip()


def parse_platforms(raw):
    """Normalise an operator's comma string (or iterable) into a set of known
    platform ids. Unknown/misspelled ids are silently ignored."""
    if not raw:
        return set()
    if isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = str(raw).split(",")
    return {p.strip().lower() for p in parts if p.strip().lower() in PLATFORM_IDS}


def _slug_from(url):
    """First path segment of a profile URL (the handle/id), unquoted."""
    try:
        path = urlparse(url).path.strip("/")
        segs = [s for s in path.split("/") if s]
        return unquote(segs[0]) if segs else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# whatsapp — instant parse, no network (phone is already in the URL)
# --------------------------------------------------------------------------- #
def _whatsapp(url):
    try:
        parsed = urlparse(url)
    except Exception:
        return {"status": "ok", "link": url}
    host = (parsed.netloc or "").lower()
    number = ""

    if "wa.me" in host:                       # wa.me/<phone>
        segs = [s for s in parsed.path.strip("/").split("/") if s]
        number = re.sub(r"\D", "", segs[0]) if segs else ""
    elif "api.whatsapp.com" in host and "send" in parsed.path:
        number = re.sub(r"\D", "", parse_qs(parsed.query).get("phone", [""])[0])
    elif "chat.whatsapp.com" in host:         # group invite, no single phone
        return {"status": "ok", "type": "group_invite", "link": url,
                "note": "WhatsApp group invite link (no single phone)"}

    payload = {"status": "ok", "link": url}
    if number:
        payload["number"] = number
        payload["phone"] = number
    else:
        payload["note"] = "WhatsApp link, no parsable phone"
    return payload


# --------------------------------------------------------------------------- #
# telegram — handle always; + title/members/description via Bot API if token set
# --------------------------------------------------------------------------- #
def _telegram(url):
    handle = _slug_from(url)
    payload = {"status": "link_only",
               "handle": ("@" + handle) if handle else "",
               "link": url}
    token = _env("TELEGRAM_BOT_TOKEN")
    if token and handle:
        try:
            import requests
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": "@" + handle}, timeout=8)
            res = (r.json() or {}).get("result") if r.ok else None
            if res:
                payload.update({
                    "status": "ok",
                    "title": res.get("title") or res.get("first_name", ""),
                    "username": res.get("username", ""),
                    "member_count": res.get("member_count"),
                })
                desc = res.get("description") or ""
                if desc:
                    payload["description"] = desc[:400]
        except Exception as e:
            payload["error"] = str(e)[:200]
    return payload


# --------------------------------------------------------------------------- #
# youtube — Data API v3 when a key is present, else best-effort link_only
# --------------------------------------------------------------------------- #
def _youtube(url):
    try:
        segs = [s for s in urlparse(url).path.strip("/").split("/") if s]
    except Exception:
        segs = []
    channel_id = None
    handle = None
    if len(segs) >= 2 and segs[0] == "channel":
        channel_id = segs[1]
    elif len(segs) >= 2 and segs[0] in ("user", "c"):
        handle = "@" + segs[1]
    elif segs and segs[0].startswith("@"):
        handle = segs[0]
    elif segs:
        handle = "@" + segs[0]

    payload = {"status": "link_only", "link": url}
    if channel_id:
        payload["channel_id"] = channel_id
    elif handle:
        payload["slug"] = handle.lstrip("@")

    key = _env("YOUTUBE_API_KEY")
    if not key or not (channel_id or handle):
        return payload
    try:
        import requests
        params = {"key": key, "part": "snippet,statistics"}
        if channel_id:
            params["id"] = channel_id
        else:
            params["forHandle"] = handle       # resolves @handle cleanly
        j = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params=params, timeout=8).json()
        it = (j.get("items") or [{}])[0]
        if not it.get("id"):
            return payload
        sn, st = it.get("snippet", {}), it.get("statistics", {})
        payload.update({
            "status": "ok",
            "channel_id": it["id"],
            "channel_title": sn.get("title", ""),
            "custom_url": sn.get("customUrl", ""),
            "country": sn.get("country", ""),
            "description": (sn.get("description") or "")[:400],
            "created_at": sn.get("publishedAt", ""),
            "subscribers": st.get("subscriberCount"),
            "total_views": st.get("viewCount"),
            "video_count": st.get("videoCount"),
        })
        thumb = sn.get("thumbnails", {}).get("default", {}).get("url")
        if thumb:
            payload["avatar"] = thumb
        # Phase 3 recency hook: the channel's MOST RECENT upload publish date is a
        # genuine last-activity signal (channel created_at is NOT -- it's age).
        # Best-effort: a failure here must not lose the channel payload already
        # gathered, so this runs last inside its own try.
        try:
            v = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"key": key, "part": "snippet", "channelId": it["id"],
                        "order": "date", "type": "video", "maxResults": "1"},
                timeout=8).json()
            vid = (v.get("items") or [{}])[0].get("snippet", {})
            if vid.get("publishedAt"):
                payload["last_video_at"] = vid["publishedAt"]
            if vid.get("title"):
                payload["last_video_title"] = (vid.get("title") or "")[:120]
        except Exception as e:
            payload["error"] = str(e)[:200]
    except Exception as e:
        payload["error"] = str(e)[:200]
    return payload


# --------------------------------------------------------------------------- #
# twitter (x.com) — public oEmbed first; fall back to link_only with handle
# --------------------------------------------------------------------------- #
def _twitter(url):
    slug = _slug_from(url)
    payload = {"status": "link_only",
               "handle": ("@" + slug) if slug else "",
               "link": url}
    try:
        import requests
        r = requests.get("https://publish.twitter.com/oembed",
                         params={"url": url, "omit_script": "true"}, timeout=8)
        j = r.json() if r.ok else {}
        if j.get("author_name"):
            payload.update({
                "status": "ok",
                "author_name": j["author_name"],
                "author_url": j.get("author_url", url),
                "title": j.get("title", ""),
            })
    except Exception as e:
        payload["error"] = str(e)[:200]
    return payload


# --------------------------------------------------------------------------- #
# Walled platforms (facebook/instagram/tiktok/linkedin/pinterest) — best-effort
# logged-out fetch of whatever public og/title tags come back. Expect walls;
# degrade to unverified/blocked, never raise.
# --------------------------------------------------------------------------- #
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")


# ---- JS-render escalation (reuses headless_fetch's shared browser) ---------
# Master switch: SOCIAL_JS_RENDER. Which platforms may escalate: comma list
# SOCIAL_JS_RENDER_PLATFORMS. Defaults to the two where a browser genuinely
# clears a consent wall (facebook, pinterest); extend the env list to try the
# rest (instagram, tiktok, ...) — that's the "feels me" hook for future ones.
def _flag_on(name):
    return _env(name).strip().lower() in ("1", "true", "yes", "on")


def _js_allowed(platform):
    if not _flag_on("SOCIAL_JS_RENDER"):
        return False
    raw = _env("SOCIAL_JS_RENDER_PLATFORMS")
    if raw:
        allowed = {p.strip().lower() for p in raw.split(",") if p.strip()}
    else:
        allowed = {"facebook", "pinterest"}
    return platform in allowed


def _payload_from_html(url, platform, html):
    """Parse og/title/canonical out of an HTML body -> payload. Used for both
    the plain logged-out fetch and the JS-rendered fallback."""
    from bs4 import BeautifulSoup
    payload = {"platform": platform, "link": url, "slug": _slug_from(url)}
    try:
        soup = BeautifulSoup(html, "lxml")
        og = {}
        for m in soup.find_all("meta"):
            k = (m.get("property") or m.get("name") or "").lower()
            if k.startswith("og:") or k in ("twitter:title", "twitter:description"):
                v = m.get("content")
                if v:
                    og.setdefault(k, v[:300])
        title = soup.find("title")
        title = title.get_text(strip=True)[:200] if title else ""
        canon = soup.find("link", attrs={"rel": "canonical"})

        hay = (og.get("og:title", "") + " " + title).lower()
        blocked = any(w in hay for w in (
            "log in", "sign up", "continue to log", "consent", "accept cookies",
            "captcha", "instagram login"))
        payload.update({
            "status": "blocked" if blocked else ("ok" if og else "unverified"),
            "title": title,
        })
        payload.update(og)
        if canon and canon.get("href"):
            payload["canonical"] = canon["href"]
    except Exception as e:
        payload.update({"status": "error", "error": str(e)[:200]})
    return payload


def _plain_html(url):
    """Best-effort logged-out fetch -> html string or None (curl_cffi first,
    plain requests fallback)."""
    try:
        try:
            from curl_cffi import requests as cr
            resp = cr.get(url, timeout=8, impersonate="chrome",
                          follow_redirects=True)
            if resp.status_code < 400:
                return resp.text
        except Exception:
            pass
        import requests
        resp = requests.get(url, timeout=8, headers={"User-Agent": _USER_AGENT})
        return resp.text if resp.status_code < 400 else None
    except Exception:
        return None


def _og_fetch(url, platform):
    html = _plain_html(url)
    if html:
        payload = _payload_from_html(url, platform, html)
    else:
        payload = {"platform": platform, "link": url, "slug": _slug_from(url),
                   "status": "unverified", "note": "no content returned"}

    # Escalate to a headless render ONLY when the cheap fetch didn't resolve the
    # page AND this platform is allowed to use the browser. Extensible by env.
    if payload.get("status") != "ok" and _js_allowed(platform):
        try:
            import headless_fetch
            rendered = headless_fetch.render_page(url)
            if rendered:
                p2 = _payload_from_html(url, platform, rendered)
                if p2.get("status") == "ok" or p2.get("og:title"):
                    p2["via_js_render"] = True
                    return p2
        except Exception:
            pass
    return payload


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_STRUCTURED = {
    "whatsapp": _whatsapp,
    "telegram": _telegram,
    "youtube": _youtube,
    "twitter": _twitter,
}


def enrich(social_links, platforms=None):
    """For each platform link present in social_links AND requested by the
    operator, return {label: payload}. Unrequested platforms are untouched (the
    operator's blank default simply deep-fetches nothing)."""
    want = parse_platforms(platforms)
    out = {}
    for label, url in (social_links or {}).items():
        lab = (label or "").lower()
        if lab not in want or not url:
            continue
        handler = _STRUCTURED.get(lab)
        try:
            out[lab] = handler(url) if handler else _og_fetch(url, lab)
        except Exception as e:
            out[lab] = {"status": "error", "error": str(e)[:200]}
    return out


# --------------------------------------------------------------------------- #
# CLI — handy for testing one social URL: python social_enrich.py <url>
# --------------------------------------------------------------------------- #
_HOST_MAP = {
    "facebook.com": "facebook", "fb.com": "facebook",
    "instagram.com": "instagram", "x.com": "twitter", "twitter.com": "twitter",
    "youtube.com": "youtube", "youtu.be": "youtube", "tiktok.com": "tiktok",
    "wa.me": "whatsapp", "chat.whatsapp.com": "whatsapp",
    "api.whatsapp.com": "whatsapp", "t.me": "telegram", "telegram.me": "telegram",
    "linkedin.com": "linkedin", "pinterest.com": "pinterest",
}


def _label_from_url(url):
    host = urlparse(url).netloc.lower().replace("www.", "")
    for dom, lab in _HOST_MAP.items():
        if dom in host:
            return lab
    return ""


def main():
    if len(sys.argv) < 2:
        print("usage: python social_enrich.py <profile-url>", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    label = _label_from_url(url)
    if not label:
        print(f"unrecognised social host in {url}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(enrich({label: url}, {label}), indent=2))


if __name__ == "__main__":
    main()

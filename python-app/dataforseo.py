#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hostable replacement for google_maps.py's FIND step.

Calls DataForSEO's LIVE Google Maps SERP endpoint
(POST /v3/serp/google/maps/live/advanced) instead of driving a headless
browser. No Chromium, no consent wall, no human-in-the-loop — so it runs in
any container. Output rows match the google_maps.py schema so n8n and the
downstream pipeline are unchanged.

Auth: HTTP Basic Auth with your DataForSEO account login (email) + password,
read from the environment or a .env file next to this script.

Credentials
-----------
    DATAFORSEO_LOGIN=you@email.com
    DATAFORSEO_PASSWORD=...

Usage
-----
    python dataforseo.py "plumbers" "Austin, TX" -n 5
    python dataforseo.py "plumbers" "Austin, TX" --location-name "Austin,Texas,United States" --json
"""

import argparse
import json
import os
import re
import sys
import requests

# --------------------------------------------------------------------------- #
# .env loader (no extra dependency)
# --------------------------------------------------------------------------- #
def _load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

HERE = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(HERE, ".env"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

LOGIN = os.environ.get("DATAFORSEO_LOGIN", "").strip()
PASSWORD = os.environ.get("DATAFORSEO_PASSWORD", "").strip()

LIVE_MAPS_URL = "https://api.dataforseo.com/v3/serp/google/maps/live/advanced"

# US state abbreviations -> full names for turning "Austin, TX" into a
# DataForSEO location_name like "Austin,Texas,United States".
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def to_places_location(place):
    """
    Best-effort turn a human place ("Austin, TX", "New York, NY") into a
    DataForSEO location_name ("Austin,Texas,United States"). If it doesn't look
    like city+US-state, pass it through untouched (a full location_name like
    "London,England,United Kingdom" already works).
    """
    parts = [p.strip() for p in place.split(",") if p.strip()]
    if len(parts) >= 2 and parts[-1].upper() in US_STATES:
        city = ",".join(parts[:-1]).replace(" ", "_")
        state = US_STATES[parts[-1].upper()].replace(" ", "_")
        return f"{city},{state},United States"
    return place.replace(" ", "_")


def _nested_get(obj, keys, default=""):
    cur = obj
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
        if cur is None:
            return default
    return cur if isinstance(cur, (str, int, float)) else default


def _parse_address(text):
    """
    The live API returns the address as one string, e.g.
    '515 Congress Ave #5700, Austin, TX 78701'. Split it into the
    street / city / region / zip fields used downstream.
    """
    if not isinstance(text, str) or not text.strip():
        return "", "", "", ""
    m = re.match(
        r"^(?P<street>.*?),\s*(?P<city>[^,]+?),\s*"
        r"(?P<region>[A-Za-z .]+?)\s*(?P<zip>\d{5}(?:-\d{4})?)?$",
        text.strip(),
    )
    if m:
        return (m.group("street").strip(), m.group("city").strip(),
                m.group("region").strip(), (m.group("zip") or "").strip())
    # Fall back: city is the last comma segment with no clean region/zip.
    segs = [s.strip() for s in text.split(",") if s.strip()]
    return (" ".join(segs[:-1]) if len(segs) > 1 else text, *segs[-1:], "", "")


def _normalize_website(raw):
    """
    DataForSEO returns websites in mixed forms: 'https://x.com', 'www.x.com',
    'x.com', or '//x.com'. Normalize so the value always has an http(s) scheme
    and no trailing junk, so downstream outreach/enrichment gets a usable URL.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    s = raw.strip().rstrip("/.,;")
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return s
    if low.startswith("//"):
        return "https:" + s
    # Bare domain (www.x.com / x.com) — prepend https.
    if low.startswith("www.") or "." in s.split("/", 1)[0]:
        return "https://" + s
    return s


def map_item(item):
    """Map one DataForSEO maps item onto the google_maps.py row schema."""
    rating = item.get("rating") or {}
    addr = item.get("address") or ""

    street, locality, region, zipcode = _parse_address(addr)

    # Website: several possible keys; prefer an http(s) value.
    website = ""
    for k in ("website", "domain", "url"):
        v = item.get(k)
        if isinstance(v, str) and v.lower().startswith(("http", "www", "//")):
            website = v
            break
    if not website:
        website = item.get("url") or ""
    website = _normalize_website(website)

    is_claimed = item.get("is_claimed")
    is_claimed = True if is_claimed is True else (
        False if is_claimed is False else "")

    return {
        "rank": str(_nested_get(item, ["rank_group"]) or
                    _nested_get(item, ["rank_absolute"]) or ""),
        "business_name": item.get("title", ""),
        "telephone": item.get("phone", ""),
        "business_page": website,
        "category": item.get("category", ""),
        "rating": str(rating.get("value", "")),
        "review_count": str(rating.get("votes_count", "")),
        "street": street,
        "locality": locality,
        "region": region,
        "zipcode": zipcode,
        "hours": item.get("work_hours", "") or "",
        "price_level": item.get("price_level", "") or "",
        "place_id": str(item.get("place_id")
                        or item.get("cid") or item.get("feature_id") or ""),
        "plus_code": "",
        "maps_url": item.get("url", ""),
        "listing_url": "",
        # --- signals DataForSEO gives that the scorer should weigh ---
        # book_online_url present => business ALREADY offers online booking, so
        # it is a weak prospect for a booking niche. Kept verbatim (may be a
        # booking-provider page, not the business's own domain).
        "book_online_url": item.get("book_online_url") or "",
        "contact_url": item.get("contact_url") or "",
        "is_claimed": is_claimed,           # True = verified/managed listing
        "domain": item.get("domain") or "",
        "contributor_url": item.get("contributor_url") or "",
        "latitude": item.get("latitude"),
        "longitude": item.get("longitude"),
    }


def scrape_maps(keyword, place, location_name=None, location_code=None,
                max_results=10):
    if not (LOGIN and PASSWORD):
        raise RuntimeError(
            "Missing credentials. Copy .env.example to .env and fill "
            "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD.")

    loc = location_name or to_places_location(place)
    request = {
        "keyword": keyword,          # raw phrase, not URL-encoded (JSON body)
        "language_name": "English",
        "depth": min(int(max_results), 20),
    }
    # location_code and location_name are mutually exclusive in the DataForSEO
    # API. Prefer an explicit location_code (exact, works anywhere incl. NG),
    # else a location_name override, else the US "City, State" guess.
    if location_code:
        request["location_code"] = int(location_code)
    else:
        request["location_name"] = loc
    payload = [request]

    resp = requests.post(
        LIVE_MAPS_URL,
        json=payload,
        auth=(LOGIN, PASSWORD),
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()

    task = ((body.get("tasks") or [{}])[0])
    result = (task.get("result") or [{}])[0]
    items = result.get("items") or []

    rows = [map_item(it) for it in items if it.get("title")]
    return rows, body


def main():
    ap = argparse.ArgumentParser(
        description="Find businesses via DataForSEO Google Maps (hostable).")
    ap.add_argument("keyword", help="e.g. 'plumbers'")
    ap.add_argument("place", help="e.g. 'Austin, TX'")
    ap.add_argument("-n", "--max-results", type=int, default=10)
    ap.add_argument("--location-name", help="override DataForSEO location_name")
    ap.add_argument("--json", action="store_true",
                    help="print mapped rows as JSON")
    ap.add_argument("--raw", action="store_true",
                    help="print the raw API response body (for debugging)")
    args = ap.parse_args()

    try:
        rows, body = scrape_maps(
            args.keyword, args.place,
            location_name=args.location_name,
            max_results=args.max_results,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.raw:
        print(json.dumps(body, indent=2)[:8000])
        return

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print("No businesses returned. Check the location_name and that your "
              "trial credit is available.")
        sys.exit(1)

    for r in rows:
        print(f"{r['rank']:>3}  {r['business_name']:<40} "
              f"{r['telephone']:<18} {(r['business_page'] or '')[:45]}")
    print(f"\n{len(rows)} businesses")


if __name__ == "__main__":
    main()

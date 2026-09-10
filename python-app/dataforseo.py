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
import unicodedata
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
GOOGLE_LOC_URL = "https://api.dataforseo.com/v3/serp/google/locations"

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


def _best_location_match(place, matches):
    """
    Pick the candidate that actually IS `place`. DataForSEO's ranking is loose
    (looking up "Lagos, Nigeria" returns Epe and Ikeja ahead of Lagos itself),
    so matches[0] must never be trusted blindly — silently searching the wrong
    city is worse than returning nothing. Accept a candidate only when its
    LEADING segment (the city) matches the place's leading segment; among those
    prefer a City over a Neighborhood, then the shortest name (most specific).
    Returns None when nothing lines up, so the caller can fall back.
    """
    want = _norm((place or "").split(",")[0])
    if not want:
        return None
    cands = [m for m in (matches or []) if m.get("location_code")]
    if not cands:
        return None

    def head(m):
        return _norm((m.get("location_name") or "").split(",")[0])

    def rank(m):
        return (head(m) != want,                          # city match first
                (m.get("location_type") or "") != "City", # City over the rest
                len(m.get("location_name") or ""))        # most specific name

    best = sorted(cands, key=rank)[0]
    return best if head(best) == want else None


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
                max_results=10, login=None, password=None):
    # login/password may be passed per-call (seller-scoped via the providers
    # source adapter); when omitted they fall back to the master env creds.
    login = (login or LOGIN or "").strip()
    password = (password or PASSWORD or "").strip()
    if not (login and password):
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
        # A non-US place ("Lagos, Nigeria") becomes the invalid location_name
        # "Lagos,_Nigeria" above, which DataForSEO matches against NOTHING — the
        # run then comes back empty with no error. So unless the caller named a
        # location_name explicitly, resolve a real location_code from the cached
        # world directory (offline, memoized). Falls back to the name guess when
        # the directory has no confident match (e.g. a place with no code of its
        # own), preserving the previous behaviour rather than guessing.
        resolved_code = None
        if not location_name:
            try:
                hit = lookup_location(place, top=25)
                if hit.get("exact"):
                    best = _best_location_match(place, hit.get("matches"))
                    if best:
                        resolved_code = best.get("location_code")
            except Exception:
                resolved_code = None      # directory unavailable -> name guess
        if resolved_code:
            request["location_code"] = int(resolved_code)
        else:
            request["location_name"] = loc
    payload = [request]

    resp = requests.post(
        LIVE_MAPS_URL,
        json=payload,
        auth=(login, password),
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()

    task = ((body.get("tasks") or [{}])[0])
    result = (task.get("result") or [{}])[0]
    items = result.get("items") or []

    rows = [map_item(it) for it in items if it.get("title")]
    return rows, body


# --------------------------------------------------------------------------- #
# Location-code lookup (for finding a location_code to hand to scrape_maps).
#
# DataForSEO's locations endpoint returns the WHOLE world (~270k rows) per call
# with no server-side country filter, so to resolve any country we download it
# exactly ONCE and cache it in a local file (data/locations.json). Afterwards
# every lookup is a local file read + in-memory scan: DataForSEO is never re-hit
# and no per-country restriction lingers. The cache is local DISK, not Supabase,
# so it costs nothing against the DB free tier. Per-place results are memoized
# to a second small file (data/location_memo.json) so a repeated place is instant.
# --------------------------------------------------------------------------- #
_DATA_DIR = os.path.join(HERE, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_LOCATIONS_FILE = os.path.join(_DATA_DIR, "locations.json")       # world list
_MEMO_FILE = os.path.join(_DATA_DIR, "location_memo.json")        # place lookups

_LOCATIONS = None            # in-memory copy of the full world list
_MEMO = None                 # place-key -> resolved result
_TYPE_RANK = {"City": 0, "Neighborhood": 1, "District": 2, "Region": 3,
              "State": 4, "Country": 6}            # City first (most specific)
# For BROAD/partial matches we want the biggest useful container first (the
# thing you'd actually set as location_code), not a random sibling.
_BROAD_ORDER = {"Country": 0, "Region": 1, "State": 2, "City": 3}


def _norm(s):
    """Case/accent/punctuation-insensitive form for matching location names.
    Turns any non-alphanumeric run into a space so "Yaba,Lagos,Nigeria" becomes
    the three words "yaba lagos nigeria" (commas/hyphens/slashes must not glue
    tokens together)."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore")
    s = s.decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _load_json_file(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _save_json_file(path, obj):
    """Atomic write (write to .tmp then replace) so a crash can't leave a
    half-written cache behind."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _load_locations():
    """Full world list, seeded once from DataForSEO into a local file; thereafter
    served from the file + in-memory. The bulk download happens exactly once."""
    global _LOCATIONS
    if _LOCATIONS is not None:
        return _LOCATIONS
    locs = _load_json_file(_LOCATIONS_FILE)
    if not locs:
        resp = requests.get(GOOGLE_LOC_URL, auth=(LOGIN, PASSWORD), timeout=180)
        resp.raise_for_status()
        body = resp.json()
        locs = ((body.get("tasks") or [{}])[0].get("result") or [])
        _save_json_file(_LOCATIONS_FILE, locs)
    _LOCATIONS = locs
    return _LOCATIONS


def _load_memo():
    global _MEMO
    if _MEMO is None:
        _MEMO = _load_json_file(_MEMO_FILE) or {}
    return _MEMO


def _save_memo():
    if _MEMO is not None:
        _save_json_file(_MEMO_FILE, _MEMO)


def _resolve(place):
    """Rank every candidate DataForSEO row for a place (uncached core). Returns
    the full ranked list; the caller slices it to its desired `top`."""
    locs = _load_locations()
    q = _norm(place)
    tokens = [t for t in re.split(r"[,\s]+", q) if t and len(t) >= 2]
    if not tokens:
        return {"place": place, "exact": False, "count": 0, "matches": [],
                "note": "Give a place like 'Yaba, Lagos' or 'Lagos, Nigeria'."}

    exact_rows, broad_rows = [], []
    for x in locs:
        name = _norm(x.get("location_name") or "")
        if not name:
            continue
        ty = x.get("location_type") or ""
        if ty == "Postal Code":                   # never useful as a selector
            continue
        words = set(name.split())
        if q and q in name:                       # whole query is a substring
            hit = len(tokens)
        else:
            # WORD-level match so short tokens ("idi") don't false-match inside
            # other words ("Vidin"). Surulere/Idi-Araba -> not a word anywhere.
            hit = sum(1 for t in tokens if t in words)
        if hit == 0:
            continue
        is_exact = hit == len(tokens)
        if is_exact:
            spec = _TYPE_RANK.get(ty, 5)          # City=0 first (most specific)
            exact_rows.append((spec, len(name), x))
        else:
            # container (State/Country) first so "Surulere, Lagos" suggests
            # Lagos State (21564) — the location you'd actually use.
            brank = _BROAD_ORDER.get(ty, 9)
            broad_rows.append((-hit, brank, len(name), x))

    exact_rows.sort(key=lambda z: (z[0], z[1]))
    broad_rows.sort(key=lambda z: (z[0], z[1], z[2]))
    exact = bool(exact_rows)

    # exact rows first (City/Neighborhood up front), then broad containers.
    matches = [{
        "location_code": x["location_code"],
        "location_name": x["location_name"],
        "location_type": x.get("location_type"),
        "country_iso_code": x.get("country_iso_code"),
        "location_code_parent": x.get("location_code_parent"),
        "exact": True,
    } for (_, _, x) in exact_rows]
    matches += [{
        "location_code": x["location_code"],
        "location_name": x["location_name"],
        "location_type": x.get("location_type"),
        "country_iso_code": x.get("country_iso_code"),
        "location_code_parent": x.get("location_code_parent"),
        "exact": False,
    } for (_, _, _, x) in broad_rows]

    note = ""
    if not exact:
        note = ("No DataForSEO location matches every word you gave (the "
                "country's list is coarse and omits some areas). Showing the "
                "nearest broader matches — for an area with no own code, pick "
                "its parent State and area-filter the results by locality.")
    return {"place": place, "exact": exact, "count": len(matches),
            "matches": matches, "note": note}


def lookup_location(place, top=8):
    """
    Turn a human place string ("Yaba, Lagos", "Lekki, Nigeria", "Houston, TX")
    into candidate DataForSEO location_code rows. Matches case-insensitively and
    ignores hyphens/accents. Ranks Cities/Neighborhoods above whole
    States/Countries. Uses the cached world list (seed once) — no per-lookup
    network call — and memoizes each result so a repeated place is instant.
    Honest caveat: DataForSEO's list is coarse — some places (Surulere,
    Idi-Araba, ...) have NO location_code. When none matches every word,
    `exact` is False and the matches are the nearest broader containers (e.g.
    Lagos State 21564) to area-filter on.
    """
    if not (LOGIN and PASSWORD):
        raise RuntimeError(
            "Missing credentials. Copy .env.example to .env and fill "
            "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD.")

    key = _norm(place)
    memo = _load_memo()
    if key not in memo:
        memo[key] = _resolve(place)
        _save_memo()

    res = dict(memo[key])                 # shallow copy: caller must not mutate
    res["matches"] = res["matches"][:max(1, int(top))]
    res["count"] = len(res["matches"])
    return res


def main():
    ap = argparse.ArgumentParser(
        description="Find businesses via DataForSEO Google Maps (hostable).")
    # keyword/place are optional because --seed-locations needs neither.
    ap.add_argument("keyword", nargs="?", help="e.g. 'plumbers'")
    ap.add_argument("place", nargs="?", help="e.g. 'Austin, TX'")
    ap.add_argument("-n", "--max-results", type=int, default=10)
    ap.add_argument("--location-name", help="override DataForSEO location_name")
    ap.add_argument("--json", action="store_true",
                    help="print mapped rows as JSON")
    ap.add_argument("--seed-locations", action="store_true",
                    help="download + cache the DataForSEO world location list "
                         "once into data/locations.json (then exit; lookups are "
                         "offline from then on)")
    ap.add_argument("--raw", action="store_true",
                    help="print the raw API response body (for debugging)")
    args = ap.parse_args()

    if args.seed_locations:
        try:
            locs = _load_locations()
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Cached {len(locs)} locations -> {_LOCATIONS_FILE}")
        sys.exit(0)

    if not (args.keyword and args.place):
        ap.error("keyword and place are required (unless using --seed-locations)")

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

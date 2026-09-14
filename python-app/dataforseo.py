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
import threading
import unicodedata
import requests
from requests.adapters import HTTPAdapter

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
# Fallback for the deployed runtime: .env lives OUTSIDE this directory
# because AgentCore's CodeZip packager copies codeLocation wholesale and
# does NOT exclude .env (only .git/.venv/__pycache__/node_modules are
# skipped). Secrets must never be inside the packaged directory.
_load_dotenv(os.path.join(HERE, os.pardir, ".env"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

LOGIN = os.environ.get("DATAFORSEO_LOGIN", "").strip()
PASSWORD = os.environ.get("DATAFORSEO_PASSWORD", "").strip()

# The `live` endpoints run a real Google search and hold the connection open
# until it finishes, so they are slow by design. Measured against the live API
# with the request this module builds (maps SERP, depth 20, Lagos):
#
#     119.0 seconds, then 76.3 seconds
#     both status 20000 "Ok.", cost 0.002, 20 items returned
#
# The cap used to be a hardcoded 60. Every search was abandoned before its
# answer arrived -- `find_leads` returned find_failed, no campaign was ever
# created, and the agent spent its whole find budget retrying the same call.
# A timeout below the operation it guards does not protect anything; it just
# guarantees the failure. 240 leaves room for a slow day and still fits inside
# the orchestrator's wall clock for a handful of calls.
DEFAULT_REQUEST_TIMEOUT_S = 240


def request_timeout_s():
    """Seconds to wait for a DataForSEO live call.

    Read per call rather than at import so an operator can raise it without a
    redeploy, and so a junk value degrades to the default instead of taking
    the finder down with a ValueError on every search.
    """
    raw = os.environ.get("DATAFORSEO_TIMEOUT_S", "").strip()
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_REQUEST_TIMEOUT_S
    return value if value > 0 else DEFAULT_REQUEST_TIMEOUT_S


LIVE_MAPS_URL = "https://api.dataforseo.com/v3/serp/google/maps/live/advanced"
GOOGLE_LOC_URL = "https://api.dataforseo.com/v3/serp/google/locations"


# One shared Session for connection reuse (TLS+TCP handshake once, not per
# search). Deliberately NO automatic retries: every call here is a BILLED
# live task, and a retried POST could fire — and bill — twice. Timeouts and
# HTTP errors fail loudly so the caller (and the run trace) sees exactly one
# attempt and one outcome.
_api_lock = threading.Lock()
_API_SESSION = None


def _api_session():
    global _API_SESSION
    if _API_SESSION is None:
        with _api_lock:
            if _API_SESSION is None:
                s = requests.Session()
                adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _API_SESSION = s
    return _API_SESSION

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


# Short tokens that name a whole country, expanded before any matching so a
# query like "London, UK" can meet "London,England,United Kingdom" on shared
# words. Kept tiny on purpose: these are the aliases sellers actually type.
_COUNTRY_ALIASES = {
    "uk": ("united", "kingdom"),
    "usa": ("united", "states"),
    "us": ("united", "states"),
    "uae": ("united", "arab", "emirates"),
}


def _query_tokens(place):
    """Every word the seller's place string asks for, normalised.

    Splits on commas and whitespace, drops one letter noise, and expands the
    abbreviations sellers type but the directory never carries: US state codes
    ("TX" -> "texas") via US_STATES, and the small country alias map above
    ("UK" -> "united kingdom"). Both _resolve and _best_location_match read
    through this, so the two agree on what "exact" means — previously the
    resolver counted raw tokens while the matcher compared one segment, and a
    query that was exact for one was partial for the other.
    """
    out = []
    for tok in re.split(r"[,\s]+", _norm(place or "")):
        if not tok or len(tok) < 2:
            continue
        upper = tok.upper()
        if upper in US_STATES:
            out.append(_norm(US_STATES[upper]))
            continue
        if tok in _COUNTRY_ALIASES:
            out.extend(_COUNTRY_ALIASES[tok])
            continue
        out.append(tok)
    return out


def _best_location_match(place, matches):
    """
    Pick the candidate that actually IS `place`. DataForSEO's ranking is loose
    (looking up "Lagos, Nigeria" returns Epe and Ikeja ahead of Lagos itself),
    so matches[0] must never be trusted blindly — silently searching the wrong
    city is worse than returning nothing.

    Ranked by how many of the query's words each candidate carries (state codes
    expanded, so "TX" meets "Texas"), then by a matching leading segment, then
    City over the rest, then the shortest name. Returns None when nothing shares
    even one word, so the caller can fall back.

    Fixed 2026-09-13. The previous version compared ONLY the leading segment,
    discarding everything after the first comma, so "Austin, TX" resolved to
    Austin,Manitoba,Canada (shorter than Austin,Texas,United States) and
    "Houston, TX" to Houston,Ohio. A bare "Texas" returned nothing at all.
    """
    want_tokens = _query_tokens(place)
    if not want_tokens:
        return None
    want_set = set(want_tokens)
    want_head = _norm((place or "").split(",")[0])
    cands = [m for m in (matches or []) if m.get("location_code")]
    if not cands:
        return None

    def words(m):
        return set(_norm(m.get("location_name") or "").split())

    def head(m):
        return _norm((m.get("location_name") or "").split(",")[0])

    def rank(m):
        w = words(m)
        overlap = len(want_set & w)
        return (-overlap,                            # most shared words first
                head(m) != want_head,                # then the leading segment
                (m.get("location_type") or "") != "City",  # City over the rest
                len(m.get("location_name") or ""),   # most specific name
                m.get("location_name") or "")        # stable, not input ordered

    best = sorted(cands, key=rank)[0]
    if not (want_set & words(best)):
        return None
    return best


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

    resp = _api_session().post(
        LIVE_MAPS_URL,
        json=payload,
        auth=(login, password),
        timeout=request_timeout_s(),
    )
    resp.raise_for_status()
    body = resp.json()

    task = ((body.get("tasks") or [{}])[0])
    result = (task.get("result") or [{}])[0]
    items = result.get("items") or []

    rows = [map_item(it) for it in items if it.get("title")]
    return rows, body


# --------------------------------------------------------------------------- #
# ONE business, by name or by id (Business Data API).
# --------------------------------------------------------------------------- #
# A different product from the Maps SERP call above: that one takes a PHRASE and
# returns a ranked list, this takes an IDENTITY and returns one entity. It is
# what makes "I already know which business I want to pitch" expressible, which
# the keyword+location search cannot express at all.
BUSINESS_INFO_URL = ("https://api.dataforseo.com/v3/business_data/google/"
                     "my_business_info/live")


def _business_keyword(place_id=None, cid=None, keyword=None):
    """The single `keyword` string, built from the best identifier on hand.

    The `cid:` / `place_id:` forms are NOT separate request fields -- despite
    the docs listing them as identifiers, they are passed INSIDE `keyword` as a
    prefix. (See the endpoint's own examples: "cid:194604053573767737".)

    Precedence is exact-before-fuzzy on purpose: a bare name resolves to the
    best match, which for a common business name is frequently the wrong
    location entirely. A place_id resolves to one entity and nothing else.
    """
    for prefix, value in (("place_id", place_id), ("cid", cid)):
        v = str(value or "").strip()
        if v:
            return v if v.startswith(prefix + ":") else f"{prefix}:{v}"
    return str(keyword or "").strip()


def _hours_from_work_time(work_time):
    """A readable hours string from the structured `work_time`, or "".

    Defensive throughout: `work_time` is documented but its inner shape varies
    by whether the listing publishes hours at all, and a hand-added business
    with no published hours must come back as "" rather than raising -- the
    absence IS the information (it is one of the gaps worth drafting around).
    """
    if not isinstance(work_time, dict):
        return ""
    hours = work_time.get("work_hours") or {}
    timetable = (hours.get("timetable") or {}) if isinstance(hours, dict) else {}
    out = []
    for day, slots in (timetable or {}).items():
        if not isinstance(slots, list) or not slots:
            continue
        first = slots[0] if isinstance(slots[0], dict) else {}
        op = (first.get("open") or {}) if isinstance(first, dict) else {}
        cl = (first.get("close") or {}) if isinstance(first, dict) else {}
        if not (op or cl):
            continue
        out.append(f"{day.title()} {op.get('hour', '')}:"
                   f"{str(op.get('minute', 0)).zfill(2)}-"
                   f"{cl.get('hour', '')}:{str(cl.get('minute', 0)).zfill(2)}")
    return " | ".join(out)


def map_business_info(item):
    """Map a my_business_info item onto the SAME keys as `map_item`.

    Same shape on purpose. A hand-added business then flows through the
    prospect insert and everything downstream exactly like a found one, with no
    second code path to drift out of sync. The Business Data extras (photo
    count, structured hours, attributes, rating spread) ride along in
    `business_info` rather than becoming columns nobody asked for -- the
    prospect row's `raw_payload` jsonb already stores whatever it is handed.
    """
    if not isinstance(item, dict):
        return None
    addr = item.get("address") or ""
    street, locality, region, zipcode = _parse_address(addr)

    # `rating` is an object on this endpoint; tolerate a bare number too rather
    # than losing a real rating to a shape change.
    rating = item.get("rating")
    if isinstance(rating, dict):
        rating_value, votes = rating.get("value"), rating.get("votes_count")
    else:
        rating_value, votes = rating, None

    work_time = item.get("work_time")
    hours = _hours_from_work_time(work_time)
    if not hours:
        hours = item.get("work_hours") or ""

    website = ""
    for k in ("url", "domain"):
        v = item.get(k)
        if isinstance(v, str) and v.lower().startswith(("http", "www", "//")):
            website = v
            break
    if not website:
        website = item.get("url") or ""
    website = _normalize_website(website)

    claimed = item.get("is_claimed")
    is_claimed = True if claimed is True else (False if claimed is False else "")

    return {
        "rank": "",
        "business_name": item.get("title", ""),
        "telephone": item.get("phone", ""),
        "business_page": website,
        "category": item.get("category", ""),
        "rating": str(rating_value if rating_value is not None else ""),
        "review_count": str(votes if votes is not None else ""),
        "street": street, "locality": locality,
        "region": region, "zipcode": zipcode,
        "hours": hours,
        "price_level": item.get("price_level") or "",
        "place_id": str(item.get("place_id") or item.get("cid")
                        or item.get("feature_id") or ""),
        "plus_code": "",
        "maps_url": item.get("url", ""),
        "listing_url": "",
        "book_online_url": item.get("book_online_url") or "",
        "contact_url": item.get("contact_url") or "",
        "is_claimed": is_claimed,
        "domain": item.get("domain") or "",
        "contributor_url": item.get("contributor_url") or "",
        "latitude": item.get("latitude"),
        "longitude": item.get("longitude"),
        # Everything the Business Data API gives that the Maps SERP call does
        # not. Kept together so the prospect row stays the shape it always was.
        "business_info": {
            "current_status": (work_time or {}).get("current_status")
                              if isinstance(work_time, dict) else None,
            "total_photos": item.get("total_photos"),
            "rating_distribution": item.get("rating_distribution"),
            "attributes": item.get("attributes"),
            "place_topics": item.get("place_topics"),
            "services": item.get("services"),
            "popular_times": item.get("popular_times"),
            "snippet": item.get("snippet"),
            "description": item.get("description"),
            "additional_categories": item.get("additional_categories"),
            "address_info": item.get("address_info"),
        },
    }


def lookup_business(place_id=None, cid=None, keyword=None, location_name=None,
                    location_code=None, login=None, password=None):
    """Look up ONE business. Returns (row, body); row is None when not found.

    Address it by whatever you have -- `place_id` (exact), `cid` (exact), or a
    `keyword` name (fuzzy). See `_business_keyword` for why the order matters.

    Location is optional here and passed through when known: a place_id or cid
    is unique on its own, while a bare name needs one to disambiguate. When
    DataForSEO rejects the request the API's own message is surfaced rather
    than swallowed, because "location required" is an operator problem with an
    operator fix, not a code failure.

    Costs one billable call per invocation. Callers MUST NOT loop this without
    a bound -- see the switch on the route that uses it.
    """
    login = (login or LOGIN or "").strip()
    password = (password or PASSWORD or "").strip()
    if not (login and password):
        raise RuntimeError(
            "Missing credentials. Copy .env.example to .env and fill "
            "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD.")

    key = _business_keyword(place_id=place_id, cid=cid, keyword=keyword)
    if not key:
        raise ValueError(
            "lookup_business needs one of place_id, cid or keyword -- there is "
            "nothing to look up.")

    request = {"keyword": key, "language_name": "English"}
    if location_code:
        request["location_code"] = int(location_code)
    elif location_name:
        request["location_name"] = location_name

    resp = _api_session().post(
        BUSINESS_INFO_URL,
        json=[request],
        auth=(login, password),
        timeout=request_timeout_s(),
    )
    resp.raise_for_status()
    body = resp.json()

    task = ((body.get("tasks") or [{}])[0])
    status = task.get("status_code")
    if status not in (20000, None):
        # A 40xxx here is usually "location is required", which we can only
        # report -- the caller decides whether to ask the operator for one.
        raise RuntimeError(
            f"DataForSEO business lookup failed ({status}): "
            f"{task.get('status_message') or 'no message'}")

    items = ((task.get("result") or [{}])[0]).get("items") or []
    for it in items:
        row = map_business_info(it)
        if row and row.get("business_name"):
            return row, body
    return None, body


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


def _save_json_file(path, obj, compact=False):
    """Atomic write (write to .tmp then replace) so a crash can't leave a
    half-written cache behind. Compact drops the spaces stdlib json emits,
    which shrinks the 15MB picker cache by about a fifth and parses faster."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        if compact:
            json.dump(obj, fh, separators=(",", ":"))
        else:
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
        resp = _api_session().get(GOOGLE_LOC_URL, auth=(LOGIN, PASSWORD), timeout=180)
        resp.raise_for_status()
        body = resp.json()
        locs = ((body.get("tasks") or [{}])[0].get("result") or [])
        _save_json_file(_LOCATIONS_FILE, locs)
    _LOCATIONS = locs
    return _LOCATIONS


# Rows shared across machines, newest first. The file holds everything ever
# learned on THIS machine; the table holds what EVERY machine learned. 2000
# rows is years of distinct cities at this write rate, and the cap is what
# keeps a "load the cache" query from becoming a full-table download.
_MEMO_DB_LIMIT = 2000
_MEMO_TABLE = "location_memo"
_MEMO_DIRTY = set()


def _memo_db():
    """supabase_store when a shared memo is possible, else None.

    Imported lazily so dataforseo.py stays usable as a standalone CLI script
    without a configured database: no creds, no table, no error — file only.
    """
    try:
        import supabase_store
    except ImportError:
        return None
    try:
        if not supabase_store.configured():
            return None
    except Exception:
        return None
    return supabase_store


def _load_memo():
    global _MEMO
    if _MEMO is None:
        file_memo = _load_json_file(_MEMO_FILE) or {}
        db = _memo_db()
        if db is None:
            _MEMO = file_memo
            return _MEMO
        try:
            rows = db.select_rows(
                _MEMO_TABLE, columns="place_key,result",
                order="updated_at.desc", limit=_MEMO_DB_LIMIT)
        except Exception:
            _MEMO = file_memo
            return _MEMO
        merged = dict(file_memo)
        for row in rows or []:
            key = row.get("place_key")
            if key and key not in merged and isinstance(
                    row.get("result"), dict):
                merged[key] = row["result"]
        _MEMO = merged
    return _MEMO


def _save_memo():
    if _MEMO is None:
        return
    _save_json_file(_MEMO_FILE, _MEMO)
    if not _MEMO_DIRTY:
        return
    db = _memo_db()
    if db is None:
        _MEMO_DIRTY.clear()
        return
    # Only newly learned keys go up: the steady state (all hits) writes
    # nothing, so a lookup-heavy process stays read-quiet.
    batch = [{"place_key": key, "result": _MEMO[key]}
             for key in list(_MEMO_DIRTY) if key in _MEMO]
    _MEMO_DIRTY.clear()
    if not batch:
        return
    try:
        db.upsert_rows(_MEMO_TABLE, batch, on_conflict="place_key")
    except Exception:
        pass  # file copy already saved above; the share can wait


def _resolve(place):
    """Rank every candidate DataForSEO row for a place (uncached core). Returns
    the full ranked list; the caller slices it to its desired `top`."""
    locs = _load_locations()
    q = _norm(place)
    tokens = _query_tokens(place)
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
        _MEMO_DIRTY.add(key)
        _save_memo()

    res = dict(memo[key])                 # shallow copy: caller must not mutate
    res["matches"] = res["matches"][:max(1, int(top))]
    res["count"] = len(res["matches"])
    return res


# Types the dashboard picker may offer. Compared case-insensitively, because
# the directory carries a long tail of lowercase variants and a case sensitive
# check would silently drop them. Everything else — postal codes (the bulk of
# the file), airports, universities, parks, TV and DMA regions, congressional
# districts — can never be a lead gen search area, so the picker never shows
# them. A picker that offers a national park as a search area is broken by
# construction.
PICKER_TYPES = {"country", "state", "region", "city", "municipality",
                "district", "neighborhood"}
_PICKER_TYPE_RANK = {"city": 0, "municipality": 1, "district": 2,
                    "neighborhood": 3, "state": 4, "region": 5, "country": 6}


_PICKER_INDEX = None  # precomputed picker rows, built once per process
# Compact picker cache: the pickable slice with normalisation already done, so
# a cold process loads it in about a second instead of paying a 44MB parse
# plus 270k normalisations (measured at 25 seconds). Written by
# `dataforseo.py --seed-picker`, shipped inside the image like locations.json.
_PICKER_FILE = os.path.join(_DATA_DIR, "location_picker.json")


def _picker_fingerprint():
    """Size + mtime of the world list, so a reseed invalidates the cache.

    Without this a refreshed locations.json would keep serving the old slice
    with no error to say so — the exact silent staleness this module exists
    to prevent elsewhere.
    """
    try:
        st = os.stat(_LOCATIONS_FILE)
        return [st.st_size, int(st.st_mtime)]
    except OSError:
        return None


def _build_picker_index(locs):
    """The pickable slice of the world list as tuples.

    One entry per row: (name, normed, head, type_rank, type, length, code,
    country, parent). The word set is rebuilt from normed at load (a split is
    cheap; storing it would bloat the cache). Rows outside PICKER_TYPES are
    dropped here, so a search never even looks at the 130k postal codes.
    """
    index = []
    for x in (locs or []):
        if not isinstance(x, dict):
            continue
        name = x.get("location_name") or ""
        normed = _norm(name)
        if not normed:
            continue
        ty = str(x.get("location_type") or "")
        if ty.lower() not in PICKER_TYPES:
            continue
        code = x.get("location_code")
        if not code:
            continue
        index.append((name, normed, _norm(name.split(",")[0]),
                      _PICKER_TYPE_RANK.get(ty.lower(), 9),
                      x.get("location_type"), len(name), code,
                      x.get("country_iso_code"),
                      x.get("location_code_parent")))
    return index


def _load_picker_index():
    """Fast path: the compact cache when it matches the world list on disk.

    Returns 9-tuples without the word set; the caller adds that (a split is
    cheap, storing it would bloat the cache). None when there is nothing
    fresh to load, and the caller falls back to a full rebuild.
    """
    if isinstance(_LOCATIONS, list) and _LOCATIONS:
        # A reseed already holds the list in memory; the file fingerprint
        # cannot speak for it, so build from memory instead of the cache.
        return _build_picker_index(_LOCATIONS)
    cached = _load_json_file(_PICKER_FILE)
    if isinstance(cached, dict) and cached.get("fingerprint") \
            == _picker_fingerprint():
        rows = cached.get("rows")
        if isinstance(rows, list) and rows:
            return [tuple(r) for r in rows]
    return None


def seed_picker_index():
    """Write the compact picker cache from the world list. Returns row count.

    Run once after seeding locations (`--seed-locations`, then this). The
    dashboard deploy ships whatever is on disk, so seeding here is what makes
    the picker's first keystroke fast in the container too.
    """
    locs = _load_locations()
    index = _build_picker_index(locs)
    _save_json_file(_PICKER_FILE,
                    {"fingerprint": _picker_fingerprint(), "rows": index},
                    compact=True)
    global _PICKER_INDEX
    _PICKER_INDEX = None
    return len(index)


def _picker_index():
    """The pickable slice of the world list, ready to scan.

    Built once per process: compact cache when fresh (about a second),
    otherwise a full rebuild from the world list. Every call after the first
    scans memory only (about half a second a query). Still offline throughout:
    no credentials, no network.
    """
    global _PICKER_INDEX
    if _PICKER_INDEX is not None:
        return _PICKER_INDEX
    index = _load_picker_index()
    if not index:
        locs = _load_json_file(_LOCATIONS_FILE)
        if not (isinstance(locs, list) and locs) \
                and isinstance(_LOCATIONS, list) and _LOCATIONS:
            locs = _LOCATIONS
        index = _build_picker_index(locs)
    _PICKER_INDEX = [(n, normed, set(normed.split()), h, tr, ty, ln, c, cc, p)
                     for (n, normed, h, tr, ty, ln, c, cc, p) in index]
    return _PICKER_INDEX


def search_locations(query, limit=10):
    """Typeahead candidates for the dashboard place picker.

    Reads the cached world list (no credentials, no network) and returns up to
    `limit` rows whose names share words with the query, restricted to
    PICKER_TYPES. Ranked by shared word count, then by type (City first), then
    by name length and name, so "Lagos" offers the City before the State and a
    same named city in another country never outranks the one asked for when
    the query names the state too.
    """
    try:
        count = max(1, min(int(limit or 10), 25))
    except (TypeError, ValueError):
        count = 10
    q = _norm(query or "")
    tokens = _query_tokens(query or "")
    if not q or len(q) < 2 or not tokens:
        return {"q": query or "", "count": 0, "matches": []}
    want = set(tokens)
    want_head = _norm(str(query or "").split(",")[0])
    scored = []
    for (name, normed, words, head, tyrank, ty, namelen, code, country,
            parent) in _picker_index():
        if q and q in normed:
            hit = len(tokens)
        else:
            hit = len(want & words)
        if hit == 0:
            continue
        scored.append((-hit, head != want_head, tyrank, namelen, name,
                       {"location_code": code,
                        "location_name": name,
                        "location_type": ty,
                        "country_iso_code": country,
                        "location_code_parent": parent,
                        "exact": hit == len(tokens)}))
    scored.sort(key=lambda z: (z[0], z[1], z[2], z[3], z[4]))
    matches = [m for (_, _, _, _, _, m) in scored[:count]]
    return {"q": query or "", "count": len(matches), "matches": matches}


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
    ap.add_argument("--seed-picker", action="store_true",
                    help="precompute the dashboard picker's compact cache from "
                         "data/locations.json into data/location_picker.json "
                         "(then exit; run once after --seed-locations so the "
                         "first keystroke is fast)")
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

    if args.seed_picker:
        try:
            count = seed_picker_index()
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Cached {count} pickable areas -> {_PICKER_FILE}")
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

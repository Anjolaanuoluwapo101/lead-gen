#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Deterministic business identity for Phase 2 dedup (NO network, NO deps).

Two jobs:
  1. dedup_key(row)     -- a stable fingerprint for cheap hashing/indexing.
  2. same_business(a,b) -- the OR-semantics decision: "are these two rows the
     same real-world business?" Strong, normalised single signals are enough on
     their own (same website, or same phone); otherwise name + locality must
     BOTH match. This is deliberately more robust than requiring one exact hash,
     because local listings vary ("Swish Dental" vs "Swish Dental LLC", a phone
     with/without country code, a site with/without www).

Row inputs mirror the fields used everywhere downstream (dataforseo.map_item /
_row_prospect): business_name, telephone/phone, business_page/website, locality.
Helpers read a couple of aliases so callers can pass either shape.
"""

import hashlib
import re

# --------------------------------------------------------------------------- #
# Normalisers
# --------------------------------------------------------------------------- #
# Legal-suffix tokens stripped when comparing names, so "Swish Dental LLC",
# "Swish Dental, Inc." and "Swish Dental" all reduce to "swish dental".
_LEGAL = re.compile(r"\b(llc|inc|incorporated|corp|corporation|ltd|limited|"
                    r"co|company|pty|plc|gmbh|sarl|sa|group|dba)\b\.?$")

# Tokens that add noise to a locality comparison ("downtown", "center").
_NOISE = re.compile(r"\b(downtown|centre|center|city|town|district)\b")


def _s(v):
    return "" if v is None else str(v)


def norm_name(name):
    """Lowercase, strip punctuation/legal suffixes, keep letters+digits."""
    s = _s(name).lower().strip()
    s = _LEGAL.sub("", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)      # punctuation -> space
    return " ".join(s.split())


def norm_phone(phone):
    """Digits only (so country codes/spacing don't split one number)."""
    return re.sub(r"\D", "", _s(phone))


def norm_website(url):
    """Scheme + host (lowercased, no 'www.'), or '' if none/placeholder."""
    s = _s(url).strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/")[0]                    # host only
    if s.startswith("www."):
        s = s[4:]
    return s


def norm_locality(locality):
    """Lowercase, strip common noise words, keep letters+digits."""
    s = _s(locality).lower()
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


# --------------------------------------------------------------------------- #
# Lookup helpers -- read either the finder shape or the prospect/lead shape.
# --------------------------------------------------------------------------- #
def _get(row, *keys, default=""):
    for k in keys:
        v = (row or {}).get(k)
        if v not in (None, "", 0, False):
            return v
    return default


def _parts(row):
    return (
        norm_name(_get(row, "business_name")),
        norm_phone(_get(row, "telephone", "phone")),
        norm_website(_get(row, "business_page", "website")),
        norm_locality(_get(row, "locality")),
    )


def dedup_key(row):
    """Stable sha1 fingerprint of the business identity, for DB-side lookups."""
    name, phone, website, locality = _parts(row)
    return hashlib.sha1(
        f"{name}|{phone}|{website}|{locality}".encode("utf-8")).hexdigest()


def same_business(a, b):
    """True if a and b look like the same business. OR-semantics:
      - a shared, non-empty website  => yes
      - a shared, non-empty phone    => yes
      - otherwise only when BOTH normalised name AND locality match.
    Empty website/phone are ignored (can't prove identity on absence).
    """
    a_name, a_phone, a_web, a_loc = _parts(a)
    b_name, b_phone, b_web, b_loc = _parts(b)

    if a_web and a_web == b_web:
        return True
    if a_phone and a_phone == b_phone:
        return True
    # Weak signal guard: name+locality only counts when we have a real name and
    # a real locality for BOTH sides, otherwise we'd match empty rows together.
    if a_name and b_name and a_loc and b_loc and a_name == b_name and a_loc == b_loc:
        return True
    return False


# --------------------------------------------------------------------------- #
# Unit checks -- run: python identity.py
# --------------------------------------------------------------------------- #
def _run_checks():
    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)
        print(("ok  " if cond else "FAIL") + "  " + name)

    # Normalisers
    check("norm_name strips legal suffix + case",
          norm_name("Swish Dental LLC") == norm_name("swish dental"))
    check("norm_phone keeps digits only",
          norm_phone("+1 (512) 555-0199") == "15125550199")
    check("norm_website strips scheme/www/path",
          norm_website("https://WWW.SwishDental.com/contact") == "swishdental.com")
    check("norm_locality strips noise + case",
          norm_locality("Austin, Downtown") == norm_locality("AUSTIN"))

    # Same business -- strong single signals.
    r1 = {"business_name": "Swish Dental LLC", "telephone": "+1 512 555 0199",
          "business_page": "https://www.swishdental.com", "locality": "Austin"}
    r2 = {"business_name": "Swish Dental", "telephone": "5125550199",
          "business_page": "https://swishdental.com/contact", "locality": "Austin"}
    r3 = {"business_name": "Primavera Dental", "telephone": "+1 512 555 0199",
          "business_page": "", "locality": "Houston"}     # same phone, diff site
    r4 = {"business_name": "Swish Dental", "telephone": "",
          "business_page": "", "locality": "Austin"}       # name+locality match
    r5 = {"business_name": "Swish Dental", "telephone": "",
          "business_page": "", "locality": "Houston"}      # name only, diff place
    r6 = {"business_name": "", "telephone": "", "business_page": "", "locality": ""}
    r7 = {"business_name": "Other Co", "telephone": "999", "business_page": "",
          "locality": "Austin"}

    check("same website (www/case/path variants) => same",
          same_business(r1, r2))
    check("same phone, different listing => same",
          same_business(r1, r3))
    check("name + locality match (no web/phone) => same",
          same_business(r1, r4))
    check("name only, different locality => NOT same",
          not same_business(r1, r5))
    check("empty row is never a match",
          not same_business(r6, r1) and not same_business(r1, r6))
    check("different everything => NOT same",
          not same_business(r1, r7))

    # Dedup key stability: identical normalised identity (different formatting,
    # legal suffix, whitespace, locality noise) must hash the same. Note r2 has
    # a different PHONE digit string (no +1), so it is NOT a key match -- it is
    # proven equal only via the website signal in same_business above.
    r8 = {"business_name": "  Swish Dental , LLC ",
          "telephone": "+1 (512) 555-0199",
          "business_page": "http://WWW.swishdental.com/",
          "locality": "Austin, Downtown"}
    check("dedup_key stable across formatting variants",
          dedup_key(r1) == dedup_key(r8))
    check("dedup_key differs for genuinely different businesses",
          dedup_key(r1) != dedup_key(r7))

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {failures}")
        return 1
    print("All identity checks passed.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_checks())

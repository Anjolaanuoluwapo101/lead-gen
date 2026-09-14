#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Deterministic evidence scoring for Phase 1 (NO LLM, NO network).

This module owns the two things that must NEVER be invented by the model:

  1. digital_presence class     -- what online footprint the business actually
                                   owns, decided purely from evidence.
  2. deterministic sub-scores   -- how reachable / active / verified / well-scraped
                                   the business is, each 0-100 from real signals.

Why it exists
-------------
The LLM (groq_ai.score_lead) is told what to *interpret* -- it never generates
the raw evidence numbers. We compute the numbers here, feed them to the model,
and let it weigh them against the niche into an opportunity + confidence pair.
To stop thin data quietly looking confident, we also derive a confidence_cap from
data quality; the caller clamps the model's confidence to it. That is the
anti-pollution guardrail: a lively Facebook can lift OPPORTUNITY, but it can
never dress a barely-scraped business up as high-CONFIDENCE.

Inputs mirror the real pipeline shapes:
  row          -- one dataforseo.map_item() dict (finder row)
  intelligence -- one email_scraper.merge_intelligence() dict (site crawl),
                  optionally with intelligence["socials"] from social_enrich.
  emails       -- list/dict of emails found by the crawler (lead_engine already
                  computes this separately from the crawl).

Pure functions only -- callable from unit checks without a database or credits.
"""

import re
import datetime

import identity  # reuse identity.norm_website for de-duping profile hosts

# --------------------------------------------------------------------------- #
# Digital-presence taxonomy (cheapest, most-legible upgrade in the product)
# --------------------------------------------------------------------------- #
# Ordinal so callers can sort/compare classes, and each level maps to a base
# "presence" sub-score below.
PRESENCE_LEVEL = {
    "OFF_GRID": 0,
    "SOCIAL_ONLY": 1,
    "WEBSITE_ONLY": 2,
    "WEBSITE_AND_SOCIAL": 3,
    "STRONG_DIGITAL_PRESENCE": 4,
}
PRESENCE_BASE_SCORE = {
    "OFF_GRID": 12,
    "SOCIAL_ONLY": 38,
    "WEBSITE_ONLY": 58,
    "WEBSITE_AND_SOCIAL": 78,
    "STRONG_DIGITAL_PRESENCE": 95,
}


def _s(row, key, default=""):
    v = (row or {}).get(key)
    return default if v is None else str(v).strip()


def _email_count(emails):
    if emails is None:
        return 0
    if isinstance(emails, dict):
        return len(emails)
    if isinstance(emails, (list, tuple, set)):
        return len(emails)
    if isinstance(emails, str) and emails.strip():
        return 1
    return 0


def _to_float(v):
    try:
        f = float(v)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


def _to_int(v):
    f = _to_float(v)
    return None if f is None else int(f)


# Messaging / direct channels (reachable for outreach without opening a website).
_MESSAGING = {"whatsapp", "telegram"}


def classify_presence(row, intelligence, emails):
    """Return the digital-presence class string.

    Honest note on SOCIAL_ONLY: in the live pipeline socials are only discovered
    by crawling a *website*, so a site-less business carries no socials and
    resolves to OFF_GRID here. SOCIAL_ONLY is kept in the taxonomy for callers
    that can supply socials another way (e.g. Phase 3 listing/social lookups) --
    the function emits it given those inputs, and the unit checks cover it.
    """
    has_site = bool(_s(row, "business_page"))
    intel = intelligence or {}

    social_links = intel.get("social_links") or {}
    n_social = sum(1 for v in social_links.values() if v)
    n_emails = _email_count(emails)
    structured = bool(intel.get("jsonld")) or bool(intel.get("open_graph"))
    cms = bool(intel.get("cms_platform"))
    meta = bool(intel.get("meta_description") or intel.get("title"))
    body = (intel.get("body_text_sample") or "").strip()

    if not has_site:
        return "SOCIAL_ONLY" if n_social > 0 else "OFF_GRID"

    if n_social > 0:
        # STRONG requires a genuinely rich site: real body content backed by
        # structured data or a recognised CMS, plus reachable contact. A thin
        # site that merely carries social icons stays WEBSITE_AND_SOCIAL.
        rich = len(body) > 200 and (structured or cms)
        if rich and (n_emails > 0 or n_social >= 2):
            return "STRONG_DIGITAL_PRESENCE"
        return "WEBSITE_AND_SOCIAL"

    return "WEBSITE_ONLY"


def _gather_signals(row, intelligence, emails):
    """Collect every raw signal the sub-scores are built from, so the breakdown
    can cite evidence (provenance) rather than just a number."""
    intel = intelligence or {}
    social_links = intel.get("social_links") or {}
    socials = intel.get("socials") or {}
    has_site = bool(_s(row, "business_page"))

    social_labels = sorted(
        lab for lab, v in social_links.items() if v)
    n_social = len(social_labels)
    messaging = [lab for lab in social_labels if lab in _MESSAGING]

    n_emails = _email_count(emails)
    phones = intel.get("phones_found") or []
    n_phones = len(phones)
    has_phone = bool(_s(row, "telephone") or n_phones)

    rating = _to_float(row.get("rating"))
    review_count = _to_int(row.get("review_count"))

    # THREE states, not two. `claimed is True` collapsed "Google says this
    # listing is unverified" and "Google told us nothing at all" into the same
    # False -- and `sig` is mirrored into the breakdown (see `compute`), which
    # the scorer receives as DETERMINISTIC EVIDENCE. So a listing with no
    # is_claimed at all was handed to the model as if it had been confirmed
    # unclaimed, and "your Google listing is unclaimed" is exactly the kind of
    # gap the model is asked to write. A business would have been accused of
    # neglecting a listing on the strength of Google's silence.
    #
    #    True  -- Google reports the owner verified the listing
    #    False -- Google reports it is NOT verified. A real gap.
    #    None  -- unknown: absent key, "", or any other value. NOT a gap.
    #
    # Scoring is deliberately unchanged: only a confirmed True earns the 40
    # points below, so an unknown listing scores conservatively exactly as it
    # did before. What changes is that the breakdown now says `null` rather
    # than `false`, so the model can tell the two apart.
    claimed = row.get("is_claimed")
    if claimed is True:
        is_claimed = True
    elif claimed is False:
        is_claimed = False
    else:
        is_claimed = None

    activity_hits = 0
    for payload in socials.values():
        if not isinstance(payload, dict):
            continue
        if any(payload.get(k) for k in (
                "subscribers", "member_count", "video_count", "author_name",
                "channel_title")):
            activity_hits += 1

    pages = intel.get("pages_analyzed") or []
    structured = bool(intel.get("jsonld")) or bool(intel.get("open_graph"))
    cms = bool(intel.get("cms_platform"))
    meta = bool(intel.get("meta_description") or intel.get("title"))
    body = (intel.get("body_text_sample") or "").strip()

    return {
        "has_site": has_site,
        "social_labels": social_labels,
        "n_social": n_social,
        "messaging": messaging,
        "n_emails": n_emails,
        "n_phones": n_phones,
        "has_phone": has_phone,
        "rating": rating,
        "review_count": review_count,
        "is_claimed": is_claimed,
        "activity_hits": activity_hits,
        "n_pages": len(pages),
        "has_structured": structured,
        "has_cms": cms,
        "has_meta": meta,
        "body_chars": len(body),
        "has_book_online": bool(_s(row, "book_online_url")),
    }


def _sub_scores(sig, presence_class):
    """Turn gathered signals into the five evidence sub-scores (0-100)."""
    # PRESENCE -- owned online footprint richness (class base + small bumps).
    presence = PRESENCE_BASE_SCORE[presence_class]
    if sig["n_social"] >= 2:
        presence = min(100, presence + 3)
    if sig["has_cms"] and sig["has_structured"]:
        presence = min(100, presence + 2)

    # CONTACTABILITY -- the channels we can actually reach them on.
    contactability = 0
    if sig["has_phone"]:
        contactability += 45
    if sig["n_emails"] > 0:
        contactability += 30
    if sig["messaging"]:
        contactability += 20
    if sig["has_book_online"] or sig["has_site"]:
        # reachable/actionable surface (booking or a real site to act on)
        contactability += 5
    contactability = min(100, contactability)

    # LISTING_QUALITY -- how verified/mature the Google listing looks.
    listing = 0
    if sig["is_claimed"]:
        listing += 40
    if sig["rating"] and sig["rating"] > 0:
        listing += 20
    if sig["review_count"] and sig["review_count"] > 0:
        listing += 20
    if sig["has_book_online"]:
        listing += 20
    listing = min(100, listing)

    # ACTIVITY -- genuinely thin today (honest). Only enriched social payloads
    # with real counts + review volume move this at all; most businesses sit low.
    activity = 0
    activity += min(25, sig["activity_hits"] * 12)      # socials w/ real counts
    if sig["review_count"]:
        activity += min(15, sig["review_count"] // 25)  # review momentum
    activity = min(100, activity)

    # INTELLIGENCE_QUALITY -- how complete / trustworthy our own scrape was.
    iq = 0
    if sig["has_site"]:
        iq += 30
    if sig["n_pages"] >= 1:
        iq += 20
    if sig["n_pages"] >= 3:
        iq += 10
    if sig["n_emails"] > 0:
        iq += 15
    if sig["has_structured"]:
        iq += 10
    if sig["has_meta"]:
        iq += 5
    if sig["has_cms"]:
        iq += 5
    if sig["body_chars"] > 200:
        iq += 5
    iq = min(100, iq)

    return {
        "presence": presence,
        "contactability": contactability,
        "listing_quality": listing,
        "activity": activity,
        "intelligence_quality": iq,
    }


def confidence_cap(subs):
    """Upper bound for the model's confidence, derived from how much trustworthy
    evidence we actually hold. Thin, unverified data => a hard low ceiling so
    the model cannot claim certainty it doesn't have."""
    cap = (0.55 * subs["intelligence_quality"]
           + 0.35 * subs["listing_quality"]
           + 0.10 * subs["activity"])
    return max(0, min(100, int(round(cap))))


# --------------------------------------------------------------------------- #
# Phase 3 — deterministic multi-source agreement + best-effort recency.
# These are NAMESPACED evidence blocks: they refine what confidence the model may
# claim (up to confidence_cap) but never rewrite opportunity/confidence or the
# cap itself. They are additive and audit-trailed (persisted on the lead).
# --------------------------------------------------------------------------- #
# count of distinct corroborating artifacts -> 0-100 agreement strength.
_AGREEMENT_BANDS = ((0, 0), (1, 30), (2, 60), (3, 80), (4, 95))


def _social_artifact_urls(intelligence):
    """Footer social_links URLs UNION schema.org sameAs profile URLs, de-duped by
    host. Both already come from the website crawl (no extra fetch), and both are
    independent corroboration of a business's real footprint."""
    intel = intelligence or {}
    urls = []
    for u in (intel.get("social_links") or {}).values():
        if u:
            urls.append(u)
    for j in (intel.get("jsonld") or []):
        sa = j.get("sameAs")
        if isinstance(sa, str):
            sa = [sa]
        for u in (sa or []):
            if u:
                urls.append(u)
    hosts = sorted({h for u in urls if (h := identity.norm_website(u))})
    return hosts


def source_agreement(intelligence, has_site):
    """How many INDEPENDENT channels corroborate the business beyond the single
    Google listing: the website itself plus each distinct social/sameAs profile
    host. count -> a bounded 0-100 score (banded). A listing-only business scores
    0; a site with several profiles scores high. Pure, no network."""
    hosts = _social_artifact_urls(intelligence)
    count = (1 if has_site else 0) + len(hosts)
    score = 0
    for n, val in _AGREEMENT_BANDS:
        if count >= n:
            score = val
    return {"count": count, "hosts": hosts, "score": score}


# Recognised "last activity" keys inside enriched social payloads, newest wins.
_RECENCY_KEYS = ("last_active", "last_video_at", "last_post_at", "latest_post_at")
# recency bands by age in days: age < days -> score.
_RECENCY_BANDS = ((30, 100), (90, 80), (180, 60), (365, 40), (730, 20))


def _parse_date(v):
    """Parse an ISO date/datetime string -> datetime.date or None."""
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    return None


def _recency_score(days_ago):
    for days, val in _RECENCY_BANDS:
        if days_ago < days:
            return val
    return 0


def activity_recency(intelligence, today=None):
    """Best-known last-activity date + a 0-100 recency score.

    BEST-EFFORT and HONESTLY SPARSE: today the only real source is a YouTube
    channel's latest-upload date (from the optional deep fetch + API key). We
    deliberately do NOT treat channel age (created_at), a site copyright year, or
    review totals as activity -- none prove recent engagement, and a template
    copyright is noise. So most real rows return last_active=None -> score 0.

    today is injectable for deterministic unit tests; defaults to date.today().
    """
    today = today or datetime.date.today()
    intel = intelligence or {}
    best_date, best_src = None, None
    for label, payload in (intel.get("socials") or {}).items():
        if not isinstance(payload, dict):
            continue
        for key in _RECENCY_KEYS:
            d = _parse_date(payload.get(key))
            if d and (best_date is None or d > best_date):
                best_date, best_src = d, label
    if best_date is None:
        return {"last_active": None, "source": None, "days_ago": None, "score": 0}
    days_ago = (today - best_date).days
    return {
        "last_active": best_date.isoformat(),
        "source": best_src,
        "days_ago": days_ago,
        "score": _recency_score(days_ago),
    }


def compute(row, intelligence=None, emails=None):
    """Full deterministic breakdown for one business. Callers feed this to the
    LLM as evidence and persist it as the score_breakdown column.

    Returns a dict with presence_class, presence, sub_scores{...},
    confidence_cap, and signals{...} (provenance). Pure -- safe to unit test.
    """
    intelligence = intelligence or {}
    presence_class = classify_presence(row, intelligence, emails)
    sig = _gather_signals(row, intelligence, emails)
    subs = _sub_scores(sig, presence_class)

    # Phase 3 namespaced evidence (never rewrites opportunity/confidence/cap).
    agreement = source_agreement(intelligence, sig["has_site"])
    recency = activity_recency(intelligence)

    # Mirror the raw evidence into signals (provenance) so the breakdown cites it.
    sig["source_agreement"] = {"count": agreement["count"],
                               "hosts": agreement["hosts"]}
    sig["activity_recency"] = {"last_active": recency["last_active"],
                               "source": recency["source"]}

    return {
        "presence_class": presence_class,
        "presence": subs["presence"],
        "sub_scores": subs,
        "confidence_cap": confidence_cap(subs),
        "source_agreement": agreement,
        "activity_recency": recency,
        "signals": sig,
    }


# --------------------------------------------------------------------------- #
# Unit checks -- run: python scoring.py   (no network, no credits, no deps)
# --------------------------------------------------------------------------- #
def _row(**kw):
    base = {
        "business_name": "Test Biz", "category": "Dentist",
        "telephone": "", "business_page": "", "rating": "", "review_count": "",
        "is_claimed": "", "book_online_url": "", "place_id": "x",
    }
    base.update({k: v for k, v in kw.items() if v is not None})
    return base


def _run_checks():
    failures = []
    cases = []

    def check(name, cond):
        if not cond:
            failures.append(name)
        print(("ok  " if cond else "FAIL") + "  " + name)

    # OFF_GRID: no website, no phone.
    r = _row(business_page="", telephone="")
    cases.append(("off-grid", classify_presence(r, {}, []) == "OFF_GRID"))
    check("off-grid (no site, no socials) -> OFF_GRID",
          classify_presence(r, {}, []) == "OFF_GRID")

    # SOCIAL_ONLY: supplied socials, no site (taxonomy kept for future sources).
    r = _row(business_page="", telephone="+1")
    intel = {"social_links": {"facebook": "https://fb.com/x"}}
    check("social-only (socials, no site) -> SOCIAL_ONLY",
          classify_presence(r, intel, []) == "SOCIAL_ONLY")

    # WEBSITE_ONLY: site, no socials, thin.
    r = _row(business_page="https://x.com")
    check("website-only (site, no socials) -> WEBSITE_ONLY",
          classify_presence(r, {"body_text_sample": "short"}, []) ==
          "WEBSITE_ONLY")

    # WEBSITE_AND_SOCIAL: site + facebook but thin content.
    intel = {"social_links": {"facebook": "https://fb.com/x"},
             "body_text_sample": "Welcome." * 3}
    check("website+social (site+fb, thin) -> WEBSITE_AND_SOCIAL",
          classify_presence(_row(business_page="https://x.com"),
                            intel, ["a@b.com"]) == "WEBSITE_AND_SOCIAL")

    # STRONG_DIGITAL_PRESENCE: site + fb + rich body + cms/structured + email.
    intel = {"social_links": {"facebook": "https://fb.com/x",
                              "instagram": "https://ig.com/x"},
             "body_text_sample": " ".join(["word"] * 400),
             "cms_platform": "WordPress", "jsonld": [{"@type": "Dentist"}],
             "meta_description": "meta"}
    check("strong (site+socials+rich+cms+email) -> STRONG_DIGITAL_PRESENCE",
          classify_presence(_row(business_page="https://x.com"),
                            intel, ["a@b.com"]) == "STRONG_DIGITAL_PRESENCE")

    # Sub-score sanity: a reachable, claimed, reviewed business should be far
    # more contactable/verified than a bare site-less row.
    rich_row = _row(business_page="https://x.com", telephone="+1 512 555 0199",
                    rating="4.8", review_count="120", is_claimed=True,
                    category="Dentist")
    rich_intel = {
        "social_links": {"whatsapp": "https://wa.me/1"},
        "phones_found": ["+1 512 555 0199"],
        "jsonld": [{"@type": "Dentist"}], "cms_platform": "WordPress",
        "meta_description": "meta", "pages_analyzed": ["/", "/contact"],
        "body_text_sample": " ".join(["word"] * 400),
    }
    rich = compute(rich_row, rich_intel, ["a@b.com", "c@d.com"])
    check("rich row -> presence_class STRONG_DIGITAL_PRESENCE",
          rich["presence_class"] == "STRONG_DIGITAL_PRESENCE")
    check("rich row -> contactability high (>= 95)",
          rich["sub_scores"]["contactability"] >= 95)
    check("rich row -> listing_quality high (>= 80)",
          rich["sub_scores"]["listing_quality"] >= 80)
    check("rich row -> confidence_cap high (>= 70)",
          rich["confidence_cap"] >= 70)

    # Anti-pollution: a thin, unverified business gets a LOW confidence_cap no
    # matter how appealing it looks (protects against false confidence).
    thin_row = _row(business_page="https://placeholder.example", telephone="",
                    rating="", review_count="", is_claimed="")
    thin = compute(thin_row, {}, [])
    check("thin row -> confidence_cap low (< 45)",
          thin["confidence_cap"] < 45)
    check("thin row -> intelligence_quality low (<= 35)",
          thin["sub_scores"]["intelligence_quality"] <= 35)

    # A social-rich but scrape-thin case must NOT get a high confidence cap
    # (the exact pollution the user worried about). Compare: strong socials but
    # almost no crawl evidence.
    social_only_thin = _row(business_page="", telephone="+1 512 555 0199",
                            rating="4.9", review_count="300", is_claimed=True)
    sos = compute(social_only_thin, {"social_links": {
        "facebook": "https://fb.com/x", "instagram": "https://ig.com/x"}}, [])
    check("social-rich but crawl-thin -> intelligence_quality stays low",
          sos["sub_scores"]["intelligence_quality"] <= 55)
    # listing/claim + phone make it contactable, but confidence must respect
    # that we never scraped a site: cap well below the strong rich case.
    check("social-rich but crawl-thin -> confidence_cap < rich row cap",
          sos["confidence_cap"] < rich["confidence_cap"])

    # ---- Phase 3: source_agreement + activity_recency (deterministic) -------- #
    import datetime as _dt
    T = _dt.date(2026, 9, 7)  # fixed reference so the checks are reproducible

    # Agreement bands: more independent artifacts -> strictly higher score.
    listing_only = compute(_row(business_page="", telephone="+1"),
                           {"social_links": {}}, [])
    site_only = compute(_row(business_page="https://site.example"),
                        {"social_links": {}}, [])
    site_1social = compute(_row(business_page="https://site.example"),
                           {"social_links": {"facebook": "https://fb.com/x"}}, [])
    # sameAs provides an independent structured corroboration even without a
    # footer link: distinct host should count separately.
    sameas_extra = compute(
        _row(business_page="https://site.example"),
        {"social_links": {"facebook": "https://fb.com/x"},
         "jsonld": [{"sameAs": ["https://www.instagram.com/handle"]}]}, [])
    check("agreement: listing-only -> 0",
          listing_only["source_agreement"]["count"] == 0
          and listing_only["source_agreement"]["score"] == 0)
    check("agreement: site-only -> mid (30)",
          site_only["source_agreement"]["score"] == 30)
    check("agreement: site + 1 footer social -> 60",
          site_1social["source_agreement"]["score"] == 60)
    check("agreement: distinct sameAs host counts separately",
          sameas_extra["source_agreement"]["count"]
          == site_1social["source_agreement"]["count"] + 1)
    check("agreement: high-corroboree caps at band ceiling",
          compute(_row(business_page="https://s.example"),
                  {"social_links": {"facebook": "https://fb.com/a",
                                    "instagram": "https://ig.com/a",
                                    "youtube": "https://yt.com/a",
                                    "twitter": "https://x.com/a"}}, [])
          ["source_agreement"]["score"] == 95)

    # Recency: None (no real activity source) == 0; recent == high; old == low.
    # Tested directly with an injected reference date so checks never drift.
    no_date = activity_recency({}, today=T)
    check("recency: no activity date -> 0, last_active None",
          no_date["score"] == 0 and no_date["last_active"] is None)
    recent = activity_recency(
        {"socials": {"youtube": {"last_video_at": "2026-09-01"}}}, today=T)
    check("recency: youtube last_video 6 days ago -> high (100)",
          recent["score"] == 100 and recent["source"] == "youtube")
    stale = activity_recency(
        {"socials": {"youtube": {"last_video_at": "2023-01-01"}}}, today=T)
    check("recency: youtube last_video ~3y ago -> 0 (very stale)",
          stale["score"] == 0)
    # created_at is channel AGE, not activity -- must NOT count as recency.
    age_not_recency = activity_recency(
        {"socials": {"youtube": {"created_at": "2026-08-01"}}}, today=T)
    check("recency: channel created_at is NOT activity -> 0",
          age_not_recency["score"] == 0)
    mid = activity_recency(
        {"socials": {"youtube": {"last_video_at": "2026-07-09"}}}, today=T)
    check("recency: ~60 days ago -> mid (80)",
          mid["score"] == 80)

    # compute() now always returns the two namespaced blocks.
    check("compute returns source_agreement + activity_recency blocks",
          "source_agreement" in rich and "activity_recency" in rich)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {failures}")
        return 1
    print("All presence/sub-score checks passed.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_checks())

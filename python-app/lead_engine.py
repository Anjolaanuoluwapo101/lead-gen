#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
End-to-end campaign orchestrator: FIND -> ENRICH -> SCORE -> STORE.

Brings the three engines together for ONE niche campaign so a run is a single
callable instead of a hand-rolled chain of HTTP calls:

    run_campaign(keyword="dentist", place="Austin, TX", niche="...")

Stages
------
1. FIND     dataforseo.scrape_maps        -> candidate businesses (hostable)
2. ENRICH   email_scraper.scrape_website  -> emails + site intelligence (moat)
3. SCORE    groq_ai.score_lead            -> qualified / score / first_line
4. STORE    supabase_store.insert_rows    -> campaigns / prospects / leads

STORE is best-effort: if Supabase isn't configured the run still completes
find/enrich/score and returns the leads as JSON, so it's testable before any
database exists. When configured, prospects are written for EVERY found
business (audit trail) and leads for every enriched/scored business.

Usage
-----
    python lead_engine.py \
      --keyword dentist --place "Austin, TX" \
      --niche "adds online booking to dental clinics" -n 5

Output: JSON with a summary + the final scored leads list.
"""

import argparse
import hashlib
import json
import os
import sys

import config
import email_scraper
import identity
import llm
import scoring
import social_enrich
import supabase_store
from providers import get_llm, get_source

# --------------------------------------------------------------------------- #
# .env loader (imports above already load it, but be explicit for CLI runs)
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

# Defaults live here (not in n8n) so a caller only sends the fields it wants to
# override. n8n's Edit-Fields node shows the same keys but can send them blank,
# and the engine fills in these values for anything it doesn't receive.
DEFAULTS = {
    "keyword": "dentist",
    "place": "Austin, TX",
    "niche": "adds online booking to dental clinics",
    "max_results": 6,
}

# Which seller owns a campaign when the caller sends no explicit seller_id.
# A single-user bridge for existing n8n workflows (see docs/multi-tenant-plan.md);
# once every workflow passes a real seller this becomes dead fallback config.
# Set DEFAULT_SELLER_ID in .env to one of your seller_profile rows' uuids.
DEFAULT_SELLER_ID = (os.environ.get("DEFAULT_SELLER_ID") or "").strip() or None


def _key_part(s):
    """Normalize one component of a campaign's target_key: trim, lowercase, and
    collapse whitespace (including spaces around commas). Without this, "Lagos,
    Nigeria" / "Lagos,Nigeria" / "lagos, nigeria" hash differently and a re-run
    silently forks a NEW campaign instead of reusing one — splitting the dedup
    history in two. The raw values are still what get sent to the finder."""
    s = str(s or "").strip().lower()
    s = ",".join(p.strip() for p in s.split(","))
    return " ".join(s.split())

# Enrichment crawl caps — keep a demo run bounded (homepage + a few links).
ENRICH_MAX_COUNT = 12
ENRICH_MAX_DEPTH = 1
ENRICH_ANALYZE_LIMIT = 3

# A prior prospect counts as "already handled" (dedup skips re-enriching it) once
# it reached at least one of these terminal/advanced statuses. A prospect still
# `discovered` (an earlier run died mid-way) is NOT a duplicate -- we re-run it.
_HANDLED = frozenset({"enriched", "scored", "qualified", "lead", "dismissed"})

# ============================================================================ #
# Scrutiny / data-quality tiers (strict | balanced | lenient).
#
# strict (default) — expects rich web presence + clear on-site evidence.
# balanced        — prefers a website but a phone-reachable business with a real
#                   reason to fit is accepted; a missing site softens, not kills.
# lenient         — assumes sparse data (emerging markets): website irrelevant,
#                   right category + reachable ≈ qualified.
#
# Every knob is overridable from .env (see .env.example), so an operator can
# tune thresholds / the phone rule / the scoring hint without touching code:
#   SCRUTINY_DEFAULT=strict
#   SCRUTINY_STRICT_THRESHOLD=70
#   SCRUTINY_STRICT_ACCEPT_PHONE_ONLY=false
#   SCRUTINY_STRICT_HINT=...
#   ...same for BALANCED / LENIENT...
# ============================================================================ #
_SCRUTINY_HINTS = {
    "strict": (
        "Data is high-quality and complete. Be strict: only qualify when there "
        "is clear evidence the business lacks the niche offering, has a real "
        "web presence, and is reachable. Prefer to disqualify over a false yes."),
    "balanced": (
        "Data quality may be incomplete. If a website exists, weigh its "
        "evidence. If not, do NOT disqualify for a missing website/email/socials "
        "when the business otherwise fits the niche and is reachable — still "
        "require a plausible reason to reach out."),
    "lenient": (
        "Data quality is often sparse (emerging market). Assume most local "
        "businesses have little or no online presence. Do NOT punish a missing "
        "website, email, or socials at all. Judge mainly on category fit and "
        "reachability — a phone number is enough."),
}
_SCRUTINY_DEFAULTS = {
    "strict":   {"threshold": 70, "accept_phone_only": False},
    "balanced": {"threshold": 55, "accept_phone_only": True},
    "lenient":  {"threshold": 40, "accept_phone_only": True},
}


def _env_bool(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _confidence_gate():
    """Optional confidence floor on qualification. OFF by default so existing
    threshold behaviour is unchanged; when turned on, a lead also needs
    confidence_score >= CONFIDENCE_FLOOR to qualify (guards against chasing a
    high-opportunity business we're too unsure about)."""
    require = _env_bool("REQUIRE_CONFIDENCE_FLOOR", False)
    try:
        floor = int(os.environ.get("CONFIDENCE_FLOOR", "30"))
    except (TypeError, ValueError):
        floor = 30
    return require, floor


def scrutiny_config(tier=None):
    """Resolve a scrutiny tier (strict/balanced/lenient) from env settings."""
    raw = (tier or os.environ.get("SCRUTINY_DEFAULT", "strict")).strip().lower()
    if raw not in _SCRUTINY_DEFAULTS:
        raw = "strict"
    base = _SCRUTINY_DEFAULTS[raw]
    up = raw.upper()
    return {
        "tier": raw,
        "threshold": int(os.environ.get(
            f"SCRUTINY_{up}_THRESHOLD", base["threshold"])),
        "accept_phone_only": _env_bool(
            f"SCRUTINY_{up}_ACCEPT_PHONE_ONLY", base["accept_phone_only"]),
        "hint": os.environ.get(f"SCRUTINY_{up}_HINT", _SCRUTINY_HINTS[raw]),
    }

# Hard wall-clock guard per website so one slow/unresponsive site can't stall
# the whole campaign. Enrichment runs in a thread with a join timeout.
import threading


def _enrich_site(website, max_count=None, max_depth=None,
                 analyze_pages_limit=None, all_domains=False, timeout=90):
    """Return (emails, intelligence). Runs the crawl in a worker thread and
    abandons it (returns partial/empty) if it exceeds `timeout` seconds.

    max_count/max_depth/analyze_pages_limit/all_domains are passed straight to
    email_scraper.scrape_website (same_domain_only is inverted all_domains)."""
    max_count = max_count if max_count else ENRICH_MAX_COUNT
    max_depth = max_depth if max_depth else ENRICH_MAX_DEPTH
    analyze = analyze_pages_limit if analyze_pages_limit else ENRICH_ANALYZE_LIMIT
    out = {}

    def worker():
        out.update(email_scraper.scrape_website(
            website,
            max_count=int(max_count),
            max_depth=int(max_depth),
            analyze_pages_limit=int(analyze),
            same_domain_only=not bool(all_domains),
        ))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    emails = out.get("emails", {})
    intelligence = out.get("intelligence", {})
    errors = out.get("errors", [])
    if t.is_alive():
        errors.append(f"{website}: enrichment exceeded {timeout}s, skipped")
    return emails, intelligence, errors


def _enrich_socials(social_links, platforms, timeout=30):
    """Fetch the operator-chosen social profiles for one business's social_links.
    Runs in a worker thread with a join timeout so a slow walled platform can't
    stall the whole campaign. Returns {label: payload} (may be {} if nothing
    selected or everything timed out)."""
    out = {}

    def worker():
        out.update(social_enrich.enrich(social_links, platforms))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    return out


def _row_prospect(row, campaign_id, source="dataforseo"):
    """Map a finder row onto the `prospects` table columns."""
    return {
        "campaign_id": campaign_id,
        "source": source,
        "business_name": row.get("business_name", ""),
        "category": row.get("category", ""),
        "phone": row.get("telephone", ""),
        "website": row.get("business_page", ""),
        "street": row.get("street", ""),
        "locality": row.get("locality", ""),
        "region": row.get("region", ""),
        "zipcode": row.get("zipcode", ""),
        "rating": _float_or_none(row.get("rating")),
        "review_count": _int_or_none(row.get("review_count")),
        "place_id": row.get("place_id", ""),
        "maps_url": row.get("maps_url", ""),
        "listing_url": row.get("listing_url", "") or None,
        "raw_payload": row,
    }


def _float_or_none(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def _int_or_none(v):
    try:
        return int(float(v)) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def run_campaign(keyword=None, place=None, niche=None, max_results=None,
                 location_name=None, location_code=None,
                 campaign_id=None, campaign_name=None, seller_id=None,
                 enrich_max_count=None, enrich_max_depth=None,
                 enrich_analyze_pages_limit=None, all_domains=False,
                 score_model=None, score_extra_hints=None,
                 scrutiny=None, social_platforms=None):
    """
    Run one niche campaign end to end. Returns a dict summary; see module
    docstring for the shape. Raises if FIND itself fails (no creds / API error)
    so the caller knows the campaign didn't run; per-business enrich/score
    failures are collected in results["errors"] and skipped, not fatal.

    Every field is optional — DEFAULTS supplies keyword/place/niche/max_results
    and enrich/score knobs fall back to their module constants, so n8n can send
    a partial payload and the engine fills the gaps.

    seller_id: who owns this campaign (a seller_profile uuid). Falls back to
    DEFAULT_SELLER_ID (.env), then to None. It scopes campaign find-or-create:
    two sellers running the same niche/place get SEPARATE campaigns, so dedup +
    history never leak across tenants. Null is allowed and simply leaves the
    campaign unowned (legacy behaviour) until a seller is resolved.
    """
    keyword = (keyword or DEFAULTS["keyword"]).strip()
    place = (place or DEFAULTS["place"]).strip()
    # Niche is resolved AFTER the seller lookup below: a caller that sends a
    # niche overrides, a blank one falls back to the seller's stored profile
    # niche, and only then to the dev default. Kept out of the defaulting here
    # so a blank never gets silently replaced by "dentist" before we look.
    niche = (niche or "").strip() or None
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = DEFAULTS["max_results"]

    resolved_seller_id = (seller_id or DEFAULT_SELLER_ID or "").strip() or None
    seller_niche = None

    # Resolve this seller's provider config (per-seller settings with env-master
    # fallback) and build the adapters ONCE for the run. A missing/undecryptable
    # key simply falls back to the master env key; if even that is absent the
    # adapter build raises and we degrade to a source-only / find-only run.
    seller_settings = None
    setup_errors = []
    if supabase_store.configured() and resolved_seller_id:
        try:
            _rows = supabase_store.select_rows(
                "seller_profile", columns="settings,niche",
                filters={"id": resolved_seller_id}, limit=1)
            if _rows:
                raw = _rows[0].get("settings")
                if isinstance(raw, str):
                    try:
                        seller_settings = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        seller_settings = None
                else:
                    seller_settings = raw
                seller_niche = _rows[0].get("niche")
        except Exception as e:
            setup_errors.append(f"seller config lookup failed: {e}")

    # Decide the niche for this run: what the seller pitches. Caller's value
    # wins; else their stored profile niche; else the dev default. Part of the
    # campaign identity key below, so it stays stable per seller as long as
    # their profile doesn't change.
    niche_source = "caller"
    if not niche:
        niche = (seller_niche or "").strip() or None
        if niche:
            niche_source = "seller_profile"
        else:
            # Nothing supplied a niche, so we fall back to the DEV placeholder
            # ("adds online booking to dental clinics"). That silently scores
            # every lead against a dental example, so surface it in the result
            # rather than let a tenant wonder why nothing qualifies.
            niche = DEFAULTS["niche"]
            niche_source = "dev_default"

    source = None
    try:
        source = get_source(config.source_cfg(seller_settings))
    except Exception as e:
        setup_errors.append(f"source not configured: {e}")

    # LLM provider (used for scoring; draft is a separate /draft call). Built
    # once and reused across all businesses in this run. Score uses the seller's
    # provider/model unless the caller overrides with score_model.
    llm_provider = None
    llm_cfg = config.llm_cfg(seller_settings, model=score_model)
    if llm_cfg.get("api_key"):
        try:
            llm_provider = get_llm(llm_cfg)
        except Exception as e:
            setup_errors.append(f"llm not configured: {e}")
    else:
        setup_errors.append("no LLM provider configured — scoring/qualification "
                            "will be skipped (find/enrich still run)")

    cfg = scrutiny_config(scrutiny)
    soc_platforms = social_enrich.parse_platforms(social_platforms)

    results = {
        "ok": True,
        "source": "dataforseo",
        "keyword": keyword,
        "place": place,
        "niche": niche,
        "niche_source": niche_source,
        "max_results": max_results,
        "scrutiny": cfg["tier"],
        "score_threshold": cfg["threshold"],
        "campaign_id": campaign_id,
        "seller_id": resolved_seller_id,
        "campaign_reused": False,
        "duplicates": 0,
        "found": 0,
        "with_website": 0,
        "enriched": 0,
        "scored": 0,
        "qualified": 0,
        "stored": False,
        "leads": [],
        "errors": [],
        "warnings": [],
    }
    results["errors"].extend(setup_errors)
    if niche_source == "dev_default":
        results["warnings"].append(
            "No niche supplied by the caller and none stored on the seller "
            "profile — falling back to the DEV placeholder %r. Every lead is "
            "being scored against that, so qualification results are not "
            "meaningful. Set the seller's niche (PATCH /seller) or pass "
            "'niche' with the run." % DEFAULTS["niche"])

    # ---------------- FIND ---------------- #
    if source is None:
        results["errors"].append(
            "FIND skipped: no business source configured (check .env creds).")
        return results
    rows = source.find_businesses(keyword, place,
                                  location_name=location_name,
                                  location_code=location_code,
                                  max_results=max_results)
    results["found"] = len(rows)
    if not rows:
        return results
    results["with_website"] = sum(1 for r in rows if r.get("business_page"))

    # ---------------- STORE setup / campaign ensure ---------------- #
    store = supabase_store.configured()
    results["stored"] = store
    if store and not campaign_id:
        # Stable per-target campaign: find-or-create by target_key (a hash of
        # seller|keyword|place|niche). Re-running the same target for the SAME
        # seller REUSES this campaign instead of spawning a duplicate, so history
        # + dedup accumulate in one place. Older rows that predate target_key
        # (all blank) are untouched.
        target_key = hashlib.sha1(
            "|".join(_key_part(p) for p in
                     (resolved_seller_id, keyword, place, niche))
            .encode("utf-8")).hexdigest()
        existing = []
        try:
            _lookup = {"target_key": target_key}
            if resolved_seller_id:
                _lookup["seller_id"] = resolved_seller_id
            existing = supabase_store.select_rows(
                "campaigns", columns="id", filters=_lookup)
        except Exception as e:
            results["errors"].append(f"campaign lookup failed: {e}")
        if existing and existing[0].get("id"):
            campaign_id = existing[0]["id"]
            results["campaign_reused"] = True
        else:
            require_cf, cf_floor = _confidence_gate()
            _new_campaign = {
                "name": campaign_name or f"{keyword} - {place}",
                "keyword": keyword,
                "location": place,
                "target_key": target_key,
                # Snapshot this run's scoring config onto the campaign so it is a
                # self-describing config object, not just loose ad-hoc fields.
                "niche_rules": {
                    "niche": niche, "source": "dataforseo",
                    "scrutiny": cfg["tier"], "threshold": cfg["threshold"],
                    "accept_phone_only": cfg["accept_phone_only"],
                    "require_confidence_floor": require_cf,
                    "confidence_floor": cf_floor,
                },
            }
            if resolved_seller_id:
                _new_campaign["seller_id"] = resolved_seller_id
            created = supabase_store.insert_rows("campaigns", _new_campaign)
            campaign_id = (created or [{}])[0].get("id")
        results["campaign_id"] = campaign_id

    # ---- EARLY DEDUP: a business is enriched at most once per campaign. ----- #
    # A found business matching an already-HANDLED prior prospect of this
    # campaign is a duplicate re-sighting: we record it (status='duplicate',
    # duplicate_of -> canonical) and SKIP crawl/score, because the prior result
    # already stands in this accumulated campaign. Prior prospects still
    # `discovered` (a run that died mid-way) are NOT duplicates -- they're re-run.
    prospect_ids = {}
    duplicates = {}
    prior_prospects = []
    if store and campaign_id:
        try:
            prior_prospects = supabase_store.select_rows(
                "prospects",
                columns="id,business_name,phone,website,locality,status",
                filters={"campaign_id": campaign_id})
        except Exception as e:
            results["errors"].append(f"prospect lookup failed: {e}")
            prior_prospects = []
        if prior_prospects:
            for i, row in enumerate(rows):
                hit = next(
                    (p for p in prior_prospects
                     if p.get("status") in _HANDLED
                     and identity.same_business(row, p)), None)
                if hit:
                    duplicates[i] = hit["id"]
        # Insert NEW prospects (status default 'discovered') then thin DUPLICATE
        # rows, and map each back to its server id for status updates / leads.
        try:
            new_built = [_row_prospect(r, campaign_id)
                         for i, r in enumerate(rows) if i not in duplicates]
            dup_built = []
            for i, r in enumerate(rows):
                if i in duplicates:
                    p = _row_prospect(r, campaign_id)
                    p["status"] = "duplicate"
                    p["duplicate_of"] = duplicates[i]
                    dup_built.append(p)
            created_prospects = []
            # PostgREST bulk insert requires UNIFORM keys across every row in one
            # POST. new and duplicate rows carry different keys (dups add
            # status/duplicate_of), so insert each set separately or a mixed batch
            # trips PGRST102 "All object keys must match".
            if new_built:
                created_prospects += supabase_store.insert_rows(
                    "prospects", new_built)
            if dup_built:
                created_prospects += supabase_store.insert_rows(
                    "prospects", dup_built)
            order = ([i for i in range(len(rows)) if i not in duplicates]
                     + list(duplicates.keys()))
            for idx, p in zip(order, created_prospects):
                prospect_ids[idx] = p.get("id")
            results["duplicates"] = len(duplicates)
        except Exception as e:
            # A store blip shouldn't lose the run's enrich/score work: forget the
            # dedup partition and process everything fresh, in-memory.
            store = False
            duplicates = {}
            results["stored"] = False
            results["errors"].append(f"prospects insert failed: {e}")

    # ---------------- ENRICH + SCORE (new prospects, one at a time) -------- #
    for idx, row in enumerate(rows):
        if idx in duplicates:
            # Already assessed in a prior run of this campaign; prior result stands.
            continue

        website = row.get("business_page", "")
        phone = (row.get("telephone") or "").strip()
        has_site = bool(website)
        # strict: a lead needs a website. balanced/lenient: a phone number also
        # makes a business reachable (call/WhatsApp outreach), so it may be a lead.
        eligible = has_site or (cfg["accept_phone_only"] and bool(phone))
        emails, intelligence, errs = [], {}, []
        results["errors"].extend(errs)

        # ENRICH only when there is a site to crawl (a site-less business has
        # nothing to crawl — leniency can't invent a website).
        if has_site:
            try:
                emails_map, intelligence, crawl_errors = _enrich_site(
                    website,
                    max_count=enrich_max_count,
                    max_depth=enrich_max_depth,
                    analyze_pages_limit=enrich_analyze_pages_limit,
                    all_domains=all_domains,
                )
                emails = list(emails_map.keys())
                results["enriched"] += 1
                results["errors"].extend(crawl_errors)
            except Exception as e:
                results["errors"].append(f"{row.get('business_name')}: {e}")

        # SOCIALS (optional): if the operator picked platforms, deep-fetch the
        # profiles found on the site. Folds into intelligence["socials"] so the
        # scorer reads it and the lead row persists it with no other changes.
        if (has_site and soc_platforms and intelligence.get("social_links")):
            soc = _enrich_socials(intelligence["social_links"], soc_platforms)
            if soc:
                intelligence["socials"] = soc

        # SCORE an eligible business (site present always; phone-only under
        # balanced/lenient). Deterministic evidence (scoring.compute) is computed
        # first and handed to the LLM so it INTERPRETS -- never invents -- the
        # numbers. We persist the two scores plus the evidence breakdown, and the
        # tier hint tells the LLM how to treat sparse data.
        breakdown = scoring.compute(row, intelligence, emails) if eligible else None
        opportunity = confidence = score = 0
        reasons, first_line = [], ""
        qualified = False
        scored_ok = False
        if row.get("business_name") and eligible and llm_provider is not None:
            try:
                hints = [str(score_extra_hints)] if score_extra_hints else []
                if row.get("book_online_url"):
                    hints.append(
                        "NOTE: this listing advertises an online booking URL "
                        f"({row['book_online_url']}). If the niche is online "
                        "booking, treat that as a strong reason this business is "
                        "NOT a prospect.")
                hints.append(cfg["hint"])
                judged = llm.score_lead(
                    llm_provider, row, intelligence, niche,
                    extra_hints="\n\n".join(hints),
                    breakdown=breakdown)
                opportunity = max(0, min(
                    100, int(judged.get("opportunity_score", 0) or 0)))
                # Clamp confidence to the deterministic cap so thin/unverified
                # data can't be dressed up as high-confidence (anti-pollution).
                cap = (breakdown or {}).get("confidence_cap", 100)
                raw_conf = max(0, min(
                    100, int(judged.get("confidence_score", 0) or 0)))
                confidence = min(raw_conf, cap)
                reasons = judged.get("reasons", []) or []
                first_line = judged.get("first_line", "") or ""
                score = opportunity
                # Thresholds (env-tunable) are authoritative, not the LLM's own.
                qualified = opportunity >= cfg["threshold"]
                require_cf, cf_floor = _confidence_gate()
                if require_cf and confidence < cf_floor:
                    qualified = False
                scored_ok = True
                results["scored"] += 1
                if qualified:
                    results["qualified"] += 1
            except Exception as e:
                results["errors"].append(
                    f"{row.get('business_name')}: score failed: {e}")

        # Prospect lifecycle: record the furthest stage this prospect reached.
        if not row.get("business_name"):
            prospect_status = "discovered"
        elif not eligible:
            prospect_status = "dismissed"          # nothing reachable to pursue
        elif has_site and not scored_ok:
            prospect_status = "enriched"           # crawled, but couldn't judge
        elif qualified:
            prospect_status = "qualified"          # a lead row now links back
        else:
            prospect_status = "dismissed"          # scored below the threshold

        # Eligible businesses become leads (and are persisted). Ineligible ones
        # stay prospects-only (still saved above for the audit trail).
        if eligible:
            lead = _row_prospect(row, campaign_id)
            lead["emails"] = emails
            lead["emails_extra"] = intelligence.get("phones_found", [])
            lead["intelligence"] = intelligence
            lead["digital_presence"] = (breakdown or {}).get("presence_class")
            lead["opportunity_score"] = opportunity
            lead["confidence_score"] = confidence
            lead["score_breakdown"] = breakdown
            # Phase 3 namespaced evidence (queryable, never rewrites the scores).
            lead["source_agreement"] = (breakdown or {}).get(
                "source_agreement", {}).get("score")
            lead["last_active_at"] = (breakdown or {}).get(
                "activity_recency", {}).get("last_active")
            lead["score"] = score
            lead["qualified"] = qualified
            lead["weakness"] = "; ".join(reasons)
            lead["first_line"] = first_line
            lead["prospect_id"] = prospect_ids.get(idx)
            results["leads"].append(lead)

            # Persist the lead row + the prospect's final lifecycle status.
            pid = prospect_ids.get(idx)
            if store and campaign_id and pid:
                try:
                    supabase_store.insert_rows("leads", {
                        "prospect_id": pid,
                        "campaign_id": campaign_id,
                        "emails": json.dumps(emails),
                        "emails_extra": lead["emails_extra"],
                        "intelligence": json.dumps(intelligence, default=str),
                        "digital_presence": (breakdown or {}).get("presence_class"),
                        "opportunity_score": opportunity,
                        "confidence_score": confidence,
                        "score_breakdown": json.dumps(breakdown, default=str),
                        "source_agreement": (breakdown or {}).get(
                            "source_agreement", {}).get("score"),
                        "last_active_at": (breakdown or {}).get(
                            "activity_recency", {}).get("last_active"),
                        "score": score,
                        "qualified": qualified,
                        "weakness": lead["weakness"],
                        "first_line": first_line,
                    })
                except Exception as e:
                    results["errors"].append(
                        f"{row.get('business_name')}: lead insert failed: {e}")

        # Record the reached lifecycle stage on the prospect row itself.
        pid = prospect_ids.get(idx)
        if store and campaign_id and pid:
            try:
                supabase_store.update_rows("prospects",
                                           {"status": prospect_status},
                                           {"id": pid})
            except Exception as e:
                results["errors"].append(
                    f"{row.get('business_name')}: status update failed: {e}")

    return results


def main():
    ap = argparse.ArgumentParser(
        description="Run a FIND->ENRICH->SCORE->STORE lead campaign end to end.")
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--place", required=True)
    ap.add_argument("--niche", required=True)
    ap.add_argument("--location-name", default=None)
    ap.add_argument("-n", "--max-results", type=int, default=6)
    ap.add_argument("--campaign-id", default=None)
    ap.add_argument("--campaign-name", default=None)
    ap.add_argument("--seller-id", default=None,
                    help="seller_profile uuid owning this campaign "
                         "(falls back to DEFAULT_SELLER_ID)")
    ap.add_argument("--scrutiny", default=None,
                    help="strict | balanced | lenient (default from .env)")
    ap.add_argument("--social-platforms", default=None,
                    help="comma-separated socials to deep-fetch, e.g. "
                         "youtube,facebook,whatsapp (blank = none)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        result = run_campaign(
            args.keyword, args.place, args.niche,
            max_results=args.max_results,
            location_name=args.location_name,
            campaign_id=args.campaign_id,
            campaign_name=args.campaign_name,
            seller_id=args.seller_id,
            scrutiny=args.scrutiny,
            social_platforms=args.social_platforms,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

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
from concurrent.futures import ThreadPoolExecutor

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

# Businesses enriched + scored at once. Every per-business step is network I/O
# against a different host (its own site, then the LLM), and workers share
# nothing mutable — each crawl builds its own Fetcher and scores go through
# the pooled session — so parallel batches are safe and turn the slowest site
# from the pace of the whole campaign into the pace of its own batch. Eight,
# not fifty: LLM rate limits still apply, but a 429 now backs off with
# Retry-After inside the shared transport instead of failing the business
# (score failed, kept as enriched). Override with ENRICH_SCORE_WORKERS.
# STORE writes stay sequential on the caller thread regardless.
ENRICH_SCORE_WORKERS = 8


def _enrich_score_workers():
    """Live worker count: the constant is the default, the env override wins
    per call (read here, not at import, so tests and operators can change it
    without a reimport)."""
    try:
        return max(1, int(os.environ.get("ENRICH_SCORE_WORKERS")
                          or ENRICH_SCORE_WORKERS))
    except (TypeError, ValueError):
        return ENRICH_SCORE_WORKERS

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
# the whole campaign. Enrichment runs on a future with a timeout, not a bare
# thread with a join: on timeout the crawl is ASKED to stop (stop_event winds
# it down after its current fetch) instead of being abandoned while it keeps
# fetching and sleeping in the background with no handle. A short grace wait
# then collects whatever it merged so far, so a slow site still contributes
# partial data rather than nothing.
import threading
from concurrent.futures import TimeoutError as _FuturesTimeout


def _enrich_site(website, max_count=None, max_depth=None,
                 analyze_pages_limit=None, all_domains=False, timeout=90,
                 stop_grace_s=5):
    """Return (emails, intelligence, errors).

    max_count/max_depth/analyze_pages_limit/all_domains are passed straight to
    email_scraper.scrape_website (same_domain_only is inverted all_domains).
    Past `timeout` seconds the crawl is signalled to stop and whatever it
    merged so far is returned, plus a timeout note in errors."""
    max_count = max_count if max_count else ENRICH_MAX_COUNT
    max_depth = max_depth if max_depth else ENRICH_MAX_DEPTH
    analyze = analyze_pages_limit if analyze_pages_limit else ENRICH_ANALYZE_LIMIT
    out = {}
    stop = threading.Event()

    def worker():
        out.update(email_scraper.scrape_website(
            website,
            max_count=int(max_count),
            max_depth=int(max_depth),
            analyze_pages_limit=int(analyze),
            same_domain_only=not bool(all_domains),
            stop_event=stop,
        ))

    timed_out = False
    with ThreadPoolExecutor(max_workers=1,
                            thread_name_prefix="enrich-site") as ex:
        fut = ex.submit(worker)
        try:
            fut.result(timeout=timeout)
        except _FuturesTimeout:
            timed_out = True
            stop.set()
            try:
                fut.result(timeout=stop_grace_s)
            except _FuturesTimeout:
                pass
    emails = out.get("emails", {})
    intelligence = out.get("intelligence", {})
    errors = list(out.get("errors", []))
    if timed_out:
        errors.append(
            f"{website}: enrichment exceeded {timeout}s, "
            f"{'partial results kept' if out else 'skipped'}")
    return emails, intelligence, errors


def _enrich_socials(social_links, platforms, timeout=30):
    """Fetch the operator-chosen social profiles for one business's social_links.
    Runs on a future with a timeout so a slow walled platform can't stall the
    whole campaign. Returns {label: payload} (may be {} if nothing selected
    or everything timed out). Unlike the crawl there is no stop hook into the
    platform fetches, so a timed-out call may finish up to ~30s of bounded
    internal timeouts in the background; each fetch carries its own 8s cap,
    so the ghost is short-lived, not a pile-up."""
    out = {}

    def worker():
        out.update(social_enrich.enrich(social_links, platforms))

    with ThreadPoolExecutor(max_workers=1,
                            thread_name_prefix="enrich-social") as ex:
        try:
            ex.submit(worker).result(timeout=timeout)
        except _FuturesTimeout:
            pass
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
                 scrutiny=None, social_platforms=None, progress_cb=None):
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

    progress_cb: optional callable(kind, message) invoked at the four stage
    boundaries of a run — before FIND ("search"), after FIND ("found"),
    before the enrich/score pool ("enrich"), after it ("scored"). The agent
    path passes a writer that appends each to the run trace, so a dashboard
    watching the run sees the story while it happens instead of silence
    between turns. Every other caller passes nothing and behaves exactly as
    before; messages are plain words with real counts, never codes.
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
    # Stage 1 of 4: the DataForSEO call below is a 5 to 15 second wait with
    # nothing written anywhere. Name it before it starts so a watcher sees
    # the run working, not stuck.
    if progress_cb:
        progress_cb("search",
                    f"Searching for '{keyword}' in {place}")
    rows = source.find_businesses(keyword, place,
                                  location_name=location_name,
                                  location_code=location_code,
                                  max_results=max_results)
    results["found"] = len(rows)
    if not rows:
        return results
    results["with_website"] = sum(1 for r in rows if r.get("business_page"))
    # Stage 2 of 4: FIND landed. Real counts, so the trace says what came
    # back before the long crawl begins.
    if progress_cb:
        progress_cb("found",
                    f"Found {results['found']} businesses, "
                    f"{results['with_website']} with websites")

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
            # Only HANDLED rows can ever match (the scan below filters on
            # status), so undiscovered leftovers and duplicate markers are
            # excluded server-side instead of downloaded and ignored. The
            # explicit limit + stable order replace the silent PostgREST
            # default cap: without them a campaign past ~1000 prospects
            # silently stops deduplicating its oldest rows. 5000 handled
            # prospects is ~250 runs at 20 a run; past that the honest fix
            # is server-side dedup (upsert on an identity key), not a
            # bigger number here.
            prior_prospects = supabase_store.select_rows(
                "prospects",
                columns="id,business_name,phone,website,locality,status",
                filters={"campaign_id": campaign_id},
                filters_in={"status": sorted(_HANDLED)},
                order="created_at.asc", limit=5000)
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

    # ---------------- ENRICH + SCORE (new prospects, in parallel) --------- #
    # This used to walk rows one at a time: crawl a site (up to 90s), score it,
    # store it, repeat. Ten businesses at ~15s of crawl+score each is minutes,
    # all spent waiting on network. Workers share nothing (see the constant),
    # so each business is crawled and judged on its own thread while the STORE
    # below stays sequential, in row order, on this thread.
    def _one(work_item):
        """Crawl + judge ONE business. Never raises: an unexpected failure is
        an error string on the outcome, because one bad business must not kill
        the other nineteen. Reads only shared state (cfg, niche, the provider),
        writes only its own outcome dict."""
        idx, row = work_item
        outcome = {"idx": idx, "eligible": False, "has_site": False,
                   "emails": [], "intelligence": {}, "breakdown": None,
                   "opportunity": 0, "confidence": 0, "score": 0,
                   "reasons": [], "gaps": [], "first_line": "",
                   "qualified": False, "scored_ok": False,
                   "enriched_n": 0, "scored_n": 0, "qualified_n": 0,
                   "errors": [], "prospect_status": "discovered"}
        try:
            website = row.get("business_page", "")
            phone = (row.get("telephone") or "").strip()
            has_site = bool(website)
            # strict: a lead needs a website. balanced/lenient: a phone number
            # also makes a business reachable (call/WhatsApp outreach), so it
            # may be a lead.
            eligible = has_site or (cfg["accept_phone_only"] and bool(phone))
            outcome["has_site"] = has_site
            outcome["eligible"] = eligible
            emails, intelligence = [], {}

            # ENRICH only when there is a site to crawl (a site-less business
            # has nothing to crawl — leniency can't invent a website).
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
                    outcome["enriched_n"] = 1
                    outcome["errors"].extend(crawl_errors)
                except Exception as e:
                    outcome["errors"].append(
                        f"{row.get('business_name')}: {e}")

            # SOCIALS (optional): if the operator picked platforms, deep-fetch
            # the profiles found on the site. Folds into intelligence so the
            # scorer reads it and the lead row persists it with no changes.
            if (has_site and soc_platforms and intelligence.get("social_links")):
                soc = _enrich_socials(intelligence["social_links"],
                                      soc_platforms)
                if soc:
                    intelligence["socials"] = soc

            # SCORE an eligible business (site present always; phone-only under
            # balanced/lenient). Deterministic evidence (scoring.compute) is
            # computed first and handed to the LLM so it INTERPRETS -- never
            # invents -- the numbers. The tier hint tells the LLM how to treat
            # sparse data.
            breakdown = scoring.compute(
                row, intelligence, emails) if eligible else None
            opportunity = confidence = score = 0
            reasons, gaps, first_line = [], [], ""
            qualified = False
            scored_ok = False
            if row.get("business_name") and eligible \
                    and llm_provider is not None:
                try:
                    hints = [str(score_extra_hints)] if score_extra_hints \
                        else []
                    if row.get("book_online_url"):
                        hints.append(
                            "NOTE: this listing advertises an online booking "
                            f"URL ({row['book_online_url']}). If the niche is "
                            "online booking, treat that as a strong reason "
                            "this business is NOT a prospect.")
                    hints.append(cfg["hint"])
                    judged = llm.score_lead(
                        llm_provider, row, intelligence, niche,
                        extra_hints="\n\n".join(hints),
                        breakdown=breakdown)
                    opportunity = max(0, min(
                        100, int(judged.get("opportunity_score", 0) or 0)))
                    # Clamp confidence to the deterministic cap so
                    # thin/unverified data can't be dressed up as
                    # high-confidence (anti-pollution).
                    cap = (breakdown or {}).get("confidence_cap", 100)
                    raw_conf = max(0, min(
                        100, int(judged.get("confidence_score", 0) or 0)))
                    confidence = min(raw_conf, cap)
                    reasons = judged.get("reasons", []) or []
                    gaps = judged.get("gaps", []) or []
                    first_line = judged.get("first_line", "") or ""
                    score = opportunity
                    # Thresholds (env-tunable) are authoritative, not the
                    # LLM's own.
                    qualified = opportunity >= cfg["threshold"]
                    require_cf, cf_floor = _confidence_gate()
                    if require_cf and confidence < cf_floor:
                        qualified = False
                    scored_ok = True
                    outcome["scored_n"] = 1
                    if qualified:
                        outcome["qualified_n"] = 1
                except Exception as e:
                    outcome["errors"].append(
                        f"{row.get('business_name')}: score failed: {e}")

            outcome.update(
                emails=emails, intelligence=intelligence, breakdown=breakdown,
                opportunity=opportunity, confidence=confidence, score=score,
                reasons=reasons, gaps=gaps, first_line=first_line,
                qualified=qualified, scored_ok=scored_ok)

            # Prospect lifecycle: the furthest stage this prospect reached.
            if not row.get("business_name"):
                prospect_status = "discovered"
            elif not eligible:
                prospect_status = "dismissed"       # nothing reachable
            elif has_site and not scored_ok:
                prospect_status = "enriched"        # crawled, couldn't judge
            elif qualified:
                prospect_status = "qualified"       # a lead row now links back
            else:
                prospect_status = "dismissed"       # scored below threshold
            outcome["prospect_status"] = prospect_status
        except Exception as e:
            # The backstop: even the outcome bookkeeping above must not let one
            # row kill the batch. The business keeps its discovered status and
            # the error says what happened.
            outcome["errors"].append(
                f"{(row or {}).get('business_name') or idx}: enrich failed: {e}")
        return outcome

    # map() preserves input order, so outcomes arrive in row order; the merge
    # below still keys by idx rather than trusting position. Duplicates were
    # already assessed in a prior run of this campaign — prior result stands,
    # so they never reach a worker.
    work = [(idx, row) for idx, row in enumerate(rows) if idx not in duplicates]
    outcomes = {}
    # Stage 3 of 4: the pool below is the longest silence in the system
    # (tens of seconds of crawl plus score). `work` is built, so the count
    # is real and post dedup. Placed here, not at the section header above,
    # because only here do we know how many businesses actually go in.
    if progress_cb and work:
        progress_cb("enrich",
                    f"Crawling {len(work)} business "
                    f"{'site' if len(work) == 1 else 'sites'} "
                    f"and scoring them")
    if work:
        with ThreadPoolExecutor(
                max_workers=_enrich_score_workers()) as ex:
            for outcome in ex.map(_one, work):
                outcomes[outcome["idx"]] = outcome
    for idx, _ in work:
        o = outcomes[idx]
        results["errors"].extend(o["errors"])
        results["enriched"] += o["enriched_n"]
        results["scored"] += o["scored_n"]
        results["qualified"] += o["qualified_n"]
    # Stage 4 of 4: crawl plus score done. Real counters close the story the
    # "enrich" event opened.
    if progress_cb and work:
        progress_cb("scored",
                    f"Enriched {results['enriched']}, "
                    f"scored {results['scored']}, "
                    f"qualified {results['qualified']}")

    # ---------------- STORE (sequential, in row order) ---------------------- #
    # The loop below only BUILDS payloads; the batched writes run after it.
    # lead_rows/lead_names stay parallel (names for per-row fallback errors),
    # status_buckets groups prospect ids by lifecycle status for one PATCH
    # per status instead of one per row.
    lead_rows = []
    lead_names = []
    status_buckets = {}
    for idx, row in enumerate(rows):
        if idx in duplicates:
            # Already assessed in a prior run of this campaign; prior result stands.
            continue
        o = outcomes[idx]
        eligible = o["eligible"]
        has_site = o["has_site"]
        emails = o["emails"]
        intelligence = o["intelligence"]
        breakdown = o["breakdown"]
        opportunity = o["opportunity"]
        confidence = o["confidence"]
        score = o["score"]
        reasons = o["reasons"]
        gaps = o["gaps"]
        first_line = o["first_line"]
        qualified = o["qualified"]
        scored_ok = o["scored_ok"]
        prospect_status = o["prospect_status"]

        # Eligible businesses become leads (and are persisted). Ineligible ones
        # stay prospects-only (still saved above for the audit trail).
        if eligible:
            lead = _row_prospect(row, campaign_id)
            lead["emails"] = emails
            # NOT the phone numbers. This line used to be
            #     lead["emails_extra"] = intelligence.get("phones_found", [])
            # which put phones, and the unix-looking junk a phone scraper also
            # collects, into a column named for email addresses -- and
            # leads_read.decoded() then JSON-decodes that column AS emails, so
            # anything iterating it looking for an address got "(512) 661-7896".
            # Nothing is lost by emptying it: the phones are still in
            # intelligence["phones_found"], where they are named correctly, and
            # that whole object is persisted on the row below. An empty list is
            # the honest value for "no addresses beyond `emails`".
            lead["emails_extra"] = []
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
            # `reasons` is the scorer's EVIDENCE for the two scores -- llm.py
            # asks for "short strings citing concrete evidence and sub-scores",
            # so for a strong lead it reads as praise. Writing it here meant
            # `weakness` held "Strong digital presence (presence=100)", which
            # the drafting prompt then printed under "WHY THEY MAY NEED THIS".
            # The model resolved that contradiction by inventing a defect, and
            # a lead scored STRONG_DIGITAL_PRESENCE got an email telling it its
            # site needed fixing. `gaps` is the scorer's separate answer to
            # "what do they LACK", and it is the one that belongs in this
            # column. The evidence is kept, just in the breakdown where it is
            # named as what it is.
            lead["weakness"] = "; ".join(gaps)
            # The evidence is not discarded, only re-homed: this is the only
            # record of WHY the two scores landed where they did, and dropping
            # it to stop it being misread would trade one bug for another. It
            # is the same dict object already on `lead["score_breakdown"]`, and
            # the row is persisted below, so the mutation reaches the database.
            if breakdown is not None:
                breakdown["model_reasons"] = reasons
            lead["first_line"] = first_line
            lead["prospect_id"] = prospect_ids.get(idx)
            results["leads"].append(lead)

            # Collect the lead row for the bulk insert below (same shape the
            # old per-row insert sent, including the persisted pid link).
            pid = prospect_ids.get(idx)
            if store and campaign_id and pid:
                lead_rows.append({
                    "prospect_id": pid,
                    "campaign_id": campaign_id,
                    "emails": json.dumps(emails),
                    "emails_extra": lead["emails_extra"],
                    "intelligence": json.dumps(intelligence, default=str),
                    "digital_presence": (breakdown or {}).get(
                        "presence_class"),
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
                lead_names.append(row.get("business_name") or pid)

        # Record the reached lifecycle stage on the prospect row itself.
        # Grouped by status below: one PATCH per status over its ids.
        pid = prospect_ids.get(idx)
        if store and campaign_id and pid:
            status_buckets.setdefault(prospect_status, []).append(pid)

    # ---------------- STORE writes (batched, in row order) ------------------ #
    # The loop above is pure bookkeeping now; ALL database writes happen here.
    # One POST for every lead row and one PATCH per lifecycle status: 20
    # businesses used to cost up to 40 sequential round trips (8 to 20s of
    # tail after all parallel work was done) and now cost a handful.
    if lead_rows:
        try:
            supabase_store.insert_rows("leads", lead_rows)
        except Exception as e:
            # Bulk failed: fall back to per-row inserts so one bad row cannot
            # sink the whole batch. Same per-row errors as the old code for
            # rows that truly fail; a full recovery is one note, not silence
            # (it explains the latency) and not a failure (nothing was lost).
            failed = 0
            for payload, name in zip(lead_rows, lead_names):
                try:
                    supabase_store.insert_rows("leads", payload)
                except Exception as one:
                    failed += 1
                    results["errors"].append(
                        f"{name}: lead insert failed: {one}")
            if not failed:
                results["errors"].append(
                    f"bulk lead insert failed but all {len(lead_rows)} rows "
                    f"saved on retry: {e}")
    for status, pids in status_buckets.items():
        try:
            supabase_store.update_rows("prospects", {"status": status},
                                       filters_in={"id": pids})
        except Exception as e:
            results["errors"].append(
                f"{len(pids)} prospect(s): status update failed: {e}")

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

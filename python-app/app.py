#!/usr/bin/env python
"""
Flask wrapper around yellow_pages.py / google_maps.py / email_scraper.py.

n8n calls these over HTTP (localhost, inside the same container) instead of
using the Execute Command node. Each endpoint maps JSON body fields to the
CLI flags the underlying script already supports.

Endpoints
---------
POST /yellowpages   {keyword, place, pages?, with_email?, min_delay?, max_delay?}
POST /googlemaps    {keyword, place, max_results?}
POST /email         {url, max_depth?, max_count?}

All three return JSON: {"ok": bool, "stdout": str, "stderr": str, "csv"/"emails": ...}
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from io import BytesIO

from flask import Flask, request, jsonify

import config  # per-seller provider config resolution
import llm  # transport-agnostic LLM task layer (score_lead / draft_email)
import lead_engine  # /campaign orchestrator (find->enrich->score->store)
import supabase_store  # /leads + /leads/status read/write helpers
import dataforseo  # /location lookup helper (resolves a place -> location_code)
from providers import (  # provider registry (adapters + name lists)
    get_llm, llm_provider_names, source_provider_names)
import at_rest as _secrets  # Fernet secrets-at-rest (module named at_rest to avoid shadowing stdlib secrets)

app = Flask(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Hard ceiling so one bad request can't hang the container forever.
SUBPROCESS_TIMEOUT = 300  # seconds


def run_script(args, timeout=SUBPROCESS_TIMEOUT):
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, cwd=SCRIPT_DIR
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out after {timeout}s"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/yellowpages", methods=["POST"])
def yellowpages():
    data = request.get_json(force=True) or {}
    keyword = data.get("keyword")
    place = data.get("place")
    if not keyword or not place:
        return jsonify({"ok": False, "error": "keyword and place are required"}), 400

    out_name = f"yp-{uuid.uuid4().hex}.csv"
    out_path = os.path.join(OUTPUT_DIR, out_name)

    args = [sys.executable, "yellow_pages.py", keyword, place, "-o", out_path, "-q"]
    if data.get("pages"):
        args += ["-p", str(int(data["pages"]))]
    if data.get("start_page"):
        args += ["--start-page", str(int(data["start_page"]))]
    if data.get("with_email"):
        args += ["--with-email"]
    if data.get("min_delay") is not None:
        args += ["--min-delay", str(data["min_delay"])]
    if data.get("max_delay") is not None:
        args += ["--max-delay", str(data["max_delay"])]
    if data.get("proxy"):
        args += ["--proxy", data["proxy"]]

    code, stdout, stderr = run_script(args)

    rows = []
    if os.path.exists(out_path):
        import csv
        with open(out_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        os.remove(out_path)  # container is ephemeral anyway; don't accumulate files

    return jsonify({
        "ok": code == 0 and bool(rows),
        "returncode": code,
        "row_count": len(rows),
        "rows": rows,
        "stdout": stdout[-4000:],
        "stderr": stderr[-2000:],
    })


@app.route("/googlemaps", methods=["POST"])
def googlemaps():
    data = request.get_json(force=True) or {}
    keyword = data.get("keyword")
    place = data.get("place")
    if not keyword or not place:
        return jsonify({"ok": False, "error": "keyword and place are required"}), 400

    out_name = f"gm-{uuid.uuid4().hex}.csv"
    out_path = os.path.join(OUTPUT_DIR, out_name)

    max_results = int(data.get("max_results", 20))  # keep default low on free tier
    offset = int(data.get("offset", 0))

    args = [sys.executable, "google_maps.py", keyword, place,
            "-r", str(max_results), "-o", out_path, "-q"]
    if offset:
        args += ["--offset", str(offset)]

    # Playwright + a headless browser is heavy; give it more time and let the
    # caller override if needed.
    code, stdout, stderr = run_script(args, timeout=data.get("timeout", 600))

    rows = []
    if os.path.exists(out_path):
        import csv
        with open(out_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        os.remove(out_path)

    return jsonify({
        "ok": code == 0 and bool(rows),
        "returncode": code,
        "row_count": len(rows),
        "rows": rows,
        "stdout": stdout[-4000:],
        "stderr": stderr[-2000:],
    })


@app.route("/places", methods=["POST"])
def places():
    """
    Hostable FIND source: DataForSEO Google Maps (no browser/consent wall).
    Same keyword/place semantics and the same row shape as /googlemaps, so the
    n8n pipeline can point at this endpoint unchanged.
    """
    data = request.get_json(force=True) or {}
    keyword = data.get("keyword")
    place = data.get("place")
    if not keyword or not place:
        return jsonify({"ok": False, "error": "keyword and place are required"}), 400

    max_results = int(data.get("max_results", 20))

    args = [sys.executable, "dataforseo.py", keyword, place,
            "-n", str(max_results), "--json"]
    if data.get("location_name"):
        args += ["--location-name", data["location_name"]]

    code, stdout, stderr = run_script(args, timeout=data.get("timeout", 120))

    rows = []
    if code == 0 and stdout.strip():
        try:
            # dataforseo.py --json prints indented, multi-line JSON — parse the
            # whole stdout, not just its last line.
            rows = json.loads(stdout.strip())
        except (json.JSONDecodeError, ValueError):
            rows = []

    return jsonify({
        "ok": code == 0 and bool(rows),
        "returncode": code,
        "row_count": len(rows),
        "rows": rows,
        "stdout": stdout[-4000:],
        "stderr": stderr[-2000:],
    })


@app.route("/score", methods=["POST"])
def score():
    """
    LLM lead-qualification pass via Groq. Takes a business row + its /email
    intelligence + a niche description, returns qualified/score/reasons/first_line.
    """
    data = request.get_json(force=True) or {}
    business = data.get("business") or {}
    niche = data.get("niche")
    if not business or not niche:
        return jsonify({"ok": False,
                        "error": "business (object) and niche (string) are "
                                 "required"}), 400

    intelligence = data.get("intelligence") or {}
    try:
        provider = get_llm(config.llm_cfg(None, model=data.get("model")))
        result = llm.score_lead(
            provider, business, intelligence, niche,
            extra_hints=data.get("extra_hints", ""),
            breakdown=data.get("breakdown"),
        )
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/campaign", methods=["POST"])
def campaign():
    """
    Run a whole niche campaign: FIND (DataForSEO /places) -> ENRICH (/email
    logic, in-process) -> SCORE (Groq) -> STORE (Supabase). Returns a summary
    plus the final scored leads. n8n points a single HTTP node at this instead
    of orchestrating the per-business loop node-by-node.
    """
    data = request.get_json(force=True) or {}
    keyword = data.get("keyword")
    place = data.get("place")
    niche = data.get("niche")

    # All fields optional: lead_engine.DEFAULTS fills keyword/place/niche and
    # max_results when the caller sends blanks. n8n's Edit-Fields node therefore
    # only needs to send the values it wants to override.

    try:
        result = lead_engine.run_campaign(
            keyword, place, niche,
            max_results=data.get("max_results"),
            location_name=data.get("location_name"),
            location_code=data.get("location_code"),
            campaign_id=data.get("campaign_id"),
            campaign_name=data.get("campaign_name"),
            seller_id=data.get("seller_id"),
            enrich_max_count=data.get("enrich_max_count"),
            enrich_max_depth=data.get("enrich_max_depth"),
            enrich_analyze_pages_limit=data.get("enrich_analyze_pages_limit"),
            all_domains=bool(data.get("all_domains")),
            score_model=data.get("score_model"),
            score_extra_hints=data.get("score_extra_hints"),
            scrutiny=data.get("scrutiny"),
            social_platforms=data.get("social_platforms"),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/location", methods=["POST"])
def location_lookup():
    """
    Resolve a human place ("Yaba, Lagos", "Lekki, Nigeria") into DataForSEO
    location_code candidates, so an operator can look one up and drop it into a
    /campaign run (which already accepts location_code). DataForSEO's list is
    coarse: exact=False means the place has no own code and the matches are the
    nearest broader containers to area-filter on.
    """
    data = request.get_json(force=True) or {}
    place = (data.get("place") or "").strip()
    if not place:
        return jsonify({"ok": False, "error": "place is required"}), 400
    try:
        top = int(data.get("top") or 8)
    except (TypeError, ValueError):
        top = 8
    try:
        result = dataforseo.lookup_location(place, top=top)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, **result})


# Lifecycle statuses a lead may take (must match leads.status).
LEAD_STATUSES = {"new", "contacted", "replied", "won", "lost"}
# Columns n8n needs for outreach + to render the lead. Business contact fields
# (name/phone/website/…) live on the linked PROSPECT, so we embed them via the
# FK and lift them to the lead's top level for a flat, n8n-friendly shape.
LEAD_COLUMNS = ("id,campaign_id,digital_presence,opportunity_score,"
                "confidence_score,source_agreement,score,qualified,status,"
                "emails,emails_extra,first_line,weakness,last_active_at,"
                "created_at,prospect_id(id,business_name,phone,website,category,"
                "locality,street,region,zipcode)")


def _flatten_leads(rows):
    """Decode jsonb/text fields PostgREST returns as strings, and lift the
    embedded prospect object's contact fields up to the lead top level (so n8n
    sees business_name/phone/website without digging into a nested object)."""
    import json as _json
    out = []
    for r in rows:
        r = dict(r or {})
        for k in ("emails", "emails_extra"):
            v = r.get(k)
            if isinstance(v, str) and v:
                try:
                    r[k] = _json.loads(v)
                except (_json.JSONDecodeError, ValueError):
                    pass
        prosp = r.get("prospect_id")
        if isinstance(prosp, dict):
            for k in ("business_name", "phone", "website", "category",
                      "locality", "street", "region", "zipcode"):
                if k in prosp and r.get(k) in (None, ""):
                    r[k] = prosp[k]
            r["prospect_id"] = prosp.get("id")
        out.append(r)
    return out


# --- Seller read-scoping -------------------------------------------------- #
# Wired NOW so auth can later inject a trusted seller_id with no redesign.
# A read is never open across sellers: every data-revealing route resolves the
# caller's seller and refuses rows it doesn't own. Pre-auth, the source is the
# body seller_id -> DEFAULT_SELLER_ID bridge (mirrors lead_engine's write path);
# when auth lands, replace _resolve_read_seller with a session-derived id and
# drop the DEFAULT leniency in _seller_owns_campaign.

def _resolve_read_seller(data):
    """Resolve the caller's seller: body seller_id -> DEFAULT_SELLER_ID bridge.
    Returns None when neither is present (callers return 400)."""
    sid = str((data or {}).get("seller_id") or "").strip()
    if not sid:
        sid = lead_engine.DEFAULT_SELLER_ID or ""
    return sid.strip() or None


def _seller_owns_campaign(seller_id, campaign_id):
    """True when `campaign_id` belongs to `seller_id`. A legacy NULL-owner
    campaign counts as owned only by the DEFAULT single-tenant bridge (so a
    pre-backfill install keeps surfacing its own rows). Unknown campaign ->
    False."""
    if not seller_id or not campaign_id:
        return False
    try:
        rows = supabase_store.select_rows(
            "campaigns", columns="id,seller_id",
            filters={"id": campaign_id}, limit=1)
    except Exception:
        return False
    if not rows:
        return False
    owner = rows[0].get("seller_id")
    if owner is not None:
        return str(owner) == seller_id
    return seller_id == (lead_engine.DEFAULT_SELLER_ID or "").strip()


@app.route("/leads", methods=["POST"])
def leads_pull():
    """
    Pull leads for a campaign so n8n can drive outreach. One endpoint serves
    BOTH modes (user-confirmed Option B):
      - accumulated: omit `since` -> every matching lead for the campaign.
      - per-run batch: pass `since` (a created_at high-water cursor) -> only
        leads newer than it.
    scope="unprocessed" (default) filters to status='new' (not yet outreach'd);
    scope="all" returns rows regardless of status. `status` is the anti-double-
    send marker n8n flips to 'contacted' after sending via /leads/status.
    """
    data = request.get_json(force=True) or {}
    campaign_id = data.get("campaign_id")
    if not campaign_id:
        return jsonify({"ok": False,
                        "error": "campaign_id is required"}), 400

    # Read-scoping: only the owning seller may pull this campaign's leads.
    seller_id = _resolve_read_seller(data)
    if not seller_id:
        return jsonify({"ok": False,
                        "error": "seller_id is required "
                                 "(or set DEFAULT_SELLER_ID)"}), 400
    if not _seller_owns_campaign(seller_id, campaign_id):
        return jsonify({"ok": False,
                        "error": "campaign not found or not owned by this "
                                 "seller"}), 403

    # A form that sends scope as an empty string (field left blank) must mean
    # the default, not an invalid value -- str("") would otherwise 400.
    scope = (str(data.get("scope") or "unprocessed").strip().lower()
             or "unprocessed")
    if scope not in ("unprocessed", "all"):
        return jsonify({"ok": False,
                        "error": "scope must be 'unprocessed' or 'all'"}), 400

    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503

    filters = {"campaign_id": campaign_id}
    if scope == "unprocessed":
        filters["status"] = "new"

    since = data.get("since")
    filters_gte = {"created_at": since} if since else None

    try:
        limit = int(data.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200

    try:
        rows = supabase_store.select_rows(
            "leads", columns=LEAD_COLUMNS, filters=filters,
            filters_gte=filters_gte, order="created_at.asc", limit=limit)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # Cursor = newest created_at returned, so n8n can advance to the next batch.
    next_cursor = None
    if rows:
        newest = max((r.get("created_at") for r in rows if r.get("created_at")),
                     default=None)
        next_cursor = newest

    return jsonify({
        "ok": True,
        "campaign_id": campaign_id,
        "scope": scope,
        "count": len(rows),
        "leads": _flatten_leads(rows),
        "next_cursor": next_cursor,
    })


@app.route("/leads/status", methods=["POST"])
def leads_status():
    """
    Mark one or more leads with a lifecycle status (the outreach handoff). n8n
    calls this after sending to flip 'new' -> 'contacted' (etc.), which removes
    the lead from the next /leads unprocessed pull (no double-send). Secrets stay
    server-side -- n8n only tells the engine which ids changed state.
    """
    data = request.get_json(force=True) or {}
    lead_ids = data.get("lead_ids") or data.get("id")
    status = str(data.get("status", "")).strip().lower()

    if not lead_ids:
        return jsonify({"ok": False,
                        "error": "lead_ids (or id) is required"}), 400
    if status not in LEAD_STATUSES:
        return jsonify({"ok": False,
                        "error": f"status must be one of "
                                 f"{sorted(LEAD_STATUSES)}"}), 400

    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503

    if isinstance(lead_ids, str):
        lead_ids = [lead_ids]
    if not isinstance(lead_ids, list):
        return jsonify({"ok": False,
                        "error": "lead_ids must be a uuid string or list"}), 400

    # Read-scoping: a seller may only change status on leads they own. All-or-
    # nothing: if ANY target lead's campaign isn't owned by the seller, refuse
    # the whole batch (no partial mutation).
    seller_id = _resolve_read_seller(data)
    if not seller_id:
        return jsonify({"ok": False,
                        "error": "seller_id is required "
                                 "(or set DEFAULT_SELLER_ID)"}), 400
    unique_ids = list(dict.fromkeys(lead_ids))  # preserve order, dedupe
    try:
        lrows = supabase_store.select_rows(
            "leads", columns="id,campaign_id", filters_in={"id": unique_ids})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if len(lrows) != len(unique_ids):
        return jsonify({"ok": False,
                        "error": "one or more leads not found or not owned by "
                                 "this seller"}), 403
    campaign_ids = sorted({str(r["campaign_id"]) for r in lrows
                           if r.get("campaign_id")})
    if not campaign_ids:
        return jsonify({"ok": False,
                        "error": "leads have no campaign owner to verify"}), 403
    try:
        crow = supabase_store.select_rows(
            "campaigns", columns="id,seller_id", filters_in={"id": campaign_ids})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    owned = set()
    for c in crow:
        owner = c.get("seller_id")
        if owner is not None:
            if str(owner) == seller_id:
                owned.add(str(c["id"]))
        elif seller_id == (lead_engine.DEFAULT_SELLER_ID or "").strip():
            owned.add(str(c["id"]))  # DEFAULT bridge owns legacy NULL-owner rows
    if not set(campaign_ids).issubset(owned):
        return jsonify({"ok": False,
                        "error": "one or more leads not found or not owned by "
                                 "this seller"}), 403

    updated = 0
    errors = []
    for lid in unique_ids:
        try:
            supabase_store.update_rows("leads", {"status": status},
                                       {"id": lid})
            updated += 1
        except Exception as e:
            errors.append(f"{lid}: {e}")
    return jsonify({"ok": not errors, "updated": updated,
                    "errors": errors})


_LEAD_STATUSES_LIST = ("new", "contacted", "replied", "won", "lost")


@app.route("/campaigns/summary", methods=["POST"])
def campaigns_summary():
    """
    Campaign-level read for a human-facing review/dashboard view (one row per
    campaign, with counts). Contrast with /leads, which is the tight machine
    feed n8n drains one batch at a time.

    Body (optional): { "campaign_id": "<uuid>" } to limit to one campaign.
    Omit it to summarise every campaign.

    Per campaign returns, over the leads table (every eligible scored business,
    qualified or not):
      total_leads  = all lead rows for the campaign (status funnel summed)
      qualified    = subset that scored above threshold (qualified=true)
      ready        = status == 'new'  -> what outreach can claim right now
                    (includes non-qualified-but-eligible rows, as agreed)
      statuses     = {new, contacted, replied, won, lost} funnel map

    Counts are aggregated in Postgres by the `campaign_summary` SQL function
    (appended to supabase-schema.sql -- run it in the Supabase SQL editor) and
    returned through a PostgREST RPC. The engine never pulls the whole leads
    table just to tally; only per-campaign summary rows cross the wire.

    The body is optional (an empty body legitimately means "every campaign"), so
    this route is tolerant: it never force-parses, and a missing/blank/non-JSON
    body just reads as {}.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503

    # Read-scoping: summary is always ONE seller's dashboard view. Resolve the
    # seller (body seller_id -> DEFAULT bridge); a summary is never all-sellers.
    seller_id = _resolve_read_seller(data)
    if not seller_id:
        return jsonify({"ok": False,
                        "error": "seller_id is required "
                                 "(or set DEFAULT_SELLER_ID)"}), 400

    try:
        rows = supabase_store.rpc(
            "campaign_summary",
            {"p_campaign_id": data.get("campaign_id") or None,
             "p_seller_id": seller_id})
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": "campaign_summary DB function unavailable. Run the "
                     "campaign_summary (seller) function from supabase-schema.sql "
                     f"in the Supabase SQL editor, then retry. ({e})",
        }), 502

    out = []
    for r in rows or []:
        sts = {s: int(r.get(f"status_{s}") or 0) for s in _LEAD_STATUSES_LIST}
        out.append({
            "campaign_id": r.get("campaign_id"),
            "name": r.get("name"),
            "keyword": r.get("keyword"),
            "location": r.get("location"),
            "niche": r.get("niche") or "",
            "created_at": r.get("created_at"),
            "total_leads": int(r.get("total_leads") or 0),
            "qualified": int(r.get("qualified") or 0),
            "ready": int(r.get("ready") or 0),
            "statuses": sts,
        })
    return jsonify({"ok": True, "count": len(out), "campaigns": out})


@app.route("/email", methods=["POST"])
def email_scrape():
    data = request.get_json(force=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400

    args = [sys.executable, "email_scraper.py", url, "--json", "-q"]
    if data.get("max_depth") is not None:
        args += ["-d", str(int(data["max_depth"]))]
    if data.get("max_count") is not None:
        args += ["-c", str(int(data["max_count"]))]
    if data.get("analyze_pages_limit") is not None:
        args += ["--analyze-pages-limit", str(int(data["analyze_pages_limit"]))]
    if data.get("all_domains"):
        args += ["--all-domains"]
    if data.get("proxy"):
        args += ["--proxy", data["proxy"]]
    if data.get("min_delay") is not None:
        args += ["--min-delay", str(data["min_delay"])]
    if data.get("max_delay") is not None:
        args += ["--max-delay", str(data["max_delay"])]
    if data.get("retries") is not None:
        args += ["--retries", str(int(data["retries"]))]

    code, stdout, stderr = run_script(args, timeout=data.get("timeout", SUBPROCESS_TIMEOUT))

    parsed = None
    if code == 0 and stdout.strip():
        try:
            # --json prints one JSON line; be tolerant of any stray output before it
            last_line = stdout.strip().splitlines()[-1]
            parsed = json.loads(last_line)
        except (json.JSONDecodeError, IndexError):
            parsed = None

    emails = parsed.get("emails", {}) if parsed else {}
    return jsonify({
        "ok": code == 0 and parsed is not None,
        "returncode": code,
        "emails": list(emails.keys()),          # flat list for easy downstream use
        "email_sources": emails,                # email -> [pages it was found on]
        "email_count": len(emails),
        "intelligence": parsed.get("intelligence", {}) if parsed else {},
        "pages_scraped": parsed.get("pages_scraped", 0) if parsed else 0,
        "scrape_errors": parsed.get("errors", []) if parsed else [],
        "stdout": stdout[-4000:],
        "stderr": stderr[-2000:],
    })


# ============================================================================== #
# Phase 4 -- multi-tenant sellers (see docs/multi-tenant-plan.md, Steps 3-5).
# seller_profile rows own campaigns. A campaign inherits its seller; leads inherit
# through their campaign. These endpoints let the n8n web form create/look up a
# seller, and an operator later PATCH config (settings jsonb) per tenant.
# ============================================================================== #
SELLER_RENDER_MODES = {"html", "js", "auto"}
_SELLER_PATCHABLE = ("name", "title", "brand", "niche", "phone",
                     "portfolio_url", "render_mode", "active")
_SELLER_COLUMNS = ("id,email,name,title,brand,niche,phone,resume_text,"
                   "portfolio_url,portfolio_text,render_mode,settings,active,"
                   "created_at,updated_at")


def _render_mode(value, default="auto"):
    v = str(value or "").strip().lower()
    return v if v in SELLER_RENDER_MODES else default


def _maybe_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@app.route("/seller", methods=["POST"])
def seller_create():
    """Find-or-create a seller by email (the natural key the n8n web form uses).
    Re-posting the same email always yields the same seller_id, so a form submit
    can safely be repeated. On an EXISTING seller, any profile field the form
    actually filled in is applied (so re-running the profile form sets a niche
    instead of silently doing nothing); fields left blank are NOT cleared, since
    a form only sends what the operator typed. render_mode defaults to 'auto'
    on create unless the form says otherwise (html | js | auto)."""
    data = request.get_json(force=True) or {}
    email = str(data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"ok": False, "error": "a valid email is required"}), 400
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503

    def _text(k):
        """Coerce any scalar (str/int/float) to a trimmed string, else None.
        n8n forms may send numbers (e.g. a phone typed as digits), so never
        call .strip() on the raw value."""
        v = data.get(k)
        return None if v is None else (str(v).strip() or None)

    try:
        existing = supabase_store.select_rows(
            "seller_profile", columns=_SELLER_COLUMNS,
            filters={"email": email}, limit=1)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    if existing:
        # Update only the fields the form actually supplied (non-empty).
        updates = {}
        for k in ("name", "title", "brand", "niche", "phone"):
            v = _text(k)
            if v:
                updates[k] = v
        if "render_mode" in data:
            updates["render_mode"] = _render_mode(data.get("render_mode"))
        if not updates:
            return jsonify({"ok": True, "created": False, "updated": False,
                            "seller": existing[0]})
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            updated = supabase_store.update_rows(
                "seller_profile", updates, {"id": existing[0]["id"]})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        return jsonify({"ok": True, "created": False, "updated": True,
                        "seller": (updated or existing)[0]})

    row = {
        "email": email,
        "name": _text("name"),
        "title": _text("title"),
        "brand": _text("brand"),
        "niche": _text("niche"),
        "phone": _text("phone"),
        "render_mode": _render_mode(data.get("render_mode")),
    }
    try:
        created = supabase_store.insert_rows("seller_profile", row)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "created": True,
                    "seller": (created or [{}])[0]})


@app.route("/seller/list", methods=["GET"])
def seller_list():
    """List sellers. ?active=false includes inactive; default lists active only.
    Returns slim rows (identity + render_mode) for dropdowns / the web form."""
    try:
        active = request.args.get("active")
    except Exception:
        active = None
    filters = None
    if active is None or str(active).strip().lower() in ("1", "true", "yes", ""):
        filters = {"active": "true"}
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503
    try:
        rows = supabase_store.select_rows(
            "seller_profile",
            columns=("id,email,name,brand,title,render_mode,active,created_at"),
            filters=filters, order="created_at.desc")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "count": len(rows), "sellers": rows})


@app.route("/seller/by-email", methods=["GET"])
def seller_by_email():
    """Look a seller up by email (n8n convenience: confirm a seller_id from the
    form's email before wiring a campaign to it)."""
    email = str(request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email query param is required"}), 400
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503
    try:
        rows = supabase_store.select_rows(
            "seller_profile", columns=_SELLER_COLUMNS,
            filters={"email": email}, limit=1)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not rows:
        return jsonify({"ok": False, "error": "seller not found"}), 404
    return jsonify({"ok": True, "seller": rows[0]})


@app.route("/seller/<seller_id>", methods=["GET"])
def seller_get(seller_id):
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503
    try:
        rows = supabase_store.select_rows(
            "seller_profile", columns=_SELLER_COLUMNS,
            filters={"id": seller_id}, limit=1)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not rows:
        return jsonify({"ok": False, "error": "seller not found"}), 404
    return jsonify({"ok": True, "seller": rows[0]})


@app.route("/seller/<seller_id>", methods=["PATCH"])
def seller_update(seller_id):
    """Update profile fields on a seller. Only the listed scalar columns are
    accepted (config/settings + resume/portfolio get their own routes)."""
    data = request.get_json(force=True) or {}
    updates = {}
    for k in _SELLER_PATCHABLE:
        if k not in data:
            continue
        v = data[k]
        if k == "render_mode":
            v = _render_mode(v)
            if v not in SELLER_RENDER_MODES:
                return jsonify({"ok": False, "error":
                                "render_mode must be html|js|auto"}), 400
        elif k == "active":
            v = _maybe_bool(v)
            if v is None:
                continue
        elif v is None:
            continue
        else:
            v = (str(v).strip() or None)
        updates[k] = v
    if not updates:
        return jsonify({"ok": False, "error": "no valid fields to update"}), 400

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503
    try:
        updated = supabase_store.update_rows(
            "seller_profile", updates, {"id": seller_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not updated:
        return jsonify({"ok": False, "error": "seller not found"}), 404
    return jsonify({"ok": True, "seller": updated[0]})


def _extract_resume_text(filename, content_b64):
    """Decode a base64 resume (.docx/.pdf) to plain text. Raises ValueError on
    bad base64 or unsupported type; the libs (python-docx/pypdf) are imported
    lazily so the app boots even if they're absent until first use."""
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except Exception:
        raise ValueError("content_base64 is not valid base64")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "docx":
        import docx
        d = docx.Document(BytesIO(raw))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append("\t".join(c.text for c in row.cells))
        text = "\n".join(p for p in parts if p and p.strip())
    elif ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    else:
        raise ValueError("unsupported file type '.%s' — expected .docx or .pdf"
                         % ext)
    text = text.strip()
    if not text:
        raise ValueError("no extractable text found in the resume "
                         "(scanned/image PDFs are not supported)")
    return text


@app.route("/seller/<seller_id>/resume", methods=["POST"])
def seller_resume_upload(seller_id):
    """Store a seller's resume. Body: {filename, content_base64}. The base64 is
    the raw file bytes; only the EXTRACTED text is kept in the DB (never the
    file), per the agreed design. .docx and .pdf are supported."""
    data = request.get_json(force=True) or {}
    filename = str(data.get("filename") or "").strip()
    content_b64 = data.get("content_base64")
    if not filename or not content_b64:
        return jsonify({"ok": False,
                        "error": "filename and content_base64 are required"}), 400
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503
    try:
        text = _extract_resume_text(filename, content_b64)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        # A file we can't read/decode is the client's bad upload, not a server
        # fault — 400 (so n8n/form knows to reject the file), not 502.
        return jsonify({"ok": False,
                        "error": f"resume could not be parsed: {e}"}), 400

    updates = {"resume_text": text, "resume_filename": filename,
               "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        updated = supabase_store.update_rows(
            "seller_profile", updates, {"id": seller_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not updated:
        return jsonify({"ok": False, "error": "seller not found"}), 404
    return jsonify({"ok": True, "chars": len(text),
                    "resume_filename": filename,
                    "seller": updated[0]})


@app.route("/seller/resume", methods=["POST"])
def seller_resume_by_email():
    """n8n form upload for the resume step: multipart file + ?email= . Resolves
    the seller by email (so n8n can send the file straight from a Form node with
    no intermediate lookup), then behaves exactly like POST /seller/<id>/resume.
    The lone uploaded file is taken regardless of the multipart field name n8n
    used, since that name is not guaranteed across n8n versions."""
    email = str(request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "email query param is required"}), 400
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503

    # Resolve the seller from the form's email.
    try:
        rows = supabase_store.select_rows(
            "seller_profile", columns=("id",),
            filters={"email": email}, limit=1)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not rows:
        return jsonify({"ok": False, "error": "seller not found"}), 404
    seller_id = rows[0]["id"]

    # Take the single uploaded file, whatever its field name.
    files = request.files or {}
    if not files:
        return jsonify({"ok": False,
                        "error": "a file upload is required"}), 400
    up = files[next(iter(files))]
    filename = (up.filename or "").strip()
    raw = up.read()
    if not filename or not raw:
        return jsonify({"ok": False,
                        "error": "empty file or missing filename"}), 400
    content_b64 = base64.b64encode(raw).decode("ascii")

    try:
        text = _extract_resume_text(filename, content_b64)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"resume could not be parsed: {e}"}), 400

    updates = {"resume_text": text, "resume_filename": filename,
               "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        updated = supabase_store.update_rows(
            "seller_profile", updates, {"id": seller_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not updated:
        return jsonify({"ok": False, "error": "seller not found"}), 404
    return jsonify({"ok": True, "chars": len(text),
                    "resume_filename": filename,
                    "seller": updated[0]})


@app.route("/seller/<seller_id>/portfolio", methods=["POST"])
def seller_portfolio_fetch(seller_id):
    """Scrape a seller's portfolio website into visible text, honoring the
    seller's stored tri-state render_mode (html|js|auto). Body: {url,
    render_mode?} — render_mode only overrides when the caller sends a valid one
    (normally the seller's creation form already fixed render_mode)."""
    import portfolio  # lazy: pulls the hardened fetch stack only when used

    data = request.get_json(force=True) or {}
    url = str(data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503
    try:
        sellers = supabase_store.select_rows(
            "seller_profile", columns="id,render_mode", filters={"id": seller_id},
            limit=1)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not sellers:
        return jsonify({"ok": False, "error": "seller not found"}), 404

    body_mode = str(data.get("render_mode") or "").strip().lower()
    mode = body_mode if body_mode in SELLER_RENDER_MODES \
        else (sellers[0].get("render_mode") or "auto")

    try:
        fetched = portfolio.fetch_portfolio(url, mode)
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"portfolio fetch failed: {e}"}), 502
    if not fetched.get("ok"):
        return jsonify({"ok": False, "error": fetched.get("error")}), 502

    updates = {"portfolio_url": url, "portfolio_text": fetched.get("text", ""),
               "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        supabase_store.update_rows("seller_profile", updates, {"id": seller_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    return jsonify({
        "ok": True,
        "portfolio_url": url,
        "render_mode_used": fetched.get("render_mode"),
        "rendered_js": fetched.get("rendered"),
        "page_title": fetched.get("page_title"),
        "chars": len(fetched.get("text", "")),
        "notes": fetched.get("notes", []),
    })


_DRAFT_LEAD_COLUMNS = (
    "id,campaign_id,prospect_id(id,business_name,phone,website,category,"
    "locality,street,region,zipcode),weakness,first_line,intelligence,emails,"
    "digital_presence,opportunity_score,qualified,created_at")


def _decoded(obj):
    if isinstance(obj, str):
        try:
            return json.loads(obj)
        except (json.JSONDecodeError, ValueError):
            return {}
    return obj or {}


def _resolve_draft_seller(data, campaign_seller_id):
    """Pick which seller drafts: explicit body seller_id wins, else the campaign
    owner, else the DEFAULT_SELLER_ID single-user fallback."""
    sid = str(data.get("seller_id") or "").strip() or None
    if not sid:
        sid = campaign_seller_id or None
    if not sid:
        sid = lead_engine.DEFAULT_SELLER_ID
    return sid


@app.route("/draft", methods=["POST"])
def draft():
    """Generate a personalized outreach email + subject + angle for ONE lead, on
    behalf of ONE seller (Step 4). Body: {lead_id, seller_id?}. The seller is
    resolved: body seller_id -> the lead's campaign owner -> DEFAULT_SELLER_ID.
    Pulls the lead's business + likely-gap context, the campaign's niche, and the
    seller's stored name/title/brand/resume/portfolio, and hands them to the LLM.
    Output {subject, email_body, angle} is returned, NOT persisted."""
    data = request.get_json(force=True) or {}
    lead_id = str(data.get("lead_id") or "").strip()
    if not lead_id:
        return jsonify({"ok": False, "error": "lead_id is required"}), 400
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503

    # 1. The lead + its embedded business (prospect) fields.
    try:
        leads = supabase_store.select_rows(
            "leads", columns=_DRAFT_LEAD_COLUMNS,
            filters={"id": lead_id}, limit=1)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not leads:
        return jsonify({"ok": False, "error": "lead not found"}), 404
    lead = leads[0]

    # 2. The campaign (for its niche + owning seller).
    campaign_id = lead.get("campaign_id")
    niche = None
    campaign_seller_id = None
    if campaign_id:
        try:
            camps = supabase_store.select_rows(
                "campaigns", columns="id,keyword,niche_rules,seller_id",
                filters={"id": campaign_id}, limit=1)
        except Exception as e:
            camps = []
            # non-fatal: continue without campaign niche
        if camps:
            rules = _decoded(camps[0].get("niche_rules"))
            niche = rules.get("niche") or camps[0].get("keyword")
            campaign_seller_id = camps[0].get("seller_id")

    # 3. Resolve the seller: fetch their identity + resume/portfolio (the draft
    # context) AND their settings (provider config). The settings jsonb (which
    # can hold encrypted provider keys) is NEVER handed to the model.
    seller_id = _resolve_draft_seller(data, campaign_seller_id)
    # Read-scoping: a draft is composed from BOTH the lead's business context and
    # the seller's own identity (name/brand/resume/portfolio) using the seller's
    # credentials, so the resolved seller must actually own the lead's campaign
    # (mirrors /leads and /leads/status). Without this gate, one tenant could
    # pass another tenant's lead_id and read that business's details back out of
    # the generated draft. A lead we can't attribute to a campaign is refused,
    # because ownership then cannot be verified at all.
    if seller_id and not _seller_owns_campaign(seller_id, campaign_id):
        return jsonify({"ok": False,
                        "error": "lead not found or not owned by this "
                                 "seller"}), 403
    seller = {}
    settings = None
    if seller_id:
        try:
            sellers = supabase_store.select_rows(
                "seller_profile",
                columns=("id,email,name,title,brand,phone,resume_text,"
                         "portfolio_text,render_mode,settings"),
                filters={"id": seller_id}, limit=1)
        except Exception as e:
            sellers = []
        if sellers:
            seller = {k: sellers[0].get(k) for k in
                      ("name", "title", "brand", "phone",
                       "resume_text", "portfolio_text")}
            settings = _decoded(sellers[0].get("settings"))

    # 4. Flatten the business fields to a clean prospect snapshot.
    prospect = lead.get("prospect_id")
    if isinstance(prospect, dict):
        business = {k: prospect.get(k) for k in
                    ("business_name", "category", "phone", "website",
                     "locality", "street", "region", "zipcode")}
        business = {k: v for k, v in business.items() if v}
    else:
        business = {"business_name": "this business"}
    for k in ("business_name", "category", "website", "locality"):
        if not business.get(k):
            business[k] = lead.get(k)

    intelligence = _decoded(lead.get("intelligence"))
    weakness = str(lead.get("weakness") or "").strip()
    first_line = str(lead.get("first_line") or "").strip()
    if not weakness and (business.get("category") or niche):
        weakness = (f"Category: {business.get('category') or '?'}. "
                    f"We're selling: {niche or '?'}.")

    # Resolve the seller's LLM provider (their stored provider/model/key, else
    # env-master). model: an explicit score_model overrides; else the seller's.
    provider = get_llm(config.llm_cfg(settings, model=data.get("score_model")))
    try:
        draft_out = llm.draft_email(
            provider,
            business=business, intelligence=intelligence, niche=niche,
            weakness=weakness, first_line=first_line,
            seller=seller,
            temperature=float(data.get("temperature") or 0.7),
        )
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"draft failed: {e}"}), 502

    return jsonify({
        "ok": True,
        "lead_id": lead_id,
        "seller_id": seller_id,
        "seller_name": (seller.get("name") or seller.get("brand")) or None,
        "campaign_id": campaign_id,
        "niche": niche,
        **draft_out,
    })


def _mask_settings(settings):
    """Return a view of a seller's settings for API responses: provider/model
    readable, but every stored secret shown only as a 'set' flag (never the
    token or plaintext)."""
    settings = _decoded(settings)
    llm = settings.get("llm") or {}
    finder = settings.get("finder") or {}
    masked = {
        "llm": {
            "provider": llm.get("provider"),
            "model": llm.get("model"),
            "base_url": llm.get("base_url"),
            "api_key": "********" if llm.get("api_key_enc") else None,
        },
        "finder": {
            "provider": finder.get("provider"),
            "login": "********" if finder.get("login_enc") else None,
            "password": "********" if finder.get("password_enc") else None,
        },
    }
    return masked


def _get_seller_settings(seller_id):
    try:
        rows = supabase_store.select_rows(
            "seller_profile", columns="id,settings", filters={"id": seller_id},
            limit=1)
    except Exception as e:
        raise RuntimeError(str(e))
    if not rows:
        return None
    return _decoded(rows[0].get("settings"))


@app.route("/seller/<seller_id>/config", methods=["GET"])
def seller_config_get(seller_id):
    """Return the seller's provider settings MASKED (no secrets, only whether
    each is set). Use this to render the config form in n8n/the UI."""
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503
    try:
        settings = _get_seller_settings(seller_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if settings is None:
        return jsonify({"ok": False, "error": "seller not found"}), 404
    return jsonify({"ok": True, "config": _mask_settings(settings)})


@app.route("/seller/<seller_id>/config", methods=["PATCH"])
def seller_config_patch(seller_id):
    """Update a seller's provider settings. Body mirrors the settings shape:
      { "llm":    {"provider","model","base_url","api_key"},
        "finder": {"provider","login","password"} }
    Secret values (api_key/login/password) are ENCRYPTED before storage; sending
    null/"" for one clears it (falls back to the env master key). Provider names
    are validated against the registry."""
    data = request.get_json(force=True) or {}
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503
    try:
        settings = _get_seller_settings(seller_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if settings is None:
        return jsonify({"ok": False, "error": "seller not found"}), 404

    body_llm = data.get("llm")
    body_finder = data.get("finder")
    llm_s = dict(settings.get("llm") or {})
    finder_s = dict(settings.get("finder") or {})

    def _store_secret(cur, enc_field, value):
        # value is plaintext from the caller; encrypt (or clear) before storing.
        if value is None or str(value).strip() == "":
            cur.pop(enc_field, None)
            return
        try:
            cur[enc_field] = _secrets.encrypt(str(value))
        except _secrets.KeyNotConfigured:
            raise RuntimeError(
                "CRED_ENCRYPTION_KEY is not set — cannot store a per-seller key. "
                "Set it in .env (see secrets.py).")

    try:
        if isinstance(body_llm, dict):
            if "provider" in body_llm:
                p = str(body_llm.get("provider") or "").strip().lower() or None
                if p and p not in llm_provider_names():
                    return jsonify({"ok": False,
                                    "error": f"unknown llm provider '{p}'. "
                                             f"Use one of "
                                             f"{sorted(llm_provider_names())}"}), 400
                llm_s["provider"] = p
            for k in ("model", "base_url"):
                if k in body_llm:
                    llm_s[k] = (str(body_llm.get(k) or "").strip() or None)
            if "api_key" in body_llm:
                _store_secret(llm_s, "api_key_enc", body_llm.get("api_key"))

        if isinstance(body_finder, dict):
            if "provider" in body_finder:
                p = str(body_finder.get("provider") or "").strip().lower() or None
                if p and p not in source_provider_names():
                    return jsonify({"ok": False,
                                    "error": f"unknown source provider '{p}'"}), 400
                finder_s["provider"] = p
            if "login" in body_finder:
                _store_secret(finder_s, "login_enc", body_finder.get("login"))
            if "password" in body_finder:
                _store_secret(finder_s, "password_enc",
                              body_finder.get("password"))
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 503

    settings["llm"] = llm_s
    settings["finder"] = finder_s
    try:
        updated = supabase_store.update_rows(
            "seller_profile",
            {"settings": json.dumps(settings, default=str),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            {"id": seller_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not updated:
        return jsonify({"ok": False, "error": "seller not found"}), 404
    return jsonify({"ok": True, "config": _mask_settings(settings)})


if __name__ == "__main__":
    # set app to debug mode
    app.run(host="127.0.0.1", port=5000, debug=True)

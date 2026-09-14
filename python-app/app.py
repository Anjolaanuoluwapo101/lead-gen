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

# THIS ORDER IS LOAD-BEARING — do not move this import down.
#
# config, llm, lead_engine, supabase_store, dataforseo, groq_ai and at_rest all
# read os.environ AT IMPORT TIME. Hydrating after them leaves each holding empty
# credentials, and the failure then surfaces deep inside a request instead of at
# boot — which is exactly the bug agentcore_app.py documents in the same place.
#
# It is here rather than in the __main__ block because that block does not run
# under gunicorn, which is how the deployed ECS image serves this app.
#
# Locally LEADGEN_SECRETS_PATH is unset, hydrate() is a no-op, and the repo-root
# .env loads exactly as before. Nothing changes for local development.
import runtime_secrets  # noqa: E402

_BOOT = runtime_secrets.hydrate()

import config  # per-seller provider config resolution
import llm  # transport-agnostic LLM task layer (score_lead / draft_email)
import lead_engine  # /campaign orchestrator (find->enrich->score->store)
import supabase_store  # /leads + /leads/status read/write helpers
import dataforseo  # /location lookup helper (resolves a place -> location_code)
from providers import (  # provider registry (adapters + name lists)
    get_llm, llm_provider_names, source_provider_names)
import at_rest as _secrets  # Fernet secrets-at-rest (module named at_rest to avoid shadowing stdlib secrets)
import leads_read  # shared lead-read helpers, also used by the Strands agent
import seller_ops  # shared seller/lead writes, also used by the Strands agent
import auth  # caller identity -> seller_id (Supabase Auth + the n8n service token)
import draft_compose  # shared draft composition, also used by routes_agent
import file_store  # where an uploaded file's BYTES live (swappable backend)

# Backwards-compatible aliases: these helpers moved to leads_read.py so the Flask
# app and the agent share ONE implementation. Call sites below are unchanged.
# (They must be deleted from this module, not just shadowed -- a later `def` at
# module level would rebind the name and silently undo the alias.)
_flatten_leads = leads_read.flatten_leads
LEAD_COLUMNS = leads_read.LEAD_COLUMNS
_DRAFT_LEAD_COLUMNS = leads_read.DRAFT_LEAD_COLUMNS
_decoded = leads_read.decoded
_seller_owns_campaign = leads_read.owns_campaign

app = Flask(__name__)

# Names only, never values. On a deployed instance this is the one line that
# says whether credentials arrived before the app is asked to use them.
#
# The level is set explicitly because it is otherwise ineffective: with debug
# off, Flask leaves the app logger at NOTSET, so it inherits the root logger's
# WARNING and an INFO call is dropped silently. A diagnostic that never prints
# is worse than no diagnostic -- it reads as "hydration did not run".
app.logger.setLevel("INFO")
app.logger.info("secrets: %s",
                _BOOT.get("skipped") or f"{_BOOT.get('loaded')} from SSM")


def _warm_picker_index():
    """Build the dashboard picker's search index off the request path.

    Cold it costs seconds (parsing the cache file); warm a query answers in
    under half a second. Warming in a daemon thread at boot means the first
    keystroke is already warm — otherwise the seller's first type pays the
    cold cost and the picker feels dead exactly once, on its first impression.
    Skipped under pytest, where a background parse would only churn CPU behind
    the suite that never touches the warmed copy.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        return
    import threading
    threading.Thread(target=dataforseo._picker_index, name="picker-warm",
                     daemon=True).start()


_warm_picker_index()

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


@app.route("/location/search", methods=["POST"])
def location_search():
    """
    Typeahead for the dashboard place picker: a few characters in, up to ten
    candidate search areas out. Reads the cached world list only, so it costs
    nothing and needs no DataForSEO credentials. Every returned row is directly
    selectable: its location_code pins the run's search area.
    """
    data = request.get_json(force=True, silent=True) or {}
    q = str(data.get("q") or data.get("place") or "").strip()
    try:
        limit = int(data.get("limit") or data.get("top") or 10)
    except (TypeError, ValueError):
        limit = 10
    try:
        result = dataforseo.search_locations(q, limit=limit)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, **result})


# Lifecycle statuses a lead may take (must match leads.status).
LEAD_STATUSES = {"new", "contacted", "replied", "won", "lost"}
# LEAD_COLUMNS / _flatten_leads moved to leads_read.py (aliased above) so the
# Flask app and the Strands agent share one implementation.


# --- Seller read-scoping -------------------------------------------------- #
# Wired so auth could inject a trusted seller_id with no redesign — and it now
# does. A read is never open across sellers: every data-revealing route resolves
# the caller's seller and refuses rows it doesn't own.
#
# This comment used to say "when auth lands, replace _resolve_read_seller with a
# session-derived id". That is exactly what happened, and it cost one function
# body: the three call sites below did not change.

@app.errorhandler(auth.AuthError)
def _auth_error(exc):
    """A credential problem is not an ownership problem.

    Without this, an expired token would surface as whatever the route's `.get`
    returned — usually a 400 "no seller", which tells a logged-out user their
    request was malformed instead of telling them to log in.
    """
    return jsonify({"ok": False, "error": exc.message}), exc.status


def _resolve_read_seller(data):
    """Resolve the caller's seller, or None when nothing identifies them.

    Delegates to auth.resolve_seller, which layers the credentials:
    bearer token -> that user's seller_profile, X-Service-Token ->
    DEFAULT_SELLER_ID (n8n), else the body seller_id -> DEFAULT_SELLER_ID bridge
    this function used to implement itself.

    Raises auth.AuthError on a bad credential; the handler above turns that into
    a 401/503. Returns None only when there is genuinely no seller to resolve,
    which callers answer with 400.
    """
    return auth.resolve_seller(data, request.headers)


def _require_seller(seller_id):
    """Gate a route whose SUBJECT comes from the URL.

    `/seller/<seller_id>/*` is the one family of routes where the seller is not
    the caller's to be inferred from the credential — the caller names it
    outright. So the credential has to be compared against it, and until
    `auth.authorize_seller` existed nothing did that: any caller could read or
    overwrite any seller's profile, resume, portfolio and provider keys.

    Raises auth.AuthError(403), which the errorhandler above renders.
    """
    return auth.authorize_seller(seller_id, {}, request.headers)


def _require_operator():
    """Refuse a route that only the platform has any business calling.

    `/seller/list` and `/seller/by-email` both take their subject from a query
    parameter rather than a credential, so there is no id to compare — they are
    operator tools by construction. A tenant wanting their own row uses
    `/seller/me`, which cannot be pointed at anyone else.

    Same switch as `authorize_seller`: open while AUTH_REQUIRED is false, closed
    the moment it is turned on.
    """
    if not auth.auth_required() or auth.is_operator(request.headers):
        return
    raise auth.AuthError("this endpoint is operator-only", status=403)


# _seller_owns_campaign moved to leads_read.owns_campaign (aliased above) so the
# ownership rule lives in exactly one place.


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
# The seller/lead RULES now live in seller_ops.py, because the Strands agent
# needs the same operations and a second copy would drift. These routes are the
# HTTP skin over it: parse the request, call, translate OpsError to a status.
#
# Aliased rather than re-defined so `app._SELLER_COLUMNS` stays one object with
# the shared module (test_app_rewire.py asserts the identity).
SELLER_RENDER_MODES = seller_ops.SELLER_RENDER_MODES
_SELLER_PATCHABLE = seller_ops.SELLER_PATCHABLE
_SELLER_COLUMNS = seller_ops.SAFE_COLUMNS
_render_mode = seller_ops.render_mode
_maybe_bool = seller_ops.maybe_bool

# OpsError.kind -> HTTP status. Every failure the shared layer can raise is
# listed; an unlisted kind is a bug, and falls through to 502 rather than 200.
_OPS_STATUS = {"bad_request": 400, "not_found": 404,
               "unavailable": 503, "upstream": 502}


def _ops_response(fn, *args, wrap=None, **kwargs):
    """Run a seller_ops call and turn its outcome into a Flask response.

    Returning (body, status) centrally is what keeps the routes from inventing
    their own error shapes — and is why a new failure mode in seller_ops cannot
    silently start reporting success.

    `wrap` names the key a bare ROW goes under. These routes have always
    answered {"ok": true, "seller": {...}}, and the n8n workflows read that
    shape, so a single-row result must not be flattened into the envelope.
    """
    try:
        result = fn(*args, **kwargs)
    except seller_ops.OpsError as e:
        return jsonify({"ok": False, "error": e.message}), \
            _OPS_STATUS.get(e.kind, 502)
    if wrap:
        return jsonify({"ok": True, wrap: result}), 200
    return jsonify({"ok": True, **result}), 200


@app.route("/seller", methods=["POST"])
def seller_create():
    """Find-or-create a seller by email (the natural key the n8n web form uses).
    Re-posting the same email always yields the same seller_id, so a form submit
    can safely be repeated. On an EXISTING seller, any profile field the form
    actually filled in is applied (so re-running the profile form sets a niche
    instead of silently doing nothing); fields left blank are NOT cleared, since
    a form only sends what the operator typed. render_mode defaults to 'auto'
    on create unless the form says otherwise (html | js | auto)."""
    # Operator-only, like the other two routes that name their subject instead
    # of being named by a credential. Unguarded this CREATES rows, so anyone
    # could reserve an email or fill the table. It is also not how a dashboard
    # user gets a profile — that is `auth.seller_for_user`, which find-or-creates
    # on the verified email of whoever is signed in — so nothing in the app
    # loses a path here.
    _require_operator()

    data = request.get_json(force=True) or {}
    return _ops_response(
        seller_ops.create_or_update_seller,
        data.get("email"),
        name=data.get("name"), title=data.get("title"), brand=data.get("brand"),
        niche=data.get("niche"), phone=data.get("phone"),
        render_mode_value=data.get("render_mode"),
        render_mode_given="render_mode" in data)


@app.route("/seller/list", methods=["GET"])
def seller_list():
    """List sellers. ?active=false includes inactive; default lists active only.
    Returns slim rows (identity + render_mode) for dropdowns / the web form."""
    # Enumerating every tenant is an operator action: this list is the input to
    # every other id-taking route, so leaving it open undoes their checks.
    _require_operator()

    active = request.args.get("active")
    active_only = (active is None
                   or str(active).strip().lower() in ("1", "true", "yes", ""))
    try:
        rows = seller_ops.list_sellers(active_only=active_only)
    except seller_ops.OpsError as e:
        return jsonify({"ok": False, "error": e.message}), \
            _OPS_STATUS.get(e.kind, 502)
    return jsonify({"ok": True, "count": len(rows), "sellers": rows})


@app.route("/seller/me", methods=["GET"])
def seller_me():
    """The caller's OWN seller profile.

    This is the multi-tenant-safe lookup, and the reason it exists is that
    `/seller/by-email` is not: that route takes the identity to fetch from the
    REQUEST (`?email=`), so it cannot tell a seller reading their own profile
    from anyone else reading it — and it returns the full safe row, resume and
    portfolio text included.

    Here the id comes from the caller's credential instead, so "read someone
    else's profile" is not an expressible request. The dashboard uses this.

    `get_seller`, not `find_seller_by_email`: there is no lookup to scope,
    because the id was never the caller's to choose.
    """
    seller_id = _resolve_read_seller({})
    if not seller_id:
        return jsonify({"ok": False,
                        "error": "no seller could be resolved for this "
                                 "request"}), 400
    return _ops_response(seller_ops.get_seller, seller_id, wrap="seller")


@app.route("/seller/by-email", methods=["GET"])
def seller_by_email():
    """Look a seller up by email (n8n convenience: confirm a seller_id from the
    form's email before wiring a campaign to it)."""
    # Resolving an arbitrary email to a seller id is the lookup step of every
    # other seller route, so it has to be at least as restricted as they are.
    _require_operator()

    email = str(request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False,
                        "error": "email query param is required"}), 400
    return _ops_response(seller_ops.find_seller_by_email, email, wrap="seller")


@app.route("/seller/<seller_id>", methods=["GET"])
def seller_get(seller_id):
    _require_seller(seller_id)
    return _ops_response(seller_ops.get_seller, seller_id, wrap="seller")


@app.route("/seller/<seller_id>", methods=["PATCH"])
def seller_update(seller_id):
    """Update profile fields on a seller. Only the listed scalar columns are
    accepted (config/settings + resume/portfolio get their own routes)."""
    _require_seller(seller_id)
    data = request.get_json(force=True) or {}
    return _ops_response(seller_ops.update_seller, seller_id, data,
                         wrap="seller")


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
    """Store a seller's resume. Body: {filename, content_base64}.

    Two things are stored now, and they are different things:

      * `resume_text` — the EXTRACTED text, in the row. It is what the drafting
        model reads, and it is why this route exists.
      * the FILE's bytes — via file_store, so the send path can attach the
        actual document. Extracted text is a summary of a resume; it is not
        the resume, and you cannot attach it to an email.

    The bytes are stored AFTER the text is extracted, and a storage failure does
    not fail the request. The text is the part drafting needs, and losing an
    upload over an unavailable bucket would break the flow that already worked.

    .docx and .pdf are supported.
    """
    _require_seller(seller_id)
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
        raw = base64.b64decode(content_b64, validate=True)
    except Exception:
        return jsonify({"ok": False,
                        "error": "content_base64 is not valid base64"}), 400
    return _persist_resume(seller_id, filename, raw)


def _persist_resume(seller_id, filename, raw):
    """Extract the text, keep the file, update the row. Both routes call this.

    There are two upload entry points — JSON with base64 (the dashboard) and
    multipart (?email=, for n8n's Form node) — and they used to each carry their
    own copy of this. That is why the n8n path would have gone on discarding the
    file bytes after the send feature started keeping them: the same rule
    written twice, and only one copy updated.

    Returns a (jsonify, status) pair like any route.
    """
    try:
        text = _extract_resume_text(
            filename, base64.b64encode(raw).decode("ascii"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        # A file we can't read/decode is the client's bad upload, not a server
        # fault — 400 (so n8n/form knows to reject the file), not 502.
        return jsonify({"ok": False,
                        "error": f"resume could not be parsed: {e}"}), 400

    updates = {"resume_text": text, "resume_filename": filename,
               "updated_at": datetime.now(timezone.utc).isoformat()}

    stored_key = None
    storage_error = None
    try:
        content_type = _resume_content_type(filename)
        stored_key = file_store.put(
            file_store.resume_key(seller_id, filename), raw, content_type)
        if stored_key:
            updates["resume_key"] = stored_key
            updates["resume_content_type"] = content_type
            updates["resume_size_bytes"] = len(raw)
    except Exception as e:
        # Recorded, not raised: the text is saved, so the seller's drafting
        # still works, and the response says plainly that the attachment half
        # did not. Failing the whole upload would break what already worked.
        storage_error = f"{type(e).__name__}: {str(e)[:200]}"

    try:
        updated = supabase_store.update_rows(
            "seller_profile", updates, {"id": seller_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not updated:
        return jsonify({"ok": False, "error": "seller not found"}), 404
    return jsonify({"ok": True, "chars": len(text),
                    "resume_filename": filename,
                    "file_stored": bool(stored_key),
                    "storage_error": storage_error,
                    "seller": updated[0]})


def _resume_content_type(filename):
    """The type to hand the storage layer and, later, the mail client.

    Fixed by extension rather than sniffed: browsers send
    `application/octet-stream` for .docx often enough that trusting the
    client's value would attach the resume as an unopenable blob.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {"pdf": "application/pdf",
            "docx": ("application/vnd.openxmlformats-officedocument"
                     ".wordprocessingml.document")}.get(
                        ext, "application/octet-stream")


@app.route("/seller/resume", methods=["POST"])
def seller_resume_by_email():
    """n8n form upload for the resume step: multipart file + ?email= . Resolves
    the seller by email (so n8n can send the file straight from a Form node with
    no intermediate lookup), then behaves exactly like POST /seller/<id>/resume.
    The lone uploaded file is taken regardless of the multipart field name n8n
    used, since that name is not guaranteed across n8n versions."""
    # Same shape as /seller/by-email: the subject is a query param, so there is
    # no id to compare against a credential and only the platform may call it.
    _require_operator()

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
    return _persist_resume(seller_id, filename, raw)


@app.route("/seller/<seller_id>/portfolio", methods=["POST"])
def seller_portfolio_fetch(seller_id):
    """Scrape a seller's portfolio website into visible text, honoring the
    seller's stored tri-state render_mode (html|js|auto). Body: {url,
    render_mode?} — render_mode only overrides when the caller sends a valid one
    (normally the seller's creation form already fixed render_mode)."""
    import portfolio  # lazy: pulls the hardened fetch stack only when used

    _require_seller(seller_id)
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


# _DRAFT_LEAD_COLUMNS / _decoded moved to leads_read.py (aliased above).


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
    if not supabase_store.configured():
        return jsonify({"ok": False,
                        "error": "Supabase not configured"}), 503

    lead_id = str(data.get("lead_id") or "").strip()
    if not lead_id:
        return jsonify({"ok": False, "error": "lead_id is required"}), 400

    # The lead has to be read here, not inside compose_draft, because its
    # campaign is what the ownership gate below is checked against — and the
    # order of the 404 and the 403 is observable by n8n. A missing lead must
    # stay "lead not found" (404), not become "not owned" (403).
    lead = leads_read.read_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "error": "lead not found"}), 404
    campaign_id = lead.get("campaign_id")
    _niche, campaign_seller_id = leads_read.read_campaign_niche(campaign_id)

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

    # The composition (prospect snapshot, niche, provider, prompt) lives in
    # draft_compose so this route and the dashboard's persisting POST /drafts
    # cannot drift apart. This route's shape is unchanged: same keys, same
    # statuses, nothing persisted.
    try:
        payload = draft_compose.compose_draft(
            lead_id, seller_id,
            temperature=float(data.get("temperature") or 0.7),
            score_model=data.get("score_model"))
    except seller_ops.OpsError as e:
        return jsonify({"ok": False, "error": e.message}),             _OPS_STATUS.get(e.kind, 502)

    return jsonify({"ok": True, **payload})


def _mask_settings(settings):
    """Return a view of a seller's settings for API responses: provider/model
    readable, but every stored secret shown only as a 'set' flag (never the
    token or plaintext)."""
    settings = _decoded(settings)
    llm = settings.get("llm") or {}
    finder = settings.get("finder") or {}
    smtp = settings.get("smtp") or {}
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
        # host/user/from_email are configuration, not secrets: showing them is
        # what lets a seller see which account their mail leaves from, and the
        # From-vs-account mismatch is the failure this block exists to prevent.
        # Only the password is masked.
        "smtp": {
            "host": smtp.get("host"),
            "port": smtp.get("port"),
            "user": smtp.get("user"),
            "from_email": smtp.get("from_email"),
            "from_name": smtp.get("from_name"),
            "password": "********" if smtp.get("password_enc") else None,
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
    # Masked or not, this is a provider-credential surface — it reports which
    # keys exist and (via PATCH) rewrites them. It is the single highest-value
    # target in the API, and it was the most open route in the file.
    _require_seller(seller_id)
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
        "finder": {"provider","login","password"},
        "smtp":   {"host","port","user","from_email","from_name","password"} }
    Secret values (api_key/login/password) are ENCRYPTED before storage; sending
    null/"" for one clears it (falls back to the env master key). Provider names
    are validated against the registry.

    An `smtp` block is how a seller sends through THEIR OWN account instead of
    the platform's. Note that `from_email` is only honoured when `host` and
    `user` are also set — see `config.smtp_cfg`, which explains why and reports
    the ignored value rather than dropping it silently."""
    # The GET reports which keys exist; this one REWRITES them. Unguarded, it
    # was remote credential overwrite: point a seller's llm.base_url at a host
    # you control and every draft they generate is exfiltrated, including the
    # prospect data in the prompt.
    _require_seller(seller_id)
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
    body_smtp = data.get("smtp")
    llm_s = dict(settings.get("llm") or {})
    finder_s = dict(settings.get("finder") or {})
    smtp_s = dict(settings.get("smtp") or {})

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

        if isinstance(body_smtp, dict):
            for k in ("host", "user", "from_email", "from_name"):
                if k in body_smtp:
                    smtp_s[k] = (str(body_smtp.get(k) or "").strip() or None)
            if "port" in body_smtp:
                raw = str(body_smtp.get("port") or "").strip()
                if not raw:
                    smtp_s["port"] = None
                else:
                    # Validated here rather than left to fail at send time. A
                    # stored port of "587 " or "five-eighty-seven" produces an
                    # SMTP connection error that names the socket, not the
                    # setting that is wrong.
                    try:
                        port = int(raw)
                    except (TypeError, ValueError):
                        port = -1
                    if not (1 <= port <= 65535):
                        return jsonify({
                            "ok": False,
                            "error": f"smtp.port must be a port number between "
                                     f"1 and 65535, got {raw!r}"}), 400
                    smtp_s["port"] = port
            if "password" in body_smtp:
                _store_secret(smtp_s, "password_enc", body_smtp.get("password"))
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 503

    settings["llm"] = llm_s
    settings["finder"] = finder_s
    settings["smtp"] = smtp_s
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


# The agent/draft/dashboard surface. Registered here rather than declared above
# so it lives in its own module: /runs and /drafts serve the dashboard, while
# everything above serves the n8n pipeline, whose routes and response shapes are
# frozen. Kept as its own import at the bottom because routes_agent reads the
# shared modules (draft_compose, seller_ops, leads_read) — it does not import
# app, so there is no cycle.
import routes_agent  # noqa: E402
import routes_ui  # noqa: E402

routes_agent.register_agent_routes(app)
routes_ui.register_ui_routes(app)


if __name__ == "__main__":
    # set app to debug mode
    app.run(host="127.0.0.1", port=5000, debug=True)

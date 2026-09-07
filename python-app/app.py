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

import json
import os
import subprocess
import sys
import tempfile
import uuid

from flask import Flask, request, jsonify

# Loaded lazily inside the /score route so app.py starts even if Groq isn't
# configured; groq_ai reads GROQ_API_KEY from the environment / .env.
import groq_ai
import lead_engine  # /campaign orchestrator (find->enrich->score->store)
import supabase_store  # /leads + /leads/status read/write helpers

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
        result = groq_ai.score_lead(
            business, intelligence, niche,
            extra_hints=data.get("extra_hints", ""),
            model=data.get("model"),
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

    scope = str(data.get("scope", "unprocessed")).strip().lower()
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

    updated = 0
    errors = []
    for lid in lead_ids:
        try:
            supabase_store.update_rows("leads", {"status": status},
                                       {"id": lid})
            updated += 1
        except Exception as e:
            errors.append(f"{lid}: {e}")
    return jsonify({"ok": not errors, "updated": updated,
                    "errors": errors})


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)

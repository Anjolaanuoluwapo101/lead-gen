#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Thin Supabase REST client for the STORE stage of the lead pipeline.

Keeps secrets out of n8n/nodes: the Flask engine owns SUPABASE_URL +
SUPABASE_SERVICE_KEY (service-role) and writes rows directly. n8n never sees the
key — it just triggers a /campaign run and gets back the results.

Uses the Supabase PostgREST API with the service-role key (bypasses RLS, so it
works for internal writes). No extra dependency beyond `requests`.

Credentials: SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment or a .env
file next to this script (see .env.example).

Usage
-----
    python supabase_store.py --table campaigns --row '{"name":"..."}'
"""

import argparse
import json
import os
import sys
import threading

import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    Retry = None

# --------------------------------------------------------------------------- #
# .env loader (shared convention)
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

URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()


def configured():
    """True when the caller set Supabase creds (so the pipeline can degrade to
    a local-only run that skips STORE until creds are present)."""
    return bool(URL and SERVICE_KEY)


def _headers():
    return {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _endpoint(table):
    return f"{URL}/rest/v1/{table}"


# One shared Session: every Supabase call in the process reuses connections
# instead of paying a fresh TLS+TCP handshake per row (the STORE phase used
# to do dozens per campaign). Retries are GET-only and deliberate: a
# retried read is harmless, but a retried POST/PATCH could insert or mutate
# twice. Writes fail loudly instead, and the callers decide what that means
# (lead_engine falls back to per-row inserts; the agent records an error).
_session_lock = threading.Lock()
_SESSION = None


def _session():
    global _SESSION
    if _SESSION is None:
        with _session_lock:
            if _SESSION is None:
                s = requests.Session()
                if Retry is not None:
                    retry = Retry(
                        total=3, backoff_factor=1,
                        status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods=frozenset(["GET"]))
                    adapter = HTTPAdapter(pool_connections=20,
                                          pool_maxsize=20, max_retries=retry)
                    s.mount("https://", adapter)
                    s.mount("http://", adapter)
                _SESSION = s
    return _SESSION


def insert_rows(table, rows):
    """
    Insert one or many rows into `table`. `rows` may be a dict (single) or a
    list of dicts. Returns the inserted rows (as returned by PostgREST) so the
    caller can read back server-generated ids (e.g. campaign/prospect uuid).
    Raises on HTTP error with the response body attached to the message.
    """
    if not configured():
        raise RuntimeError(
            "Supabase not configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY "
            "to .env (see .env.example).")
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return []

    resp = _session().post(
        _endpoint(table),
        headers={**_headers(), "Prefer": "return=representation"},
        json=rows,
        timeout=30,
    )
    if resp.status_code >= 300:
        raise RuntimeError(
            f"Supabase insert into {table} failed "
            f"({resp.status_code}): {resp.text[:500]}")
    try:
        return resp.json()
    except ValueError:
        return []


def _raise(table, method, resp):
    raise RuntimeError(
        f"Supabase {method} on {table} failed "
        f"({resp.status_code}): {resp.text[:500]}")


def _eq_filters(filters):
    """Turn {col: value} into PostgREST equality query params."""
    return {col: f"eq.{val}" for col, val in (filters or {}).items()}


def _gte_filters(filters):
    """Turn {col: value} into PostgREST greater-or-equal range params, e.g. a
    high-water cursor on created_at (created_at >= since)."""
    return {col: f"gte.{val}" for col, val in (filters or {}).items()}


def _in_filters(filters):
    """Turn {col: [values]} into PostgREST `in` params (col=in.(a,b,c)). Used to
    scope reads to a set of ids (e.g. a seller's campaign/lead ownership check)."""
    return {col: f"in.({','.join(str(v) for v in values)})"
            for col, values in (filters or {}).items() if values}


def select_rows(table, columns="*", filters=None, filters_gte=None,
                filters_in=None, limit=None, order=None):
    """SELECT rows via PostgREST. Returns a list of dicts.

    columns: PostgREST `select` projection (e.g. "id,business_name" or a nested
             embed like "id,campaigns(name)"). filters: {col: value} equality
             matches (eq). filters_gte: {col: value} greater-or-equal matches
             (gte) -- e.g. {"created_at": since} for incremental pulls.
             filters_in: {col: [values]} `in` matches (col=in.(a,b,c)).
             limit: optional int cap. order: optional PostgREST order spec, e.g.
             "created_at.asc" for stable cursor advancement.
    """
    if not configured():
        raise RuntimeError(
            "Supabase not configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY "
            "to .env (see .env.example).")
    params = {"select": columns}
    params.update(_eq_filters(filters))
    params.update(_gte_filters(filters_gte))
    params.update(_in_filters(filters_in))
    if order:
        params["order"] = order
    if limit:
        params["limit"] = int(limit)
    resp = _session().get(_endpoint(table), headers=_headers(), params=params,
                        timeout=30)
    if resp.status_code >= 300:
        _raise(table, "select", resp)
    try:
        return resp.json()
    except ValueError:
        return []


def update_rows(table, updates, filters=None, filters_in=None):
    """PATCH rows matching `filters` (eq) and `filters_in` (in). Returns the
    updated rows. `filters_in` exists for grouped writes: one PATCH per status
    value over a set of ids, instead of one PATCH per row."""
    if not configured():
        raise RuntimeError(
            "Supabase not configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY "
            "to .env (see .env.example).")
    params = _eq_filters(filters)
    params.update(_in_filters(filters_in))
    resp = _session().patch(
        _endpoint(table),
        headers={**_headers(), "Prefer": "return=representation"},
        params=params,
        json=updates, timeout=30,
    )
    if resp.status_code >= 300:
        _raise(table, "update", resp)
    try:
        return resp.json()
    except ValueError:
        return []


def upsert_rows(table, rows, on_conflict=None):
    """Insert rows, or overwrite them when they collide on `on_conflict` (a
    unique column). Returns the resulting rows, so it doubles as find-or-create
    (the returned row carries the server id whether it was inserted or merged).
    """
    if not configured():
        raise RuntimeError(
            "Supabase not configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY "
            "to .env (see .env.example).")
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return []
    params = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    resp = _session().post(
        _endpoint(table),
        headers={**_headers(), "Prefer":
                 f"resolution=merge-duplicates,return=representation"},
        params=params or None, json=rows, timeout=30,
    )
    if resp.status_code >= 300:
        _raise(table, "upsert", resp)
    try:
        return resp.json()
    except ValueError:
        return []


def rpc(name, payload=None):
    """Call a Postgres function exposed by PostgREST (POST /rpc/<name>). Lets the
    engine run server-side aggregation (e.g. campaign_summary) instead of pulling
    rows to tally in Python. `payload` is the named-argument JSON body."""
    if not configured():
        raise RuntimeError(
            "Supabase not configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY "
            "to .env (see .env.example).")
    resp = _session().post(
        f"{URL}/rest/v1/rpc/{name}",
        headers=_headers(),
        json=payload or {},
        timeout=30,
    )
    if resp.status_code >= 300:
        _raise(name, "rpc", resp)
    try:
        return resp.json()
    except ValueError:
        return []


def main():
    ap = argparse.ArgumentParser(description="Supabase REST write helper.")
    ap.add_argument("--table", required=True)
    ap.add_argument("--row", required=True,
                    help="JSON object or array of objects to insert")
    args = ap.parse_args()

    try:
        rows = json.loads(args.row)
    except json.JSONDecodeError as e:
        print(f"ERROR: could not parse --row JSON: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        created = insert_rows(args.table, rows)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(created, indent=2, default=str))


if __name__ == "__main__":
    main()

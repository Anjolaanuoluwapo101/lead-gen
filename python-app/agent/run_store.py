"""Persistence for agent runs, their event trace, and the drafts awaiting review.

Three tables, defined in supabase-schema-agent.sql: runs / run_events / drafts.

This module is the ONLY writer of those tables. It imports nothing from strands
or bedrock_agentcore, so the Flask dashboard can read runs back without pulling
the agent's dependency stack.

Deliberately dependency-light on supabase_store (insert_rows / select_rows /
update_rows cover everything) — no bespoke HTTP here.
"""

from datetime import datetime, timezone

import supabase_store

RUN_TABLE = "runs"
EVENT_TABLE = "run_events"
DRAFT_TABLE = "drafts"

# Statuses that hold the single "live draft" slot per lead (see the partial
# unique index idx_drafts_lead_live). Sending or rejecting frees it.
LIVE_DRAFT_STATUSES = ("draft", "approved", "sending")


def _now():
    return datetime.now(timezone.utc).isoformat()


# --- runs ----------------------------------------------------------------- #

def create_run(*, seller_id, campaign_id=None, trigger="dashboard", goal=None,
               bounds=None):
    """Open a run. Returns the created row (carries the server-generated id).

    `bounds` is stored verbatim so a finished run can be judged against the
    caps it actually started with, not whatever the defaults are by then.
    """
    row = {
        "seller_id": seller_id,
        "campaign_id": campaign_id,
        "trigger": trigger,
        "status": "running",
        "goal": goal,
        "bounds": bounds or {},
        "progress": {},
        "estimated_spend_usd": 0,
    }
    rows = supabase_store.insert_rows(RUN_TABLE, row)
    return rows[0] if rows else {}


def get_run(run_id):
    """Fetch one run, or None when it does not exist."""
    rows = supabase_store.select_rows(
        RUN_TABLE, filters={"id": run_id}, limit=1)
    return rows[0] if rows else None


def list_runs(seller_id, limit=20):
    """Most recent runs for a seller (dashboard list view).

    Refuses a falsy seller_id rather than passing it through: PostgREST renders
    None as `seller_id=eq.None`, which fails as an invalid uuid 400 and tells
    you nothing. In practice this fires when DEFAULT_SELLER_ID is unset.
    """
    if not seller_id:
        raise ValueError(
            "list_runs requires a seller_id (got none) — is DEFAULT_SELLER_ID "
            "set in .env? A dashboard with no seller would show every tenant.")
    return supabase_store.select_rows(
        RUN_TABLE, filters={"seller_id": seller_id},
        order="created_at.desc", limit=limit)


def request_cancel(run_id):
    """Ask a run to stop, in a way that reaches it ACROSS processes.

    The dashboard's cancel arrives at Flask, but with AGENT_BACKEND=agentcore
    the loop is running in a container in us-west-2 — a threading.Event in this
    process cannot reach it. So the request is a column, and the orchestrator
    reads it between turns.

    A boolean column rather than a status value, because `runs.status` is
    constrained to ('running','succeeded','failed','stopped') by a CHECK
    constraint: there is no 'cancelling' state to move to. The flag is the
    REQUEST; `status` stays the OUTCOME, so a cancelled run still ends as
    'stopped' with terminal_reason 'user_cancelled'.
    """
    rows = supabase_store.update_rows(RUN_TABLE, {"cancel_requested": True},
                                      {"id": run_id})
    return rows[0] if rows else {}


def is_cancel_requested(run_id):
    """True when someone asked this run to stop.

    Never raises. It is called once per turn by a run that is otherwise
    healthy, and a transient Supabase blip must not convert "keep working"
    into "crash". A read that keeps failing disables cancel until it recovers,
    which is the safer of the two failure directions.
    """
    try:
        run = get_run(run_id)
    except Exception:
        return False
    return bool(run and run.get("cancel_requested"))


def update_progress(run_id, progress, estimated_spend_usd=None):
    """Persist the live counters mid-run, so the dashboard can poll progress.

    Called often; kept to a single PATCH.
    """
    updates = {"progress": progress or {}}
    if estimated_spend_usd is not None:
        updates["estimated_spend_usd"] = float(estimated_spend_usd)
    rows = supabase_store.update_rows(RUN_TABLE, updates, {"id": run_id})
    return rows[0] if rows else {}


def finish_run(run_id, *, status, terminal_reason, report=None, error=None,
               progress=None, estimated_spend_usd=None):
    """Close a run and record WHY it stopped.

    `terminal_reason` is the point of this table — a run that ends without one
    cannot answer "why did it stop?", which is the question the whole bounds
    design exists to make answerable. Required, not optional.

    status: running | succeeded | failed | stopped
    terminal_reason: target_met | budget_exhausted | max_attempts | timeout |
                     no_results | error | user_cancelled
                     (free text — see the schema comment; kept in sync with
                     agent/budget.py's TerminalReason constants)
    """
    if not terminal_reason:
        raise ValueError("finish_run requires a terminal_reason")
    updates = {
        "status": status,
        "terminal_reason": terminal_reason,
        "report": report,
        "error": error,
        "finished_at": _now(),
    }
    if progress is not None:
        updates["progress"] = progress or {}
    if estimated_spend_usd is not None:
        updates["estimated_spend_usd"] = float(estimated_spend_usd)
    rows = supabase_store.update_rows(RUN_TABLE, updates, {"id": run_id})
    return rows[0] if rows else {}


# --- run_events ----------------------------------------------------------- #

def append_event(run_id, kind, message=None, payload=None):
    """Append one trace event. seq is assigned here, 1-based, per run.

    Preferred path is the append_run_event RPC (one round trip: the database
    assigns max(seq)+1 inside the INSERT itself). It needs
    supabase-schema-agent.sql applied; when the function is missing — or any
    blip interrupts the call — the legacy read-max-then-insert below runs
    instead, so an unmigrated database keeps working with zero code change.

    A run has exactly one writer by design, so both paths are safe in the
    normal case. If two writers ever did interleave, the (run_id, seq) unique
    constraint makes it a loud error rather than a silently reordered trace.
    """
    try:
        rows = supabase_store.rpc("append_run_event", {
            "p_run_id": run_id,
            "p_kind": kind,
            "p_message": message,
            "p_payload": payload,
        })
    except Exception:
        rows = None
    if rows is not None:
        if isinstance(rows, dict):
            rows = [rows]
        return dict(rows[0]) if rows else {}
    row = {
        "run_id": run_id,
        "seq": _next_seq(run_id),
        "kind": kind,
        "message": message,
        "payload": payload,
    }
    rows = supabase_store.insert_rows(EVENT_TABLE, row)
    return rows[0] if rows else {}


def list_run_events(run_id, limit=500, after=0):
    """Ordered trace for a run, newest-first by construction of the caller.

    `after` is the dashboard's poll cursor: only rows with seq greater than
    it are returned, filtered BY the database. Without this every 5s poll
    re-downloaded the whole trace (up to `limit` full rows) and sliced in
    Python, so a poll's payload grew with the run instead of staying O(new).
    `after=0` keeps the full-replay behaviour for anything that wants it.
    """
    try:
        cursor = int(after or 0)
    except (TypeError, ValueError):
        cursor = 0
    extra = {"filters_gte": {"seq": cursor + 1}} if cursor else {}
    return supabase_store.select_rows(
        EVENT_TABLE, filters={"run_id": run_id},
        order="seq.asc", limit=limit, **extra)


def _next_seq(run_id):
    rows = supabase_store.select_rows(
        EVENT_TABLE, columns="seq", filters={"run_id": run_id},
        order="seq.desc", limit=1)
    if not rows:
        return 1
    try:
        return int(rows[0]["seq"]) + 1
    except (KeyError, TypeError, ValueError):
        return 1


# --- drafts --------------------------------------------------------------- #

def create_draft(*, lead_id, subject, email_body, campaign_id=None,
                 seller_id=None, angle=None, to_email=None, run_id=None):
    """Persist a draft awaiting human approval. The agent never sends.

    Raises on the idx_drafts_lead_live unique index when a live draft already
    exists for this lead — that is the database enforcing "one live draft per
    lead", not a bug. Callers should surface it, not retry blindly.
    """
    row = {
        "lead_id": lead_id,
        "campaign_id": campaign_id,
        "seller_id": seller_id,
        "run_id": run_id,
        "subject": subject,
        "email_body": email_body,
        "angle": angle,
        "to_email": to_email,
        "status": "draft",
        "revision": 1,
    }
    rows = supabase_store.insert_rows(DRAFT_TABLE, row)
    return rows[0] if rows else {}


def list_drafts(seller_id=None, campaign_id=None, status=None, limit=50,
                run_id=None):
    """Drafts for a seller/campaign/run, newest first. status may be a single
    value or a list (e.g. the live statuses awaiting review).

    `run_id` is what the run page filters on. It is NOT the same question as
    campaign_id: a run drafts against leads it found, so its drafts span the
    campaigns it created, and the dashboard's human-triggered draft route
    attaches no run at all. Filtering the run page by campaign showed the wrong
    set in both directions.
    """
    filters = {}
    if seller_id:
        filters["seller_id"] = seller_id
    if campaign_id:
        filters["campaign_id"] = campaign_id
    if run_id:
        filters["run_id"] = run_id
    if isinstance(status, (list, tuple)):
        rows = supabase_store.select_rows(
            DRAFT_TABLE, filters=filters, filters_in={"status": list(status)},
            order="created_at.desc", limit=limit)
    else:
        if status:
            filters["status"] = status
        rows = supabase_store.select_rows(
            DRAFT_TABLE, filters=filters,
            order="created_at.desc", limit=limit)
    return rows


def get_draft(draft_id):
    rows = supabase_store.select_rows(
        DRAFT_TABLE, filters={"id": draft_id}, limit=1)
    return rows[0] if rows else None


def update_draft(draft_id, *, bump_revision=False, **fields):
    """PATCH a draft. bump_revision increments `revision` so an edited draft is
    distinguishable from the model's original output.

    Kept generic (any column) because approval, rejection, edit and send-result
    all land here; run_store does not police the status transitions — the
    partial unique indexes are the real guard.

    NOTE: keys whose value is None are dropped, so a field cannot be cleared
    through this function. Intentional — every None here would otherwise mean
    "wipe this column", which is never what a status update wants.
    """
    updates = {k: v for k, v in fields.items() if v is not None}
    if bump_revision:
        current = get_draft(draft_id)
        try:
            updates["revision"] = int(current.get("revision") or 1) + 1
        except (AttributeError, TypeError, ValueError):
            updates["revision"] = 1
    updates["updated_at"] = _now()
    rows = supabase_store.update_rows(DRAFT_TABLE, updates, {"id": draft_id})
    return rows[0] if rows else {}

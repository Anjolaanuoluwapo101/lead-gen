"""AgentCore Runtime entrypoint — the deployed artifact.

`agentcore deploy` ships this directory and runs `python agentcore_app.py`,
which must serve two endpoints on 0.0.0.0:8080:

    GET  /ping         -> {"status": "Healthy"} ("HealthyBusy" mid-invocation)
    POST /invocations  -> the run report

A DELIBERATE DEVIATION FROM THE SPEC
The design spec said "Flask on 8080". This uses BedrockAgentCoreApp instead.
The AgentCore contract is a SERVER contract, so Flask could satisfy it — but
BedrockAgentCoreApp already gets right the parts that are easy to get subtly
wrong, notably reporting HealthyBusy while a session is mid-invocation, and it
is the implementation verified end-to-end on Day 0. Reimplementing it would buy
nothing and risk the one thing that has actually been proven to deploy.
It is a Starlette app underneath; that is why the contract test uses a Starlette
test client rather than Flask's.

WHY /invocations BLOCKS
AgentCore may reap a session it believes is idle, so this handler runs the whole
orchestrated run synchronously and returns only when it has stopped. The
dashboard's start-a-job-and-poll pattern is a different surface and does not
apply here. This is also why /ping must be able to report HealthyBusy: a long
invocation is normal, not a hang.

This module is thin on purpose. All the decisions — when a run stops, what it
records — live in agent/orchestrator.py, which is testable without a model.
"""

import json
import os
import re

from bedrock_agentcore.runtime import BedrockAgentCoreApp

import runtime_secrets

# THIS ORDER IS LOAD-BEARING — do not move these imports up.
# On the deployed runtime the zip contains no .env (the packager copies
# codeLocation wholesale, so credentials live in SSM instead). lead_engine and
# the four modules under it read os.environ AT IMPORT TIME, so hydrating first is
# what makes them see real credentials. Import them first and the runtime boots
# with empty strings and fails somewhere deep inside a run.
#
# Locally LEADGEN_SECRETS_PATH is unset, hydrate() is a no-op, and nothing
# changes: the repo-root .env still loads exactly as before.
_BOOT = runtime_secrets.hydrate()

import lead_engine  # noqa: E402,F401  — side effect: loads .env (local dev)
from agent import run_store  # noqa: E402
from agent.budget import DEFAULT_BOUNDS  # noqa: E402
from agent.orchestrator import Orchestrator  # noqa: E402

app = BedrockAgentCoreApp()
log = app.logger

# Names only — never the values. This is the one line that tells you whether a
# deployed run has credentials before it has spent any DataForSEO credit.
log.info("secrets: %s", _BOOT.get("skipped") or f"{_BOOT['loaded']} from SSM")

MAX_GOAL_CHARS = 500
MAX_ID_CHARS = 64

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _is_uuid(value):
    """Checked here as well as in the routes: the payload arrives from the
    network, and `runs.id` is a uuid column that rejects malformed input with
    Postgres 22P02 rather than a useful message."""
    return bool(_UUID_RE.match(str(value or "").strip()))


def _text(value, limit=200):
    """Coerce to a trimmed string. Payloads arrive from the network, so every
    field is treated as hostile rather than assumed to be the right type."""
    return str(value or "").strip()[:limit]


def _normalize(payload):
    """Accept the AgentCore contract AND the `agentcore invoke` envelope.

    Two callers send two different shapes, and this cost a debugging cycle:

      - The runtime CONTRACT is arbitrary JSON: our dashboard and curl POST
        {"goal": "..."} straight through.
      - The `agentcore CLI` wraps it as
        {"prompt": "<text>", "agent": ..., "runtimeArn": ..., "region": ...}
        where prompt is a STRING, not an object.

    Without this, every CLI invocation is rejected as goal_required no matter
    what you pass, which reads as a broken agent rather than a payload mismatch.
    The CLI is the documented way to smoke-test a deployed runtime, so it has to
    work.

    A bare prompt string is treated as the goal, so `agentcore invoke "find
    plumbers in Austin"` behaves the way it reads.
    """
    if not isinstance(payload, dict):
        return {}
    if "goal" in payload:
        return payload

    prompt = _text(payload.get("prompt"), MAX_GOAL_CHARS + 1)
    if not prompt:
        return payload

    if prompt.startswith("{"):
        try:
            parsed = json.loads(prompt)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed

    merged = dict(payload)
    merged["goal"] = prompt
    return merged


def _resolve_seller_id(payload):
    """Explicit seller wins, else the single-tenant default.

    Mirrors the read gate in app.py. When Supabase Auth lands, the session's
    seller id is passed here and the DEFAULT fallback is removed — the runtime
    has no session identity of its own, so it must be told.
    """
    return (_text(payload.get("seller_id"), MAX_ID_CHARS)
            or _text(os.getenv("DEFAULT_SELLER_ID"), MAX_ID_CHARS) or None)


@app.entrypoint
def invoke(payload, context=None):
    """Run one bounded lead-gen run.

    Payload: {"goal": "...", "seller_id"?, "campaign_id"?, "bounds"?, "trigger"?}

    Never raises for a run failure. An unrecorded crash is worse than a recorded
    one, so failures come back as a report with terminal_reason "error".
    """
    payload = _normalize(payload)

    goal = _text(payload.get("goal"), MAX_GOAL_CHARS)
    if not goal:
        return {"ok": False, "error": "goal_required",
                "message": "Pass {\"goal\": \"find 5 qualified dentists in Austin\"}."}

    seller_id = _resolve_seller_id(payload)
    if not seller_id:
        return {"ok": False, "error": "no_seller",
                "message": "No seller_id in the payload and DEFAULT_SELLER_ID is "
                           "not set in the runtime environment."}

    bounds = payload.get("bounds")
    if not isinstance(bounds, dict) or not bounds:
        bounds = None              # orchestrator applies DEFAULT_BOUNDS
    else:
        unknown = sorted(set(bounds) - set(DEFAULT_BOUNDS))
        if unknown:
            # A misspelled bound would silently fall back to the default, which
            # reads as "the cap I set was ignored". Refuse instead of guessing.
            return {"ok": False, "error": "unknown_bounds",
                    "message": f"Unknown bound(s): {unknown}",
                    "known_bounds": sorted(DEFAULT_BOUNDS)}

    # The caller may have created the run row already, so that its dashboard
    # could answer with an id before any work started. Adopting that id is what
    # keeps live progress working here: the runtime writes events and counters
    # into the row the browser is already polling, instead of a private one
    # nobody learns about until this response finally lands.
    run_id = _text(payload.get("run_id"), MAX_ID_CHARS) or None
    if run_id and not _is_uuid(run_id):
        return {"ok": False, "error": "bad_run_id",
                "message": "run_id must be a uuid."}
    if run_id and not run_store.get_run(run_id):
        # Accepting an unknown id would write events against a row that does
        # not exist -- orphaned trace, and a foreign-key error mid-run.
        return {"ok": False, "error": "run_not_found",
                "message": f"No run row with id {run_id}."}

    session_id = getattr(context, "session_id", None)
    log.info("invocation start session=%s seller=%s run=%s",
             session_id, seller_id, run_id)

    def _bool(value):
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        return None

    # Unknown tiers fall back to the engine default rather than refusing the
    # run: a misspelled strictness must not cost a whole run when the engine
    # already defines the fallback. (The dashboard 400s junk first, so this
    # path only serves hand-written payloads.)
    scrutiny = _text(payload.get("scrutiny"), 20).lower() or None
    if scrutiny not in ("strict", "balanced", "lenient", None):
        scrutiny = None
    try:
        location_code = payload.get("location_code")
        try:
            location_code = int(location_code) \
                if location_code not in ("", None) else None
        except (TypeError, ValueError):
            location_code = None
        if location_code is not None and location_code <= 0:
            location_code = None
        orchestrator = Orchestrator(
            seller_id=seller_id, goal=goal, bounds=bounds,
            campaign_id=_text(payload.get("campaign_id"), MAX_ID_CHARS) or None,
            trigger="agentcore", run_id=run_id,
            location_code=location_code,
            location_name=_text(payload.get("location_name"), 200) or None,
            scrutiny=scrutiny,
            niche=_text(payload.get("niche"), 200) or None,
            single_search=_bool(payload.get("single_search")))
        report = orchestrator.run()
    except Exception as exc:
        # Orchestrator.run() already converts run failures into reports, so
        # reaching here means the run could not even be CREATED — bad Supabase
        # credentials, or supabase-schema-agent.sql not applied.
        log.exception("invocation failed before the run started")
        return {"ok": False, "error": "run_not_started",
                "message": f"{type(exc).__name__}: {exc}"[:300]}

    report["session_id"] = session_id
    log.info("invocation done session=%s run=%s reason=%s",
             session_id, report.get("run_id"), report.get("terminal_reason"))
    return report


if __name__ == "__main__":
    # 0.0.0.0:8080 is mandated by the AgentCore runtime contract.
    app.run(host="0.0.0.0", port=8080)

"""HTTP surface for agent runs and outreach drafts.

WHY THIS IS SEPARATE FROM app.py

app.py serves the pipeline n8n drives: find, enrich, score, store, draft. Those
routes are a published contract — five workflows hardcode them and their
response shapes are frozen. This module serves a different client: the
dashboard, which needs to start a run, watch it, and act on what it produced.

Splitting them is what lets `/drafts` coexist with `/draft`. The spec is
explicit that `/draft` (no 's') must stay byte-identical for n8n, and it does
not persist. `/drafts` persists and is a different noun.

WHY /runs IS ASYNC HERE AND SYNCHRONOUS ON AGENTCORE

AgentCore's contract is request/response: `POST /invocations` runs the whole
thing and returns the report. That is correct for a runtime and wrong for a
dashboard — a six-minute run behind an HTTP request means a spinner and a proxy
timeout. So this route returns immediately with a run_id and does the work on a
daemon thread, which is exactly what makes the 5-second progress polling in
`routes_ui.py` possible.

The thread is daemon so a run never blocks process exit, and nothing needs to
join it: the run's own state is in the database, so a restart mid-run leaves a
`running` row rather than losing work silently.

DRAFT STATE MACHINE

    draft ──approve──> approved ──export──> (CSV)
      └───reject───> rejected

Three gates guard the export, in order, and each answers a different question:

1. **ownership (403)** — is this draft yours at all?
2. **status is `approved` (409)** — did a human actually approve it?
3. **compare-and-swap `approved -> sending` (409)** — did THIS request win the
   race? A read-then-write would let two concurrent exports both pass.

A fourth gate — **verified recipient (400)** — belongs to SENDING, not
exporting, and lives in `recipient_blocked`. It used to run here too, and
because SES is not wired it refused every request: the export 400'd
unconditionally and `_csv_response` was unreachable code. Exporting and sending
are different questions, and a draft with no address is precisely the one you
want in a CSV.

On success the status returns to `approved`, because that is TRUE: the draft is
approved and has not been sent. Nothing here claims otherwise, and `sent` is
never written by this module.
"""

import csv
import io
import json
import os
import re
import threading
from datetime import datetime, timezone

from flask import jsonify, render_template, request

import auth
import config
import dataforseo
import draft_compose
import draft_send
import leads_read
import seller_ops
import send_provider
import supabase_store
from agent import run_store
from agent.budget import DEFAULT_BOUNDS, TerminalReason
from agent.orchestrator import Orchestrator

# run_id -> threading.Event, the FAST path for POST /runs/<id>/cancel when the
# orchestrator is a thread in this process. It is not the only path: the same
# route also sets runs.cancel_requested, which is what reaches an AgentCore run
# in another process. This dict is deliberately in-process only — a run started
# before a restart has no thread on either backend, so there is nothing to
# signal and nothing to miss.
_cancels = {}
_cancels_lock = threading.Lock()

# How many runs may be in flight from THIS process at once. Every POST /runs
# takes a slot and the background body returns it in `finally`, on BOTH
# backends: a local orchestrator fans out to 5 enrich threads plus inner
# crawls, and an AgentCore dispatch parks a gunicorn worker thread for up to
# 960s. Unbounded, 20 simultaneous Starts is 100+ threads in one gunicorn
# worker (8 threads) — polls and health checks starve and the ALB calls us
# dead. A rejection is a 429 with a retryable message, never an orphan row:
# the slot is taken BEFORE the run row is created.
_RUN_SLOTS = threading.Semaphore(int(os.environ.get("MAX_CONCURRENT_RUNS")
                                     or 4))

LIVE_DRAFT_STATUSES = ("draft", "approved", "sending")
SETTABLE_STATUSES = ("approved", "rejected")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ok(**fields):
    return jsonify({"ok": True, **fields}), 200


def _err(message, status):
    return jsonify({"ok": False, "error": message}), status


def _body():
    return request.get_json(force=True, silent=True) or {}


# `runs.id` and `drafts.id` are uuid columns. Postgres rejects a malformed uuid
# with a 22P02 error, which would surface as a 500 — for a URL anyone can type
# by hand. Rejecting it up front keeps a bad id a plain 404 and, just as
# importantly, keeps it away from the database entirely.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _is_uuid(value):
    return bool(_UUID_RE.match(str(value or "").strip()))


def _niche_on_file(seller_id, campaign_id):
    """True when a niche is already known without the caller sending one.

    Checks the seller profile first, then the continued campaign's stored
    niche. Any lookup failure (unknown seller, unknown campaign, database
    blip) counts as "not on file" for the not_found cases and fails OPEN for
    upstream blips: a run the engine can still judge must not 400 because a
    read stumbled, and the engine's dev-default warning covers that case
    loudly on the run page instead.
    """
    try:
        row = seller_ops.get_seller(seller_id) or {}
    except seller_ops.OpsError as exc:
        if exc.kind == "not_found":
            row = {}
        else:
            # Upstream blip, not an answer: let the run start rather than
            # 400 on a read stumble. The engine's dev-default warning covers
            # a genuinely missing niche loudly on the run page.
            return True
    except Exception:
        return True
    if str(row.get("niche") or "").strip():
        return True
    if campaign_id:
        try:
            niche, _ = leads_read.read_campaign_niche(campaign_id)
            if str(niche or "").strip():
                return True
        except Exception:
            pass
    return False


def _parse_bool(value):
    """A lenient boolean reader for run options. Returns True, False, or None
    when the value is not recognisably boolean — None so the caller can 400
    rather than guessing what the seller meant."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def _wants_html():
    """True only when the client asks for HTML in preference to JSON.

    `*/*` — what curl sends — resolves to JSON, so the API keeps working for a
    script while a browser gets the page. The list order is what makes that
    work: best_match returns the first candidate when the offer is open.
    """
    return request.accept_mimetypes.best_match(
        ["application/json", "text/html"]) == "text/html"


def _seller(data):
    """(seller_id, error_response). Mirrors app.py's read-scoping exactly."""
    try:
        sid = auth.resolve_seller(data, request.headers)
    except auth.AuthError as exc:
        return None, _err(exc.message, exc.status)
    if not sid:
        return None, _err("no seller could be resolved for this request", 400)
    return sid, None


def _owns_draft(draft, seller_id):
    """A draft with no seller is unowned, not public.

    `drafts.seller_id` is nullable and nullable FKs cascade to NULL when a
    seller is deleted. Treating that as "anyone may act on it" would make
    deleting a seller a way to hand their drafts to the next caller.
    """
    return bool(draft) and draft.get("seller_id") is not None \
        and str(draft["seller_id"]) == str(seller_id)


def _owned_draft(draft_id, seller_id):
    """(draft, error_response) — 404, not 403.

    A draft belonging to someone else must be indistinguishable from one that
    does not exist, or the 403 becomes an oracle for which ids are real.
    """
    if not _is_uuid(draft_id):
        # A malformed uuid is a 22P02 from Postgres, i.e. a 500 for what is
        # really a 404. Checked before the query so the database never sees it.
        return None, _err("draft not found", 404)
    draft = run_store.get_draft(draft_id)
    if not _owns_draft(draft, seller_id):
        return None, _err("draft not found", 404)
    return draft, None


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def _run_in_background(run_id, seller_id, goal, bounds, campaign_id,
                       location_code=None, location_name=None,
                       scrutiny=None, niche=None, single_search=None):
    """The daemon-thread body. Never raises: a crash here would be invisible.

    Which executor owns the run depends on AGENT_BACKEND:

      local      an Orchestrator in THIS process (the default)
      agentcore  POST /invocations on the deployed runtime in us-west-2

    A switch rather than a replacement, because both write progress to the
    SAME run row -- so the dashboard, the event log and the stop reason work
    identically either way, and the demo can flip between them live.

    location_code/name is the seller's pinned search area from the dashboard
    picker. scrutiny/niche/single_search are the run options chosen beside it.
    All travel in memory to the orchestrator (or inside the AgentCore payload)
    and need no database column: the place is recorded on the run trace as a
    `place` event, so the row stays queryable without a migration.
    """
    try:
        backend = config.agent_backend()
        if backend == "agentcore":
            _run_on_agentcore(run_id, seller_id, goal, bounds, campaign_id,
                              location_code=location_code,
                              location_name=location_name,
                              scrutiny=scrutiny, niche=niche,
                              single_search=single_search)
        else:
            # run_id is handed in because the route already created the row (so
            # it could answer with an id before any work started). Without it
            # the orchestrator would create a SECOND row, orphaning this one as
            # a `running` run that never finishes.
            orch = Orchestrator(seller_id=seller_id, goal=goal, bounds=bounds,
                                trigger="dashboard", campaign_id=campaign_id,
                                cancel_event=_cancel_event(run_id),
                                run_id=run_id,
                                location_code=location_code,
                                location_name=location_name,
                                scrutiny=scrutiny, niche=niche,
                                single_search=single_search)
            orch.run()
    except Exception as exc:
        try:
            run_store.finish_run(run_id, status="failed",
                                 terminal_reason=TerminalReason.ERROR,
                                 error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass        # nothing left to report TO; do not mask the original
    finally:
        with _cancels_lock:
            _cancels.pop(run_id, None)
        _RUN_SLOTS.release()


# The dispatch client's timeouts and retries. These are load bearing.
#
# A bare boto3.client() gets two defaults that are both wrong for this call, and
# together they duplicated real runs:
#
#   read_timeout = 60, and the response body is STREAMED
#       So this is the socket read that waits for the run to finish. A run takes
#       minutes. The read gave up at 60 seconds, every time.
#
#   retries = legacy mode, which is 5 attempts
#       botocore treats that read timeout as retryable -- and InvokeAgentRuntime
#       STARTS A RUN. It is not idempotent. Each retry re-delivered the payload,
#       the runtime began ANOTHER orchestrator, and one dashboard run became
#       five concurrent runs sharing one run_id.
#
# Measured on run 35b2f879 against the live runtime: one `dispatch` event, then
# invocations arriving at +0s, +53s, +61s, +63s and +122s. Five `start` events,
# five `stop` events, five campaigns in campaign_ids, and roughly five times the
# DataForSEO spend. The last writer to finish decided the run's final status, so
# the recorded reason was whichever one won a race.
#
# So: retries OFF (a genuine dispatch failure must surface as ONE recorded
# error, never as a silent duplicate) and the read timeout raised past both the
# orchestrator's own cap and AgentCore's 900 second ceiling, so a stalled
# invocation reports the platform's error rather than our own timeout.
AGENTCORE_READ_TIMEOUT_S = 960
AGENTCORE_CONNECT_TIMEOUT_S = 10


def _agentcore_client_config():
    from botocore.config import Config
    return Config(
        read_timeout=AGENTCORE_READ_TIMEOUT_S,
        connect_timeout=AGENTCORE_CONNECT_TIMEOUT_S,
        retries={"max_attempts": 1, "mode": "standard"},
    )


def _run_on_agentcore(run_id, seller_id, goal, bounds, campaign_id,
                      location_code=None, location_name=None,
                      scrutiny=None, niche=None, single_search=None):
    """Hand the run to the deployed runtime and wait for its report.

    This BLOCKS for the whole run — the runtime's contract is request/response,
    so nothing comes back until the run has stopped. That is acceptable here
    and only here: we are on a daemon thread, and the browser never touches it.
    The browser polls /runs/<id>, which reads the row the runtime is writing to
    as it goes.

    run_id travels in the payload so the runtime ADOPTS the row this process
    already created rather than opening a second one. Without it the dashboard
    would have nothing to poll until this call returned — which is the entire
    run, and would be the whole progress bar.

    Raises on transport failure or refusal; the caller converts that into a
    recorded `error` run. A run that stops normally is already recorded by the
    runtime, which owns the row for the duration.
    """
    import boto3        # lazy: the local backend needs no AWS client configured

    arn = config.agentcore_runtime_arn()
    if not arn:
        raise RuntimeError(
            "AGENT_BACKEND=agentcore requires AGENTCORE_RUNTIME_ARN; it is "
            "unset, so there is nowhere to send the run")

    payload = {"goal": goal, "seller_id": seller_id, "run_id": run_id,
               "bounds": bounds, "trigger": "agentcore"}
    if campaign_id:
        payload["campaign_id"] = campaign_id
    if location_code:
        payload["location_code"] = location_code
    if location_name:
        payload["location_name"] = location_name
    if scrutiny:
        payload["scrutiny"] = scrutiny
    if niche:
        payload["niche"] = niche
    if single_search is not None:
        payload["single_search"] = bool(single_search)

    region = config.agentcore_region()
    # Written BEFORE the call so the cold-start gap is a labelled step in the
    # trace rather than a spinner with nothing behind it. This is safe despite
    # run_events having a single-writer assumption: this append completes
    # before the request is sent, so the runtime's first event still orders
    # after it rather than racing it.
    run_store.append_event(run_id, "dispatch",
                           f"sent to the deployed runtime ({region})")

    client = boto3.client("bedrock-agentcore", region_name=region,
                          config=_agentcore_client_config())
    # One runtime session per run, named for it. The runtime itself is
    # stateless per invocation (a fresh Orchestrator each time), so this buys
    # traceability first (AgentCore logs/traces group by session) and session
    # affinity second: concurrent runs carry different sessions and never
    # serialize behind each other. The "run-" prefix keeps it human in the
    # console; the 40-char total clears the API's 33-char minimum.
    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=f"run-{run_id}",
        contentType="application/json",
        payload=json.dumps(payload).encode("utf-8"))

    status = response.get("statusCode")
    body = response["response"].read()
    try:
        report = json.loads(body)
    except ValueError:
        raise RuntimeError(
            f"the runtime answered {status} with a non-JSON body: "
            f"{body[:200]!r}")

    if not isinstance(report, dict):
        raise RuntimeError(
            f"the runtime answered {status} with {type(report).__name__}, "
            f"not a report object")

    # The discriminator is terminal_reason, NOT `ok`.
    #
    # `ok` in the orchestrator's report means "met its target" -- it is set by
    # `reason == TARGET_MET`. So a run that correctly stopped on
    # budget_exhausted, max_attempts, no_results or user_cancelled comes back
    # `ok: False`, and a normal bounded stop would be indistinguishable from a
    # refusal. Treating it as one would make the caller overwrite a truthful
    # `stopped/budget_exhausted` with a fabricated `failed/error` -- the exact
    # dishonesty this project exists to prevent.
    #
    # A refusal returns EARLY, before the orchestrator ever runs, so it has an
    # `error` code and no terminal_reason. That absence is the real signal.
    if not report.get("terminal_reason"):
        # Nobody else will close this row, so raising here (and letting the
        # caller record it) is what stops it sitting at `running` forever.
        detail = (report.get("message") or report.get("error")
                  or report.get("error_type") or "no reason given")
        raise RuntimeError(f"the runtime refused the run: {detail}")

    run_store.append_event(run_id, "dispatch_done",
                           f"runtime returned {report.get('terminal_reason')}")
    return report


def _cancel_event(run_id):
    with _cancels_lock:
        event = _cancels.get(run_id)
        if event is None:
            event = threading.Event()
            _cancels[run_id] = event
        return event


def register_agent_routes(app):

    @app.route("/runs", methods=["POST"])
    def runs_start():
        """Start a run and return immediately with its id.

        The row is created HERE, synchronously, so a client that starts a run
        and instantly polls cannot miss it. The work happens on a thread.
        """
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error

        goal = str(data.get("goal") or "").strip()
        campaign_id = str(data.get("campaign_id") or "").strip() or None
        if not goal and not campaign_id:
            return _err("a goal or a campaign_id is required", 400)

        # The dashboard picker's pinned search area. REQUIRED, like every
        # other campaign input: a pin-less run lets the model search wherever
        # its reading of the goal points, which is how runs ended up judging
        # the wrong city. The dashboard refuses to submit without a pick, and
        # the API 400s one for the same reason. A code that is not a positive
        # integer is a 400, because silently dropping it would search the
        # wrong place with no error.
        location_code = data.get("location_code")
        if location_code in ("", None):
            return _err("location_code is required: pick a search area",
                        400)
        else:
            try:
                location_code = int(location_code)
            except (TypeError, ValueError):
                return _err("location_code must be an integer", 400)
            if location_code <= 0:
                return _err("location_code must be an integer", 400)
        location_name = str(data.get("location_name") or "").strip() or None

        # How strictly to judge: strict | balanced | lenient. REQUIRED: there
        # is no safe default (strict discards emerging-market leads, lenient
        # waves through noise), so an omitted tier is a 400, not a guess.
        # Unknown values are a 400 for the same reason: silently searching
        # under the wrong tier judges every business by a rule the seller
        # did not choose.
        scrutiny = str(data.get("scrutiny") or "").strip().lower() or None
        if scrutiny is None:
            return _err("scrutiny is required: strict, balanced or lenient",
                        400)
        if scrutiny not in ("strict", "balanced", "lenient"):
            return _err("scrutiny must be strict, balanced or lenient", 400)

        # What the seller pitches, in their own words. REQUIRED unless it is
        # already known: the body value wins, else the seller profile niche,
        # else the continued campaign's niche. Only when all three are blank
        # does the engine fall back to its dev placeholder — and that silent
        # fallback once scored bakeries as dental clinics, so the API refuses
        # to start such a run instead of hoping the model fills the gap.
        niche = str(data.get("niche") or "").strip()[:200] or None
        if niche is None and not _niche_on_file(seller_id, campaign_id):
            return _err("niche is required: pass what you sell, or set it "
                        "once on the seller profile", 400)

        # Exactly one search for the pinned area, then draft from what it
        # returned. Defaults ON for a pinned run (one area, one search) —
        # and every API run is pinned now that the code is required, so an
        # omitted flag means single search unless explicitly set false.
        single_search = data.get("single_search")
        if single_search is not None:
            single_search = _parse_bool(single_search)
            if single_search is None:
                return _err("single_search must be true or false", 400)

        bounds = data.get("bounds") or dict(DEFAULT_BOUNDS)
        if not isinstance(bounds, dict):
            return _err("bounds must be an object", 400)
        bounds = dict(bounds)
        # How many qualified leads to find. REQUIRED: the old default of 10
        # was prose nobody chose ("Find 5" in the goal while the loop aimed
        # for 10), so an omitted count is a 400, not a guess.
        if data.get("target_qualified") is None:
            return _err("target_qualified is required: how many qualified "
                        "leads to find (1-100)", 400)
        # The "Find 5" in the goal is prose; this is the bound the loop
        # actually enforces. Kept inside bounds so the finished run is
        # judged against the caps it started with, like every other cap.
        try:
            target = int(data.get("target_qualified"))
        except (TypeError, ValueError):
            return _err("target_qualified must be an integer", 400)
        if target < 1 or target > 100:
            return _err("target_qualified must be between 1 and 100", 400)
        bounds["target_qualified"] = target
        if not _RUN_SLOTS.acquire(blocking=False):
            return _err("too many runs in flight right now; wait for one "
                        "to finish and retry", 429)
        try:
            run = run_store.create_run(
                seller_id=seller_id, campaign_id=campaign_id,
                trigger="dashboard", goal=goal or "continue this campaign",
                bounds=bounds)
        except Exception as exc:
            _RUN_SLOTS.release()
            return _err(str(exc)[:300], 502)
        run_id = run.get("id")
        if not run_id:
            _RUN_SLOTS.release()
            return _err("could not create the run row", 502)

        # SCALING CEILING, stated plainly: this thread dies with the worker.
        # An ECS redeploy (deploy-dashboard.sh step 4) kills in-flight runs
        # mid-turn and leaves their rows `running` forever — the slots above
        # bound how many can run, not how long they survive a deploy. Do not
        # redeploy while runs are live (check POST /runs/list first). The real
        # fix is externalizing runs (SQS + worker fleet, one ECS task per run
        # instead of one thread), which is new AWS infrastructure and needs
        # its own provisioning change — deliberately not smuggled in here.
        thread = threading.Thread(
            target=_run_in_background,
            args=(run_id, seller_id, goal or "continue this campaign",
                  bounds, campaign_id, location_code, location_name,
                  scrutiny, niche, single_search),
            name=f"leadgen-run-{run_id[:8]}", daemon=True)
        try:
            thread.start()
        except Exception:
            _RUN_SLOTS.release()
            raise
        return _ok(run_id=run_id, status=run.get("status", "running"),
                   run=run)

    @app.route("/runs/list", methods=["POST"])
    def runs_list():
        """POST, not GET: the seller travels in the body like every other read."""
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error
        try:
            rows = run_store.list_runs(seller_id, limit=int(data.get("limit") or 20))
        except Exception as exc:
            return _err(str(exc)[:300], 502)
        return _ok(count=len(rows), runs=rows)

    @app.route("/runs/<run_id>", methods=["GET"])
    def runs_get(run_id):
        """One URL, two representations.

        A browser is served the run page; everything else gets JSON. Doing this
        in one view rather than two routes is not a stylistic choice — Flask
        allows the same rule twice and silently keeps whichever was registered
        first, so two registrations would have meant one of them never
        running, with no error to say so.

        Ownership is NOT checked for the HTML branch, and that is safe: the page
        renders a shell containing only the run id, and every request it then
        makes goes through the ownership-checked JSON API. Checking here too
        would be a second implementation of the rule.
        """
        if _wants_html():
            import routes_ui      # lazy: routes_ui must not import this module
            return render_template("run.html", run_id=run_id,
                                   **routes_ui.page_config())

        seller_id, error = _seller(_body())
        if error:
            return error
        if not _is_uuid(run_id):
            return _err("run not found", 404)
        run = run_store.get_run(run_id)
        # Same 404-not-403 rule as drafts.
        if not run or str(run.get("seller_id") or "") != str(seller_id):
            return _err("run not found", 404)
        return _ok(run=run, progress=run.get("progress") or {},
                   report=run.get("report"))

    @app.route("/runs/<run_id>/events", methods=["GET"])
    def runs_events(run_id):
        """Incremental event pull for the dashboard's 5s poll.

        `?after=<seq>` makes each poll cheap and idempotent: the client keeps
        the highest seq it has seen and asks only for what is newer. The
        cursor is pushed to the database (seq >= after+1), so a poll's
        payload stays O(new events) instead of re-downloading the whole
        trace and slicing in Python. Polling rather than SSE is a deliberate
        cut — same UX, a third of the code.
        """
        seller_id, error = _seller(_body())
        if error:
            return error
        if not _is_uuid(run_id):
            return _err("run not found", 404)
        run = run_store.get_run(run_id)
        if not run or str(run.get("seller_id") or "") != str(seller_id):
            return _err("run not found", 404)

        try:
            after = int(request.args.get("after") or 0)
        except (TypeError, ValueError):
            return _err("after must be an integer", 400)
        fresh = run_store.list_run_events(run_id, after=after)
        # Seqs are 1-based and contiguous per run (single writer), so the
        # max of the fresh rows IS the global max when any arrived; when
        # none did, the cursor stands still by echoing `after` back.
        last_seq = max([after] + [int(e.get("seq") or 0) for e in fresh])
        return _ok(
            run_id=run_id,
            status=run.get("status"),
            terminal_reason=run.get("terminal_reason"),
            progress=run.get("progress") or {},
            report=run.get("report"),
            events=fresh,
            last_seq=last_seq,
        )

    @app.route("/runs/<run_id>/cancel", methods=["POST"])
    def runs_cancel(run_id):
        seller_id, error = _seller(_body())
        if error:
            return error
        if not _is_uuid(run_id):
            return _err("run not found", 404)
        run = run_store.get_run(run_id)
        if not run or str(run.get("seller_id") or "") != str(seller_id):
            return _err("run not found", 404)

        if run.get("status") != "running":
            # Already finished. Saying so beats a silent no-op, and 409 is the
            # same "state machine refuses this" code the send path uses.
            return _err(f"run is already {run.get('status')}", 409)

        # BOTH signals, because this process cannot know which executor owns
        # the run. The Event reaches a thread in THIS process (the local
        # backend); the column reaches the AgentCore runtime, which is a
        # different process on a different host and cannot see our memory.
        # Setting only one would leave cancel silently broken on one backend.
        _cancel_event(run_id).set()
        try:
            run_store.request_cancel(run_id)
            durable = True
            detail = None
        except Exception as exc:
            # Whether this is fatal depends on which executor owns the run.
            #
            # The in-process Event above already covers a LOCAL run, so a
            # failed column write there does not un-cancel anything -- and
            # reporting an error would tell the operator their stop button
            # failed when it worked. On the agentcore backend the column is the
            # ONLY route to the run, so a failure there really does mean the
            # cancel was not delivered.
            durable = False
            detail = str(exc)[:200]
            if config.agent_backend() == "agentcore":
                return _err(
                    f"cancel could not be delivered to the runtime: {detail}",
                    502)

        return _ok(run_id=run_id, cancel_requested=True,
                   durable=durable,
                   note="The run stops at the top of its next turn; "
                        "work already in flight still finishes."
                        + ("" if durable else
                           " NOTE: recorded in this process only — run "
                           "supabase-schema-agent.sql to add "
                           "runs.cancel_requested."))

    # ----------------------------------------------------------------------- #
    # Drafts
    # ----------------------------------------------------------------------- #
    @app.route("/drafts", methods=["POST"])
    def drafts_create():
        """Generate a draft for a lead AND persist it, awaiting approval.

        Distinct from /draft (app.py), which generates without persisting for
        n8n. Same underlying generator, different contract.
        """
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error
        lead_id = str(data.get("lead_id") or "").strip()
        if not lead_id:
            return _err("lead_id is required", 400)

        lead = leads_read.read_lead(lead_id)
        if not lead:
            return _err("lead not found", 404)
        campaign_id = lead.get("campaign_id")
        if not leads_read.owns_campaign(seller_id, campaign_id):
            return _err("lead not found or not owned by this seller", 403)

        # Same composition as /draft (n8n's route) — shared so the two cannot
        # drift. This route's difference is only that it persists the result.
        try:
            out = draft_compose.compose_draft(
                lead_id, seller_id,
                temperature=float(data.get("temperature") or 0.7),
                score_model=data.get("score_model"))
        except seller_ops.OpsError as exc:
            return _err(exc.message, {"bad_request": 400, "not_found": 404,
                                      "unavailable": 503}.get(exc.kind, 502))

        to_email = data.get("to_email") or draft_compose.first_email(lead)
        try:
            draft = run_store.create_draft(
                lead_id=lead_id, campaign_id=campaign_id, seller_id=seller_id,
                subject=out.get("subject"), email_body=out.get("email_body"),
                angle=out.get("angle"), to_email=to_email)
        except Exception as exc:
            # Most often idx_drafts_lead_live: this lead already has a live
            # draft, which is a state conflict rather than a server error.
            return _err(f"could not save the draft: {exc}", 409)
        return _ok(draft=draft)

    @app.route("/drafts/list", methods=["POST"])
    def drafts_list():
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error
        status = data.get("status") or None
        # `run_id` lets the run page ask for ITS drafts. Ownership is enforced
        # by seller_id above and applied together with it, so a run id
        # belonging to another seller narrows to nothing rather than leaking.
        run_id = str(data.get("run_id") or "").strip() or None
        try:
            rows = run_store.list_drafts(
                seller_id=seller_id,
                campaign_id=str(data.get("campaign_id") or "").strip() or None,
                run_id=run_id, status=status, limit=int(data.get("limit") or 50))
        except Exception as exc:
            return _err(str(exc)[:300], 502)
        return _ok(count=len(rows), drafts=rows)

    @app.route("/drafts/<draft_id>", methods=["GET"])
    def drafts_get(draft_id):
        seller_id, error = _seller(_body())
        if error:
            return error
        draft, error = _owned_draft(draft_id, seller_id)
        if error:
            return error
        return _ok(draft=draft)

    @app.route("/drafts/<draft_id>", methods=["PATCH"])
    def drafts_patch(draft_id):
        """Human edits. Bumps the revision so an edit is traceable in the video
        and in the audit trail."""
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error
        draft, error = _owned_draft(draft_id, seller_id)
        if error:
            return error
        if draft.get("status") not in ("draft", "approved"):
            return _err(f"cannot edit a {draft.get('status')} draft", 409)

        editable = {}
        for key in ("subject", "email_body", "angle", "to_email"):
            if key in data and data[key] is not None:
                editable[key] = str(data[key])
        if not editable:
            return _err("no editable fields supplied", 400)
        try:
            updated = run_store.update_draft(draft_id, bump_revision=True,
                                             **editable)
        except Exception as exc:
            return _err(str(exc)[:300], 502)
        return _ok(draft=updated)

    @app.route("/drafts/<draft_id>/approve", methods=["POST"])
    def drafts_approve(draft_id):
        return _set_status(draft_id, "approved")

    @app.route("/drafts/<draft_id>/reject", methods=["POST"])
    def drafts_reject(draft_id):
        return _set_status(draft_id, "rejected")

    @app.route("/drafts/<draft_id>/export", methods=["POST"])
    def drafts_export_one(draft_id):
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error
        draft, error = _owned_draft(draft_id, seller_id)
        if error:
            return error
        claimed, error = _claim_for_export(draft, seller_id)
        if error:
            return error
        release_claim(claimed)
        return _csv_response([claimed])

    @app.route("/drafts/<draft_id>/send", methods=["POST"])
    def drafts_send(draft_id):
        """Send one approved draft. The four gates, then the provider.

        This is the only route in the app that can mark a draft `sent`, and it
        does so only after a provider returned a message id. There is no path
        that writes `sent` on the strength of having tried.

        Gate 4 is checked BEFORE the claim, unlike export: a draft we already
        know cannot be sent should not be moved to `sending` at all, because
        the claim exists to stop two sends racing and there is nothing to race
        for.
        """
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error
        draft, error = _owned_draft(draft_id, seller_id)
        if error:
            return error
        if draft.get("status") != "approved":
            return _err(f"draft is {draft.get('status')}, not approved — a "
                        f"human must approve it before it can be sent", 409)
        # Resolved ONCE, before the gate, and handed to the send below. A send
        # that re-resolved would be asking a second question about the same
        # seller, and if their settings changed in between, the gate that
        # allowed the send and the send itself would disagree about who the
        # sender is -- which is not a race worth being able to lose.
        sender = draft_send.read_sender(seller_id)
        cfg, reply_to = draft_send.sender_config(seller_id, sender)

        reason = recipient_blocked(draft, cfg)
        if reason:
            return _err(reason, 400)

        try:
            claimed = supabase_store.update_rows(
                "drafts", {"status": "sending"},
                {"id": draft["id"], "status": "approved"})
        except Exception as exc:
            return _err(str(exc)[:300], 502)
        if not claimed:
            return _err("another request is already handling this draft", 409)

        try:
            result = draft_send.send(claimed[0], seller_id, sender=sender,
                                     cfg=cfg, reply_to=reply_to)
        except send_provider.SendRefused as exc:
            release_claim(draft)
            return _err(exc.reason, exc.status)
        except send_provider.SendFailed as exc:
            _fail(draft["id"], str(exc))
            return _err(f"the provider refused the message: {exc}", 502)
        except Exception as exc:
            _fail(draft["id"], f"{type(exc).__name__}: {exc}")
            return _err(str(exc)[:300], 502)

        # `sent` is terminal and `idx_drafts_sent_once` makes it so: the partial
        # unique index on (lead_id) where status='sent' means a second send for
        # the same lead is impossible below the app, not just discouraged here.
        try:
            updated = run_store.update_draft(
                draft["id"], status="sent",
                sent_at=datetime.now(timezone.utc).isoformat(), **result)
        except Exception as exc:
            return _err(str(exc)[:300], 502)
        return _ok(draft=updated, sent=True)


    @app.route("/drafts/export", methods=["POST"])
    def drafts_export_many():
        """Batch export of approved drafts, all four gates applied to each.

        All-or-nothing: if any draft fails a gate the whole request is refused,
        so a partial CSV can never be mistaken for a complete batch.
        """
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error

        ids = data.get("draft_ids")
        if isinstance(ids, str):
            ids = [ids]
        # The run page exports the drafts OF THAT RUN. Without this filter
        # "Export approved as CSV" on an old run silently swept in every other
        # approved draft the seller owns -- a batch that does not match the page
        # it was clicked from. Accepting run_id here is what makes the button
        # mean what its heading says.
        run_id = str(data.get("run_id") or "").strip() or None
        try:
            rows = run_store.list_drafts(
                seller_id=seller_id,
                campaign_id=str(data.get("campaign_id") or "").strip() or None,
                run_id=run_id,
                status="approved", limit=int(data.get("limit") or 100))
        except Exception as exc:
            return _err(str(exc)[:300], 502)
        if ids:
            wanted = {str(i) for i in ids}
            rows = [r for r in rows if str(r.get("id")) in wanted]
            missing = wanted - {str(r.get("id")) for r in rows}
            if missing:
                return _err(f"{len(missing)} draft(s) not found, not owned, "
                            f"or not approved", 404)
        if not rows:
            return _err("no approved drafts to export", 404)

        claimed = []
        for row in rows:
            held, error = _claim_for_export(row, seller_id)
            if error:
                for c in claimed:       # release what we already took
                    release_claim(c)
                return error
            claimed.append(held)
        for c in claimed:
            release_claim(c)
        return _csv_response(claimed)

    @app.route("/prospects/list", methods=["POST"])
    def prospects_list():
        """Every prospect a campaign found, grouped by campaign for display.

        Takes EITHER `campaign_id` (one campaign) or `run_id` (every campaign
        that run created — a run fans out across several, and the dashboard's
        run page wants all of them in one call). Counts ride along per
        campaign, so a run that found a hundred prospects and qualified none
        reads as what it is instead of as an empty page.

        Read-scoped like drafts: another seller's campaign or run is 404, not
        403, so the response never confirms another tenant's ids exist.
        """
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error
        campaign_id = str(data.get("campaign_id") or "").strip() or None
        run_id = str(data.get("run_id") or "").strip() or None
        if not campaign_id and not run_id:
            return _err("a campaign_id or a run_id is required", 400)
        try:
            limit = int(data.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 500))

        cids = []
        if run_id:
            if not _is_uuid(run_id):
                return _err("run not found", 404)
            run = run_store.get_run(run_id)
            if not run or str(run.get("seller_id") or "") != str(seller_id):
                return _err("run not found", 404)
            report = run.get("report") or {}
            for cid in (report.get("campaign_ids") or []):
                if cid and cid not in cids:
                    cids.append(cid)
            if run.get("campaign_id") and run["campaign_id"] not in cids:
                cids.append(run["campaign_id"])
            # A live run has no report yet (it is written when the run
            # stops), so the live progress carries the campaigns created so
            # far instead. Without this fallback the prospects table would
            # sit empty until the run stops — exactly when nobody is
            # watching anymore.
            progress_cids = ((run.get("progress") or {}).get("campaign_ids")
                             or [])
            for cid in progress_cids:
                if cid and cid not in cids:
                    cids.append(cid)
        else:
            if not _is_uuid(campaign_id):
                return _err("campaign not found", 404)
            if not leads_read.owns_campaign(seller_id, campaign_id):
                return _err("campaign not found", 404)
            cids = [campaign_id]

        try:
            grows = supabase_store.select_rows(
                "campaigns", columns="id,name,keyword,location",
                filters={"seller_id": seller_id},
                filters_in={"id": cids}) if cids else []
        except Exception as exc:
            return _err(str(exc)[:300], 502)
        try:
            prows = supabase_store.select_rows(
                "prospects",
                columns="id,campaign_id,business_name,category,phone,"
                        "website,locality,region,status,created_at",
                filters_in={"campaign_id": [g["id"] for g in grows]} if grows
                else {"campaign_id": cids},
                order="created_at.asc", limit=limit) if cids else []
        except Exception as exc:
            return _err(str(exc)[:300], 502)

        by_campaign = {}
        for p in prows or []:
            by_campaign.setdefault(p.get("campaign_id"), []).append({
                "business_name": p.get("business_name"),
                "category": p.get("category"),
                "phone": p.get("phone"),
                "website": p.get("website"),
                "locality": p.get("locality"),
                "region": p.get("region"),
                "status": p.get("status"),
            })
        campaigns = []
        for g in grows:
            rows = by_campaign.get(g["id"], [])
            campaigns.append({
                "campaign_id": g["id"],
                "name": g.get("name"),
                "keyword": g.get("keyword"),
                "location": g.get("location"),
                "prospect_count": len(rows),
                "prospects": rows,
            })
        # Campaign rows the seller no longer owns (or that vanished) contribute
        # prospects under an unnamed group rather than dropping them silently.
        known = {g["id"] for g in grows}
        orphans = [p for cid, ps in by_campaign.items() if cid not in known
                   for p in ps]
        if orphans:
            campaigns.append({
                "campaign_id": None, "name": None, "keyword": None,
                "location": None, "prospect_count": len(orphans),
                "prospects": orphans,
            })
        return _ok(count=sum(c["prospect_count"] for c in campaigns),
                    campaigns=campaigns)

    @app.route("/prospects/direct", methods=["POST"])
    def prospects_direct():
        """Add ONE business the seller already knows they want to pitch.

        A SECOND WAY IN, alongside the keyword+location search. The search can
        only express "show me plumbers in Austin"; this expresses "add Mike's
        Plumbing" -- a referral, a shop they drove past, someone from a
        networking event. Those are ordinary ways a freelancer finds work and
        the search box cannot express any of them.

        It addresses the business by whatever is strongest: `place_id` (exact),
        `cid` (exact), or a `keyword` name (fuzzy). One billable DataForSEO call
        per request, and only for an authenticated seller -- the caller's finger
        is the budget, so there is no loop here to bound.

        It stores the prospect and STOPS. No crawl, no score, no draft, and no
        run: a run exists to bound a search that can run long and cost money,
        and there is no search here. Enriching and drafting stay on-demand, so
        this route cannot spend anything beyond its own single lookup.
        """
        data = _body()
        seller_id, error = _seller(data)
        if error:
            return error

        place_id = data.get("place_id")
        cid = data.get("cid")
        keyword = data.get("keyword") or data.get("name")
        if not any(str(v or "").strip()
                   for v in (place_id, cid, keyword)):
            return _err("give a place_id, a cid, or a business name", 400)

        # The seller's own DataForSEO credentials when they have them, the
        # platform master otherwise -- same resolution the finder uses, so a
        # hand-added business is billed to whoever would have found it.
        try:
            settings = draft_send.read_settings(seller_id)
            src = config.source_cfg(settings)
        except Exception:
            src = config.source_cfg(None)

        try:
            row, _body_json = dataforseo.lookup_business(
                place_id=place_id, cid=cid, keyword=keyword,
                location_name=(str(data.get("location") or "").strip() or None),
                location_code=data.get("location_code"),
                login=src.get("login"), password=src.get("password"))
        except RuntimeError as exc:
            # Credentials missing, or DataForSEO refused (most often: a bare
            # name needs a location to disambiguate). Both are operator-fixable
            # settings, so surface the real message rather than a generic 502.
            return _err(str(exc), 502)

        if not row:
            return _err("no business matched that name or id. Try a fuller "
                        "name, or add a location to narrow it down.", 404)

        try:
            out = seller_ops.add_direct_prospect(seller_id, row)
        except seller_ops.OpsError as exc:
            return _err(exc.message, {"bad_request": 400, "not_found": 404,
                                      "unavailable": 503}.get(exc.kind, 502))
        return _ok(prospect=row, **out)

    return app


# --------------------------------------------------------------------------- #
# Draft state transitions
# --------------------------------------------------------------------------- #
def _set_status(draft_id, status):
    data = _body()
    seller_id, error = _seller(data)
    if error:
        return error
    draft, error = _owned_draft(draft_id, seller_id)
    if error:
        return error
    if draft.get("status") == status:
        return _ok(draft=draft, changed=False)
    if draft.get("status") not in ("draft", "approved"):
        return _err(f"cannot move a {draft.get('status')} draft to {status}", 409)
    fields = {"status": status}
    if status == "approved":
        fields["approved_by"] = str(data.get("approved_by")
                                    or request.headers.get("X-Actor")
                                    or "dashboard")
    try:
        updated = run_store.update_draft(draft_id, **fields)
    except Exception as exc:
        return _err(str(exc)[:300], 502)
    return _ok(draft=updated)


def _claim_for_export(draft, seller_id):
    """Gates 1-3 for one draft. Returns (claimed_row, error_response).

    The compare-and-swap is the load-bearing part: `update_rows` filtered on
    `status='approved'` returns no rows if someone else already moved it, which
    is how two concurrent exports are prevented from both proceeding. A
    read-then-write would pass both.

    Gate 4 (`recipient_blocked`) deliberately does NOT run here. It used to, and
    because SES is not wired it returns a reason unconditionally — so every
    export 400'd and `_csv_response` was unreachable code. That conflated "this
    address cannot be sent to" with "you may not have this CSV", and exporting
    is the one thing this build CAN do. The check belongs on the send path,
    where a blocked recipient actually blocks something; it is asserted there by
    tests/test_send.py, not by the export tests.
    """
    if not _owns_draft(draft, seller_id):
        return None, _err("draft not found", 403)                    # gate 1
    if draft.get("status") != "approved":
        return None, _err(
            f"draft is {draft.get('status')}, not approved — a human must "
            f"approve it before it can be exported", 409)            # gate 2

    try:
        claimed = supabase_store.update_rows(
            "drafts", {"status": "sending"},
            {"id": draft["id"], "status": "approved"})               # gate 3
    except Exception as exc:
        return None, _err(str(exc)[:300], 502)
    if not claimed:
        return None, _err("another request is already handling this draft", 409)

    return claimed[0], None


def recipient_blocked(draft, cfg=None):
    """Why this draft cannot be SENT, or None. Not called by the export path.

    Returns a reason whenever there is no way to deliver the message: no
    address, or no sender configured. With SEND_BACKEND=none — the default —
    that is every draft, and the refusal says which setting is missing rather
    than blaming the draft.

    `cfg` is the resolved sender config for THIS draft's seller, and the caller
    resolves it once and passes the same dict to the send. A sender is now a
    per-seller fact (a seller may bring their own SMTP account), so asking
    "is sending configured?" without a seller answers a different question than
    the one the send will ask — and the two disagreeing is the failure this
    signature exists to prevent. `draft["seller_id"]` is the seller when the
    caller does not supply one.
    """
    to_email = str(draft.get("to_email") or "").strip()
    if not to_email or "@" not in to_email:
        return "this draft has no recipient address"
    if cfg is None:
        cfg, _reply_to = draft_send.sender_config(draft.get("seller_id"))
    if not send_provider.configured(cfg):
        return ("sending is not configured for this seller (SEND_BACKEND, and "
                "neither their settings nor SEND_FROM_EMAIL, provide a sender), "
                "so nothing was sent. The draft stays approved and is available "
                "as CSV.")
    return None


def _fail(draft_id, message):
    """Record a send that the provider rejected. Never raises.

    The claim is deliberately NOT released to `approved`: something may or may
    not have been delivered, and offering the draft for a second attempt would
    be inviting a duplicate. `failed` is terminal for this reason, and
    `send_error` says why.
    """
    try:
        supabase_store.update_rows(
            "drafts", {"status": "failed", "send_error": str(message)[:500]},
            {"id": draft_id, "status": "sending"})
    except Exception:
        pass


def release_claim(draft):
    """Undo the `sending` claim, because nothing was actually sent.

    This is the honest half of the cut: the draft returns to `approved` —
    which is true — rather than being marked `sent`, which would be a lie the
    dashboard, the CSV and the demo would all repeat.
    """
    try:
        supabase_store.update_rows(
            "drafts", {"status": "approved"},
            {"id": draft["id"], "status": "sending"})
    except Exception:
        pass


def _csv_response(drafts):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["draft_id", "lead_id", "campaign_id", "to_email",
                     "subject", "email_body", "angle", "revision", "status"])
    for d in drafts:
        writer.writerow([d.get("id"), d.get("lead_id"), d.get("campaign_id"),
                         d.get("to_email"), d.get("subject"),
                         d.get("email_body"), d.get("angle"),
                         d.get("revision"), d.get("status")])
    return _ok(count=len(drafts), csv=buf.getvalue(),
               note="Nothing was sent. These drafts remain approved.")

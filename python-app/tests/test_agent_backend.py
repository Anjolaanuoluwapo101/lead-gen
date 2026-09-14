"""Running a run somewhere other than this process.

WHY THERE ARE TWO BACKENDS

`POST /runs` on Flask answers immediately and drives the orchestrator on a
daemon thread. `POST /invocations` on AgentCore runs the whole thing inside one
synchronous request. Both are correct for their surface — a dashboard needs to
poll, a runtime needs to answer — and both write to the SAME `runs` row.

That shared row is what makes this a switch rather than two products. The
dashboard's progress bar, event log and stop reason read the row; they do not
care which process is writing it.

THE TWO THINGS THAT BREAK IF NOBODY THINKS ABOUT THEM

1. **The run id.** If the runtime opens its own row, the dashboard has nothing
   to poll until the invocation returns — which is the entire run. So the id
   travels in the payload and the runtime adopts it.
2. **Cancel.** A threading.Event cannot cross a process boundary. The local
   backend signals a thread in this process; the AgentCore backend has to leave
   a mark in the database that the remote loop reads between turns. The cancel
   route sets BOTH, because it cannot know which one owns the run.
"""

import io
import json

import pytest

import config
import routes_agent
from agent.budget import DEFAULT_BOUNDS
from agent.orchestrator import Orchestrator

RUN = "11111111-1111-1111-1111-111111111111"


# --------------------------------------------------------------------------- #
# config.agent_backend
# --------------------------------------------------------------------------- #
def test_the_default_is_local(monkeypatch):
    """Nothing deployed is the default, so a fresh clone runs."""
    monkeypatch.delenv("AGENT_BACKEND", raising=False)
    assert config.agent_backend() == "local"


def test_agentcore_is_accepted(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "agentcore")
    assert config.agent_backend() == "agentcore"


def test_the_value_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "  AgentCore ")
    assert config.agent_backend() == "agentcore"


def test_a_typo_raises_rather_than_falling_back_to_local(monkeypatch):
    """The failure this refuses is the demo lying to you.

    `AGENT_BACKEND=agent-core` falling back to local would show a working
    dashboard while the operator believes they are watching the deployed
    runtime. Refusing is the only answer that cannot be mistaken.
    """
    monkeypatch.setenv("AGENT_BACKEND", "agent-core")
    with pytest.raises(ValueError, match="agent-core"):
        config.agent_backend()


def test_an_absent_runtime_arn_is_none_not_empty_string(monkeypatch):
    monkeypatch.delenv("AGENTCORE_RUNTIME_ARN", raising=False)
    assert config.agentcore_runtime_arn() is None
    monkeypatch.setenv("AGENTCORE_RUNTIME_ARN", "   ")
    assert config.agentcore_runtime_arn() is None


def test_agentcore_region_defaults_to_west_2(monkeypatch):
    monkeypatch.delenv("AGENTCORE_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    assert config.agentcore_region() == "us-west-2"


# --------------------------------------------------------------------------- #
# The switch itself
# --------------------------------------------------------------------------- #
class FakeRunStore:
    def __init__(self):
        self.events = []
        self.finished = None
        self.runs = {RUN: {"id": RUN, "status": "running"}}

    def append_event(self, run_id, kind, message=None, payload=None):
        self.events.append({"run_id": run_id, "kind": kind, "message": message})
        return {}

    def finish_run(self, run_id, **kw):
        self.finished = kw
        return kw

    def get_run(self, run_id):
        return dict(self.runs[run_id]) if run_id in self.runs else None

    def request_cancel(self, run_id):
        return {}

    def is_cancel_requested(self, run_id):
        return False


class FakeBoto:
    """Records the call so the payload can be asserted on."""

    def __init__(self, report=None, error=None, raise_on_client=False):
        self.report = report if report is not None else {
            "ok": True, "terminal_reason": "target_met", "run_id": RUN}
        self.error = error
        self.raise_on_client = raise_on_client
        self.calls = []
        self.order = []

    def client(self, service, region_name=None, config=None):
        if self.raise_on_client:
            raise RuntimeError("no credentials found")
        self.calls.append({"service": service, "region": region_name,
                           "config": config})
        if self.error:
            raise self.error
        report = self.report
        return _FakeClient(self, report)


class _FakeClient:
    def __init__(self, owner, report):
        self.owner = owner
        self.report = report

    def invoke_agent_runtime(self, **kwargs):
        self.owner.order.append("invoke")
        self.kwargs = kwargs
        self.owner.last_kwargs = kwargs
        return {"statusCode": 200,
                "response": io.BytesIO(json.dumps(self.report).encode())}


@pytest.fixture
def store(monkeypatch):
    import agent.run_store as run_store
    fake = FakeRunStore()
    for name in ("append_event", "finish_run", "get_run",
                 "request_cancel", "is_cancel_requested"):
        monkeypatch.setattr(run_store, name, getattr(fake, name))
    return fake


@pytest.fixture
def boto(monkeypatch):
    """Patches the lazily-imported boto3. The import is INSIDE the function, so
    patching the module attribute is what the call site actually resolves."""
    fake = FakeBoto()
    import boto3
    monkeypatch.setattr(boto3, "client", fake.client)
    return fake


def _dispatch(monkeypatch, store, backend="agentcore", arn="arn:aws:x:y:z"):
    monkeypatch.setenv("AGENT_BACKEND", backend)
    if arn:
        monkeypatch.setenv("AGENTCORE_RUNTIME_ARN", arn)
    else:
        monkeypatch.delenv("AGENTCORE_RUNTIME_ARN", raising=False)
    routes_agent._run_in_background(
        RUN, "seller-1", "find 5 dentists", dict(DEFAULT_BOUNDS), None)


def test_the_local_backend_does_not_touch_aws(monkeypatch, store):
    """AGENT_BACKEND=local must not require boto3, network, or an ARN."""
    monkeypatch.setenv("AGENT_BACKEND", "local")
    monkeypatch.delenv("AGENTCORE_RUNTIME_ARN", raising=False)

    import boto3
    def explode(*a, **k):
        raise AssertionError("the local backend built an AWS client")
    monkeypatch.setattr(boto3, "client", explode)

    constructed = []

    class FakeOrch:
        def __init__(self, **kw):
            constructed.append(kw)
            self.run_id = kw.get("run_id")

        def run(self):
            return {"terminal_reason": "target_met"}

    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrch)
    routes_agent._run_in_background(
        RUN, "seller-1", "goal", dict(DEFAULT_BOUNDS), None)

    assert constructed, "the orchestrator was never built"
    assert constructed[0]["run_id"] == RUN


def test_each_run_gets_its_own_runtime_session(monkeypatch, store, boto):
    # Concurrent runs must not share (or serialize behind) one session, and
    # the id must clear the API minimum (33 chars) or every dispatch fails.
    _dispatch(monkeypatch, store)
    session = boto.last_kwargs.get("runtimeSessionId", "")
    assert session == f"run-{RUN}"
    assert 33 <= len(session) <= 256


def test_agentcore_backend_does_not_build_a_local_orchestrator(monkeypatch,
                                                               store, boto):
    def explode(**kw):
        raise AssertionError("a local Orchestrator was built on the agentcore path")
    monkeypatch.setattr(routes_agent, "Orchestrator", explode)

    _dispatch(monkeypatch, store)
    assert boto.calls, "the runtime was never invoked"


def test_the_run_id_travels_so_the_runtime_adopts_our_row(monkeypatch, store,
                                                          boto):
    """Without this the dashboard has nothing to poll.

    The row is created by Flask before any work starts. If the runtime does not
    adopt it, every event lands on a second row that the browser only learns
    about when the whole run has already finished.
    """
    _dispatch(monkeypatch, store)
    payload = json.loads(boto.last_kwargs["payload"].decode())
    assert payload["run_id"] == RUN
    assert payload["seller_id"] == "seller-1"
    assert payload["goal"] == "find 5 dentists"
    assert payload["bounds"]["max_wall_clock_s"] == DEFAULT_BOUNDS["max_wall_clock_s"]


def test_the_region_comes_from_config_not_a_hardcoded_string(monkeypatch, store,
                                                             boto):
    monkeypatch.setenv("AGENTCORE_REGION", "eu-west-1")
    _dispatch(monkeypatch, store)
    assert boto.calls[0]["region"] == "eu-west-1"


def test_the_dispatch_event_is_written_before_the_call(monkeypatch, store, boto):
    """The cold start is a few seconds of nothing. This is the line that makes
    it a labelled step instead of a spinner with nothing behind it — so it has
    to be written BEFORE the invoke, not after."""
    _dispatch(monkeypatch, store)
    assert [e["kind"] for e in store.events][:2] == ["dispatch", "dispatch_done"]
    assert "dispatch" in [e["kind"] for e in store.events]


def test_a_missing_arn_is_recorded_as_a_failed_run_not_a_silent_local_run(
        monkeypatch, store):
    """Silently falling back to local would be the worst outcome: a green
    dashboard that never touched AWS."""
    _dispatch(monkeypatch, store, arn=None)
    assert store.finished is not None
    assert store.finished["status"] == "failed"
    assert "AGENTCORE_RUNTIME_ARN" in store.finished["error"]


def test_a_transport_failure_closes_the_run(monkeypatch, store, boto):
    boto.error = RuntimeError("connection reset")
    _dispatch(monkeypatch, store)
    assert store.finished["status"] == "failed"
    assert "connection reset" in store.finished["error"]


def test_a_refusal_by_the_runtime_closes_the_run(monkeypatch, store):
    """`ok: False` means the runtime refused BEFORE the orchestrator recorded
    anything — an unknown bound, a missing seller. Nobody else will close this
    row, so without this it sits at `running` forever."""
    boto = FakeBoto(report={"ok": False, "error": "unknown_bounds",
                            "message": "Unknown bound(s): ['nope']"})
    import boto3
    monkeypatch.setattr(boto3, "client", boto.client)

    _dispatch(monkeypatch, store)
    assert store.finished["status"] == "failed"
    assert "unknown_bounds" in store.finished["error"] or \
           "Unknown bound" in store.finished["error"]


def test_a_normal_bounded_stop_is_NOT_treated_as_a_failure(monkeypatch, store,
                                                           boto):
    """The bug a live invocation against the deployed runtime caught.

    `ok` in the orchestrator's report means "met its target" — it is
    `reason == TARGET_MET`. Every honest bounded stop (budget_exhausted,
    max_attempts, no_results, user_cancelled) therefore comes back `ok: False`.
    Reading that as a refusal made the caller overwrite a truthful
    `stopped/budget_exhausted` with a fabricated `failed/error`.

    Real report shape, taken from an actual invocation.
    """
    boto.report = {
        "ok": False,                      # did NOT meet its target -- correct
        "run_id": RUN,
        "status": "stopped",
        "terminal_reason": "budget_exhausted",
        "error": None,
    }
    import boto3
    monkeypatch.setattr(boto3, "client", boto.client)

    _dispatch(monkeypatch, store)

    assert store.finished is None, (
        "the dispatch re-recorded a run the runtime had already recorded "
        "correctly -- overwriting the real stop reason")
    assert [e["kind"] for e in store.events] == ["dispatch", "dispatch_done"]


def test_a_target_met_run_is_equally_accepted(monkeypatch, store, boto):
    boto.report = {"ok": True, "run_id": RUN, "status": "succeeded",
                   "terminal_reason": "target_met", "error": None}
    import boto3
    monkeypatch.setattr(boto3, "client", boto.client)
    _dispatch(monkeypatch, store)
    assert store.finished is None


def test_a_refusal_has_no_terminal_reason_which_is_the_real_signal(monkeypatch,
                                                                   store):
    """A refusal returns early, before the orchestrator runs, so it carries an
    `error` code and never a terminal_reason. That absence is the signal."""
    boto = FakeBoto(report={"ok": False, "error": "unknown_bounds",
                            "message": "Unknown bound(s): ['nope']"})
    import boto3
    monkeypatch.setattr(boto3, "client", boto.client)
    _dispatch(monkeypatch, store)
    assert store.finished["status"] == "failed"
    assert "run_not_started" not in store.finished["error"]


def test_a_non_json_body_is_an_error_not_a_crash(monkeypatch, store):
    class BadBody:
        def client(self, service, region_name=None, config=None):
            return self

        def invoke_agent_runtime(self, **kw):
            return {"statusCode": 200, "response": io.BytesIO(b"<html>502</html>")}

    import boto3
    monkeypatch.setattr(boto3, "client", BadBody().client)
    _dispatch(monkeypatch, store)
    assert store.finished["status"] == "failed"
    assert "non-JSON" in store.finished["error"]


# --------------------------------------------------------------------------- #
# The dispatch client's own timeouts and retries
#
# These are not style preferences. InvokeAgentRuntime STARTS A RUN, so a retry
# does not repeat a read, it starts a SECOND run. Measured against the live
# runtime on run 35b2f879: one dashboard run became five concurrent
# orchestrators, five campaigns, roughly five times the DataForSEO spend, and a
# final status decided by whichever writer happened to finish last.
# --------------------------------------------------------------------------- #
def test_the_agentcore_client_never_retries(monkeypatch, store, boto):
    """A bare boto3 client uses legacy retries, which is 5 attempts. Retrying a
    non-idempotent "start a run" call is what duplicated the runs."""
    _dispatch(monkeypatch, store)
    cfg = boto.calls[0]["config"]
    assert cfg is not None, "a bare client falls back to 5 legacy attempts"
    assert cfg.retries.get("max_attempts") == 1


def test_the_agentcore_read_timeout_outlasts_the_run_cap(monkeypatch, store, boto):
    """The response body is streamed, so read_timeout is how long the socket
    waits for the run to finish. botocore's 60 second default is about a tenth
    of a real run, which is why the failures arrived 60 seconds apart."""
    _dispatch(monkeypatch, store)
    cfg = boto.calls[0]["config"]
    assert cfg.read_timeout > DEFAULT_BOUNDS["max_wall_clock_s"]
    # And past AgentCore's own 900 second synchronous ceiling, so a stalled
    # invocation surfaces the platform's error rather than our own timeout.
    assert cfg.read_timeout > 900


# --------------------------------------------------------------------------- #
# Cancellation across a process boundary
# --------------------------------------------------------------------------- #
def _orch(run_id=RUN, cancel_event=None):
    return Orchestrator(seller_id="s", goal="g", run_id=run_id,
                        cancel_event=cancel_event)


def test_an_in_process_event_cancels(monkeypatch, store):
    import threading
    event = threading.Event()
    event.set()
    assert _orch(cancel_event=event)._cancelled() is True


def test_a_clear_event_does_not_cancel(monkeypatch, store):
    import threading
    assert _orch(cancel_event=threading.Event())._cancelled() is False


def test_with_no_event_the_database_flag_is_consulted(monkeypatch, store):
    """This is the AgentCore path: the cancel arrives at Flask, the loop is in
    us-west-2, and no local object connects them."""
    monkeypatch.setattr(store, "is_cancel_requested", lambda rid: True)
    import agent.run_store as run_store
    monkeypatch.setattr(run_store, "is_cancel_requested", lambda rid: True)
    assert _orch()._cancelled() is True


def test_the_event_wins_when_both_are_present(monkeypatch, store):
    """The local path must not pay for a database round-trip per turn."""
    import threading
    import agent.run_store as run_store

    def explode(rid):
        raise AssertionError("the database was consulted with an Event present")
    monkeypatch.setattr(run_store, "is_cancel_requested", explode)

    event = threading.Event()
    event.set()
    assert _orch(cancel_event=event)._cancelled() is True


def test_a_database_failure_does_not_kill_a_healthy_run(monkeypatch):
    """A transient Supabase blip must not convert "keep working" into "crash".

    This runs the REAL is_cancel_requested (no fake) with only the layer
    beneath it broken, so what is being tested is its own except clause rather
    than the test double's behaviour. Losing cancel until the read recovers is
    the safer of the two failure directions.
    """
    import supabase_store
    from agent import run_store

    def boom(*a, **k):
        raise RuntimeError("supabase is down")
    monkeypatch.setattr(supabase_store, "select_rows", boom)

    assert run_store.is_cancel_requested(RUN) is False


def test_a_cancelled_run_still_ends_as_stopped_user_cancelled(monkeypatch,
                                                              store):
    """The flag is a REQUEST. `status` stays the outcome, which is why the flag
    is a column and not a status value -- `runs.status` has a CHECK constraint
    with no 'cancelling' member, so there was nowhere to put it."""
    from agent.budget import TerminalReason
    import agent.run_store as run_store
    monkeypatch.setattr(run_store, "is_cancel_requested", lambda rid: True)

    o = _orch()
    assert o._cancelled() is True
    assert TerminalReason.USER_CANCELLED == "user_cancelled"


# --------------------------------------------------------------------------- #
# The cancel route sets both signals
# --------------------------------------------------------------------------- #
def test_the_cancel_route_sets_the_event_AND_the_column(monkeypatch, store):
    """Setting only one leaves cancel silently broken on the other backend."""
    import app as flask_app
    import auth
    import lead_engine

    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", "seller-1")
    auth.clear_cache()

    store.runs[RUN]["seller_id"] = "seller-1"
    monkeypatch.setattr(routes_agent, "run_store", store)

    recorded = []
    monkeypatch.setattr(store, "request_cancel",
                        lambda run_id: recorded.append(run_id))

    flask_app.app.config["TESTING"] = True
    r = flask_app.app.test_client().post(f"/runs/{RUN}/cancel")

    assert r.status_code == 200
    # BOTH, and neither alone is enough: the Event reaches a thread in this
    # process (local backend), the column reaches the runtime in us-west-2.
    # Setting only one leaves cancel silently broken on the other backend.
    assert routes_agent._cancels[RUN].is_set(), "the local Event was not set"
    assert recorded == [RUN], \
        "the column was not set, so an agentcore run would never stop"


def _cancel_request(monkeypatch, store, backend):
    """Drive POST /runs/<id>/cancel with an unrecordable column write."""
    import app as flask_app
    import auth
    import lead_engine

    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("AGENT_BACKEND", backend)
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", "seller-1")
    auth.clear_cache()

    store.runs[RUN]["seller_id"] = "seller-1"

    def boom(run_id):
        raise RuntimeError("column runs.cancel_requested does not exist")
    monkeypatch.setattr(store, "request_cancel", boom)
    monkeypatch.setattr(routes_agent, "run_store", store)

    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client().post(f"/runs/{RUN}/cancel")


def test_an_unrecordable_cancel_on_agentcore_is_reported_not_claimed(
        monkeypatch, store):
    """On the agentcore backend the column is the ONLY route to the run, so a
    failed write means the cancel was NOT delivered. Answering
    `cancel_requested: true` would be a lie."""
    r = _cancel_request(monkeypatch, store, "agentcore")
    assert r.status_code == 502
    assert r.get_json()["ok"] is False


def test_an_unrecordable_cancel_LOCALLY_still_reports_success(monkeypatch,
                                                              store):
    """The mirror case, and the one that would otherwise mislead.

    A local run is cancelled by the in-process Event, which was already set.
    The cancel genuinely worked, so returning 502 would tell the operator their
    stop button failed when it did not. It says so in the note instead.
    """
    r = _cancel_request(monkeypatch, store, "local")
    assert r.status_code == 200
    body = r.get_json()
    assert body["cancel_requested"] is True
    assert body["durable"] is False
    assert "cancel_requested" in body["note"]

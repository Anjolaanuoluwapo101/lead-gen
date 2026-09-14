"""Ranked test #1 in the design spec — the highest-value test in the repo.

Why it outranks everything else: this is not testing our logic, it is testing a
contract we do not control. AgentCore probes GET /ping, and if the body stops
saying exactly {"status": "Healthy"} the runtime is pulled out of service — no
exception, no stack trace, just a deploy that silently stops answering. Nothing
in the codebase would tell us. So /ping is asserted with EXACT dict equality
rather than a substring check: "Healthy" inside a longer string is not Healthy.

The handler is replaced with a stand-in, so these run offline and in
milliseconds — the point is the HTTP contract, not the agent.
"""

import threading

import pytest
from bedrock_agentcore.runtime.models import PingStatus
from starlette.testclient import TestClient

import agentcore_app

REPORT = {
    "ok": True, "run_id": "run-1", "status": "stopped",
    "terminal_reason": "target_met", "progress": {"find_calls": 1},
    "report": {"estimate": True}, "error": None,
}


class _Control:
    """Handle for steering the fake Orchestrator from inside a test."""

    def __init__(self):
        self.kwargs = None
        self.report = REPORT
        self.gate = None          # threading.Event to hold a run open
        self.error = None         # raise this instead of running
        self.started = threading.Event()


@pytest.fixture
def orch(monkeypatch):
    c = _Control()

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            c.kwargs = kwargs
            if c.error:
                raise c.error

        def run(self):
            c.started.set()
            if c.gate:
                c.gate.wait(timeout=5)
            return dict(c.report)

    monkeypatch.setattr(agentcore_app, "Orchestrator", FakeOrchestrator)
    return c


@pytest.fixture
def client():
    return TestClient(agentcore_app.app)


# --- /ping: the contract we do not control -------------------------------- #

def test_ping_reports_healthy_when_idle(client):
    r = client.get("/ping")
    assert r.status_code == 200
    body = r.json()
    # Asserted on the FIELD, not the whole dict: "Healthy" appearing inside a
    # longer or nested value would not satisfy AgentCore.
    assert body["status"] == "Healthy"
    assert isinstance(body["time_of_last_update"], int)


def test_ping_reports_busy_when_the_runtime_is_busy(client):
    """The endpoint must be able to surface HealthyBusy, not just Healthy.

    Note how this is driven: BedrockAgentCoreApp computes busy from its
    registry of active async tasks — NOT from our handler being mid-flight.
    That is not a gap for us, and the next test pins why.
    """
    agentcore_app.app.force_ping_status(PingStatus.HEALTHY_BUSY)
    try:
        assert client.get("/ping").json()["status"] == "HealthyBusy"
    finally:
        agentcore_app.app.clear_forced_ping_status()
    assert client.get("/ping").json()["status"] == "Healthy"


def test_a_synchronous_invocation_keeps_ping_healthy(client, orch):
    """Deliberate documentation of a fact the spec previously got wrong.

    The design spec claimed /ping returns HealthyBusy while a run is in
    progress. It does not: our entrypoint is synchronous, so the run does not
    register an async task, and the OPEN HTTP CONNECTION is what tells
    AgentCore the session is alive. That is why /invocations must block to
    completion — return early and the only liveness signal we have is gone.

    If someone later moves the run onto @app.async_task, this test fails, and
    that is the correct moment to notice.
    """
    orch.gate = threading.Event()
    worker_client = TestClient(agentcore_app.app)
    out = {}

    def call():
        out["r"] = worker_client.post(
            "/invocations", json={"goal": "find dentists", "seller_id": "s1"})

    worker = threading.Thread(target=call)
    worker.start()
    assert orch.started.wait(timeout=5), "invocation never started"

    assert client.get("/ping").json()["status"] == "Healthy"

    orch.gate.set()
    worker.join(timeout=5)
    assert out["r"].status_code == 200


# --- /invocations: the happy path ----------------------------------------- #

def test_invocation_runs_the_orchestrator_and_returns_its_report(client, orch):
    r = client.post("/invocations",
                    json={"goal": "find 5 dentists in Austin", "seller_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["terminal_reason"] == "target_met"
    assert body["run_id"] == "run-1"


def test_invocation_passes_the_goal_and_seller_through(client, orch):
    client.post("/invocations",
                json={"goal": "find 5 dentists", "seller_id": "s1"})
    assert orch.kwargs["seller_id"] == "s1"
    assert orch.kwargs["goal"] == "find 5 dentists"
    assert orch.kwargs["trigger"] == "agentcore"
    assert orch.kwargs["bounds"] is None      # defaults applied downstream


# --- /invocations: refusals are answers, not crashes ---------------------- #

def test_a_missing_goal_is_refused_before_anything_runs(client, orch):
    r = client.post("/invocations", json={"seller_id": "s1"})
    assert r.status_code == 200
    assert r.json()["error"] == "goal_required"
    assert orch.kwargs is None


def test_a_whitespace_only_goal_counts_as_missing(client, orch):
    r = client.post("/invocations", json={"goal": "   ", "seller_id": "s1"})
    assert r.json()["error"] == "goal_required"
    assert orch.kwargs is None


def test_seller_falls_back_to_the_environment(client, orch, monkeypatch):
    monkeypatch.setenv("DEFAULT_SELLER_ID", "from-env")
    client.post("/invocations", json={"goal": "find dentists"})
    assert orch.kwargs["seller_id"] == "from-env"


def test_an_explicit_seller_beats_the_environment(client, orch, monkeypatch):
    monkeypatch.setenv("DEFAULT_SELLER_ID", "from-env")
    client.post("/invocations", json={"goal": "find dentists", "seller_id": "me"})
    assert orch.kwargs["seller_id"] == "me"


def test_with_no_seller_anywhere_the_run_is_refused(client, orch, monkeypatch):
    # Without this the run would write rows with a null owner — data that no
    # dashboard query would ever show, which is worse than an error message.
    monkeypatch.delenv("DEFAULT_SELLER_ID", raising=False)
    r = client.post("/invocations", json={"goal": "find dentists"})
    assert r.json()["error"] == "no_seller"
    assert orch.kwargs is None


def test_an_unknown_bound_is_refused_rather_than_ignored(client, orch):
    # A typo'd cap silently falling back to its default reads as "the limit I
    # set was ignored" — the exact failure this project exists to avoid.
    r = client.post("/invocations", json={
        "goal": "find dentists", "seller_id": "s1",
        "bounds": {"max_fnd_calls": 3}})
    assert r.json()["error"] == "unknown_bounds"
    assert "max_fnd_calls" in r.json()["message"]
    assert orch.kwargs is None


def test_real_bounds_are_passed_through(client, orch):
    client.post("/invocations", json={
        "goal": "find dentists", "seller_id": "s1",
        "bounds": {"max_find_calls": 3}})
    assert orch.kwargs["bounds"] == {"max_find_calls": 3}


def test_a_failed_run_returns_a_report_not_a_500(client, orch):
    """A 500 tells AgentCore nothing and the reason is lost.

    Reaching this path means the run could not even be created — Supabase
    unconfigured, or the agent schema not applied. That is worth reporting
    plainly, in the same shape as every other answer.
    """
    orch.error = RuntimeError("could not create a run row")
    r = client.post("/invocations", json={"goal": "find dentists", "seller_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "run_not_started"
    assert "could not create a run row" in body["message"]


def test_a_nonsense_payload_is_refused(client, orch):
    # AgentCore passes through whatever the caller sent; a string or a list must
    # not blow up the handler.
    r = client.post("/invocations", json=["not", "a", "dict"])
    assert r.status_code == 200
    assert r.json()["error"] == "goal_required"


# --------------------------------------------------------------------------- #
# Payload shapes — the `agentcore invoke` envelope.
#
# These exist because the CLI cost a live debugging cycle: it does NOT send
# {"goal": ...}; it sends {"prompt": "<string>", "agent": ..., "runtimeArn": ...}.
# The entrypoint originally only read "goal", so every CLI invocation was
# rejected as goal_required no matter what was passed — which looks like a
# broken agent, not a payload mismatch. The CLI is the documented way to
# smoke-test a deployment, so it must work.
# --------------------------------------------------------------------------- #

def test_the_cli_envelope_is_understood(client, orch):
    """Exactly what `agentcore invoke --prompt '{"goal": ...}'` puts on the wire."""
    r = client.post("/invocations", json={
        "timestamp": "2026-09-11T08:47:01.911Z",
        "agent": "leadgen",
        "runtimeArn": "arn:aws:bedrock-agentcore:us-west-2:123:runtime/x",
        "region": "us-west-2",
        "prompt": '{"goal": "find qualified plumbers in Austin, TX"}',
    })
    assert r.json()["ok"] is True
    assert orch.kwargs["goal"] == "find qualified plumbers in Austin, TX"


def test_the_cli_envelope_carries_a_seller_through(client, orch):
    client.post("/invocations", json={
        "prompt": '{"goal": "find dentists", "seller_id": "seller-42"}'})
    assert orch.kwargs["seller_id"] == "seller-42"


def test_a_bare_prompt_string_is_treated_as_the_goal(client, orch):
    """`agentcore invoke "find plumbers in Austin"` should read as the goal."""
    r = client.post("/invocations", json={"prompt": "find plumbers in Austin"})
    assert r.json()["ok"] is True
    assert orch.kwargs["goal"] == "find plumbers in Austin"


def test_a_direct_goal_payload_still_wins_over_a_prompt(client, orch):
    """The contract shape is checked first; adding "prompt" must not break it."""
    client.post("/invocations", json={
        "goal": "the real goal", "prompt": "a decoy"})
    assert orch.kwargs["goal"] == "the real goal"


def test_a_prompt_of_malformed_json_is_used_as_the_goal(client, orch):
    """Braces but broken JSON must not be silently swallowed into an empty run."""
    r = client.post("/invocations", json={"prompt": '{"goal": "unterminated'})
    assert r.json()["ok"] is True
    assert orch.kwargs["goal"] == '{"goal": "unterminated'


def test_an_envelope_with_neither_goal_nor_prompt_is_still_refused(client, orch):
    r = client.post("/invocations", json={"agent": "leadgen", "region": "us-west-2"})
    assert r.status_code == 200
    assert r.json()["error"] == "goal_required"
    assert orch.kwargs is None


# --- run_id: the runtime ADOPTS the caller's row, or opens its own --------- #
# The dashboard creates the run row before dispatching so it can answer with an
# id before any work starts. If this handler ignores that id and opens its own,
# every event lands on a row the browser only learns about once the whole run
# has already finished -- no progress bar, no event log, no live stop reason.

RUN = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def runs(monkeypatch):
    """Just enough run_store for the run_id checks."""
    from agent import run_store
    monkeypatch.setattr(run_store, "get_run",
                        lambda rid: {"id": rid} if rid == RUN else None)
    return run_store


def test_without_a_run_id_the_runtime_opens_its_own(client, orch, runs):
    """The n8n / `agentcore invoke` path: no caller row to adopt."""
    client.post("/invocations", json={"goal": "find dentists", "seller_id": "s1"})
    assert orch.kwargs["run_id"] is None


def test_a_known_run_id_is_adopted(client, orch, runs):
    client.post("/invocations",
                json={"goal": "find dentists", "seller_id": "s1", "run_id": RUN})
    assert orch.kwargs["run_id"] == RUN


def test_a_malformed_run_id_is_refused_before_anything_runs(client, orch, runs):
    """`runs.id` is a uuid column: a malformed value would come back as a
    Postgres 22P02 from deep inside the first write, not as a usable message."""
    r = client.post("/invocations",
                    json={"goal": "find dentists", "seller_id": "s1",
                          "run_id": "not-a-uuid"})
    assert r.json()["error"] == "bad_run_id"
    assert orch.kwargs is None


def test_an_unknown_run_id_is_refused_rather_than_orphaning_events(client, orch,
                                                                   runs):
    """Accepting it would write events against a row that does not exist."""
    r = client.post("/invocations",
                    json={"goal": "find dentists", "seller_id": "s1",
                          "run_id": "22222222-2222-2222-2222-222222222222"})
    assert r.json()["error"] == "run_not_found"
    assert orch.kwargs is None

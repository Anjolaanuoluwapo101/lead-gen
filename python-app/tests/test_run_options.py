"""Run options chosen on the dashboard: scrutiny, niche, target, one search.

Each option is accepted on POST /runs, carried to the orchestrator (or the
AgentCore payload), and honoured by find_leads. Junk is refused at the HTTP
layer; the orchestrator coerces whatever slips through to the safe default.
"""

import threading
from types import SimpleNamespace

import app as flask_app
import auth
import lead_engine
import routes_agent

SELLER = "seller-1"


class SyncThread:
    def __init__(self, target=None, args=(), name=None, daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class FakeRunStore:
    def create_run(self, **kw):
        return {"id": "run-1", "status": "running"}


class FakeOrchestrator:
    last = None

    def __init__(self, **kwargs):
        FakeOrchestrator.last = kwargs

    def run(self):
        return {"ok": True}


def _post(body, monkeypatch):
    """POST /runs with the orchestrator replaced by a recorder."""
    flask_app.app.config["TESTING"] = True
    monkeypatch.setattr(routes_agent, "run_store", FakeRunStore())
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(routes_agent, "threading",
                         SimpleNamespace(Thread=SyncThread,
                                         Event=threading.Event))
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", SELLER)
    auth.clear_cache()
    try:
        return flask_app.app.test_client().post("/runs", json=body)
    finally:
        auth.clear_cache()


# --------------------------------------------------------------------------- #
# scrutiny, target and niche are all REQUIRED (no silent defaults)
# --------------------------------------------------------------------------- #
def _valid(**over):
    """A start body satisfying the whole contract; overrides win."""
    body = {"goal": "g", "scrutiny": "lenient", "target_qualified": 5,
            "niche": "web design", "location_code": 1010294}
    body.update(over)
    return body


def test_scrutiny_reaches_the_orchestrator(monkeypatch):
    r = _post(_valid(scrutiny="lenient"), monkeypatch)
    assert r.status_code == 200
    assert FakeOrchestrator.last["scrutiny"] == "lenient"


def test_an_unknown_scrutiny_is_400(monkeypatch):
    r = _post(_valid(scrutiny="ruthless"), monkeypatch)
    assert r.status_code == 400


def test_a_missing_scrutiny_is_400_not_a_default(monkeypatch):
    body = _valid()
    del body["scrutiny"]
    r = _post(body, monkeypatch)
    assert r.status_code == 400
    assert "scrutiny" in r.get_json()["error"]


def test_find_leads_passes_the_run_scrutiny_to_the_engine(monkeypatch):
    import agent.tools as T
    from agent.budget import Budget

    captured = {}

    def fake_campaign(**kw):
        captured.update(kw)
        return {"ok": True, "campaign_id": "c1", "found": 1, "qualified": 0,
                "scored": 1, "stored": True, "leads": [], "errors": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget(), "r", seller_id="s", scrutiny="balanced")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    tools["find_leads"](keyword="k", place="p")
    assert captured["scrutiny"] == "balanced"


def test_an_explicit_scrutiny_beats_the_run_default(monkeypatch):
    import agent.tools as T
    from agent.budget import Budget

    captured = {}

    def fake_campaign(**kw):
        captured.update(kw)
        return {"ok": True, "campaign_id": "c1", "found": 1, "qualified": 0,
                "scored": 1, "stored": True, "leads": [], "errors": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget(), "r", seller_id="s", scrutiny="strict")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    tools["find_leads"](keyword="k", place="p", scrutiny="lenient")
    assert captured["scrutiny"] == "lenient"


# --------------------------------------------------------------------------- #
# niche
# --------------------------------------------------------------------------- #
def test_niche_reaches_the_orchestrator(monkeypatch):
    r = _post(_valid(niche="web design for bakeries"), monkeypatch)
    assert r.status_code == 200
    assert FakeOrchestrator.last["niche"] == "web design for bakeries"


def test_a_missing_niche_is_400_when_nothing_is_on_file(monkeypatch):
    import seller_ops
    monkeypatch.setattr(
        seller_ops, "get_seller",
        lambda sid: (_ for _ in ()).throw(
            seller_ops.OpsError("not_found", "seller not found")))
    body = _valid()
    del body["niche"]
    r = _post(body, monkeypatch)
    assert r.status_code == 400
    assert "niche" in r.get_json()["error"]


def test_a_missing_niche_passes_when_the_profile_has_one(monkeypatch):
    import seller_ops
    monkeypatch.setattr(seller_ops, "get_seller",
                        lambda sid: {"id": sid, "niche": "profile niche"})
    body = _valid()
    del body["niche"]
    r = _post(body, monkeypatch)
    assert r.status_code == 200
    assert FakeOrchestrator.last["niche"] is None


def test_a_missing_niche_passes_when_the_campaign_has_one(monkeypatch):
    import leads_read
    import seller_ops
    monkeypatch.setattr(
        seller_ops, "get_seller",
        lambda sid: (_ for _ in ()).throw(
            seller_ops.OpsError("not_found", "seller not found")))
    monkeypatch.setattr(leads_read, "read_campaign_niche",
                        lambda cid: ("campaign niche", "s"))
    body = _valid()
    del body["niche"]
    body["campaign_id"] = "11111111-2222-3333-4444-555555555555"
    r = _post(body, monkeypatch)
    assert r.status_code == 200


def test_a_db_blip_does_not_400_a_runnable_run(monkeypatch):
    import seller_ops
    monkeypatch.setattr(
        seller_ops, "get_seller",
        lambda sid: (_ for _ in ()).throw(
            seller_ops.OpsError("upstream", "database down")))
    body = _valid()
    del body["niche"]
    r = _post(body, monkeypatch)
    # Fail open: the engine's dev-default warning covers a genuinely missing
    # niche loudly on the run page, which a 400 on a read stumble would not.
    assert r.status_code == 200


def test_the_run_niche_beats_the_model_guessed_one(monkeypatch):
    import agent.tools as T
    from agent.budget import Budget

    captured = {}

    def fake_campaign(**kw):
        captured.update(kw)
        return {"ok": True, "campaign_id": "c1", "found": 1, "qualified": 0,
                "scored": 1, "stored": True, "leads": [], "errors": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget(), "r", seller_id="s",
                       niche="web design for bakeries")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    tools["find_leads"](keyword="k", place="p", niche="dentist marketing")
    assert captured["niche"] == "web design for bakeries"


# --------------------------------------------------------------------------- #
# target_qualified via bounds
# --------------------------------------------------------------------------- #
def test_a_target_reaches_the_bounds_the_run_started_with(monkeypatch):
    r = _post(_valid(target_qualified=5), monkeypatch)
    assert r.status_code == 200
    assert FakeOrchestrator.last["bounds"]["target_qualified"] == 5


def test_a_non_integer_target_is_400(monkeypatch):
    assert _post(_valid(target_qualified="lots"),
                 monkeypatch).status_code == 400
    assert _post(_valid(target_qualified=0),
                 monkeypatch).status_code == 400


def test_without_a_target_the_run_does_not_start(monkeypatch):
    # The old default of 10 was prose nobody chose ("Find 5" in the goal
    # while the loop aimed for 10). An omitted count is a 400, not a guess.
    body = _valid()
    del body["target_qualified"]
    r = _post(body, monkeypatch)
    assert r.status_code == 400
    assert "target_qualified" in r.get_json()["error"]


# --------------------------------------------------------------------------- #
# single search
# --------------------------------------------------------------------------- #
def _no_db_run(monkeypatch):
    """Stub the run row creation: these tests assert on the orchestrator's
    in-memory wiring, not on Supabase (which the suite keeps off-network)."""
    import agent.run_store as run_store
    monkeypatch.setattr(run_store, "create_run",
                        lambda **k: {"id": "run-1", **k})


def test_single_search_defaults_on_for_a_pinned_run(monkeypatch):
    import agent.orchestrator as O
    _no_db_run(monkeypatch)
    orch = O.Orchestrator(seller_id="s", goal="g", location_code=1010294,
                          agent_factory=lambda ctx: (lambda prompt: None))
    assert orch.single_search is True
    assert orch.ctx.single_search is True


def test_single_search_defaults_off_without_a_pin(monkeypatch):
    import agent.orchestrator as O
    _no_db_run(monkeypatch)
    orch = O.Orchestrator(seller_id="s", goal="g",
                          agent_factory=lambda ctx: (lambda prompt: None))
    assert orch.single_search is False


def test_an_explicit_false_overrides_the_pin_default(monkeypatch):
    r = _post(_valid(location_code=1010294, single_search=False),
              monkeypatch)
    assert r.status_code == 200
    assert FakeOrchestrator.last["single_search"] is False


def test_a_non_boolean_single_search_is_400(monkeypatch):
    assert _post(_valid(single_search="sometimes"),
                 monkeypatch).status_code == 400


def test_the_second_search_in_single_mode_is_refused_free(monkeypatch):
    import agent.tools as T
    from agent.budget import Budget

    calls = []

    def fake_campaign(**kw):
        calls.append(kw)
        return {"ok": True, "campaign_id": "c1", "found": 2,
                "with_website": 2, "qualified": 0, "scored": 2,
                "stored": True, "leads": [], "errors": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget({"max_find_calls": 5}), "r", seller_id="s",
                       single_search=True)
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    first = tools["find_leads"](keyword="bakery", place="Lagos")
    assert first["ok"] is True
    second = tools["find_leads"](keyword="plumbers", place="Lagos")
    assert second["ok"] is False
    assert second["error"] == "single_search"
    assert len(calls) == 1, "the refused search ran the engine"
    assert ctx.budget.find_calls == 1, "the refusal charged credit"


def test_the_prompt_names_the_single_search_rule(monkeypatch):
    import agent.orchestrator as O
    _no_db_run(monkeypatch)
    orch = O.Orchestrator(seller_id="s", goal="g", location_code=1010294,
                          agent_factory=lambda ctx: (lambda prompt: None))
    assert "single_search" in orch._prompt()


def test_the_prompt_names_the_scrutiny(monkeypatch):
    import agent.orchestrator as O
    _no_db_run(monkeypatch)
    orch = O.Orchestrator(seller_id="s", goal="g", scrutiny="lenient",
                          agent_factory=lambda ctx: (lambda prompt: None))
    assert "lenient" in orch._prompt()


# --------------------------------------------------------------------------- #
# agentcore carries the options too
# --------------------------------------------------------------------------- #
def test_agentcore_options_reach_the_orchestrator():
    from starlette.testclient import TestClient

    import agentcore_app

    seen = {}

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self):
            return {"ok": True}

    agentcore_app.Orchestrator = FakeOrchestrator
    try:
        TestClient(agentcore_app.app).post(
            "/invocations",
            json={"goal": "g", "seller_id": "s1", "scrutiny": "balanced",
                  "niche": "web design", "single_search": True,
                  "location_code": 1010294})
    finally:
        import agent.orchestrator as O
        agentcore_app.Orchestrator = O.Orchestrator
    assert seen["scrutiny"] == "balanced"
    assert seen["niche"] == "web design"
    assert seen["single_search"] is True


def test_agentcore_coerces_a_bad_scrutiny_to_default():
    from starlette.testclient import TestClient

    import agentcore_app

    seen = {}

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self):
            return {"ok": True}

    agentcore_app.Orchestrator = FakeOrchestrator
    try:
        TestClient(agentcore_app.app).post(
            "/invocations",
            json={"goal": "g", "seller_id": "s1", "scrutiny": "ruthless"})
    finally:
        import agent.orchestrator as O
        agentcore_app.Orchestrator = O.Orchestrator
    assert seen["scrutiny"] is None


# --------------------------------------------------------------------------- #
# stage events: the engine narrates search/found/enrich/scored to the trace
# --------------------------------------------------------------------------- #
def _campaign_with_stages(monkeypatch, events, fail_find=False):
    import agent.tools as T
    import lead_engine
    from agent import run_store

    def fake_campaign(**kw):
        cb = kw.get("progress_cb")
        if fail_find:
            raise RuntimeError("source exploded")
        assert callable(cb), "find_leads must pass a stage callback"
        cb("search", "Searching for 'k' in p")
        cb("found", "Found 1 businesses, 1 with websites")
        cb("enrich", "Crawling 1 business site and scoring them")
        cb("scored", "Enriched 1, scored 1, qualified 0")
        return {"ok": True, "campaign_id": "c1", "found": 1, "qualified": 0,
                "scored": 1, "stored": True, "leads": [], "errors": [],
                "warnings": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    monkeypatch.setattr(run_store, "append_event",
                        lambda rid, kind, msg=None, payload=None:
                        events.append((rid, kind, msg)) or {})
    return T


def test_find_leads_appends_each_stage_to_the_trace(monkeypatch):
    import agent.tools as T
    from agent.budget import Budget

    events = []
    _campaign_with_stages(monkeypatch, events)
    ctx = T.RunContext(Budget(), "run-9", seller_id="s")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    out = tools["find_leads"](keyword="k", place="p")
    assert out["ok"] is True
    assert [kind for _, kind, _ in events] == [
        "search", "found", "enrich", "scored"]
    assert all(rid == "run-9" for rid, _, _ in events)
    assert events[0][2] == "Searching for 'k' in p"


def test_a_failing_trace_write_never_fails_the_find(monkeypatch):
    import agent.tools as T
    import lead_engine
    from agent import run_store
    from agent.budget import Budget

    def fake_campaign(**kw):
        kw["progress_cb"]("search", "hi")
        return {"ok": True, "campaign_id": "c1", "found": 1, "qualified": 0,
                "scored": 1, "stored": True, "leads": [], "errors": [],
                "warnings": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)

    def boom(*a, **k):
        raise RuntimeError("supabase blip")

    monkeypatch.setattr(run_store, "append_event", boom)
    ctx = T.RunContext(Budget(), "run-9", seller_id="s")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    out = tools["find_leads"](keyword="k", place="p")
    assert out["ok"] is True
    assert out["found"] == 1


def test_a_failed_find_emits_no_stages_and_reports_cleanly(monkeypatch):
    import agent.tools as T
    from agent.budget import Budget

    events = []
    _campaign_with_stages(monkeypatch, events, fail_find=True)
    ctx = T.RunContext(Budget(), "run-9", seller_id="s")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    out = tools["find_leads"](keyword="k", place="p")
    assert out == {"ok": False, "error": "find_failed",
                   "message": "source exploded"}
    assert events == []


# --------------------------------------------------------------------------- #
# engine warnings reach the report and the live trace
# --------------------------------------------------------------------------- #
def test_find_leads_surfaces_engine_warnings(monkeypatch):
    import agent.tools as T
    from agent.budget import Budget

    def fake_campaign(**kw):
        return {"ok": True, "campaign_id": "c1", "found": 1, "qualified": 0,
                "scored": 1, "stored": True, "leads": [], "errors": [],
                "warnings": ["falling back to the DEV placeholder"]}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget(), "r", seller_id="s")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    out = tools["find_leads"](keyword="k", place="p")
    assert out["warnings"] == ["falling back to the DEV placeholder"]
    assert ctx.warnings == ["falling back to the DEV placeholder"]


def test_warnings_are_deduplicated_and_traced(monkeypatch):
    import agent.tools as T
    from agent import run_store
    from agent.budget import Budget

    events = []
    monkeypatch.setattr(run_store, "append_event",
                        lambda rid, kind, msg=None, payload=None:
                        events.append((kind, msg)) or {})

    calls = []

    def fake_campaign(**kw):
        calls.append(kw)
        return {"ok": True, "campaign_id": "c1", "found": 1, "qualified": 0,
                "scored": 1, "stored": True, "leads": [], "errors": [],
                "warnings": ["same warning"]}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget(), "r", seller_id="s")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    # Varying keywords so the find cache does not replay the first answer.
    tools["find_leads"](keyword="k1", place="p")
    tools["find_leads"](keyword="k2", place="p")
    assert ctx.warnings == ["same warning"]
    assert events == [("warning", "same warning")]


def test_the_report_carries_the_collected_warnings(monkeypatch):
    import agent.orchestrator as O
    import agent.run_store as run_store

    _no_db_run(monkeypatch)
    monkeypatch.setattr(run_store, "append_event",
                        lambda *a, **k: {})
    monkeypatch.setattr(run_store, "update_progress",
                        lambda *a, **k: {})
    finished = {}
    monkeypatch.setattr(run_store, "finish_run",
                        lambda rid, **k: finished.update(k) or k)

    import agent.tools as T
    import lead_engine as le

    def fake_campaign(**kw):
        return {"ok": True, "campaign_id": "c1", "found": 1, "qualified": 0,
                "scored": 1, "stored": True, "leads": [], "errors": [],
                "warnings": ["falling back to the DEV placeholder"]}

    monkeypatch.setattr(le, "run_campaign", fake_campaign)

    def factory(ctx):
        tools = {t.tool_name: t for t in T.build_tools(ctx)}

        def agent(prompt):
            tools["find_leads"](keyword="k", place="p")

        return agent

    O.Orchestrator(seller_id="s", goal="g",
                   bounds={"max_find_calls": 1, "target_qualified": 99},
                   agent_factory=factory).run()
    report = finished["report"]
    assert report["warnings"] == ["falling back to the DEV placeholder"]


def test_the_run_page_labels_warnings():
    flask_app.app.config["TESTING"] = True
    body = flask_app.app.test_client().get(
        "/runs/11111111-2222-3333-4444-555555555555",
        headers={"Accept": "text/html"}).get_data(as_text=True)
    assert 'warnings:' in body and '"Warnings"' in body


# --------------------------------------------------------------------------- #
# dashboard copy: the goal carries no place anymore
# --------------------------------------------------------------------------- #
def test_the_dashboard_goal_and_chips_name_no_place():
    flask_app.app.config["TESTING"] = True
    body = flask_app.app.test_client().get("/").get_data(as_text=True)
    assert 'value="Find 5 bakeries with no website"' in body
    for place in ("Lagos, Nigeria", "Chicago, IL", "Miami, FL"):
        assert place not in body, f"dashboard still names {place}"
    assert 'id="scrutiny"' in body
    assert 'id="target"' in body
    assert 'id="niche"' in body
    # No silent defaults: strictness starts unchosen, the count starts
    # unfilled, and the submitter says plainly what each missing input costs.
    assert '<option value="" selected>Choose strictness</option>' in body
    assert 'value="10"' not in body
    assert "Choose a shortlist strictness" in body
    assert "how many qualified leads to find" in body
    assert "scores against a placeholder" in body


def test_the_run_page_lists_prospects():
    flask_app.app.config["TESTING"] = True
    body = flask_app.app.test_client().get(
        "/runs/11111111-2222-3333-4444-555555555555",
        headers={"Accept": "text/html"}).get_data(as_text=True)
    assert 'id="prospects-body"' in body
    assert "/prospects/list" in body

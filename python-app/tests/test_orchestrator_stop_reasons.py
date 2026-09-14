"""Ranked test #2 in the design spec, and the evidence for the video's central
claim: the bounds are enforced in CODE, not by the model.

Each test drives an agent that has been deliberately built to misbehave — one of
them never stops. If the caps were only stated in the system prompt, the
stubborn-agent test would loop forever. It terminates, which is the point.

That is also why the sandbox is worth stating plainly: this proves termination
for a model that IGNORES the budget warnings. It does not prove anything about
a model that follows them, because that case was never in doubt.
"""

import pytest

import agent.orchestrator as O
import lead_engine
from agent.budget import TerminalReason
from agent.tools import build_tools


@pytest.fixture
def recorded(monkeypatch):
    """Capture run_store traffic so the loop can be tested without Supabase."""
    box = {"events": [], "finished": None, "progress": []}

    import agent.run_store as run_store
    monkeypatch.setattr(run_store, "create_run",
                        lambda **k: {"id": "run-1", **k})
    monkeypatch.setattr(run_store, "append_event",
                        lambda rid, kind, msg=None, payload=None:
                        box["events"].append((kind, msg)) or {})
    monkeypatch.setattr(run_store, "update_progress",
                        lambda rid, prog, spend=None:
                        box["progress"].append(prog) or {})
    monkeypatch.setattr(run_store, "finish_run",
                        lambda rid, **k: box.update(finished=k) or k)
    return box


def _campaign_result(qualified=0, campaign_id="c1"):
    return {"ok": True, "campaign_id": campaign_id, "found": 5,
            "with_website": 3, "qualified": qualified, "scored": 2,
            "stored": True, "leads": [], "errors": []}


def _stubborn_agent(call):
    """An agent that does `call` every single turn and never decides to stop."""
    def factory(ctx):
        tools = {t.tool_name: t for t in build_tools(ctx)}

        def agent(prompt):
            call(tools)
        return agent
    return factory


def _search(tools):
    tools["find_leads"](keyword="dentist", place="Austin, TX")


def _varying_search():
    """A stubborn agent that still spends: a NEW search every turn.

    find_leads replays an IDENTICAL search for free (see the find cache), so a
    stubborn agent repeating one call never reaches the find cap — the idle
    detector ends it as a dead end instead. Tests that mean to exhaust the
    find cap, or touch several campaigns, must vary the search per turn, which
    is what this returns.
    """
    n = [0]

    def call(tools):
        n[0] += 1
        tools["find_leads"](keyword=f"dentist-{n[0]}", place="Austin, TX")
    return call


# --- the caps hold against an agent that ignores them --------------------- #

def test_stubborn_agent_terminates_at_the_find_cap(monkeypatch, recorded):
    monkeypatch.setattr(lead_engine, "run_campaign",
                        lambda **k: _campaign_result(qualified=0))
    turns = []
    # Built ONCE: the counter must survive across turns, or every turn repeats
    # dentist-1 and the cache ends this as a dead end instead of exhausting
    # the cap — which is the other test below, not this one.
    search = _varying_search()
    orch = O.Orchestrator(
        seller_id="s", goal="find leads",
        bounds={"max_find_calls": 3, "target_qualified": 10},
        agent_factory=_stubborn_agent(
            lambda tools: (turns.append(1), search(tools))))
    out = orch.run()

    assert out["terminal_reason"] == TerminalReason.BUDGET_EXHAUSTED
    assert out["progress"]["find_calls"] == 3
    assert len(turns) == 3          # TERMINATED — the whole assertion
    assert out["status"] == "stopped"


def test_stubborn_agent_terminates_at_the_turn_cap(monkeypatch, recorded):
    # Find credit is plentiful; the turn cap is what must stop it.
    monkeypatch.setattr(lead_engine, "run_campaign",
                        lambda **k: _campaign_result(qualified=0))
    orch = O.Orchestrator(
        seller_id="s", goal="find leads",
        bounds={"max_agent_turns": 2, "max_find_calls": 999,
                "target_qualified": 10},
        agent_factory=_stubborn_agent(_search))
    out = orch.run()
    assert out["terminal_reason"] == TerminalReason.MAX_ATTEMPTS
    assert out["progress"]["agent_turns"] == 2


def test_agent_that_does_nothing_is_called_a_dead_end(recorded):
    orch = O.Orchestrator(
        seller_id="s", goal="find leads",
        agent_factory=_stubborn_agent(lambda tools: None))
    out = orch.run()
    assert out["terminal_reason"] == TerminalReason.NO_RESULTS


def test_an_exploding_agent_fails_the_run_instead_of_raising(recorded):
    def boom(prompt):
        raise RuntimeError("bedrock throttled")
    orch = O.Orchestrator(seller_id="s", goal="find leads",
                          agent_factory=lambda ctx: boom)
    out = orch.run()          # must NOT raise
    assert out["terminal_reason"] == TerminalReason.ERROR
    assert out["status"] == "failed"
    assert "bedrock throttled" in out["error"]


# --- the goal stops it early, and cheaply ---------------------------------- #

def test_target_reached_stops_before_spending_more(monkeypatch, recorded):
    monkeypatch.setattr(lead_engine, "run_campaign",
                        lambda **k: _campaign_result(qualified=5))
    turns = []
    orch = O.Orchestrator(
        seller_id="s", goal="find leads",
        bounds={"target_qualified": 5, "max_find_calls": 6},
        agent_factory=_stubborn_agent(lambda tools: (turns.append(1), _search(tools))))
    out = orch.run()

    assert out["terminal_reason"] == TerminalReason.TARGET_MET
    assert out["status"] == "succeeded"
    assert len(turns) == 1              # stopped the moment the goal was met
    assert out["progress"]["find_calls"] == 1


# --- every run says why it stopped ---------------------------------------- #

@pytest.mark.parametrize("bounds,factory_kind,expected", [
    ({"max_find_calls": 1, "target_qualified": 99}, "search",
     TerminalReason.BUDGET_EXHAUSTED),
    ({"max_agent_turns": 1, "max_find_calls": 99, "target_qualified": 99},
     "search", TerminalReason.MAX_ATTEMPTS),
    ({}, "idle", TerminalReason.NO_RESULTS),
])
def test_every_stop_is_recorded_with_a_reason(monkeypatch, recorded, bounds,
                                              factory_kind, expected):
    monkeypatch.setattr(lead_engine, "run_campaign",
                        lambda **k: _campaign_result(qualified=0))
    call = _search if factory_kind == "search" else (lambda tools: None)
    orch = O.Orchestrator(seller_id="s", goal="g", bounds=bounds,
                          agent_factory=_stubborn_agent(call))
    orch.run()

    assert recorded["finished"]["terminal_reason"] == expected
    assert recorded["finished"]["report"]["estimate"] is True
    assert ("stop", expected) in recorded["events"]


# --------------------------------------------------------------------------- #
# The report must name every campaign the run touched, not just the last one.
#
# A live run made several find calls, drafted three emails against its FIRST
# campaign, and reported the LAST one — which was empty. Anyone using
# report["campaign_id"] to find the run's output found nothing, and the report
# looked perfectly healthy. These tests pin that down.
# --------------------------------------------------------------------------- #
def test_an_agent_repeating_one_search_stops_as_a_dead_end(recorded,
                                                          monkeypatch):
    """Identical repeats are free replays, so a loop that never varies its
    search makes no progress: the idle detector ends it, spending one search.
    Termination holds — by a different, truer reason than budget exhaustion."""
    monkeypatch.setattr(lead_engine, "run_campaign",
                        lambda **k: _campaign_result(qualified=0))
    turns = []
    orch = O.Orchestrator(
        seller_id="s", goal="find leads",
        bounds={"max_find_calls": 3, "target_qualified": 10},
        agent_factory=_stubborn_agent(
            lambda tools: (turns.append(1), _search(tools))))
    out = orch.run()

    assert out["terminal_reason"] == TerminalReason.NO_RESULTS
    assert out["progress"]["find_calls"] == 1
    assert out["status"] == "stopped"


def test_report_keeps_every_campaign_not_just_the_last(recorded, monkeypatch):
    ids = iter(["c1", "c2", "c3"])
    monkeypatch.setattr(lead_engine, "run_campaign",
                        lambda **k: _campaign_result(campaign_id=next(ids)))

    turns = []
    search = _varying_search()

    def counting_search(tools):
        turns.append(1)
        search(tools)

    orch = O.Orchestrator(seller_id="s", goal="g", bounds={"max_find_calls": 3},
                          agent_factory=_stubborn_agent(counting_search))
    out = orch.run()

    report = out["report"]
    # The last campaign stays available for "where to read next"...
    assert report["campaign_id"] == "c3"
    # ...but the durable record is every campaign the run created, in order.
    assert report["campaign_ids"] == ["c1", "c2", "c3"]


def test_a_seeded_campaign_id_is_in_the_report(recorded, monkeypatch):
    # A run handed a campaign up front must not forget it: that is the campaign
    # its drafts may land in even if a later find call creates another.
    monkeypatch.setattr(lead_engine, "run_campaign",
                        lambda **k: _campaign_result(campaign_id="c-new"))
    orch = O.Orchestrator(seller_id="s", goal="g", campaign_id="c-seed",
                          bounds={"max_find_calls": 1},
                          agent_factory=_stubborn_agent(_search))
    report = orch.run()["report"]
    assert report["campaign_ids"] == ["c-seed", "c-new"]
    assert report["campaign_id"] == "c-new"


def test_a_repeated_campaign_is_not_listed_twice(recorded, monkeypatch):
    # run_campaign can return an EXISTING campaign for a repeat keyword/place,
    # so the same id arrives more than once and the list must stay a set.
    monkeypatch.setattr(lead_engine, "run_campaign",
                        lambda **k: _campaign_result(campaign_id="c1"))
    orch = O.Orchestrator(seller_id="s", goal="g", bounds={"max_find_calls": 3},
                          agent_factory=_stubborn_agent(_search))
    report = orch.run()["report"]
    assert report["campaign_ids"] == ["c1"]


def test_a_run_that_never_searched_reports_no_campaigns(recorded):
    orch = O.Orchestrator(seller_id="s", goal="g",
                          agent_factory=_stubborn_agent(lambda tools: None))
    report = orch.run()["report"]
    assert report["campaign_ids"] == []
    assert report["campaign_id"] is None


def test_refuses_to_run_unrecorded(recorded, monkeypatch):
    # create_run returning no id means there is nothing to attach a reason to,
    # so the run must refuse rather than proceed unrecorded.
    import agent.run_store as run_store
    monkeypatch.setattr(run_store, "create_run", lambda **k: {})
    with pytest.raises(RuntimeError, match="run row"):
        O.Orchestrator(seller_id="s", goal="g",
                       agent_factory=_stubborn_agent(lambda tools: None))

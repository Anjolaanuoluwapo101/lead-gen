"""Two load-bearing rules live in tools.py:

1. Tools return COMPACT dicts, never the engine's full leads[].
2. A refused call costs nothing — charge() runs before any work.
"""

import json

import pytest

import agent.tools as T
import lead_engine
import supabase_store
from agent.budget import Budget


def _tools(ctx):
    return {getattr(t, "tool_name", None): t for t in T.build_tools(ctx)}


# --- rule 1: compact returns ---------------------------------------------- #

def test_digest_ranks_by_opportunity_and_caps():
    leads = [{"business_name": n, "opportunity_score": s} for n, s in
             [("A", 10), ("B", 90), ("C", 50), ("D", 70), ("E", 20),
              ("F", 99), ("G", 5)]]
    d = T._digest(leads)
    assert [x["business_name"] for x in d] == ["F", "B", "D", "C", "E"]
    assert len(d) == T.DIGEST_LIMIT


def test_digest_never_leaks_lead_ids():
    # Ids only exist on persisted rows, so drafting must go through
    # read_leads_tool — otherwise the agent could draft against an unstored lead.
    d = T._digest([{"id": "lead-1", "prospect_id": "p", "opportunity_score": 1}])
    assert "lead_id" not in d[0]
    assert "id" not in d[0]


def test_digest_truncates_long_weakness():
    d = T._digest([{"weakness": "x" * 500, "opportunity_score": 1}])
    assert len(d[0]["weakness"]) == T.WEAKNESS_CHARS


def test_digest_tolerates_empty_and_none():
    assert T._digest([]) == []
    assert T._digest(None) == []


def test_first_email_handles_every_shape():
    assert T._first_email({"emails": ["a@b.com"]}) == "a@b.com"
    assert T._first_email({"emails": '["c@d.com"]'}) == "c@d.com"
    assert T._first_email({"emails": "not-json"}) is None
    assert T._first_email({"emails": []}) is None
    assert T._first_email({"emails": None}) is None
    assert T._first_email({}) is None


def test_first_email_skips_non_addresses():
    assert T._first_email({"emails": ["", "nope", "real@x.com"]}) == "real@x.com"


# --- rule 2: a refused call costs nothing --------------------------------- #

def test_find_leads_refuses_without_touching_the_engine(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("engine was called despite zero credit")
    monkeypatch.setattr(lead_engine, "run_campaign", explode)

    ctx = T.RunContext(Budget({"max_find_calls": 0}), "r", seller_id="s")
    out = _tools(ctx)["find_leads"](keyword="dentist", place="Austin, TX")
    assert out["ok"] is False
    assert out["error"] == "budget_exceeded"
    assert out["detail"]["counter"] == "find_calls"
    assert ctx.campaign_id is None  # nothing ran, so nothing was created


def _stub_draft_chain(monkeypatch, captured):
    """Everything draft_outreach touches between charge_llm() and create_draft."""
    import leads_read
    import llm as llm_layer      # tools.py aliases the `llm` module this way
    import agent.run_store as run_store

    monkeypatch.setattr(T, "_load_lead",
                        lambda lid: ({"campaign_id": "c1", "weakness": "w",
                                      "first_line": "f", "emails": ["a@b.com"]},
                                     None))
    monkeypatch.setattr(leads_read, "owns_campaign", lambda s, c: True)
    monkeypatch.setattr(leads_read, "read_campaign_niche", lambda c: ("niche", {}))
    monkeypatch.setattr(leads_read, "read_seller_for_draft",
                        lambda s: ({"name": "S"}, {}))
    monkeypatch.setattr(leads_read, "business_from_lead", lambda l: {"name": "B"})
    monkeypatch.setattr(leads_read, "decoded", lambda x: {})
    monkeypatch.setattr(T, "get_llm", lambda cfg: object())
    monkeypatch.setattr(llm_layer, "draft_email",
                        lambda *a, **k: {"subject": "S", "email_body": "B",
                                         "angle": "A"})

    def fake_create(**kw):
        captured.update(kw)
        return {"id": "draft-1"}
    monkeypatch.setattr(run_store, "create_draft", fake_create)


def test_draft_outreach_stamps_the_run_that_wrote_it(monkeypatch):
    """The run page reads drafts by run_id. If the agent does not stamp it,
    every draft is invisible to the run that produced it -- and the page falls
    back to showing the seller's whole library, which is the bug this fixes."""
    captured = {}
    _stub_draft_chain(monkeypatch, captured)

    ctx = T.RunContext(Budget({"max_llm_calls": 5}), "run-42", seller_id="s")
    out = _tools(ctx)["draft_outreach"](lead_id="l1")

    assert captured["run_id"] == "run-42"
    assert out["ok"] is True
    assert ctx.drafts == 1


def test_repeat_drafts_read_ownership_niche_seller_once(monkeypatch):
    # Every draft used to re-read ownership + campaign niche + the seller row
    # (4-5 identical SELECTs per draft). All three are immutable within a run,
    # so the first answer stands; only the per-lead read repeats.
    import leads_read
    counts = {"owns": 0, "niche": 0, "seller": 0}
    captured = {}
    _stub_draft_chain(monkeypatch, captured)
    orig_owns = leads_read.owns_campaign
    orig_niche = leads_read.read_campaign_niche
    orig_seller = leads_read.read_seller_for_draft
    monkeypatch.setattr(
        leads_read, "owns_campaign",
        lambda s, c: (counts.__setitem__("owns", counts["owns"] + 1),
                      orig_owns(s, c))[1])
    monkeypatch.setattr(
        leads_read, "read_campaign_niche",
        lambda c: (counts.__setitem__("niche", counts["niche"] + 1),
                   orig_niche(c))[1])
    monkeypatch.setattr(
        leads_read, "read_seller_for_draft",
        lambda s: (counts.__setitem__("seller", counts["seller"] + 1),
                   orig_seller(s))[1])

    ctx = T.RunContext(Budget({"max_llm_calls": 5}), "run-42", seller_id="s")
    tools = _tools(ctx)
    assert tools["draft_outreach"](lead_id="l1")["ok"] is True
    assert tools["draft_outreach"](lead_id="l2")["ok"] is True
    assert counts == {"owns": 1, "niche": 1, "seller": 1}


def test_draft_outreach_refuses_without_touching_the_llm(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("LLM provider was built despite zero credit")
    monkeypatch.setattr(T, "get_llm", explode)
    out = _tools(T.RunContext(Budget({"max_llm_calls": 0}), "r",
                              seller_id="s"))["draft_outreach"](lead_id="x")
    assert out["ok"] is False
    assert out["detail"]["counter"] == "llm_calls"


def test_read_tool_stops_once_the_run_is_over(monkeypatch):
    class Clock:
        t = 0.0

        def __call__(self):
            return self.t
    clock = Clock()
    ctx = T.RunContext(Budget({"max_wall_clock_s": 10}, clock=clock), "r",
                       seller_id="s", campaign_id="c")
    clock.t = 99.0
    out = _tools(ctx)["read_leads_tool"]()
    assert out["ok"] is False
    assert out["cap"] == "timeout"


# --- argument handling ----------------------------------------------------- #

def test_database_error_is_not_reported_as_a_missing_lead(monkeypatch):
    # A DB outage must not read as "this lead doesn't exist" — different codes.
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(supabase_store, "select_rows", boom)
    ctx = T.RunContext(Budget({"max_llm_calls": 5}), "r", seller_id="s")
    out = _tools(ctx)["draft_outreach"](lead_id="l1")
    assert out["ok"] is False
    assert out["error"] == "read_failed"
    assert "db down" in out["message"]


def test_missing_lead_is_reported_as_not_found(monkeypatch):
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    ctx = T.RunContext(Budget({"max_llm_calls": 5}), "r", seller_id="s")
    out = _tools(ctx)["draft_outreach"](lead_id="l1")
    assert out["error"] == "not_found"


def test_read_tool_without_a_campaign_explains_itself():
    out = _tools(T.RunContext(Budget(), "r", seller_id="s"))["read_leads_tool"]()
    assert out["ok"] is False
    assert out["error"] == "no_campaign"


def test_read_tool_refuses_a_campaign_the_seller_does_not_own(monkeypatch):
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    out = _tools(T.RunContext(Budget(), "r", seller_id="s",
                             campaign_id="c"))["read_leads_tool"]()
    assert out["ok"] is False
    assert out["error"] == "not_found"


def test_engine_failure_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("dataforseo 500")
    monkeypatch.setattr(lead_engine, "run_campaign", boom)
    out = _tools(T.RunContext(Budget(), "r", seller_id="s"))["find_leads"](
        keyword="dentist", place="Austin, TX")
    assert out["ok"] is False
    assert out["error"] == "find_failed"
    assert "dataforseo 500" in out["message"]


def test_build_tools_returns_the_twelve_documented_tools():
    names = [getattr(t, "tool_name", None)
             for t in T.build_tools(T.RunContext(Budget(), "r"))]
    assert names == ["find_leads", "read_leads_tool", "campaign_summary",
                     "score_niche_fit", "draft_outreach", "revise_draft",
                     "seller_profile", "update_seller_profile",
                     "set_resume_text", "fetch_portfolio", "provider_config",
                     "set_lead_status"]


# --------------------------------------------------------------------------- #
# The seller-scoped tools
# --------------------------------------------------------------------------- #
SELLER_ROW_WITH_BLOBS = {
    "id": "s1", "email": "a@b.co", "name": "Ada", "brand": "Ada Co",
    "resume_text": "R" * 40000, "portfolio_text": "P" * 40000,
}


def _ctx(seller_id="s1"):
    return T.RunContext(Budget(), "r", seller_id=seller_id)


def _props(tool):
    schema = getattr(tool, "tool_spec", {}) or {}
    return (schema.get("inputSchema") or schema.get("input_schema") or {}) \
        .get("json", {}).get("properties", {})


def test_no_seller_scoped_tool_exposes_a_seller_id_argument():
    # The scope must come from the run, never from the model. If a seller_id
    # parameter ever appears here, the agent can name another tenant.
    tools = T.build_tools(_ctx())
    by_name = {getattr(t, "tool_name", ""): t for t in tools}

    # Prove the schema walk actually reads something, so a rename of the
    # strands attribute surfaces as a failure here instead of turning the
    # loop below into a no-op that always passes.
    assert set(_props(by_name["set_lead_status"])) == {"lead_ids", "status"}

    for tool in tools:
        assert "seller_id" not in _props(tool), \
            f"{tool} can be pointed at another tenant"


def test_a_run_without_a_seller_refuses_instead_of_guessing(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not touch the database")

    monkeypatch.setattr(T.seller_ops, "get_seller", explode)
    monkeypatch.setattr(T.seller_ops, "set_lead_status", explode)
    monkeypatch.setattr(T.seller_ops, "get_seller_config", explode)

    for name in ("seller_profile", "provider_config"):
        out = _tools(_ctx(seller_id=None))[name]()
        assert out["ok"] is False
        assert out["error"] == "no_seller"


def test_seller_profile_reports_blob_sizes_not_contents(monkeypatch):
    monkeypatch.setattr(T.seller_ops, "get_seller", lambda sid: dict(SELLER_ROW_WITH_BLOBS))
    out = _tools(_ctx())["seller_profile"]()

    assert out["ok"] is True
    assert out["seller"]["has_resume"] is True
    assert out["seller"]["resume_chars"] == 40000
    assert out["seller"]["has_portfolio_text"] is True
    # The whole point: 80KB of text must not enter the context window.
    assert "resume_text" not in out["seller"]
    assert "portfolio_text" not in out["seller"]
    assert len(json.dumps(out)) < 2000


def test_update_profile_echoes_fields_not_the_whole_row(monkeypatch):
    monkeypatch.setattr(T.seller_ops, "update_seller",
                        lambda sid, fields: dict(SELLER_ROW_WITH_BLOBS))
    out = _tools(_ctx())["update_seller_profile"](brand="New Co")

    assert out["ok"] is True
    assert out["updated"] == ["brand"]
    assert len(json.dumps(out)) < 500


def test_update_profile_with_nothing_supplied_is_refused(monkeypatch):
    monkeypatch.setattr(T.seller_ops, "update_seller",
                        lambda *a, **k: pytest.fail("must not write"))
    out = _tools(_ctx())["update_seller_profile"](name="   ")
    assert out["ok"] is False and out["error"] == "bad_request"


def test_set_resume_text_does_not_return_the_resume(monkeypatch):
    monkeypatch.setattr(T.seller_ops, "set_resume_text",
                        lambda sid, value: {"chars": len(value),
                                            "seller": dict(SELLER_ROW_WITH_BLOBS)})
    out = _tools(_ctx())["set_resume_text"](resume_text="R" * 40000)
    assert out["ok"] is True
    assert out["chars"] == 40000
    assert len(json.dumps(out)) < 500


def test_ops_error_becomes_the_error_code_the_model_can_act_on(monkeypatch):
    def not_found(sid):
        raise T.seller_ops.OpsError("not_found", "seller not found")

    monkeypatch.setattr(T.seller_ops, "get_seller", not_found)
    out = _tools(_ctx())["seller_profile"]()
    assert out == {"ok": False, "error": "not_found",
                   "message": "seller not found"}


def test_an_unexpected_failure_is_reported_not_raised(monkeypatch):
    def boom(sid):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(T.seller_ops, "get_seller", boom)
    out = _tools(_ctx())["seller_profile"]()
    assert out["ok"] is False and "connection reset" in out["message"]


def test_set_lead_status_passes_the_runs_seller_to_the_ownership_check(monkeypatch):
    seen = {}

    def fake(sid, lead_ids, status):
        seen.update(seller=sid, lead_ids=lead_ids, status=status)
        return {"updated": 2, "errors": [], "status": "won"}

    monkeypatch.setattr(T.seller_ops, "set_lead_status", fake)
    out = _tools(_ctx("s1"))["set_lead_status"](lead_ids=["l1", "l2"], status="won")

    assert seen["seller"] == "s1"
    assert seen["lead_ids"] == ["l1", "l2"]
    assert out["ok"] is True and out["updated"] == 2


def test_a_lone_lead_id_as_a_string_is_accepted(monkeypatch):
    # The model will sometimes send one id instead of a list.
    seen = {}
    monkeypatch.setattr(T.seller_ops, "set_lead_status",
                        lambda sid, ids, status: seen.update(ids=ids) or {"updated": 1})
    _tools(_ctx())["set_lead_status"](lead_ids="l1", status="contacted")
    assert seen["ids"] == ["l1"]


def test_fetch_portfolio_returns_metadata_not_the_page(monkeypatch):
    monkeypatch.setattr(T.seller_ops, "fetch_portfolio",
                        lambda sid, url, mode=None: {
                            "portfolio_url": url, "chars": 90000,
                            "page_title": "Ada", "notes": []})
    out = _tools(_ctx())["fetch_portfolio"](url="https://ada.dev")
    assert out["ok"] is True and out["chars"] == 90000
    assert "text" not in out


def test_there_is_no_tool_that_writes_api_keys():
    # The agent reads untrusted pages; a key-writing tool would let a page
    # repoint the seller's account. Keys are set by a human via the Flask API.
    names = [getattr(t, "tool_name", "") for t in T.build_tools(_ctx())]
    assert not [n for n in names if "key" in n or "config" in n
                and n != "provider_config" and "set" in n]

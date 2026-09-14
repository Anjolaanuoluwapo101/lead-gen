"""run_store is the only writer of runs/run_events/drafts. No network here —
supabase_store is mocked at the boundary."""

import pytest

import supabase_store
from agent import run_store


@pytest.fixture
def captured(monkeypatch):
    """Capture what run_store would send, so we assert on the payload."""
    box = {"insert": [], "update": [], "rpc": []}

    def fake_insert(table, rows):
        rows = rows if isinstance(rows, list) else [rows]
        box["insert"].append((table, rows[0]))
        return [{"id": "generated-id", **rows[0]}]

    def fake_update(table, updates, filters):
        box["update"].append((table, updates, filters))
        return [{**updates, "id": filters.get("id")}]

    def fake_rpc_missing(name, payload=None):
        # The database without the migration: the function does not exist,
        # so every append exercises the legacy fallback below.
        box["rpc"].append((name, payload))
        raise RuntimeError("function append_run_event does not exist")

    monkeypatch.setattr(supabase_store, "insert_rows", fake_insert)
    monkeypatch.setattr(supabase_store, "update_rows", fake_update)
    monkeypatch.setattr(supabase_store, "rpc", fake_rpc_missing)
    return box


# --- runs ------------------------------------------------------------------ #

def test_create_run_opens_running_with_zero_spend(captured):
    row = run_store.create_run(seller_id="s1", goal="find dentists")
    assert row["id"] == "generated-id"
    table, sent = captured["insert"][0]
    assert table == "runs"
    assert sent["status"] == "running"
    assert sent["estimated_spend_usd"] == 0
    # An open run has no terminal reason yet — the column stays unset so the
    # DB default (null) applies.
    assert "terminal_reason" not in sent


def test_finish_run_requires_a_reason():
    # A run that stops without saying why is the one outcome this table exists
    # to prevent.
    with pytest.raises(ValueError):
        run_store.finish_run("id", status="stopped", terminal_reason=None)
    with pytest.raises(ValueError):
        run_store.finish_run("id", status="stopped", terminal_reason="")


def test_finish_run_records_reason_and_timestamp(captured):
    run_store.finish_run("id", status="stopped",
                         terminal_reason="budget_exhausted", report={"a": 1})
    _, sent, filters = captured["update"][0]
    assert sent["terminal_reason"] == "budget_exhausted"
    assert sent["report"] == {"a": 1}
    assert sent["finished_at"]
    assert filters == {"id": "id"}


def test_list_runs_refuses_a_missing_seller():
    # Regression: passing None produced `seller_id=eq.None` -> opaque uuid 400.
    # Fires in practice when DEFAULT_SELLER_ID is unset.
    with pytest.raises(ValueError, match="DEFAULT_SELLER_ID"):
        run_store.list_runs(None)
    with pytest.raises(ValueError):
        run_store.list_runs("")


def test_update_progress_omits_spend_when_not_given(captured):
    run_store.update_progress("id", {"find_calls": 2})
    _, sent, _ = captured["update"][0]
    assert sent["progress"] == {"find_calls": 2}
    assert "estimated_spend_usd" not in sent


# --- run_events ------------------------------------------------------------ #

def test_first_event_gets_seq_1(monkeypatch, captured):
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    run_store.append_event("run-1", "start", "beginning")
    _, sent = captured["insert"][0]
    assert sent["seq"] == 1
    assert sent["kind"] == "start"


def test_seq_increments_from_the_highest_existing(monkeypatch, captured):
    monkeypatch.setattr(supabase_store, "select_rows",
                        lambda *a, **k: [{"seq": 7}])
    run_store.append_event("run-1", "find")
    _, sent = captured["insert"][0]
    assert sent["seq"] == 8


def test_seq_recovers_from_a_corrupt_counter(monkeypatch, captured):
    monkeypatch.setattr(supabase_store, "select_rows",
                        lambda *a, **k: [{"seq": "garbage"}])
    run_store.append_event("run-1", "find")
    _, sent = captured["insert"][0]
    assert sent["seq"] == 1


def test_append_prefers_the_single_call_rpc(monkeypatch, captured):
    # Migrated database: one round trip, no select, no separate insert, and
    # the returned row (with the database-assigned seq) is what comes back.
    row = {"id": "ev-1", "run_id": "run-1", "seq": 4, "kind": "search",
           "message": "Searching", "payload": None,
           "created_at": "2026-09-13T14:01:05+00:00"}
    def fake_rpc(name, payload):
        captured["rpc"].append((name, payload))
        return [row]

    monkeypatch.setattr(supabase_store, "rpc", fake_rpc)
    out = run_store.append_event("run-1", "search", "Searching")
    assert out == row
    assert captured["rpc"][0][0] == "append_run_event"
    assert captured["rpc"][0][1]["p_run_id"] == "run-1"
    assert captured["insert"] == []


def test_append_without_the_migration_falls_back_silently(monkeypatch,
                                                          captured):
    # The captured fixture's rpc always raises (function missing): the legacy
    # read-max-then-insert path runs, with the same row shape as before.
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    out = run_store.append_event("run-1", "start", "beginning")
    assert captured["rpc"], "the fast path must be attempted first"
    assert out["seq"] == 1 and out["kind"] == "start"


# --- drafts ----------------------------------------------------------------- #

def test_create_draft_starts_as_a_draft(captured):
    row = run_store.create_draft(lead_id="l1", subject="Hi",
                                 email_body="Body", to_email="a@b.com")
    _, sent = captured["insert"][0]
    assert sent["status"] == "draft"
    assert sent["revision"] == 1
    assert row["id"] == "generated-id"


def test_create_draft_records_the_run_that_wrote_it(captured):
    """Provenance. Without it the run page cannot tell its own drafts from the
    seller's whole library -- and it was showing the whole library."""
    run_store.create_draft(lead_id="l1", subject="Hi", email_body="Body",
                           run_id="run-7")
    _, sent = captured["insert"][0]
    assert sent["run_id"] == "run-7"


def test_a_human_composed_draft_has_no_run_and_that_is_not_an_error(captured):
    """The dashboard's draft route has no run behind it. NULL is the honest
    value -- an invented run id would be worse than an absent one."""
    run_store.create_draft(lead_id="l1", subject="Hi", email_body="Body")
    _, sent = captured["insert"][0]
    assert sent["run_id"] is None


def test_list_drafts_filters_by_run_when_asked(monkeypatch):
    seen = {}

    def fake_select(table, columns="*", filters=None, **kw):
        seen.update(filters or {})
        return []

    monkeypatch.setattr(supabase_store, "select_rows", fake_select)
    run_store.list_drafts(seller_id="s1", run_id="run-7")
    assert seen == {"seller_id": "s1", "run_id": "run-7"}


def test_list_drafts_omits_the_run_filter_when_not_asked(monkeypatch):
    """A falsy run_id must not narrow the seller's library to nothing -- the
    dashboard's draft list asks for no run at all."""
    seen = {}

    def fake_select(table, columns="*", filters=None, **kw):
        seen.update(filters or {})
        return []

    monkeypatch.setattr(supabase_store, "select_rows", fake_select)
    run_store.list_drafts(seller_id="s1")
    assert "run_id" not in seen


def test_update_draft_drops_none_values(monkeypatch, captured):
    # None must not mean "wipe this column".
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    run_store.update_draft("d1", status="approved", send_error=None)
    _, sent, _ = captured["update"][0]
    assert sent["status"] == "approved"
    assert "send_error" not in sent


def test_update_draft_bumps_revision_from_current(monkeypatch, captured):
    monkeypatch.setattr(supabase_store, "select_rows",
                        lambda *a, **k: [{"id": "d1", "revision": 3}])
    run_store.update_draft("d1", bump_revision=True, subject="New")
    _, sent, _ = captured["update"][0]
    assert sent["revision"] == 4


def test_update_draft_bump_survives_a_missing_row(monkeypatch, captured):
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    run_store.update_draft("d1", bump_revision=True, subject="New")
    _, sent, _ = captured["update"][0]
    assert sent["revision"] == 1


def test_live_statuses_match_the_partial_index():
    # supabase-schema-agent.sql: idx_drafts_lead_live covers exactly these.
    assert set(run_store.LIVE_DRAFT_STATUSES) == {"draft", "approved", "sending"}

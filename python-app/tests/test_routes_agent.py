"""The dashboard's routes: /runs/* and /drafts/*.

Four properties here are security-relevant, and each is a bug that would not
show up in a happy-path demo:

1. **A draft or run belonging to another seller is 404, never 403.** A 403
   confirms the id exists, which turns the endpoint into an oracle for
   enumerating other tenants' data. 404 says "not yours" and "not there"
   indistinguishably.
2. **A draft with `seller_id = NULL` is unowned, not public.** `seller_id` is a
   nullable FK that cascades to NULL when a seller is deleted — treating NULL as
   "anyone may act on it" would make deleting a seller a way to hand their
   drafts to whoever asks next.
3. **Export is compare-and-swap, not read-then-write.** The status filter is in
   the UPDATE, so two concurrent exports cannot both pass the `approved` check
   and both proceed.
4. **Export never claims a draft was sent.** SES is not wired in this build;
   the draft returns to `approved`, and `sent` is never written from here.

The threads in `POST /runs` are made synchronous in these tests so the run body
is actually exercised rather than raced against.
"""

import threading
from types import SimpleNamespace

import pytest

import app as flask_app
import auth
import lead_engine
import leads_read
import routes_agent
import seller_ops

SELLER = "seller-1"


class SyncThread:
    """Runs the target immediately on .start(), so assertions after the request
    see a finished run instead of a race."""

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class FakeRunStore:
    """In-memory stand-in. Records calls so the tests can assert on what was
    WRITTEN, not only on what was returned."""

    def __init__(self):
        self.runs = {}
        self.drafts = {}
        self.events = []
        self.updated = []

    def create_run(self, *, seller_id, campaign_id=None, trigger="dashboard",
                   goal=None, bounds=None):
        rid = f"run-{len(self.runs) + 1}"
        self.runs[rid] = {"id": rid, "seller_id": seller_id, "status": "running",
                          "campaign_id": campaign_id, "goal": goal,
                          "progress": {}, "report": None,
                          "terminal_reason": None}
        return dict(self.runs[rid])

    def get_run(self, run_id):
        row = self.runs.get(run_id)
        return dict(row) if row else None

    def list_runs(self, seller_id, limit=20):
        return [dict(r) for r in self.runs.values()
                if str(r["seller_id"]) == str(seller_id)][:limit]

    def list_run_events(self, run_id, limit=500, after=0):
        # Mirrors the real cursor: the route pushes `after` down, so the
        # fake must accept (and honour) it rather than explode on the kwarg.
        return [e for e in self.events
                if e["run_id"] == run_id and e["seq"] > after][:limit]

    def append_event(self, run_id, kind, message=None, data=None):
        self.events.append({"run_id": run_id, "seq": len(self.events) + 1,
                            "kind": kind, "message": message, "data": data})

    def update_progress(self, run_id, progress, spend=None):
        if run_id in self.runs:
            self.runs[run_id]["progress"] = progress

    def request_cancel(self, run_id):
        """The cross-process half of POST /runs/<id>/cancel.

        Mirrors the real column: it RECORDS the request and does not itself
        change `status`, because the run is what ends itself.
        """
        row = self.runs.get(run_id)
        if row is not None:
            row["cancel_requested"] = True
        return dict(row) if row else {}

    def is_cancel_requested(self, run_id):
        row = self.runs.get(run_id)
        return bool(row and row.get("cancel_requested"))

    def finish_run(self, run_id, *, status=None, terminal_reason=None,
                   report=None, error=None, progress=None,
                   estimated_spend_usd=None):
        if run_id in self.runs:
            self.runs[run_id].update(status=status,
                                     terminal_reason=terminal_reason,
                                     report=report)

    def create_draft(self, *, lead_id, subject=None, email_body=None,
                     campaign_id=None, seller_id=None, angle=None,
                     to_email=None, run_id=None):
        live = [d for d in self.drafts.values()
                if d["lead_id"] == lead_id and d["status"] in
                ("draft", "approved", "sending")]
        if live:
            raise ValueError("duplicate key value violates unique constraint "
                             "idx_drafts_lead_live")
        did = f"draft-{len(self.drafts) + 1}"
        self.drafts[did] = {
            "id": did, "lead_id": lead_id, "campaign_id": campaign_id,
            "seller_id": seller_id, "run_id": run_id, "subject": subject,
            "email_body": email_body,
            "angle": angle, "to_email": to_email, "status": "draft",
            "revision": 1}
        return dict(self.drafts[did])

    def list_drafts(self, seller_id=None, campaign_id=None, status=None,
                    limit=50, run_id=None):
        out = []
        for d in self.drafts.values():
            if seller_id is not None and str(d.get("seller_id")) != str(seller_id):
                continue
            if campaign_id and d.get("campaign_id") != campaign_id:
                continue
            if run_id and str(d.get("run_id") or "") != str(run_id):
                continue
            if status and d.get("status") != status:
                continue
            out.append(dict(d))
        return out[:limit]

    def get_draft(self, draft_id):
        row = self.drafts.get(draft_id)
        return dict(row) if row else None

    def update_draft(self, draft_id, *, bump_revision=False, **fields):
        row = self.drafts.get(draft_id)
        if row is None:
            raise ValueError("draft not found")
        row.update(fields)
        if bump_revision:
            row["revision"] = row.get("revision", 1) + 1
        return dict(row)


class FakeSupabase:
    """Only the compare-and-swap used by export."""

    def __init__(self, store):
        self.store = store
        self.cas_calls = []

    def update_rows(self, table, values, filters):
        self.cas_calls.append({"table": table, "values": values,
                               "filters": dict(filters)})
        row = self.store.drafts.get(filters.get("id"))
        # The real Supabase filters on EVERY key in `filters`, so a status
        # mismatch must yield zero rows. Modelling that is the whole point:
        # without it the CAS would look like it always succeeds.
        if row is None or row.get("status") != filters.get("status"):
            return []
        row.update(values)
        return [dict(row)]


@pytest.fixture
def store(monkeypatch):
    fake = FakeRunStore()
    monkeypatch.setattr(routes_agent, "run_store", fake)
    # The routes reject a non-uuid id before touching the store, because
    # Postgres answers a malformed uuid with a 22P02 (a 500 for what is really
    # a 404). These tests use readable ids like "run-1", so the check is
    # bypassed here and asserted directly in the two tests at the bottom of the
    # "id validation" section — keeping the guard covered without threading
    # uuids through every URL in this file.
    monkeypatch.setattr(routes_agent, "_is_uuid", lambda v: True)
    monkeypatch.setattr(routes_agent, "supabase_store", FakeSupabase(fake))
    monkeypatch.setattr(routes_agent, "threading",
                        SimpleNamespace(Thread=SyncThread, Event=threading.Event))
    # `owns_campaign` is called as a leads_read attribute, and it reads the
    # database itself. Default it to True so the tests that are not about
    # ownership can reach the code under test; the ownership tests override it.
    monkeypatch.setattr(leads_read, "owns_campaign", lambda s, c: True)
    return fake


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


@pytest.fixture(autouse=True)
def resolve_to_seller(monkeypatch):
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", SELLER)
    auth.clear_cache()
    yield
    auth.clear_cache()


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
class FakeOrchestrator:
    """Records the kwargs it was built with — `run_id` being one of them is the
    fix for the route creating a row and the orchestrator creating a second."""

    last = None

    def __init__(self, **kwargs):
        FakeOrchestrator.last = kwargs

    def run(self):
        return {"ok": True}


def test_starting_a_run_returns_an_id_immediately(client, store, monkeypatch):
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    r = client.post("/runs", json={"goal": "find bakeries", "scrutiny": "lenient",
              "target_qualified": 5, "niche": "web design",
              "location_code": 1010294})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["run_id"] == "run-1"


def test_a_run_when_all_slots_are_busy_is_429_with_no_row(client, store,
                                                      monkeypatch):
    # Run slots bound in-flight runs so 20 simultaneous Starts cannot spawn
    # 100+ threads in one gunicorn worker. A rejection happens BEFORE the
    # row is created: a 429 must never leave an orphan `running` run.
    import threading as _threading
    monkeypatch.setattr(routes_agent, "_RUN_SLOTS",
                        _threading.Semaphore(1))
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    routes_agent._RUN_SLOTS.acquire()
    try:
        r = client.post("/runs", json={"goal": "find bakeries", "scrutiny": "lenient",
              "target_qualified": 5, "niche": "web design",
              "location_code": 1010294})
        assert r.status_code == 429
        assert r.get_json()["ok"] is False
        assert store.runs == {}
    finally:
        routes_agent._RUN_SLOTS.release()
    # The slot is usable again afterwards: the SyncThread body ran inline
    # and returned its slot in `finally`.
    r = client.post("/runs", json={"goal": "find bakeries", "scrutiny": "lenient",
              "target_qualified": 5, "niche": "web design",
              "location_code": 1010294})
    assert r.status_code == 200


def test_the_run_row_is_created_once_not_twice(client, store, monkeypatch):
    # The route creates the row so it can answer with an id before the work
    # starts; the orchestrator must adopt that id rather than make its own. Two
    # rows would leave an orphan `running` run that never finishes.
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    client.post("/runs", json={"goal": "find bakeries", "scrutiny": "lenient",
              "target_qualified": 5, "niche": "web design",
              "location_code": 1010294})
    assert len(store.runs) == 1
    assert FakeOrchestrator.last["run_id"] == "run-1"


def test_a_goal_is_passed_to_the_orchestrator(client, store, monkeypatch):
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    client.post("/runs", json={"goal": "find bakeries", "scrutiny": "lenient",
              "target_qualified": 5, "niche": "web design",
              "location_code": 1010294})
    assert FakeOrchestrator.last["goal"] == "find bakeries"
    assert FakeOrchestrator.last["seller_id"] == SELLER


def test_a_run_with_neither_a_goal_nor_a_campaign_is_400(client, store):
    r = client.post("/runs", json={})
    assert r.status_code == 400


def test_a_run_without_scrutiny_or_target_is_400(client, store, monkeypatch):
    # Strictness and count have no safe defaults: omitting either refuses to
    # start instead of guessing. Niche rides along so only the missing field
    # is under test in each case.
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    base = {"goal": "g", "niche": "web design", "location_code": 1010294}
    r = client.post("/runs", json={**base, "target_qualified": 5})
    assert r.status_code == 400
    assert "scrutiny" in r.get_json()["error"]
    r = client.post("/runs", json={**base, "scrutiny": "lenient"})
    assert r.status_code == 400
    assert "target_qualified" in r.get_json()["error"]


def test_another_sellers_run_is_404_not_403(client, store):
    # THE ORACLE TRAP. 403 would confirm the id exists.
    store.runs["run-x"] = {"id": "run-x", "seller_id": "other",
                           "status": "running", "progress": {}}
    r = client.get("/run" + "s/run-x")
    assert r.status_code == 404


def test_events_are_pulled_incrementally_with_after(client, store, monkeypatch):
    # The dashboard polls every 5s; `after` is what makes each poll cheap.
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    client.post("/runs", json={"goal": "g", "scrutiny": "lenient", "target_qualified": 5,
              "niche": "web design", "location_code": 1010294})
    for i in range(3):
        store.append_event("run-1", "turn", f"turn {i}")

    first = client.get("/runs/run-1/events?after=0").get_json()
    assert len(first["events"]) == 3 and first["last_seq"] == 3

    second = client.get("/runs/run-1/events?after=3").get_json()
    assert second["events"] == []
    assert second["last_seq"] == 3


def test_event_timestamps_pass_through_to_the_trace(client, store,
                                                   monkeypatch):
    # The run page renders WHEN from created_at. The endpoint must not strip
    # it: it passes rows through, and this pins that contract so a future
    # "cleanup" cannot silently drop the trail timestamps.
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    client.post("/runs", json={"goal": "g", "scrutiny": "lenient", "target_qualified": 5,
              "niche": "web design", "location_code": 1010294})
    store.events.append({"run_id": "run-1", "seq": 1, "kind": "search",
                         "message": "Searching for 'k' in p",
                         "created_at": "2026-09-13T14:01:05+00:00"})
    out = client.get("/runs/run-1/events?after=0").get_json()
    assert out["events"][0]["created_at"] == "2026-09-13T14:01:05+00:00"


def test_the_after_cursor_is_pushed_to_the_store(client, store, monkeypatch):
    # The whole point of ?after= is a payload that stays O(new): if the
    # route fetched everything and sliced in Python, the cursor would be
    # theatre. Spy that it travels down to the store call.
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    client.post("/runs", json={"goal": "g", "scrutiny": "lenient", "target_qualified": 5,
              "niche": "web design", "location_code": 1010294})
    seen = []
    orig = store.list_run_events

    def spy(run_id, limit=500, after=0):
        seen.append(after)
        return orig(run_id, limit=limit, after=after)

    monkeypatch.setattr(store, "list_run_events", spy)
    client.get("/runs/run-1/events?after=7")
    assert seen == [7]


def test_a_non_integer_after_is_400(client, store, monkeypatch):
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    client.post("/runs", json={"goal": "g", "scrutiny": "lenient", "target_qualified": 5,
              "niche": "web design", "location_code": 1010294})
    assert client.get("/runs/run-1/events?after=abc").status_code == 400


def test_cancelling_a_running_run_sets_the_event(client, store, monkeypatch):
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    client.post("/runs", json={"goal": "g", "scrutiny": "lenient", "target_qualified": 5,
              "niche": "web design", "location_code": 1010294})
    r = client.post("/runs/run-1/cancel", json={})
    assert r.status_code == 200
    assert routes_agent._cancels["run-1"].is_set()


def test_cancelling_a_finished_run_is_409(client, store, monkeypatch):
    monkeypatch.setattr(routes_agent, "Orchestrator", FakeOrchestrator)
    client.post("/runs", json={"goal": "g", "scrutiny": "lenient", "target_qualified": 5,
              "niche": "web design", "location_code": 1010294})
    store.runs["run-1"]["status"] = "succeeded"
    r = client.post("/runs/run-1/cancel", json={})
    assert r.status_code == 409


def test_another_sellers_run_cannot_be_cancelled(client, store):
    store.runs["run-x"] = {"id": "run-x", "seller_id": "other",
                           "status": "running", "progress": {}}
    assert client.post("/runs/run-x/cancel", json={}).status_code == 404


def test_a_crashing_run_body_is_recorded_not_lost(client, store, monkeypatch):
    # The daemon thread's exception would otherwise vanish into stderr and the
    # run would sit at `running` forever.
    class Boom:
        def __init__(self, **kw):
            pass

        def run(self):
            raise RuntimeError("model exploded")

    monkeypatch.setattr(routes_agent, "Orchestrator", Boom)
    client.post("/runs", json={"goal": "g", "scrutiny": "lenient", "target_qualified": 5,
              "niche": "web design", "location_code": 1010294})
    assert store.runs["run-1"]["status"] == "failed"
    assert "model exploded" in (store.runs["run-1"]["report"] or "") or \
        store.runs["run-1"]["terminal_reason"] == "error"


# --------------------------------------------------------------------------- #
# Drafts
# --------------------------------------------------------------------------- #
def _seed_draft(store, **over):
    row = {"id": "draft-1", "lead_id": "lead-1", "campaign_id": "camp-1",
           "seller_id": SELLER, "subject": "Hi", "email_body": "Body",
           "angle": "a", "to_email": "owner@biz.com", "status": "draft",
           "revision": 1}
    row.update(over)
    store.drafts[row["id"]] = row
    return row


def test_the_run_page_gets_only_its_own_runs_drafts(client, store):
    """The bug: /drafts/list ignored the run, so the run page listed the
    seller's whole draft library under every run -- a run that wrote nothing
    appeared to have written drafts by other runs."""
    _seed_draft(store, id="draft-mine", run_id="run-1")
    _seed_draft(store, id="draft-theirs", run_id="run-2", lead_id="lead-2")
    _seed_draft(store, id="draft-human", run_id=None, lead_id="lead-3")

    r = client.post("/drafts/list", json={"run_id": "run-1"})
    assert r.status_code == 200
    assert [d["id"] for d in r.get_json()["drafts"]] == ["draft-mine"]


def test_the_sellers_draft_library_is_still_unfiltered(client, store):
    """The dashboard's drafts list asks for NO run and must keep seeing
    everything, including drafts a human composed with no run behind them."""
    _seed_draft(store, id="draft-mine", run_id="run-1")
    _seed_draft(store, id="draft-theirs", run_id="run-2", lead_id="lead-2")
    _seed_draft(store, id="draft-human", run_id=None, lead_id="lead-3")

    r = client.post("/drafts/list", json={})
    assert r.status_code == 200
    assert len(r.get_json()["drafts"]) == 3


def test_another_sellers_run_id_narrows_to_nothing_rather_than_leaking(
        client, store):
    """run_id is applied TOGETHER with seller_id, never instead of it."""
    _seed_draft(store, id="draft-theirs", run_id="run-2",
                seller_id="someone-else")

    r = client.post("/drafts/list", json={"run_id": "run-2"})
    assert r.status_code == 200
    assert r.get_json()["drafts"] == []


def test_a_draft_can_be_read_get_patched_and_approved(client, store):
    _seed_draft(store)
    assert client.get("/drafts/draft-1", json={}).status_code == 200

    r = client.patch("/drafts/draft-1", json={"subject": "Better subject"})
    assert r.status_code == 200
    assert r.get_json()["draft"]["subject"] == "Better subject"
    # The revision bump is what makes an edit traceable.
    assert r.get_json()["draft"]["revision"] == 2

    r = client.post("/drafts/draft-1/approve", json={})
    assert r.status_code == 200
    assert r.get_json()["draft"]["status"] == "approved"


def test_another_sellers_draft_is_404(client, store):
    _seed_draft(store, seller_id="other")
    assert client.get("/drafts/draft-1", json={}).status_code == 404
    assert client.post("/drafts/draft-1/approve", json={}).status_code == 404
    assert client.post("/drafts/draft-1/export", json={}).status_code == 404


def test_a_null_owner_draft_is_not_public(client, store):
    # A deleted seller cascades drafts.seller_id to NULL. If NULL counted as
    # "anyone's", deleting a seller would donate their drafts to the next caller.
    _seed_draft(store, seller_id=None)
    assert client.get("/drafts/draft-1", json={}).status_code == 404
    assert client.post("/drafts/draft-1/export", json={}).status_code == 404


def test_a_rejected_draft_cannot_be_edited(client, store):
    _seed_draft(store, status="rejected")
    assert client.patch("/drafts/draft-1",
                        json={"subject": "x"}).status_code == 409


# --------------------------------------------------------------------------- #
# Export — the four gates
# --------------------------------------------------------------------------- #
def test_an_unapproved_draft_cannot_be_exported(client, store):
    # GATE 2. The whole point of the approval step.
    _seed_draft(store, to_email="real@verified.com")
    r = client.post("/drafts/draft-1/export", json={})
    assert r.status_code == 409
    assert "not approved" in r.get_json()["error"]


def test_export_uses_a_compare_and_swap_not_a_read_then_write(client, store):
    # GATE 3, and the reason two concurrent exports cannot both win.
    _seed_draft(store, status="approved", to_email="real@verified.com")
    client.post("/drafts/draft-1/export", json={})
    cas = store and routes_agent.supabase_store.cas_calls[0]
    assert cas["values"] == {"status": "sending"}
    # The status is part of the WHERE, so a concurrent winner makes this match
    # zero rows and the loser gets a 409 instead of exporting twice.
    assert cas["filters"]["status"] == "approved"


def test_a_lost_race_is_409_not_a_second_export(client, store, monkeypatch):
    _seed_draft(store, status="approved", to_email="real@verified.com")
    # Simulate the other request having already claimed it.
    store.drafts["draft-1"]["status"] = "sending"
    r = client.post("/drafts/draft-1/export", json={})
    assert r.status_code == 409


def test_export_never_marks_a_draft_sent(client, store):
    # THE HONESTY GATE. SES is not wired; nothing was sent, so `sent` must never
    # be written, and the draft returns to `approved` because that is true.
    _seed_draft(store, status="approved", to_email="real@verified.com")
    r = client.post("/drafts/draft-1/export", json={})
    assert r.status_code == 200
    assert store.drafts["draft-1"]["status"] == "approved"
    for call in routes_agent.supabase_store.cas_calls:
        assert call["values"]["status"] != "sent"


def test_export_does_not_ask_whether_the_recipient_can_be_emailed(client,
                                                                  store):
    # THE REGRESSION. Every test above used to patch `recipient_blocked` to
    # None, so they all exercised the export with gate 4 switched off — and gate
    # 4 is never off in production. Export 400'd on every request and
    # `_csv_response` was unreachable code. This test patches NOTHING, which is
    # the only way the bug is visible: a real CSV has to come back.
    _seed_draft(store, status="approved", to_email="real@verified.com")
    r = client.post("/drafts/draft-1/export", json={})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["count"] == 1
    assert "real@verified.com" in body["csv"]
    assert body["csv"].splitlines()[0].startswith("draft_id,")


def test_a_draft_with_no_recipient_still_exports(client, store):
    # Exporting and sending are different questions. A draft with no address
    # cannot be SENT, and `recipient_blocked` still says so — but it is still
    # perfectly exportable, which is how you find out it needs an address.
    _seed_draft(store, status="approved", to_email=None)
    assert client.post("/drafts/draft-1/export",
                       json={}).status_code == 200
    assert routes_agent.recipient_blocked({"to_email": None})
    assert routes_agent.recipient_blocked({"to_email": "real@verified.com"})


def test_a_batch_export_contains_only_approved_drafts(client, store):
    _seed_draft(store, id="draft-1", status="approved",
                to_email="a@x.com")
    _seed_draft(store, id="draft-2", status="draft", lead_id="lead-2",
                to_email="b@x.com")
    body = client.post("/drafts/export", json={}).get_json()
    assert body["count"] == 1
    assert "draft-1" in body["csv"] and "draft-2" not in body["csv"]
    assert "Nothing was sent" in body["note"]


def test_a_batch_export_can_be_scoped_to_one_run(client, store):
    # The run page's export button. Unscoped, it swept in every approved draft
    # the seller owned, so the CSV did not match the table above it.
    _seed_draft(store, id="draft-1", status="approved", to_email="a@x.com",
                run_id="run-a")
    _seed_draft(store, id="draft-2", status="approved", lead_id="lead-2",
                to_email="b@x.com", run_id="run-b")
    body = client.post("/drafts/export", json={"run_id": "run-a"}).get_json()
    assert body["count"] == 1
    assert "draft-1" in body["csv"] and "draft-2" not in body["csv"]
    # And the unscoped call still sees both, so this is a filter, not a default.
    assert client.post("/drafts/export", json={}).get_json()["count"] == 2


def test_an_empty_batch_is_404(client, store):
    assert client.post("/drafts/export", json={}).status_code == 404


def test_a_failed_batch_releases_every_claim_it_took(client, store, monkeypatch):
    # All-or-nothing: a half-claimed batch would leave drafts stuck in
    # `sending` with no CSV, so nothing would ever export them again.
    _seed_draft(store, id="draft-1", status="approved", to_email="a@x.com")
    _seed_draft(store, id="draft-2", status="approved", lead_id="lead-2",
                to_email="b@x.com")

    real = routes_agent.supabase_store.update_rows

    def lose_the_race_on_the_second(ids, values, filters):
        if filters.get("id") == "draft-2":
            return []                     # another request got there first
        return real(ids, values, filters)

    monkeypatch.setattr(routes_agent.supabase_store, "update_rows",
                        lose_the_race_on_the_second)
    assert client.post("/drafts/export", json={}).status_code == 409
    assert store.drafts["draft-1"]["status"] == "approved"
    assert store.drafts["draft-2"]["status"] == "approved"


# --------------------------------------------------------------------------- #
# Send — the four gates, and the only path that writes `sent`
# --------------------------------------------------------------------------- #
@pytest.fixture
def sender(monkeypatch):
    """A seller profile and a stubbed provider, recording what was sent.

    `read_sender` is patched rather than the database faked: it is the seam
    between "a row" and "a message", and stubbing there means these tests
    exercise the real body-building and real attachment code.
    """
    import draft_send

    monkeypatch.setattr(draft_send, "read_sender",
                        lambda sid: {"id": sid, "name": "Alex",
                                     "portfolio_url": "https://alex.dev",
                                     "resume_key": "seller-1/cv.pdf",
                                     "resume_filename": "Alex-CV.pdf",
                                     "resume_content_type": "application/pdf"})
    monkeypatch.setattr(draft_send.file_store, "get",
                        lambda k: (b"%PDF-1.4 fake", "application/pdf"))
    sent = []

    def fake_send(**kwargs):
        sent.append(kwargs)
        return "provider-msg-1"

    monkeypatch.setattr(draft_send.send_provider, "send_email", fake_send)
    monkeypatch.setattr(routes_agent.send_provider, "configured", lambda *a, **k: True)
    return sent


def test_a_draft_must_be_approved_before_it_can_be_sent(client, store, sender):
    _seed_draft(store, status="draft", to_email="lead@biz.com")
    r = client.post("/drafts/draft-1/send", json={})
    assert r.status_code == 409
    assert "not approved" in r.get_json()["error"]
    assert sender == [], "nothing may be sent for an unapproved draft"


def test_nothing_is_sent_when_sending_is_switched_off(client, store, monkeypatch):
    # THE DEFAULT. SEND_BACKEND=none must refuse loudly and leave the draft
    # exactly where it was, not silently no-op or half-claim it.
    monkeypatch.setattr(routes_agent.send_provider, "configured", lambda *a, **k: False)
    monkeypatch.setattr(routes_agent.send_provider, "send_email",
                        lambda **k: pytest.fail("send_email must not be called"))
    _seed_draft(store, status="approved", to_email="lead@biz.com")
    r = client.post("/drafts/draft-1/send", json={})
    assert r.status_code == 400
    assert "SEND_BACKEND" in r.get_json()["error"]
    assert store.drafts["draft-1"]["status"] == "approved"


def test_a_successful_send_marks_sent_with_the_message_id(client, store,
                                                          sender):
    _seed_draft(store, status="approved", to_email="lead@biz.com")
    r = client.post("/drafts/draft-1/send", json={})
    assert r.status_code == 200, r.get_json()
    assert store.drafts["draft-1"]["status"] == "sent"
    assert store.drafts["draft-1"]["provider_message_id"] == "provider-msg-1"
    assert store.drafts["draft-1"]["send_backend"] is not None


def test_the_email_carries_the_portfolio_link_and_the_resume(client, store,
                                                             sender):
    # The two things that were asked for, asserted on the message that would
    # actually have gone out rather than on the code that was meant to build it.
    _seed_draft(store, status="approved", to_email="lead@biz.com")
    client.post("/drafts/draft-1/send", json={})
    assert len(sender) == 1
    assert "Portfolio: https://alex.dev" in sender[0]["body_text"]
    assert sender[0]["attachments"] == [
        ("Alex-CV.pdf", "application/pdf", b"%PDF-1.4 fake")]
    # And the row records what went with it, because the seller may replace
    # their resume tomorrow and this row must still answer "what did we send?".
    assert store.drafts["draft-1"]["attachment_name"] == "Alex-CV.pdf"
    assert store.drafts["draft-1"]["attachment_key"] == "seller-1/cv.pdf"


def test_a_provider_rejection_is_failed_not_approved(client, store,
                                                     monkeypatch, sender):
    # We do not know whether the message was delivered. Returning it to
    # `approved` would offer it for a second attempt and invite a duplicate.
    import draft_send
    import send_provider

    def boom(**kwargs):
        raise send_provider.SendFailed("SES refused: not verified")

    monkeypatch.setattr(draft_send.send_provider, "send_email", boom)
    _seed_draft(store, status="approved", to_email="lead@biz.com")
    r = client.post("/drafts/draft-1/send", json={})
    assert r.status_code == 502
    assert store.drafts["draft-1"]["status"] == "failed"
    assert "not verified" in store.drafts["draft-1"]["send_error"]


def test_a_refused_send_releases_the_draft_back_to_approved(client, store,
                                                            monkeypatch,
                                                            sender):
    # SendRefused means nothing left the building, so the draft is genuinely
    # still approved and the human can fix the reason and try again.
    import draft_send
    import send_provider

    def refuse(**kwargs):
        raise send_provider.SendRefused("no sender configured")

    monkeypatch.setattr(draft_send.send_provider, "send_email", refuse)
    _seed_draft(store, status="approved", to_email="lead@biz.com")
    r = client.post("/drafts/draft-1/send", json={})
    assert r.status_code == 400
    assert store.drafts["draft-1"]["status"] == "approved"


def test_a_send_never_leaves_the_draft_stuck_in_sending(client, store, sender):
    _seed_draft(store, status="approved", to_email="lead@biz.com")
    client.post("/drafts/draft-1/send", json={})
    assert store.drafts["draft-1"]["status"] != "sending"


def test_sending_another_sellers_draft_is_404_not_403(client, store, sender):
    _seed_draft(store, seller_id="someone-else", status="approved")
    assert client.post("/drafts/draft-1/send", json={}).status_code == 404
    assert sender == []


def test_an_unapproved_draft_cannot_be_sent_even_by_a_second_attempt(client,
                                                                     store,
                                                                     sender):
    # `sent` is terminal: idx_drafts_sent_once makes double-sending impossible
    # below the app, and the route refuses to move a sent draft anywhere.
    _seed_draft(store, status="approved", to_email="lead@biz.com")
    assert client.post("/drafts/draft-1/send", json={}).status_code == 200
    r = client.post("/drafts/draft-1/send", json={})
    assert r.status_code == 409
    assert len(sender) == 1, "the second attempt must not reach the provider"


# --------------------------------------------------------------------------- #
# Generate-and-persist
# --------------------------------------------------------------------------- #
def test_a_new_draft_is_persisted_for_review(client, store, monkeypatch):
    import draft_compose
    import leads_read
    monkeypatch.setattr(leads_read, "read_lead",
                        lambda lid: {"id": lid, "campaign_id": "camp-1",
                                     "emails": ["owner@biz.com"]})
    monkeypatch.setattr(draft_compose, "compose_draft",
                        lambda *a, **k: {"subject": "S", "email_body": "B",
                                         "angle": "A"})
    r = client.post("/drafts", json={"lead_id": "lead-1"})
    assert r.status_code == 200
    draft = r.get_json()["draft"]
    assert draft["status"] == "draft"          # awaits human approval
    assert draft["to_email"] == "owner@biz.com"
    assert draft["seller_id"] == SELLER


def test_a_second_live_draft_for_the_same_lead_is_409(client, store,
                                                      monkeypatch):
    # idx_drafts_lead_live, surfaced as a state conflict rather than a 500.
    import draft_compose
    import leads_read
    monkeypatch.setattr(leads_read, "read_lead",
                        lambda lid: {"id": lid, "campaign_id": "camp-1",
                                     "emails": ["owner@biz.com"]})
    monkeypatch.setattr(draft_compose, "compose_draft",
                        lambda *a, **k: {"subject": "S", "email_body": "B",
                                         "angle": "A"})
    assert client.post("/drafts", json={"lead_id": "lead-1"}).status_code == 200
    assert client.post("/drafts", json={"lead_id": "lead-1"}).status_code == 409


def test_drafting_another_sellers_lead_is_403(client, store, monkeypatch):
    import draft_compose
    import leads_read
    monkeypatch.setattr(leads_read, "read_lead",
                        lambda lid: {"id": lid, "campaign_id": "camp-other"})
    monkeypatch.setattr(leads_read, "owns_campaign", lambda s, c: False)
    # compose_draft must not even be reached — if it were, we would have built a
    # draft using another tenant's resume and provider key.
    monkeypatch.setattr(draft_compose, "compose_draft",
                        lambda *a, **k: pytest.fail("composed an unowned lead"))
    r = client.post("/drafts", json={"lead_id": "lead-1"})
    assert r.status_code == 403


def test_drafting_without_a_lead_id_is_400(client, store):
    assert client.post("/drafts", json={}).status_code == 400


# --------------------------------------------------------------------------- #
# Id validation — the guard the fixture above switches off
# --------------------------------------------------------------------------- #
def test_a_real_uuid_is_accepted():
    assert routes_agent._is_uuid("11111111-2222-3333-4444-555555555555")
    assert routes_agent._is_uuid("ABCDEF01-2222-3333-4444-555555555555")


@pytest.mark.parametrize("bad", ["", None, "run-1", "not-a-uuid",
                                 "11111111-2222-3333-4444", "'; drop table--"])
def test_a_non_uuid_is_refused(bad):
    assert routes_agent._is_uuid(bad) is False


def test_a_malformed_id_never_reaches_the_store(client, monkeypatch):
    """The real guard, on a route, with the store watching.

    Deliberately does NOT use the `store` fixture, which stubs `_is_uuid` out.
    """
    touched = []
    monkeypatch.setattr(routes_agent.run_store, "get_run",
                        lambda rid: touched.append(rid))
    assert client.get("/runs/not-a-uuid").status_code == 404
    assert client.get("/drafts/not-a-uuid", json={}).status_code == 404
    assert touched == [], "a malformed id reached the store"

"""POST /prospects/list: every prospect a campaign found, grouped for display.

Read-scoped like drafts: another seller's campaign or run is 404, never 403,
so the response never confirms another tenant's ids exist. A live run resolves
through its progress (campaigns created so far), a finished run through its
report.
"""

import app as flask_app
import auth
import lead_engine
import leads_read
import routes_agent
from agent import run_store

SELLER = "seller-1"
RUN = "11111111-2222-3333-4444-555555555555"
CAMP = "22222222-2222-2222-2222-222222222222"
FOREIGN_RUN = "33333333-3333-3333-3333-333333333333"


class FakeDB:
    """In-memory campaigns + prospects behind supabase_store.select_rows."""

    def __init__(self):
        self.campaigns = {
            CAMP: {"id": CAMP, "seller_id": SELLER, "name": "bakeries",
                   "keyword": "bakery", "location": "Lagos"},
            "44444444-4444-4444-4444-444444444444": {
                "id": "44444444-4444-4444-4444-444444444444",
                "seller_id": "someone-else", "name": "theirs",
                "keyword": "k", "location": "l"},
        }
        self.prospects = [
            {"campaign_id": CAMP, "business_name": "A Bakery",
             "category": "bakery", "phone": "0801", "website": "https://a.ng",
             "locality": "Lagos", "region": "", "status": "dismissed",
             "created_at": "2026-09-13T10:00:00+00:00"},
            {"campaign_id": CAMP, "business_name": "B Cakes",
             "category": "cakes", "phone": "", "website": "",
             "locality": "Lagos", "region": "", "status": "qualified",
             "created_at": "2026-09-13T10:01:00+00:00"},
        ]
        self.runs = {
            RUN: {"id": RUN, "seller_id": SELLER,
                  "campaign_id": None,
                  "report": {"campaign_ids": [CAMP]},
                  "progress": {}},
            "55555555-5555-5555-5555-555555555555": {
                "id": "55555555-5555-5555-5555-555555555555",
                "seller_id": SELLER, "campaign_id": None, "report": None,
                "progress": {"campaign_ids": [CAMP]}},
            FOREIGN_RUN: {"id": FOREIGN_RUN, "seller_id": "someone-else",
                          "campaign_id": None, "report": None,
                          "progress": {}},
        }
        self.seen = []

    def select_rows(self, table, columns="*", filters=None, filters_in=None,
                    filters_gte=None, limit=None, order=None):
        self.seen.append({"table": table, "limit": limit})
        if table == "campaigns":
            rows = [c for c in self.campaigns.values()
                    if all(c.get(k) == v for k, v in (filters or {}).items())
                    and all(c.get(k) in v for k, v in
                            (filters_in or {}).items())]
            return [dict(r) for r in rows]
        if table == "prospects":
            rows = [p for p in self.prospects
                    if all(p.get(k) in v for k, v in
                           (filters_in or {}).items())]
            rows = sorted(rows, key=lambda p: p.get("created_at") or "")
            return [dict(r) for r in rows[:limit]] if limit else rows
        raise AssertionError(f"unexpected table {table}")


def _client(monkeypatch, db):
    flask_app.app.config["TESTING"] = True
    monkeypatch.setattr(routes_agent, "supabase_store", db)
    monkeypatch.setattr(run_store, "get_run",
                        lambda rid: dict(db.runs.get(rid) or {})
                        if rid in db.runs else None)
    monkeypatch.setattr(leads_read, "owns_campaign",
                        lambda s, c: str(s) == SELLER and c == CAMP)
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", SELLER)
    auth.clear_cache()
    return flask_app.app.test_client()


def test_a_finished_run_lists_its_campaigns_prospects(monkeypatch):
    db = FakeDB()
    body = _client(monkeypatch, db).post(
        "/prospects/list", json={"run_id": RUN}).get_json()
    assert body["ok"] is True
    assert body["count"] == 2
    assert len(body["campaigns"]) == 1
    camp = body["campaigns"][0]
    assert camp["name"] == "bakeries"
    assert camp["prospect_count"] == 2
    assert [p["business_name"] for p in camp["prospects"]] == \
        ["A Bakery", "B Cakes"]
    assert camp["prospects"][0]["status"] == "dismissed"


def test_a_live_run_resolves_through_progress(monkeypatch):
    db = FakeDB()
    body = _client(monkeypatch, db).post(
        "/prospects/list",
        json={"run_id": "55555555-5555-5555-5555-555555555555"}).get_json()
    assert body["ok"] is True
    assert body["count"] == 2


def test_one_campaign_lists_just_itself(monkeypatch):
    db = FakeDB()
    body = _client(monkeypatch, db).post(
        "/prospects/list", json={"campaign_id": CAMP}).get_json()
    assert body["count"] == 2
    assert [c["campaign_id"] for c in body["campaigns"]] == [CAMP]


def test_another_sellers_run_is_404_not_403(monkeypatch):
    # A well-formed id, so this exercises the ownership check rather than the
    # uuid guard: a 403 would confirm the run exists.
    db = FakeDB()
    r = _client(monkeypatch, db).post(
        "/prospects/list", json={"run_id": FOREIGN_RUN})
    assert r.status_code == 404


def test_another_sellers_campaign_is_404_not_403(monkeypatch):
    db = FakeDB()
    r = _client(monkeypatch, db).post(
        "/prospects/list",
        json={"campaign_id": "44444444-4444-4444-4444-444444444444"})
    assert r.status_code == 404


def test_neither_id_is_400(monkeypatch):
    db = FakeDB()
    assert _client(monkeypatch, db).post(
        "/prospects/list", json={}).status_code == 400


def test_a_malformed_run_id_never_reaches_the_store(monkeypatch):
    db = FakeDB()
    r = _client(monkeypatch, db).post(
        "/prospects/list", json={"run_id": "not-a-uuid"})
    assert r.status_code == 404
    assert db.seen == [], "a malformed id reached the database"


def test_the_limit_is_clamped(monkeypatch):
    db = FakeDB()
    _client(monkeypatch, db).post(
        "/prospects/list", json={"campaign_id": CAMP, "limit": 99999})
    limits = [s["limit"] for s in db.seen if s["table"] == "prospects"]
    assert limits and all(l <= 500 for l in limits)

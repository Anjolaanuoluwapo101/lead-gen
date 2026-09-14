"""`POST /draft` is the route n8n reads, and it had NO test coverage.

That matters because its body was just rewritten to delegate to
`draft_compose.compose_draft` so the dashboard's persisting `POST /drafts` can
share one composition path. A refactor of an untested, externally-consumed route
is exactly how a contract breaks silently: n8n would not error, it would just
read `undefined` for a field the workflow maps downstream.

So these tests pin the two things the refactor could have changed and the
workflow would not have noticed:

1. **The response shape.** `{ok, lead_id, seller_id, seller_name, campaign_id,
   niche, subject, email_body, angle}` — no `drafts`, no envelope change, and
   crucially no persistence fields leaking in.
2. **The status codes and their ORDER.** A missing lead is 404 "lead not found";
   a lead owned by someone else is 403. Those were produced by different
   branches before and are still different branches now.
"""

import pytest

import app as flask_app
import draft_compose
import lead_engine
import leads_read
import seller_ops


LEAD = {"id": "lead-1", "campaign_id": "camp-1", "weakness": "no site",
        "first_line": "Saw your listing", "emails": ["owner@biz.com"],
        "intelligence": '{"summary": "old site"}',
        "prospect_id": {"business_name": "Ada's Bakery", "category": "bakery"}}

PAYLOAD = {"lead_id": "lead-1", "seller_id": "seller-1",
           "seller_name": "Ada", "campaign_id": "camp-1", "niche": "web design",
           "subject": "Quick idea for Ada's Bakery",
           "email_body": "Hi Ada, ...", "angle": "slow site"}

# What /draft returned before the refactor. If this list changes, n8n's workflow
# breaks without an error — which is the failure this test exists to prevent.
EXPECTED_KEYS = {"ok", "lead_id", "seller_id", "seller_name", "campaign_id",
                 "niche", "subject", "email_body", "angle"}


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


@pytest.fixture
def lead_ok(monkeypatch):
    monkeypatch.setattr(leads_read, "read_lead", lambda lead_id: dict(LEAD))
    monkeypatch.setattr(leads_read, "read_campaign_niche",
                        lambda cid: ("web design", "seller-1"))
    # Patched on the APP, not on leads_read: app.py binds
    # `_seller_owns_campaign = leads_read.owns_campaign` at import time, so
    # patching the source module leaves the app's alias pointing at the original.
    monkeypatch.setattr(flask_app, "_seller_owns_campaign", lambda s, c: True)
    monkeypatch.setattr("supabase_store.configured", lambda: True, raising=False)


def test_draft_returns_exactly_the_frozen_shape(client, lead_ok, monkeypatch):
    monkeypatch.setattr(draft_compose, "compose_draft",
                        lambda *a, **k: dict(PAYLOAD))
    r = client.post("/draft", json={"lead_id": "lead-1"})
    assert r.status_code == 200
    body = r.get_json()
    # Exact key set, not a subset: an ADDED key is as much a contract change as
    # a missing one when a workflow maps the whole object forward.
    assert set(body) == EXPECTED_KEYS
    assert body["subject"] == PAYLOAD["subject"]
    assert body["email_body"] == PAYLOAD["email_body"]
    assert body["angle"] == PAYLOAD["angle"]


def test_draft_still_persists_nothing(client, lead_ok, monkeypatch):
    """The whole reason /drafts (plural) had to be a separate route."""
    monkeypatch.setattr(draft_compose, "compose_draft",
                        lambda *a, **k: dict(PAYLOAD))
    called = []
    import agent.run_store as run_store
    monkeypatch.setattr(run_store, "create_draft",
                        lambda **kw: called.append(kw))
    client.post("/draft", json={"lead_id": "lead-1"})
    assert called == []


def test_a_missing_lead_is_404_not_403(client, monkeypatch):
    # THE ORDERING TRAP. The route reads the lead to find its campaign, and the
    # ownership gate needs that campaign. If the ownership check ran first, a
    # nonexistent lead would look like an unowned one and n8n would start
    # getting 403s where it used to get 404s.
    monkeypatch.setattr("supabase_store.configured", lambda: True, raising=False)
    monkeypatch.setattr(leads_read, "read_lead", lambda lead_id: None)
    r = client.post("/draft", json={"lead_id": "nope"})
    assert r.status_code == 404
    assert "not found" in r.get_json()["error"]


def test_a_lead_with_no_lead_id_is_400(client, monkeypatch):
    monkeypatch.setattr("supabase_store.configured", lambda: True, raising=False)
    r = client.post("/draft", json={})
    assert r.status_code == 400


def test_an_unconfigured_supabase_is_503(client, monkeypatch):
    monkeypatch.setattr("supabase_store.configured", lambda: False, raising=False)
    r = client.post("/draft", json={"lead_id": "lead-1"})
    assert r.status_code == 503


def test_someone_elses_lead_is_403(client, monkeypatch):
    monkeypatch.setattr("supabase_store.configured", lambda: True, raising=False)
    monkeypatch.setattr(leads_read, "read_lead", lambda lead_id: dict(LEAD))
    monkeypatch.setattr(leads_read, "read_campaign_niche",
                        lambda cid: ("web design", "someone-else"))
    monkeypatch.setattr(flask_app, "_seller_owns_campaign", lambda s, c: False)
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", "seller-1")
    r = client.post("/draft", json={"lead_id": "lead-1",
                                    "seller_id": "seller-1"})
    assert r.status_code == 403


def test_a_compose_failure_maps_to_a_status_not_a_500(client, lead_ok,
                                                      monkeypatch):
    def boom(*a, **k):
        raise seller_ops.OpsError("upstream", "draft failed: model timeout")

    monkeypatch.setattr(draft_compose, "compose_draft", boom)
    r = client.post("/draft", json={"lead_id": "lead-1"})
    assert r.status_code == 502
    assert "draft failed" in r.get_json()["error"]


def test_a_bad_request_from_compose_is_400(client, lead_ok, monkeypatch):
    def boom(*a, **k):
        raise seller_ops.OpsError("bad_request", "lead_id is required")

    monkeypatch.setattr(draft_compose, "compose_draft", boom)
    r = client.post("/draft", json={"lead_id": "lead-1"})
    assert r.status_code == 400


def test_draft_is_not_registered_on_the_agent_surface(client):
    """`/draft` and `/drafts` must stay different nouns.

    If a future edit collapses them, n8n starts writing rows it never asked for
    and the dashboard's one-live-draft-per-lead index starts rejecting its own
    pipeline runs.
    """
    rules = {r.rule for r in flask_app.app.url_map.iter_rules()}
    assert "/draft" in rules
    assert "/drafts" in rules

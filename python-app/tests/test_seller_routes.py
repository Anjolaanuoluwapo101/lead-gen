"""The seller routes are now a skin over seller_ops, so two things can break
silently and neither shows up in seller_ops' own tests:

1. **The response shape.** The n8n workflows read {"ok": true, "seller": {...}}.
   Routing a bare row through a generic envelope handler would flatten it to
   {"ok": true, "id": ...} and return 200 — a green test run and a broken form.
2. **The settings leak at the edge.** seller_ops can be perfectly clean while a
   route still reaches around it for the raw column.

These tests speak HTTP, so both are visible.
"""

import json

import pytest

import app as flask_app
import seller_ops


class RouteDB:
    """A supabase_store stand-in good enough to drive the seller routes."""

    def __init__(self):
        self.row = {
            "id": "s1", "email": "ada@example.com", "name": "Ada",
            "title": None, "brand": "Ada Co", "niche": "dentists",
            "phone": None, "resume_text": None, "portfolio_url": None,
            "portfolio_text": None, "render_mode": "auto", "active": True,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        self.settings = {"llm": {"provider": "groq", "api_key_enc": "CIPHERTEXT"}}
        self.selects = []
        self.inserts = []
        self.updates = []

    def configured(self):
        return True

    def select_rows(self, table, columns="*", filters=None, filters_gte=None,
                    filters_in=None, limit=None, order=None):
        self.selects.append(columns)
        if "settings" in (columns or ""):
            return [{"id": "s1", "settings": json.dumps(self.settings)}]
        row = dict(self.row)
        if filters and filters.get("email") not in (None, row["email"]):
            return []
        if filters and filters.get("id") not in (None, row["id"]):
            return []
        return [row]

    def insert_rows(self, table, rows):
        self.inserts.append(rows)
        return [{**self.row, **rows, "id": "s-new"}]

    def update_rows(self, table, updates, filters):
        self.updates.append(updates)
        return [{**self.row, **updates}]


@pytest.fixture
def client(monkeypatch):
    db = RouteDB()
    # Both modules: the seller routes go through seller_ops, but the
    # /config routes still read the raw column via app.py's own helper (they
    # need the untruncated settings to re-encrypt on PATCH), so they must see
    # the same fake or they hit the real unconfigured client and 502.
    monkeypatch.setattr(seller_ops, "supabase_store", db)
    monkeypatch.setattr(flask_app, "supabase_store", db)
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client(), db


# --------------------------------------------------------------------------- #
# Response shapes the n8n workflows depend on
# --------------------------------------------------------------------------- #
def test_get_seller_nests_the_row_under_seller(client):
    c, _ = client
    body = c.get("/seller/s1").get_json()
    assert body["ok"] is True
    assert body["seller"]["id"] == "s1"          # NOT flattened to body["id"]
    assert "id" not in body or body.get("id") is None


def test_get_seller_by_email_nests_the_row(client):
    c, _ = client
    body = c.get("/seller/by-email?email=ada@example.com").get_json()
    assert body["ok"] is True
    assert body["seller"]["email"] == "ada@example.com"


def test_patch_seller_nests_the_row(client):
    c, _ = client
    body = c.patch("/seller/s1", json={"brand": "New"}).get_json()
    assert body["ok"] is True
    assert body["seller"]["brand"] == "New"


def test_create_seller_keeps_its_created_updated_flags(client):
    c, _ = client
    body = c.post("/seller", json={"email": "new@example.com",
                                   "name": "New"}).get_json()
    assert body["ok"] is True
    assert body["created"] is True and body["updated"] is False
    assert "seller" in body


def test_list_keeps_its_count_and_sellers_wrapper(client):
    c, _ = client
    body = c.get("/seller/list").get_json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["sellers"][0]["id"] == "s1"


def test_a_bare_row_is_never_returned_as_a_200_error(client):
    # The failure mode of a generic envelope: a row spread into the top level
    # looks like success but has no "seller" key, and n8n reads undefined.
    c, _ = client
    for method, path, kwargs in (
            ("get", "/seller/s1", {}),
            ("get", "/seller/by-email?email=ada@example.com", {}),
            ("patch", "/seller/s1", {"json": {"brand": "x"}})):
        body = getattr(c, method)(path, **kwargs).get_json()
        assert set(body) & {"seller"}, f"{path} lost its seller wrapper"


# --------------------------------------------------------------------------- #
# Status codes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload,status", [
    ({"email": "not-an-email"}, 400),      # bad_request
    ({}, 400),
])
def test_create_rejects_a_bad_email(client, payload, status):
    c, _ = client
    assert c.post("/seller", json=payload).status_code == status


def test_patch_rejects_an_unpatchable_field(client):
    c, _ = client
    r = c.patch("/seller/s1", json={"settings": {"llm": {}}})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_patch_rejects_a_render_mode_typo(client):
    c, _ = client
    assert c.patch("/seller/s1", json={"render_mode": "htm"}).status_code == 400


def test_by_email_without_an_email_is_400(client):
    c, _ = client
    assert c.get("/seller/by-email").status_code == 400


def test_an_unconfigured_database_is_503_not_500(client, monkeypatch):
    c, db = client
    monkeypatch.setattr(db, "configured", lambda: False)
    r = c.get("/seller/s1")
    assert r.status_code == 503
    assert "not configured" in r.get_json()["error"]


def test_a_missing_seller_is_404(client, monkeypatch):
    c, db = client
    monkeypatch.setattr(db, "select_rows",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError))
    monkeypatch.setattr(db, "select_rows", lambda *a, **k: [])
    assert c.get("/seller/s1").status_code == 404


# --------------------------------------------------------------------------- #
# The leak, at the edge
# --------------------------------------------------------------------------- #
def test_no_seller_route_leaks_the_settings_column(client):
    c, db = client
    responses = [
        c.post("/seller", json={"email": "ada@example.com"}),
        c.get("/seller/list"),
        c.get("/seller/by-email?email=ada@example.com"),
        c.get("/seller/s1"),
        c.patch("/seller/s1", json={"brand": "x"}),
        c.get("/seller/s1/config"),
    ]
    for resp in responses:
        assert resp.status_code == 200, resp.status_code
        assert "CIPHERTEXT" not in resp.get_data(as_text=True), resp.request.path


def test_the_config_route_still_masks_rather_than_hides(client):
    # The masked view is the supported way to see config; it must keep working
    # (and keep saying a key IS set) after the column was dropped from reads.
    c, _ = client
    body = c.get("/seller/s1/config").get_json()
    assert body["ok"] is True
    assert body["config"]["llm"]["provider"] == "groq"
    assert body["config"]["llm"]["api_key"] == "********"


def test_the_config_route_is_the_only_reader_of_the_column(client):
    # Proves the masking is real: the column IS reachable, via exactly one
    # route, and that route masks it.
    c, db = client
    c.get("/seller/s1/config")
    assert any("settings" in (col or "") for col in db.selects)

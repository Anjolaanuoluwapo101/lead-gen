"""`/seller/me` — the dashboard's own-profile read.

WHY THIS ROUTE EXISTS

`/seller/by-email?email=<any>` returns the full safe row — `resume_text` and
`portfolio_text` included — and chooses which row to return from the REQUEST,
not from the caller's credential. So there is no value of `email` it must refuse:
one seller reads another's resume by typing their address.

That route is left alone here because n8n's workflow reads it, and gating it
could break the pipeline. `/seller/me` is the safe primitive the dashboard uses
instead: the id comes from the credential, so "fetch someone else's profile" is
not a request the route can express.

These tests pin that difference, because the two routes look interchangeable
from the outside and are not.
"""

import pytest

import app as flask_app
import auth
import lead_engine
import seller_ops


MINE = {"id": "seller-mine", "email": "me@example.com", "name": "Ada",
        "resume_text": "MY PRIVATE RESUME", "portfolio_text": "MY PORTFOLIO"}

THEIRS = {"id": "seller-theirs", "email": "them@example.com", "name": "Bob",
          "resume_text": "SOMEONE ELSE'S RESUME",
          "portfolio_text": "SOMEONE ELSE'S PORTFOLIO"}


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", "seller-mine")
    auth.clear_cache()
    yield
    auth.clear_cache()


@pytest.fixture
def db(monkeypatch):
    """Every seller row the routes can see, keyed by id and by email."""
    def get_seller(seller_id):
        # BOTH rows are reachable by id — so a test that fetches the wrong one
        # fails on the assertion rather than on a missing fixture row, which
        # would make the leak test pass for the wrong reason.
        for row in (MINE, THEIRS):
            if str(seller_id) == row["id"]:
                return dict(row)
        raise seller_ops.OpsError("not_found", "seller not found")

    def find_by_email(email):
        for row in (MINE, THEIRS):
            if row["email"] == email:
                return dict(row)
        raise seller_ops.OpsError("not_found", "seller not found")

    monkeypatch.setattr(seller_ops, "get_seller", get_seller)
    monkeypatch.setattr(seller_ops, "find_seller_by_email", find_by_email)
    return {"mine": MINE, "theirs": THEIRS}


# --------------------------------------------------------------------------- #
# The route returns the caller's own profile
# --------------------------------------------------------------------------- #
def test_it_returns_the_resolved_sellers_profile(client, db):
    body = client.get("/seller/me").get_json()
    assert body["ok"] is True
    assert body["seller"]["id"] == "seller-mine"
    assert body["seller"]["name"] == "Ada"


def test_the_shape_matches_what_n8n_reads_from_by_email(client, db):
    # `{"ok": true, "seller": {...}}` — the same nesting, so the dashboard and
    # the workflow can share response handling.
    body = client.get("/seller/me").get_json()
    assert set(body) == {"ok", "seller"}


def test_a_verified_user_gets_their_own_row_not_the_default(client, db,
                                                            monkeypatch):
    # The whole point: with a bearer token, the id comes from the token.
    monkeypatch.setattr(auth, "verify_token",
                        lambda token, client=None: {"id": "u", "email": "x@y.z"})
    monkeypatch.setattr(seller_ops, "create_or_update_seller",
                        lambda email, **kw: {"seller": dict(THEIRS)})
    body = client.get("/seller/me",
                      headers={"Authorization": "Bearer t"}).get_json()
    assert body["seller"]["id"] == "seller-theirs"


# --------------------------------------------------------------------------- #
# The bug it exists to prevent
# --------------------------------------------------------------------------- #
def test_there_is_no_way_to_name_another_seller(client, db):
    """A query param must not be able to redirect the read.

    This is the difference from /seller/by-email, which chooses the row from
    `?email=`. If /seller/me ever honours a caller-supplied id, it becomes that
    route again — with the leak attached.
    """
    for attempt in ("?email=them@example.com", "?seller_id=seller-theirs",
                    "?id=seller-theirs", "?seller_id=them@example.com"):
        body = client.get("/seller/me" + attempt).get_json()
        assert body["seller"]["id"] == "seller-mine", attempt
        assert "SOMEONE ELSE'S" not in body["seller"]["resume_text"]


def test_it_does_not_echo_a_body_supplied_seller_id(client, db):
    # `_resolve_read_seller` DOES honour a body seller_id on the legacy path —
    # that bridge is what keeps n8n working. It must not be reachable here
    # through a GET body, because that would recreate the leak.
    body = client.get("/seller/me", json={"seller_id": "seller-theirs"}).get_json()
    assert body["seller"]["id"] == "seller-mine"


def test_an_unresolvable_caller_is_refused_not_defaulted(client, db,
                                                         monkeypatch):
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", "")
    r = client.get("/seller/me")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_the_by_email_route_still_works_for_n8n(client, db):
    """Left un-gated on purpose, and this is the test that says so.

    Gating it would mean n8n looking up any address other than the default
    tenant's gets a 404 mid-workflow. That is a product decision, not a
    refactor — so it is pinned here rather than quietly changed.
    """
    body = client.get("/seller/by-email?email=them@example.com").get_json()
    assert body["seller"]["id"] == "seller-theirs"

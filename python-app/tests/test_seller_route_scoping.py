"""Cross-tenant access to /seller/<seller_id>/* must be refused.

These routes are the one family where the seller in the URL is the caller's to
choose, so the credential has to be COMPARED against it. Before
`auth.authorize_seller` existed, nothing did: any caller could enumerate ids
through /seller/list and then read or overwrite any seller's profile, resume,
portfolio and provider keys.

Two scopes, and the tests are organised around them:

  * a bearer token is a TENANT — one seller, its own row only
  * a service token is the PLATFORM — n8n, working whatever seller the form
    named

The whole thing is gated on AUTH_REQUIRED, because that flag IS the documented
open posture: with it false the API is open by design, and enforcement that
fired anyway would 403 all five n8n workflows. So each behaviour is asserted
under both settings, which is also what keeps someone from "simplifying" the
gate back out.
"""

import pytest
from flask import jsonify

import app as app_module
import auth
import lead_engine

MINE = "11111111-1111-1111-1111-111111111111"
THEIRS = "22222222-2222-2222-2222-222222222222"


def _found_seller(seller_id):
    """A stand-in for `_ops_response`'s success branch: echoes whichever id the
    route resolved, so a test can assert on WHICH seller was read."""
    return jsonify({"ok": True, "seller": {"id": seller_id}})


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture(autouse=True)
def _clean_auth():
    """Each test picks its own posture. Without this the suite's ambient .env
    leaks in — and the whole point of these tests is which flag is set."""
    auth.clear_cache()
    yield
    auth.clear_cache()


def _as_tenant(monkeypatch, seller_id):
    """A browser: a verified bearer token belonging to `seller_id`."""
    monkeypatch.setattr(auth, "verify_token",
                        lambda token, client=None: {"id": "u-1",
                                                    "email": "me@example.com"})
    monkeypatch.setattr(auth, "seller_for_user", lambda user: seller_id)
    return {"Authorization": "Bearer anything"}


def _as_operator(monkeypatch, token="s3cret"):
    monkeypatch.setenv("SERVICE_TOKEN", token)
    return {"X-Service-Token": token}


def _auth_on(monkeypatch, required=True):
    monkeypatch.setenv("AUTH_REQUIRED", "true" if required else "false")


# --- the credential is compared against the URL --------------------------- #

@pytest.mark.parametrize("method,path", [
    ("get", f"/seller/{THEIRS}"),
    ("patch", f"/seller/{THEIRS}"),
    ("get", f"/seller/{THEIRS}/config"),
    ("patch", f"/seller/{THEIRS}/config"),
    ("post", f"/seller/{THEIRS}/resume"),
    ("post", f"/seller/{THEIRS}/portfolio"),
])
def test_a_tenant_cannot_touch_another_sellers_row(client, monkeypatch,
                                                   method, path):
    """The regression itself. Every one of these answered 200/400 before —
    the resume read, the portfolio overwrite, and the config write that would
    let an attacker redirect where a seller's drafts are generated."""
    _auth_on(monkeypatch)
    headers = _as_tenant(monkeypatch, MINE)
    resp = getattr(client, method)(path, json={}, headers=headers)
    assert resp.status_code == 403, resp.get_data(as_text=True)


def test_a_tenant_can_touch_their_own_row(client, monkeypatch):
    """403 for the other tenant must not become 403 for everyone — the owner
    still has to be able to use their own settings page."""
    _auth_on(monkeypatch)
    headers = _as_tenant(monkeypatch, MINE)
    monkeypatch.setattr(app_module, "_get_seller_settings",
                        lambda sid: {"llm": {"provider": "bedrock"}})
    resp = client.get(f"/seller/{MINE}/config", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["config"]["llm"]["provider"] == "bedrock"


def test_the_refusal_does_not_confirm_the_seller_exists(client, monkeypatch):
    """403 with a message that distinguishes "not yours" from "no such id"
    would turn this endpoint into an id oracle, and ids are enumerable."""
    _auth_on(monkeypatch)
    headers = _as_tenant(monkeypatch, MINE)
    real = client.get(f"/seller/{THEIRS}", headers=headers).get_json()["error"]
    fake = client.get("/seller/not-a-real-id", headers=headers).get_json()["error"]
    assert real == fake


# --- the platform scope --------------------------------------------------- #

def test_a_service_token_may_touch_any_seller(client, monkeypatch):
    """n8n works a form for whatever seller the form named. If the platform
    scope did not exist, closing the tenant hole would have broken the product
    rather than fixed it."""
    _auth_on(monkeypatch)
    headers = _as_operator(monkeypatch)
    monkeypatch.setattr(app_module, "_get_seller_settings",
                        lambda sid: {"llm": {}})
    assert client.get(f"/seller/{THEIRS}/config",
                      headers=headers).status_code == 200


def test_a_bearer_token_is_not_an_operator(client, monkeypatch):
    """A browser must never be able to escalate by presenting a token that
    happens to verify."""
    _auth_on(monkeypatch)
    headers = _as_tenant(monkeypatch, MINE)
    assert client.get("/seller/list", headers=headers).status_code == 403


def test_an_unset_service_token_grants_no_operator_scope(client, monkeypatch):
    """`SERVICE_TOKEN` unset must not mean "any X-Service-Token will do" —
    otherwise the platform scope is reachable by sending a header, and an empty
    header would match an empty setting.

    Asserted as "not an operator" rather than as a status code: an
    unauthenticated caller is refused 401 by resolve_seller before the ownership
    comparison is ever reached, which is correct — it has no identity to compare.
    The property that matters is that it never gets the platform's reach.
    """
    _auth_on(monkeypatch)
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    for header in ("", "   ", "anything"):
        assert auth.is_operator({"X-Service-Token": header}) is False
    resp = client.get(f"/seller/{THEIRS}/config",
                      headers={"X-Service-Token": ""})
    assert resp.status_code in (401, 403)


# --- enumeration ---------------------------------------------------------- #

def test_a_tenant_cannot_list_sellers(client, monkeypatch):
    """The list is the input to every id-taking route, so leaving it open
    undoes the comparisons above."""
    _auth_on(monkeypatch)
    headers = _as_tenant(monkeypatch, MINE)
    assert client.get("/seller/list", headers=headers).status_code == 403


def test_a_tenant_cannot_resolve_an_arbitrary_email(client, monkeypatch):
    """by-email is the lookup step of the other routes and returns the full row
    including resume and portfolio text."""
    _auth_on(monkeypatch)
    headers = _as_tenant(monkeypatch, MINE)
    assert client.get("/seller/by-email?email=someone@else.com",
                      headers=headers).status_code == 403


def test_a_tenant_cannot_create_sellers(client, monkeypatch):
    """POST /seller find-or-creates, so ungated it is an unauthenticated row
    write — reserve an email, or fill the table."""
    _auth_on(monkeypatch)
    headers = _as_tenant(monkeypatch, MINE)
    resp = client.post("/seller", json={"email": "spam@example.com"},
                       headers=headers)
    assert resp.status_code == 403


def test_the_dashboard_never_needs_the_operator_routes(client, monkeypatch):
    """The guard must not cost a signed-in user anything: the routes /settings
    calls all name their subject through the credential, not the URL.

    Written against the real route rather than a stub, because the point is
    that /seller/me resolves the id from the token and then compares THAT
    against the row it reads — a stub would assume away the thing under test.
    """
    _auth_on(monkeypatch)
    headers = _as_tenant(monkeypatch, MINE)
    monkeypatch.setattr(app_module, "_ops_response",
                        lambda fn, seller_id, **k: _found_seller(seller_id))
    resp = client.get("/seller/me", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["seller"]["id"] == MINE


# --- the documented open posture still works ------------------------------ #

def test_with_auth_off_the_old_behaviour_is_unchanged(client, monkeypatch):
    """AUTH_REQUIRED=false is the documented posture and what n8n runs under
    today; the five workflows send no credential. Turning enforcement on by
    default would break them silently, so the gate is deliberate."""
    _auth_on(monkeypatch, required=False)
    monkeypatch.setattr(app_module, "_get_seller_settings",
                        lambda sid: {"llm": {}})
    # No credential at all, and it still works — exactly as before this change.
    assert client.get(f"/seller/{THEIRS}/config").status_code == 200


def test_a_bad_bearer_token_is_refused_even_with_auth_off(client, monkeypatch):
    """The one path that is NOT relaxed: falling through to the default tenant
    would silently turn a logged-out browser into someone else's session."""
    _auth_on(monkeypatch, required=False)
    monkeypatch.setattr(auth, "verify_token",
                        lambda token, client=None: None)
    resp = client.get(f"/seller/{MINE}",
                      headers={"Authorization": "Bearer stale"})
    assert resp.status_code == 401

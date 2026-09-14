"""auth.py decides who every route thinks you are, so its failure modes are the
expensive kind.

Three of them are worth naming, because each is a silent security bug that no
happy-path test would catch:

1. **An unset SERVICE_TOKEN must not match an absent header.** If "no secret
   configured" counted as "secret matched", the default tenant would be
   reachable by sending nothing at all.
2. **A bad bearer token must be refused even when AUTH_REQUIRED is false.**
   Falling through to the default tenant would turn a logged-out browser into a
   different tenant's session.
3. **A network failure is not an invalid token.** Conflating them logs every
   user out during a Supabase blip.
"""

import pytest

import auth
import seller_ops


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    def __init__(self, status_code=200, payload=None, raises=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {
            "id": "auth-user-1", "email": "Ada@Example.com"}
        self.raises = raises
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers})
        if self.raises:
            raise self.raises
        return FakeResponse(self.status_code, self.payload)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    auth.clear_cache()
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("SERVICE_TOKEN", "svc-secret")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setattr("lead_engine.DEFAULT_SELLER_ID", "default-seller", raising=False)
    yield
    auth.clear_cache()


@pytest.fixture
def created(monkeypatch):
    """Capture the seller lookups the resolver performs."""
    box = []

    def fake(email):
        box.append(email)
        return {"created": True, "updated": False,
                "seller": {"id": "seller-42", "email": email}}

    monkeypatch.setattr(seller_ops, "create_or_update_seller", fake)
    return box


# --------------------------------------------------------------------------- #
# The n8n path — nothing may change here
# --------------------------------------------------------------------------- #
def test_no_credentials_falls_back_to_the_default_tenant():
    # n8n sends no token. If this ever returns None, all five workflows 400.
    assert auth.resolve_seller({}, {}) == "default-seller"


def test_a_body_seller_id_wins_over_the_default():
    assert auth.resolve_seller({"seller_id": "s-from-body"}, {}) == "s-from-body"


def test_a_correct_service_token_maps_to_the_default_tenant():
    assert auth.resolve_seller(
        {}, {"X-Service-Token": "svc-secret"}) == "default-seller"


def test_a_wrong_service_token_falls_through_to_the_body():
    assert auth.resolve_seller(
        {"seller_id": "s-body"}, {"X-Service-Token": "wrong"}) == "s-body"


def test_an_absent_header_does_not_match_an_unset_service_token(monkeypatch):
    # THE TRAP. Unset secret + unsent header must NOT be a match, or the default
    # tenant is reachable by presenting nothing.
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    assert auth._service_token_ok(None, auth.service_token()) is False
    assert auth._service_token_ok("", auth.service_token()) is False


def test_an_empty_header_does_not_match_a_configured_token():
    assert auth._service_token_ok("", "svc-secret") is False


# --------------------------------------------------------------------------- #
# The browser path
# --------------------------------------------------------------------------- #
def test_a_valid_token_resolves_to_that_users_seller(created):
    client = FakeClient()
    sid = auth.resolve_seller({}, {"Authorization": "Bearer good-token"},
                              client=client)
    assert sid == "seller-42"
    # The email is lowercased before the lookup, matching seller_profile's
    # natural key (which is stored lowercase).
    assert created == ["ada@example.com"]


def test_repeat_polls_reuse_the_cached_seller_id(created):
    # A dashboard polls every 5s; each poll resolving the seller must not
    # re-read the profile row. The mapping is cached for CACHE_TTL_S, so
    # two resolves cost one lookup. Misses are never cached (first sight
    # still creates), and signup-time behaviour is unchanged.
    client = FakeClient()
    headers = {"Authorization": "Bearer good-token"}
    assert auth.resolve_seller({}, headers, client=client) == "seller-42"
    assert auth.resolve_seller({}, headers, client=client) == "seller-42"
    assert created == ["ada@example.com"]


def test_the_bearer_prefix_is_case_insensitive(created):
    auth.resolve_seller({}, {"Authorization": "bearer good-token"},
                        client=FakeClient())
    assert created == ["ada@example.com"]


def test_the_auth_header_beats_the_body_seller_id(created):
    # Otherwise a logged-in browser could name any tenant in the body.
    sid = auth.resolve_seller({"seller_id": "someone-elses-id"},
                              {"Authorization": "Bearer good"}, client=FakeClient())
    assert sid == "seller-42"


def test_an_invalid_token_is_refused(created):
    client = FakeClient(status_code=401, payload={"msg": "invalid"})
    with pytest.raises(auth.AuthError) as e:
        auth.resolve_seller({}, {"Authorization": "Bearer bad"}, client=client)
    assert e.value.status == 401


def test_an_invalid_token_is_refused_even_when_auth_is_not_required(created):
    # THE TRAP. Falling through here would hand a logged-out browser the
    # default tenant's data instead of an error.
    client = FakeClient(status_code=401, payload={})
    with pytest.raises(auth.AuthError):
        auth.resolve_seller({"seller_id": "s-body"},
                            {"Authorization": "Bearer bad"}, client=client)


def test_a_user_with_no_email_cannot_be_matched(monkeypatch):
    client = FakeClient(payload={"id": "u1", "email": ""})
    with pytest.raises(auth.AuthError) as e:
        auth.resolve_seller({}, {"Authorization": "Bearer t"}, client=client)
    assert e.value.status == 403


def test_a_seller_who_cannot_be_created_is_reported_not_swallowed(monkeypatch):
    def boom(email):
        raise seller_ops.OpsError("upstream", "database down")

    monkeypatch.setattr(seller_ops, "create_or_update_seller", boom)
    with pytest.raises(auth.AuthError) as e:
        auth.resolve_seller({}, {"Authorization": "Bearer t"}, client=FakeClient())
    assert e.value.status == 503


# --------------------------------------------------------------------------- #
# Enforcement
# --------------------------------------------------------------------------- #
def test_auth_required_refuses_an_anonymous_caller():
    import os
    os.environ["AUTH_REQUIRED"] = "true"
    try:
        with pytest.raises(auth.AuthError) as e:
            auth.resolve_seller({}, {})
        assert e.value.status == 401
    finally:
        os.environ["AUTH_REQUIRED"] = "false"


def test_auth_required_still_admits_the_service_token(monkeypatch):
    # n8n must keep working with enforcement ON — that is what the service
    # token exists for.
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    assert auth.resolve_seller(
        {}, {"X-Service-Token": "svc-secret"}) == "default-seller"


def test_auth_required_still_admits_a_real_user(created, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    assert auth.resolve_seller(
        {}, {"Authorization": "Bearer t"}, client=FakeClient()) == "seller-42"


def test_auth_required_defaults_to_off_when_unset(monkeypatch):
    monkeypatch.delenv("AUTH_REQUIRED", raising=False)
    assert auth.auth_required() is False


# --------------------------------------------------------------------------- #
# Verification: caching, and what counts as valid
# --------------------------------------------------------------------------- #
def test_verification_is_cached_across_calls(created):
    # The dashboard polls every 5s; without this every poll is an Auth call.
    # `created` stubs the seller look-up too — without it this reaches the real
    # Supabase, which the network guard in conftest.py now refuses.
    client = FakeClient()
    auth.resolve_seller({}, {"Authorization": "Bearer t"}, client=client)
    auth.resolve_seller({}, {"Authorization": "Bearer t"}, client=client)
    assert len(client.calls) == 1


def test_the_cache_is_keyed_on_the_token_not_shared(created):
    client = FakeClient()
    auth.resolve_seller({}, {"Authorization": "Bearer one"}, client=client)
    auth.resolve_seller({}, {"Authorization": "Bearer two"}, client=client)
    assert len(client.calls) == 2


def test_the_raw_token_is_never_used_as_a_cache_key():
    # A cache dump or a log line must not yield a usable session.
    key = auth._token_key("super-secret-token")
    assert "super-secret-token" not in key
    assert len(key) == 64


def test_a_refused_token_is_not_re_asked_on_every_call():
    client = FakeClient(status_code=401, payload={})
    for _ in range(3):
        with pytest.raises(auth.AuthError):
            auth.resolve_seller({}, {"Authorization": "Bearer bad"}, client=client)
    assert len(client.calls) == 1


def test_clear_cache_forces_a_fresh_verification(created):
    client = FakeClient()
    auth.resolve_seller({}, {"Authorization": "Bearer t"}, client=client)
    auth.clear_cache()
    auth.resolve_seller({}, {"Authorization": "Bearer t"}, client=client)
    assert len(client.calls) == 2


def test_the_anon_key_and_url_are_sent_to_supabase():
    client = FakeClient()
    auth.verify_token("t", client=client)
    sent = client.calls[0]["headers"]
    assert sent["apikey"] == "anon-key"
    assert sent["Authorization"] == "Bearer t"
    assert client.calls[0]["url"] == "https://proj.supabase.co/auth/v1/user"


def test_a_missing_anon_key_is_a_503_not_a_silent_allow(monkeypatch):
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    with pytest.raises(auth.AuthError) as e:
        auth.resolve_seller({}, {"Authorization": "Bearer t"})
    assert e.value.status == 503


def test_a_network_failure_is_not_reported_as_a_bad_token():
    # THE TRAP. If this were a 401, a Supabase blip would log everyone out.
    client = FakeClient(raises=RuntimeError("connection reset"))
    with pytest.raises(auth.AuthError) as e:
        auth.resolve_seller({}, {"Authorization": "Bearer t"}, client=client)
    assert e.value.status == 503


def test_a_500_from_supabase_is_not_treated_as_valid():
    client = FakeClient(status_code=500, payload={"id": "u", "email": "a@b.co"})
    assert auth.verify_token("t", client=client) is None


def test_a_200_with_no_user_id_is_not_valid():
    client = FakeClient(payload={"email": "a@b.co"})
    assert auth.verify_token("t", client=client) is None


def test_unparseable_json_is_not_valid():
    client = FakeClient(payload=ValueError("not json"))
    assert auth.verify_token("t", client=client) is None


def test_an_empty_token_never_reaches_the_network():
    client = FakeClient()
    assert auth.verify_token("", client=client) is None
    assert auth.verify_token("   ", client=client) is None
    assert client.calls == []


# --------------------------------------------------------------------------- #
# Through a real route — the error handler is what makes AuthError a status
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    import app as flask_app
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def test_a_bad_token_through_a_route_is_401_not_400(client, monkeypatch):
    # Without the errorhandler, AuthError would escape as a 500 — or, if the
    # resolver swallowed it, as a 400 "no seller", telling a logged-out user
    # their request was malformed instead of telling them to log in.
    monkeypatch.setattr(auth, "verify_token", lambda token, client=None: None)
    r = client.post("/leads", json={"campaign_id": "c1"},
                    headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401
    body = r.get_json()
    assert body["ok"] is False and "token" in body["error"]


def test_an_unconfigured_auth_through_a_route_is_503(client, monkeypatch):
    # Config is missing, so we cannot tell — which must not read as "valid".
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    r = client.post("/leads", json={"campaign_id": "c1"},
                    headers={"Authorization": "Bearer t"})
    assert r.status_code == 503


def test_a_request_with_no_credential_still_reaches_the_route(client, monkeypatch):
    # n8n's path. It must NOT be rejected by auth — proving the swap did not
    # quietly turn the default tenant into an error.
    import lead_engine
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", "default-seller",
                        raising=False)
    seen = {}
    real = auth.resolve_seller          # capture before patching, or the fake
                                        # calls itself forever
    def fake(data, headers):
        seen["seller"] = real(data, headers)
        raise auth.AuthError("stop here", status=418)

    monkeypatch.setattr(auth, "resolve_seller", fake)
    r = client.post("/leads", json={"campaign_id": "c1"})
    assert r.status_code == 418
    assert seen["seller"] == "default-seller"


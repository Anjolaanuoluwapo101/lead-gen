"""Make python-app/ importable, and keep the tests off the network.

Every module there imports its siblings by bare name (import leads_read), so
python-app/ must be on sys.path.
"""

import os
import sys

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


class NetworkBlocked(AssertionError):
    """Raised instead of making a real request."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """Fail any test that reaches for a real socket.

    WHY THIS EXISTS

    The suite loads the repo's `.env`, so `supabase_store` is fully configured
    in tests — `SUPABASE_URL` and the service key are real, and the project is a
    real one. A test that forgets to stub a read does not fail; it quietly
    queries the LIVE database. That happened: a run-page test passed an id that
    was not a uuid, the request went out, and Postgres answered with a 22P02
    instead of nothing at all. Reading is the mild case. A test that reaches a
    write path would mutate production rows and still report success.

    So the guard is a failure, not a warning: a test that needs the database
    must stub it, and a test that needs the real thing must say so out loud
    with `@pytest.mark.live`.
    """
    if request.node.get_closest_marker("live"):
        return

    import requests

    def blocked(method):
        def _call(url, *args, **kwargs):
            raise NetworkBlocked(
                f"a test tried to {method.upper()} {url!r}. Stub the call, or "
                f"mark the test @pytest.mark.live to allow real requests.")
        return _call

    for name in ("get", "post", "patch", "put", "delete", "request", "head"):
        monkeypatch.setattr(requests, name, blocked(name), raising=False)
    # Sessions bypass the module-level verbs above: Session.post never calls
    # requests.post. Every session in the codebase funnels through
    # Session.request, so that is the choke point. Fake sessions in tests do
    # not touch it, so stubbed tests are unaffected.
    monkeypatch.setattr("requests.sessions.Session.request",
                        blocked("session"), raising=False)


@pytest.fixture(autouse=True)
def pinned_send_and_file_backends(monkeypatch):
    """Pin the two outbound switches, for the same reason as AGENT_BACKEND.

    A developer who sets SEND_BACKEND=smtp in `.env` — exactly what you do to
    demo a real send — would otherwise change what every route test exercises:
    `recipient_blocked` would stop refusing, and the tests asserting the honest
    default would fail for a reason that has nothing to do with the code.

    `none` is also the safe value: it is the default, and it is the only one
    under which an un-stubbed code path cannot reach a real mail provider.
    Tests that want a live backend set it themselves.

    The credential vars are cleared for the same reason, and this is not
    belt-and-braces: with SEND_BACKEND pinned to `none` but SEND_FROM_EMAIL left
    in the environment, a test that set only the BACKEND saw a configured
    sender and opened a real SMTP connection to a hostname from the
    developer's own `.env`. The switches decide WHETHER, the credentials decide
    WHETHER IT CAN — leaving one side pinned and the other live is worse than
    pinning neither, because it looks hermetic.
    """
    monkeypatch.setenv("SEND_BACKEND", "none")
    monkeypatch.setenv("FILE_BACKEND", "none")
    for name in ("SEND_FROM_EMAIL", "SEND_FROM_NAME", "SMTP_HOST", "SMTP_PORT",
                 "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def local_agent_backend(monkeypatch):
    """Pin the executor so the suite does not depend on the developer's .env.

    This is the same failure mode as the network guard below, one layer up.
    `.env` is loaded by lead_engine at import time, so an operator who sets
    AGENT_BACKEND=agentcore to drive the DEPLOYED runtime -- exactly what you do
    to demo the switch -- would silently change what every route test exercises.
    They would start asserting against the AgentCore path and fail, or worse,
    keep passing while testing something other than what they name.

    Tests that specifically want the remote backend set it themselves; that is
    an explicit, visible choice rather than an accident of local config.
    """
    monkeypatch.setenv("AGENT_BACKEND", "local")


@pytest.fixture(autouse=True)
def pinned_auth_posture(monkeypatch):
    """Pin the enforcement switch to the documented default.

    Third instance of the same failure mode. `AUTH_REQUIRED` lives in `.env`,
    and an operator who sets it to `true` — which is what you do before putting
    the API anywhere public — would otherwise watch 57 tests fail across files
    that have nothing to do with authorization: they call the seller routes
    with no credential and assert on response SHAPE, so they get a 401 where
    they expect a 404 or a wrapper key. Nothing in those names says "and auth is
    off", so the failures read as a real regression.

    Authorization itself is fully covered, from both sides, in
    test_seller_route_scoping.py — every test there sets the flag explicitly
    rather than inheriting it. So pinning here does not leave the enforced
    posture untested; it stops the OTHER tests from silently joining in.
    """
    monkeypatch.setenv("AUTH_REQUIRED", "false")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: this test may make real network requests")

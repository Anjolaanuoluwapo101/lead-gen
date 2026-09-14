"""How long we are willing to wait for a DataForSEO answer.

Both live endpoints are slow by design: they run a real Google search and wait
for it, then hand back the finished SERP. Measured against the live API with
the exact request `scrape_maps` builds:

    maps SERP, depth 20, location_code 1010294  ->  119.0s and 76.3s
    (status 20000 Ok., cost 0.002, 20 items returned both times)

The app used to cap this at 60 seconds. Every search was therefore abandoned
before the answer arrived, `find_leads` returned `find_failed`, no campaign was
ever created, and the agent burned its whole find budget retrying -- which is
exactly the "run is slow and campaign not returning any campaign results"
report. A timeout shorter than the operation it guards is not a safety net, it
is a guaranteed failure.

These tests pin the cap above the measured worst case so the next person to
"tighten up" a timeout has to argue with the measurement.
"""

import pytest

import dataforseo

# The slowest honest call we have measured. Anything at or below this abandons
# real searches, so the cap must clear it with room for a bad day.
MEASURED_SLOWEST_S = 119.0


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _stub_post(monkeypatch, capture, body=None):
    payload = body if body is not None else {
        "tasks": [{"status_code": 20000, "status_message": "Ok.",
                   "result": [{"items": [{"title": "Bake2moist",
                                          "phone": "+2348082328185"}]}]}]
    }

    def fake_post(url, json=None, auth=None, timeout=None):
        capture["url"] = url
        capture["timeout"] = timeout
        return FakeResp(payload)

    class FakeSession:
        def post(self, *args, **kwargs):
            return fake_post(*args, **kwargs)

    monkeypatch.setattr(dataforseo, "LOGIN", "l")
    monkeypatch.setattr(dataforseo, "PASSWORD", "p")
    monkeypatch.setattr(dataforseo, "_api_session", lambda: FakeSession())


# --------------------------------------------------------------------------- #
# The finder: the call that was failing
# --------------------------------------------------------------------------- #
def test_a_maps_search_waits_longer_than_the_slowest_measured_answer(monkeypatch):
    sent = {}
    _stub_post(monkeypatch, sent)
    # location_code given, so nothing reaches the location directory.
    dataforseo.scrape_maps("bakery", "Lagos, Nigeria", location_code=1010294)

    assert sent["url"] == dataforseo.LIVE_MAPS_URL
    assert sent["timeout"] > MEASURED_SLOWEST_S, (
        "a %ss cap abandons searches that really do take %ss"
        % (sent["timeout"], MEASURED_SLOWEST_S))


def test_the_single_business_lookup_gets_the_same_patience(monkeypatch):
    # Same endpoint family, same shape of wait. Leaving this one at 60 while
    # raising the other is the kind of asymmetry that comes back as a bug.
    sent = {}
    _stub_post(monkeypatch, sent, body={
        "tasks": [{"status_code": 20000, "status_message": "Ok.",
                   "result": [{"items": [{"title": "Mike's Plumbing"}]}]}]})
    dataforseo.lookup_business(keyword="Mike's Plumbing")

    assert sent["url"] == dataforseo.BUSINESS_INFO_URL
    assert sent["timeout"] > MEASURED_SLOWEST_S


def test_the_cap_can_be_raised_without_a_code_change(monkeypatch):
    # On a bad day the endpoint may be slower still; the operator needs a dial
    # that is not a redeploy.
    monkeypatch.setenv("DATAFORSEO_TIMEOUT_S", "300")
    assert dataforseo.request_timeout_s() == 300


def test_the_default_cap_is_sane(monkeypatch):
    monkeypatch.delenv("DATAFORSEO_TIMEOUT_S", raising=False)
    assert dataforseo.request_timeout_s() > MEASURED_SLOWEST_S


def test_a_junk_cap_falls_back_rather_than_crashing(monkeypatch):
    # An unset or unparseable env var must not take the finder down.
    for junk in ("", "   ", "soon", "0", "-5"):
        monkeypatch.setenv("DATAFORSEO_TIMEOUT_S", junk)
        assert dataforseo.request_timeout_s() > MEASURED_SLOWEST_S

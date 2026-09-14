"""Adding ONE business by name -- the second way in.

The finder can only express "show me plumbers in Austin". This expresses "add
Mike's Plumbing": a referral, a shop the seller drove past, someone from a
networking event. Four properties matter and each is a way the feature could be
quietly wrong:

1. **The id forms are a PREFIX INSIDE `keyword`.** DataForSEO documents `cid`
   and `place_id` as identifiers, but they are not request fields -- they go
   into `keyword` as "cid:..." / "place_id:...". Sending them as their own keys
   gets a lookup that silently searches for the literal name of an id.
2. **A hand-added business has no keyword and no location**, so it cannot be a
   normal campaign -- yet it cannot be campaign-less either, because ownership
   runs through `campaigns.seller_id` and an ownerless prospect is a row no
   route can authorise. Hence one synthetic per-seller campaign whose key does
   NOT include the business (or every add would mint a campaign).
3. **The stored row must have the same keys as a found one.** A second shape
   means a second code path downstream, and the two drift.
4. **It stores and stops.** No crawl, no score, no run -- so it cannot spend
   anything beyond its own single billable lookup.
"""

import pytest

import app as flask_app
import dataforseo
import seller_ops

SELLER = "11111111-1111-1111-1111-111111111111"

# What `map_item` returns, used to pin the shapes together. If either side
# renames a key, the prospect insert and everything downstream see it.
FINDER_KEYS = set(dataforseo.map_item({"title": "X"}).keys())

ITEM = {
    "title": "Mike's Plumbing",
    "category": "Plumber",
    "phone": "+1 512-555-0100",
    "url": "https://mikesplumbing.example",
    "address": "100 Main St, Austin, TX 78701",
    "place_id": "GhIJQWdl0CIeQUARxks3icF8U8A",
    "cid": 194604053573767737,
    "is_claimed": False,
    "rating": {"value": 4.4, "votes_count": 87},
    "book_online_url": "",
    "latitude": 30.27, "longitude": -97.74,
    "total_photos": 0,
    "work_time": {
        "current_status": "closed",
        "work_hours": {"timetable": {
            "monday": [{"open": {"hour": 9, "minute": 0},
                        "close": {"hour": 17, "minute": 0}}],
            "tuesday": [{"open": {"hour": 8, "minute": 30},
                         "close": {"hour": 16, "minute": 0}}],
        }},
    },
    "attributes": [{"name": "Online appointments"}],
    "place_topics": {"plumbing repair": 12},
    "rating_distribution": {"1": 3, "2": 1, "3": 4, "4": 10, "5": 69},
}


# --------------------------------------------------------------------------- #
# Addressing: exact ids beat a name
# --------------------------------------------------------------------------- #
def test_a_place_id_is_prefixed_into_the_keyword():
    # Not a separate request field -- see the module docstring.
    assert dataforseo._business_keyword(place_id="GhIJ") == "place_id:GhIJ"


def test_a_cid_is_prefixed_into_the_keyword():
    assert dataforseo._business_keyword(cid="194604") == "cid:194604"


def test_an_already_prefixed_value_is_not_prefixed_twice():
    assert dataforseo._business_keyword(cid="cid:194604") == "cid:194604"
    assert (dataforseo._business_keyword(place_id="place_id:GhIJ")
            == "place_id:GhIJ")


def test_an_exact_id_beats_a_name():
    """The reason this order exists: a bare name resolves to the best match,
    which for a common business name is frequently the wrong location."""
    assert dataforseo._business_keyword(
        place_id="GhIJ", cid="999", keyword="Mike's Plumbing") == "place_id:GhIJ"
    assert dataforseo._business_keyword(
        cid="999", keyword="Mike's Plumbing") == "cid:999"


def test_a_bare_name_is_passed_through():
    assert (dataforseo._business_keyword(keyword=" Mike's Plumbing ")
            == "Mike's Plumbing")


def test_whitespace_is_not_an_identifier():
    assert dataforseo._business_keyword(place_id="  ", cid=" ", keyword="  ") == ""


# --------------------------------------------------------------------------- #
# The mapped row
# --------------------------------------------------------------------------- #
def test_the_row_has_exactly_the_finder_keys():
    """Ties the two halves together across a rename.

    A hand-added business flows through the same prospect insert as a found
    one, so a key this function forgets is a column that silently goes blank
    for every hand-added business and only for those. `business_info` is the
    one permitted addition -- it rides in `raw_payload`, needing no column.
    """
    assert set(dataforseo.map_business_info(ITEM)) - {"business_info"} == FINDER_KEYS


def test_the_extra_business_info_rides_along_without_new_columns():
    row = dataforseo.map_business_info(ITEM)
    info = row["business_info"]
    assert info["total_photos"] == 0            # a real, checkable defect
    assert info["current_status"] == "closed"
    assert info["rating_distribution"]["5"] == 69
    assert info["attributes"] == [{"name": "Online appointments"}]
    # And it did not leak into the finder-shaped keys.
    assert "total_photos" not in {k for k in row if k != "business_info"}


def test_a_dict_rating_is_unpacked():
    row = dataforseo.map_business_info(ITEM)
    assert row["rating"] == "4.4"
    assert row["review_count"] == "87"


def test_a_bare_number_rating_is_tolerated():
    """The shape differs between endpoints; losing a real rating to that would
    be a silent data loss."""
    row = dataforseo.map_business_info(dict(ITEM, rating=4.9))
    assert row["rating"] == "4.9"


@pytest.mark.parametrize("claimed,expected", [
    (True, True), (False, False), (None, ""), ("", "")])
def test_claim_status_keeps_its_three_states(claimed, expected):
    # "" means unknown, which is NOT the same as False -- writing an email that
    # says "your listing is unclaimed" on the strength of Google's silence is
    # the failure this protects against.
    row = dataforseo.map_business_info(dict(ITEM, is_claimed=claimed))
    assert row["is_claimed"] == expected


def test_hours_are_flattened_from_the_structured_timetable():
    row = dataforseo.map_business_info(ITEM)
    assert "Monday 9:00-17:00" in row["hours"]
    assert "Tuesday 8:30-16:00" in row["hours"]


def test_a_listing_with_no_hours_yields_an_empty_string_not_a_crash():
    # The absence IS the information -- it is one of the gaps worth drafting
    # around, so it must survive as "" rather than raising. Note the shapes
    # nest three deep, and how deep Google stops supplying them is not ours to
    # choose, so every level is exercised.
    no_hours_key = {k: v for k, v in ITEM.items() if k != "work_time"}
    for item in (no_hours_key,
                 dict(ITEM, work_time=None),
                 dict(ITEM, work_time={}),
                 dict(ITEM, work_time={"work_hours": {}}),
                 dict(ITEM, work_time={"work_hours": {"timetable": None}}),
                 dict(ITEM, work_time={"work_hours": {"timetable": {}}})):
        assert dataforseo.map_business_info(item)["hours"] == ""


def test_no_matching_business_maps_to_none():
    assert dataforseo.map_business_info(None) is None
    assert dataforseo.map_business_info("not a dict") is None


# --------------------------------------------------------------------------- #
# lookup_business: the call and its failures
# --------------------------------------------------------------------------- #
def test_lookup_refuses_with_nothing_to_look_up(monkeypatch):
    monkeypatch.setattr(dataforseo, "LOGIN", "l")
    monkeypatch.setattr(dataforseo, "PASSWORD", "p")
    with pytest.raises(ValueError) as e:
        dataforseo.lookup_business()
    assert "place_id" in str(e.value)


def test_lookup_reports_missing_credentials(monkeypatch):
    monkeypatch.setattr(dataforseo, "LOGIN", "")
    monkeypatch.setattr(dataforseo, "PASSWORD", "")
    with pytest.raises(RuntimeError) as e:
        dataforseo.lookup_business(keyword="x")
    assert "DATAFORSEO_LOGIN" in str(e.value)


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeSession:
    """Stands in for the shared DataForSEO session: only .post is stubbed,
    anything else is a loud failure rather than real network."""

    def __init__(self, post):
        self._post = post

    def post(self, *args, **kwargs):
        return self._post(*args, **kwargs)

    def get(self, *args, **kwargs):
        raise AssertionError("unstubbed session GET in test")


def _stub_post(monkeypatch, body, capture=None):
    def fake_post(url, json=None, auth=None, timeout=None):
        assert url == dataforseo.BUSINESS_INFO_URL
        if capture is not None:
            capture.append(json)
        return FakeResp(body)
    monkeypatch.setattr(dataforseo, "LOGIN", "l")
    monkeypatch.setattr(dataforseo, "PASSWORD", "p")
    monkeypatch.setattr(dataforseo, "_api_session",
                        lambda: _FakeSession(fake_post))


def _body_with(items, status=20000, message=None):
    return {"tasks": [{"status_code": status, "status_message": message,
                       "result": [{"items": items}]}]}


def test_a_found_business_comes_back_as_a_row(monkeypatch):
    _stub_post(monkeypatch, _body_with([ITEM]))
    row, _body = dataforseo.lookup_business(keyword="Mike's Plumbing")
    assert row["business_name"] == "Mike's Plumbing"
    assert row["place_id"] == "GhIJQWdl0CIeQUARxks3icF8U8A"


def test_the_request_carries_the_prefixed_keyword_and_the_location(monkeypatch):
    sent = []
    _stub_post(monkeypatch, _body_with([ITEM]), capture=sent)
    dataforseo.lookup_business(place_id="GhIJ", location_name="Austin,Texas,US")
    assert sent[0] == [{"keyword": "place_id:GhIJ", "language_name": "English",
                        "location_name": "Austin,Texas,US"}]


def test_no_items_is_none_not_an_error(monkeypatch):
    _stub_post(monkeypatch, _body_with([]))
    row, _body = dataforseo.lookup_business(keyword="Nobody")
    assert row is None


def test_a_rejected_request_surfaces_the_reason(monkeypatch):
    """A bare name with no location is the common one. It is an operator
    problem with an operator fix, so the API's own message must reach them
    rather than a generic 'upstream failed'."""
    _stub_post(monkeypatch, _body_with([], status=40501,
                                       message="location is required"))
    with pytest.raises(RuntimeError) as e:
        dataforseo.lookup_business(keyword="Mike's Plumbing")
    assert "40501" in str(e.value) and "location is required" in str(e.value)


def test_an_item_with_no_title_is_skipped(monkeypatch):
    _stub_post(monkeypatch, _body_with([{"phone": "1"}, ITEM]))
    row, _body = dataforseo.lookup_business(keyword="x")
    assert row["business_name"] == "Mike's Plumbing"


# --------------------------------------------------------------------------- #
# The per-seller campaign
# --------------------------------------------------------------------------- #
def test_the_direct_campaign_key_does_not_include_the_business():
    """If it did, every add would mint its own campaign and the seller's list
    would fill with one-business campaigns."""
    a = seller_ops.direct_target_key("seller-1")
    b = seller_ops.direct_target_key("seller-1")
    assert a == b, "must be stable across calls"
    assert len(a) == 40


def test_two_sellers_do_not_share_a_direct_campaign():
    assert (seller_ops.direct_target_key("seller-1")
            != seller_ops.direct_target_key("seller-2"))


class FakeDB:
    def __init__(self, existing_campaign=None, existing_prospect=None):
        self.existing_campaign = existing_campaign or []
        self.existing_prospect = existing_prospect or []
        self.inserted = []
        self._id = 0

    def select_rows(self, table, columns=None, filters=None, limit=None):
        if table == "campaigns":
            return list(self.existing_campaign)
        return list(self.existing_prospect)

    def insert_rows(self, table, row):
        self._id += 1
        self.inserted.append((table, row))
        return [{"id": f"{table}-{self._id}", **row}]

    def configured(self):
        return True


def _db(monkeypatch, db):
    monkeypatch.setattr(seller_ops, "supabase_store", db)
    return db


def test_the_campaign_is_created_once_and_reused_after(monkeypatch):
    db = _db(monkeypatch, FakeDB())
    first = seller_ops.ensure_direct_campaign(SELLER)
    assert first == "campaigns-1"
    created = db.inserted[0][1]
    assert created["keyword"] == "direct" and created["location"] == "direct"
    assert created["seller_id"] == SELLER
    assert created["niche_rules"]["direct_add"] is True

    # A second call finds the existing row rather than making another.
    db.existing_campaign = [{"id": first}]
    assert seller_ops.ensure_direct_campaign(SELLER) == first
    assert len([t for t, _ in db.inserted if t == "campaigns"]) == 1


def test_the_campaign_allows_a_blank_keyword_and_location(monkeypatch):
    """`campaigns.keyword`/`location` are NOT NULL, which is exactly why a
    named business cannot be a normal campaign -- so the synthetic values must
    never be None/empty or the insert fails."""
    db = _db(monkeypatch, FakeDB())
    seller_ops.ensure_direct_campaign(SELLER)
    created = db.inserted[0][1]
    assert created["keyword"] and created["location"]


def test_adding_requires_a_business_name(monkeypatch):
    _db(monkeypatch, FakeDB())
    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.add_direct_prospect(SELLER, {"business_name": "  "})
    assert e.value.kind == "bad_request"


def test_adding_stores_the_prospect_and_stops(monkeypatch):
    """No crawl, no score, no draft, no run -- nothing here may reach for one."""
    db = _db(monkeypatch, FakeDB())
    out = seller_ops.add_direct_prospect(
        SELLER, dataforseo.map_business_info(ITEM))
    assert out["created"] is True and out["status"] == "discovered"
    table, payload = db.inserted[-1]
    assert table == "prospects"
    assert payload["business_name"] == "Mike's Plumbing"
    assert payload["campaign_id"] == "campaigns-1"      # the direct campaign
    assert payload["dedup_key"]
    # The whole mapped row, extras included, survives in raw_payload.
    assert payload["raw_payload"]["business_info"]["total_photos"] == 0


def test_adding_the_same_business_twice_reuses_the_row(monkeypatch):
    db = _db(monkeypatch, FakeDB())
    seller_ops.add_direct_prospect(SELLER, dataforseo.map_business_info(ITEM))
    db.existing_prospect = [{"id": "prospects-1", "status": "enriched"}]
    out = seller_ops.add_direct_prospect(SELLER,
                                         dataforseo.map_business_info(ITEM))
    assert out["created"] is False and out["prospect_id"] == "prospects-1"
    assert len([t for t, _ in db.inserted if t == "prospects"]) == 1


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    """A client whose auth outcome does not depend on the developer's .env.

    `AUTH_REQUIRED` is pinned false rather than inherited: the repo's `.env` is
    loaded by the suite, so a local `AUTH_REQUIRED=true` would 401 these routes
    and make the suite pass or fail on an unrelated setting.
    """
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def test_the_route_refuses_when_there_is_nothing_to_look_up(client):
    resp = client.post("/prospects/direct", json={"seller_id": SELLER})
    assert resp.status_code == 400
    assert "place_id" in resp.get_json()["error"]


def test_the_route_reports_a_name_that_matched_nothing(client, monkeypatch):
    monkeypatch.setattr(dataforseo, "lookup_business",
                        lambda **kw: (None, {}))
    resp = client.post("/prospects/direct",
                       json={"seller_id": SELLER, "keyword": "Nobody"})
    assert resp.status_code == 404
    assert "no business matched" in resp.get_json()["error"]


def test_the_route_surfaces_a_refused_lookup_as_502(client, monkeypatch):
    def boom(**kw):
        raise RuntimeError("DataForSEO business lookup failed (40501): "
                           "location is required")
    monkeypatch.setattr(dataforseo, "lookup_business", boom)
    resp = client.post("/prospects/direct",
                       json={"seller_id": SELLER, "keyword": "Mike's"})
    assert resp.status_code == 502
    assert "location is required" in resp.get_json()["error"]


def test_the_route_adds_the_business_and_returns_it(client, monkeypatch):
    monkeypatch.setattr(dataforseo, "lookup_business",
                        lambda **kw: (dataforseo.map_business_info(ITEM), {}))
    monkeypatch.setattr(seller_ops, "add_direct_prospect",
                        lambda sid, row, source="dataforseo":
                        {"prospect_id": "p1", "campaign_id": "c1",
                         "created": True, "status": "discovered"})
    out = client.post("/prospects/direct", json={
        "seller_id": SELLER, "keyword": "Mike's Plumbing"}).get_json()
    assert out["ok"] is True
    assert out["prospect_id"] == "p1"
    assert out["prospect"]["business_name"] == "Mike's Plumbing"


def test_the_route_passes_the_identifier_through_untouched(client, monkeypatch):
    seen = {}

    def capture(**kw):
        seen.update(kw)
        return dataforseo.map_business_info(ITEM), {}

    monkeypatch.setattr(dataforseo, "lookup_business", capture)
    monkeypatch.setattr(seller_ops, "add_direct_prospect",
                        lambda sid, row, source="dataforseo":
                        {"prospect_id": "p1", "campaign_id": "c1",
                         "created": True, "status": "discovered"})
    client.post("/prospects/direct", json={
        "seller_id": SELLER, "place_id": "GhIJ", "keyword": "ignored",
        "location": "Austin, TX"})
    assert seen["place_id"] == "GhIJ"
    assert seen["keyword"] == "ignored"       # precedence is dataforseo's job
    assert seen["location_name"] == "Austin, TX"


def test_a_blank_location_is_passed_as_none_not_empty_string(client,
                                                              monkeypatch):
    """An empty location_name would be sent to DataForSEO as a real value and
    match nothing, rather than being omitted."""
    seen = {}
    monkeypatch.setattr(dataforseo, "lookup_business",
                        lambda **kw: (seen.update(kw),
                                      (dataforseo.map_business_info(ITEM),
                                       {}))[1])
    monkeypatch.setattr(seller_ops, "add_direct_prospect",
                        lambda sid, row, source="dataforseo":
                        {"prospect_id": "p1", "campaign_id": "c1",
                         "created": True, "status": "discovered"})
    client.post("/prospects/direct",
                json={"seller_id": SELLER, "keyword": "x", "location": "  "})
    assert seen["location_name"] is None

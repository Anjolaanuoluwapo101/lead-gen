"""The compulsory search area picker and the disambiguation fix behind it.

Three layers, each tested where it lives:

1. dataforseo._best_location_match ranks by shared words (state codes
   expanded), so same named cities in different states stop resolving to the
   wrong country.
2. dataforseo.search_locations reads the cached world list with no credentials
   and no network, offering only pickable area types.
3. POST /runs accepts the picked code and pins it through the orchestrator into
   find_leads, so the model cannot wander off to another place mid run.
"""

import dataforseo
import lead_engine


# --------------------------------------------------------------------------- #
# _best_location_match: the logged defect, fixed
# --------------------------------------------------------------------------- #
AUSTINS = [
    {"location_code": 1, "location_name": "Austin,Manitoba,Canada",
     "location_type": "City"},
    {"location_code": 2, "location_name": "Austin,Texas,United States",
     "location_type": "City"},
    {"location_code": 200635, "location_name": "Austin, TX,Texas,United States",
     "location_type": "DMA Region"},
]


def test_austin_tx_resolves_to_texas_not_manitoba():
    # THE BUG. The old rank fell through to the shortest name, and
    # "Austin,Manitoba,Canada" (23 chars) beat "Austin,Texas,United States".
    best = dataforseo._best_location_match("Austin, TX", AUSTINS)
    assert best["location_name"] == "Austin,Texas,United States"


def test_houston_tx_resolves_to_texas_not_ohio():
    rivals = [
        {"location_code": 3, "location_name": "Houston,Ohio,United States",
         "location_type": "City"},
        {"location_code": 4, "location_name": "Houston,Texas,United States",
         "location_type": "City"},
    ]
    best = dataforseo._best_location_match("Houston, TX", rivals)
    assert best["location_name"] == "Houston,Texas,United States"


def test_a_bare_state_name_returns_the_state():
    # The old matcher had no candidate whose leading segment was the state
    # itself, so the whole state case returned None and the run fell back to a
    # name guess that matched nothing.
    best = dataforseo._best_location_match(
        "Texas", [{"location_code": 5, "location_name": "Texas,United States",
                   "location_type": "State"}])
    assert best["location_code"] == 5


def test_london_uk_meets_london_england():
    best = dataforseo._best_location_match(
        "London, UK",
        [{"location_code": 6, "location_name": "London,England,United Kingdom",
          "location_type": "City"}])
    assert best["location_code"] == 6


def test_nothing_shared_returns_none():
    assert dataforseo._best_location_match("Nowhereville Xyz", AUSTINS) is None


# --------------------------------------------------------------------------- #
# search_locations: the picker's data source (offline, credential free)
# --------------------------------------------------------------------------- #
def test_search_needs_no_credentials(monkeypatch):
    # The picker must work for a seller who never sees a key. If this read the
    # network or required a login, a signed in user would face a 502.
    monkeypatch.setattr(dataforseo, "LOGIN", "")
    monkeypatch.setattr(dataforseo, "PASSWORD", "")
    res = dataforseo.search_locations("Lagos", limit=5)
    assert res["count"] > 0


def test_search_puts_the_city_called_lagos_first():
    names = [m["location_name"] for m in
             dataforseo.search_locations("Lagos", limit=5)["matches"]]
    assert names[0] == "Lagos,Lagos,Nigeria"


def test_search_expands_a_state_code():
    names = [m["location_name"] for m in
             dataforseo.search_locations("Austin, TX", limit=3)["matches"]]
    assert names[0] == "Austin,Texas,United States"
    assert all("Manitoba" not in n for n in names)


def test_search_never_offers_postal_codes_or_parks():
    res = dataforseo.search_locations("Austin", limit=25)
    types = {str(m.get("location_type") or "").lower()
             for m in res["matches"]}
    assert "postal code" not in types
    assert res["count"] > 0


def test_search_matches_types_case_insensitively():
    # The directory carries lowercase variants; a case sensitive filter would
    # silently drop them from the picker.
    assert "city" in dataforseo.PICKER_TYPES
    res = dataforseo.search_locations("Chicago, IL", limit=5)
    assert res["matches"][0]["location_name"] == \
        "Chicago,Illinois,United States"


def test_search_refuses_a_single_character():
    assert dataforseo.search_locations("x")["matches"] == []


def test_every_match_carries_what_a_run_needs():
    for m in dataforseo.search_locations("Miami, FL", limit=5)["matches"]:
        assert int(m["location_code"]) > 0
        assert m["location_name"]


# --------------------------------------------------------------------------- #
# POST /location/search: the route the picker calls
# --------------------------------------------------------------------------- #
def test_the_search_route_answers(client=None):
    import app as flask_app
    flask_app.app.config["TESTING"] = True
    c = flask_app.app.test_client()
    r = c.post("/location/search", json={"q": "Lagos", "limit": 5})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["matches"][0]["location_name"] == "Lagos,Lagos,Nigeria"


# --------------------------------------------------------------------------- #
# POST /runs: the picked code travels to the orchestrator
# --------------------------------------------------------------------------- #
def test_a_run_accepts_a_picked_search_area(client=None):
    import threading
    from types import SimpleNamespace

    import app as flask_app
    import auth
    import routes_agent

    flask_app.app.config["TESTING"] = True
    c = flask_app.app.test_client()

    seen = {}

    class FakeRunStore:
        def create_run(self, **kw):
            return {"id": "run-1", "status": "running"}

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self):
            return {"ok": True}

    orig_store, orig_orch, orig_thread = (
        routes_agent.run_store, routes_agent.Orchestrator, threading.Thread)

    class SyncThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    routes_agent.run_store = FakeRunStore()
    routes_agent.Orchestrator = FakeOrchestrator
    routes_agent.threading = SimpleNamespace(Thread=SyncThread,
                                             Event=threading.Event)
    try:
        import lead_engine as le
        old_default = le.DEFAULT_SELLER_ID
        le.DEFAULT_SELLER_ID = "seller-1"
        auth.clear_cache()
        r = c.post("/runs", json={
            "goal": "Find 5 bakeries with no website",
            "location_code": 1010294,
            "location_name": "Lagos,Lagos,Nigeria",
            "scrutiny": "lenient", "target_qualified": 5,
            "niche": "web design"})
        le.DEFAULT_SELLER_ID = old_default
        auth.clear_cache()
    finally:
        routes_agent.run_store = orig_store
        routes_agent.Orchestrator = orig_orch
        routes_agent.threading = orig_thread

    assert r.status_code == 200
    assert seen["location_code"] == 1010294
    assert seen["location_name"] == "Lagos,Lagos,Nigeria"


def test_a_run_without_a_pick_is_400():
    # The old backward-compat rule (pin-less runs resolve the place from
    # goal prose) is retired: a run without a search area lets the model
    # search wherever its reading points, which is how runs ended up judging
    # the wrong city. Compulsion now holds end to end, dashboard and API.
    import threading
    from types import SimpleNamespace

    import app as flask_app
    import auth
    import routes_agent

    flask_app.app.config["TESTING"] = True
    c = flask_app.app.test_client()

    seen = {}

    class FakeRunStore:
        def create_run(self, **kw):
            return {"id": "run-1", "status": "running"}

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self):
            return {"ok": True}

    class SyncThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    orig_store, orig_orch, orig_thread = (
        routes_agent.run_store, routes_agent.Orchestrator, threading.Thread)
    routes_agent.run_store = FakeRunStore()
    routes_agent.Orchestrator = FakeOrchestrator
    routes_agent.threading = SimpleNamespace(Thread=SyncThread,
                                             Event=threading.Event)
    try:
        import lead_engine as le
        old_default = le.DEFAULT_SELLER_ID
        le.DEFAULT_SELLER_ID = "seller-1"
        auth.clear_cache()
        r = c.post("/runs", json={"goal": "find bakeries",
                                    "scrutiny": "lenient",
                                    "target_qualified": 5,
                                    "niche": "web design"})
        le.DEFAULT_SELLER_ID = old_default
        auth.clear_cache()
    finally:
        routes_agent.run_store = orig_store
        routes_agent.Orchestrator = orig_orch
        routes_agent.threading = orig_thread

    assert r.status_code == 400
    assert "location_code" in r.get_json()["error"]
    assert seen == {}, "a refused run must not reach the orchestrator"


def test_a_non_integer_location_code_is_400():
    import app as flask_app
    import auth

    flask_app.app.config["TESTING"] = True
    c = flask_app.app.test_client()
    import lead_engine as le
    old_default = le.DEFAULT_SELLER_ID
    le.DEFAULT_SELLER_ID = "seller-1"
    auth.clear_cache()
    r = c.post("/runs", json={"goal": "g", "location_code": "Lagos"})
    le.DEFAULT_SELLER_ID = old_default
    auth.clear_cache()
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Orchestrator: the pin reaches the model and the trace
# --------------------------------------------------------------------------- #
def test_the_prompt_names_the_pinned_area(monkeypatch):
    import agent.orchestrator as O
    import agent.run_store as run_store

    monkeypatch.setattr(run_store, "create_run",
                        lambda **k: {"id": "run-1", **k})
    orch = O.Orchestrator(seller_id="s", goal="find bakeries",
                          location_code=1010294,
                          location_name="Lagos,Lagos,Nigeria",
                          agent_factory=lambda ctx: (lambda prompt: None))
    prompt = orch._prompt()
    assert "1010294" in prompt
    assert "Lagos,Lagos,Nigeria" in prompt


def test_the_run_trace_records_the_pinned_area(monkeypatch):
    import agent.orchestrator as O
    import agent.run_store as run_store

    events = []
    monkeypatch.setattr(run_store, "create_run",
                        lambda **k: {"id": "run-1", **k})
    monkeypatch.setattr(run_store, "append_event",
                        lambda rid, kind, msg=None, payload=None:
                        events.append((kind, msg, payload)) or {})
    monkeypatch.setattr(run_store, "update_progress",
                        lambda rid, prog, spend=None: {})
    monkeypatch.setattr(run_store, "finish_run", lambda rid, **k: k)
    orch = O.Orchestrator(seller_id="s", goal="find bakeries",
                          location_code=1010294,
                          location_name="Lagos,Lagos,Nigeria",
                          agent_factory=lambda ctx: (lambda prompt: None))
    orch.run()
    kinds = [k for (k, _, _) in events]
    assert "place" in kinds
    place = next(p for (k, _, p) in events if k == "place")
    assert place["location_code"] == 1010294


def test_no_pin_means_no_place_event(monkeypatch):
    import agent.orchestrator as O
    import agent.run_store as run_store

    events = []
    monkeypatch.setattr(run_store, "create_run",
                        lambda **k: {"id": "run-1", **k})
    monkeypatch.setattr(run_store, "append_event",
                        lambda rid, kind, msg=None, payload=None:
                        events.append(kind) or {})
    monkeypatch.setattr(run_store, "update_progress",
                        lambda rid, prog, spend=None: {})
    monkeypatch.setattr(run_store, "finish_run", lambda rid, **k: k)
    O.Orchestrator(seller_id="s", goal="find bakeries",
                   agent_factory=lambda ctx: (lambda prompt: None)).run()
    assert "place" not in events


# --------------------------------------------------------------------------- #
# find_leads: the pin wins over whatever the model types
# --------------------------------------------------------------------------- #
def test_find_leads_defaults_to_the_pinned_code(monkeypatch):
    import agent.tools as T
    from agent.budget import Budget

    captured = {}

    def fake_campaign(**kw):
        captured.update(kw)
        return {"ok": True, "campaign_id": "c1", "found": 0, "qualified": 0,
                "scored": 0, "leads": [], "errors": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget(), "r", seller_id="s",
                       pinned_location_code=1010294,
                       pinned_location_name="Lagos,Lagos,Nigeria")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    out = tools["find_leads"](keyword="bakery", place="Lagos, Nigeria")
    assert out["ok"] is True
    assert captured["location_code"] == 1010294


def test_an_explicit_code_beats_the_pin(monkeypatch):
    import agent.tools as T
    from agent.budget import Budget

    captured = {}

    def fake_campaign(**kw):
        captured.update(kw)
        return {"ok": True, "campaign_id": "c1", "found": 0, "qualified": 0,
                "scored": 0, "leads": [], "errors": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget(), "r", seller_id="s",
                       pinned_location_code=1010294)
    tools = {t.tool_name: t for t in T.build_tools(ctx)}
    tools["find_leads"](keyword="bakery", place="Lagos", location_code=21564)
    assert captured["location_code"] == 21564


# --------------------------------------------------------------------------- #
# Dashboard: the picker is rendered and selection is mandatory
# --------------------------------------------------------------------------- #
def test_the_dashboard_renders_the_picker():
    import app as flask_app
    flask_app.app.config["TESTING"] = True
    body = flask_app.app.test_client().get("/").get_data(as_text=True)
    assert 'id="place"' in body
    assert 'id="place-list"' in body
    assert 'id="location_code"' in body
    assert "/location/search" in body
    # The submitter blocks a start with no pick: typed text alone is never
    # sent, which is the whole point of the picker.
    assert "Pick a search area from the list" in body


# --------------------------------------------------------------------------- #
# Shared memo: the file cache is per-machine, the table is shared
# --------------------------------------------------------------------------- #
def _memo_result(code):
    return {"place": "p", "exact": True, "count": 1,
            "matches": [{"location_code": code,
                         "location_name": "P,Place,Nowhere",
                         "location_type": "City",
                         "country_iso_code": "NG",
                         "location_code_parent": None,
                         "exact": True}],
            "note": ""}


class _FakeMemoDB:
    """In-memory location_memo table: capped newest-first reads, upserts."""

    def __init__(self, rows=None, explode=False):
        self.rows = list(rows or [])
        self.upserts = []
        self.explode = explode

    def select_rows(self, table, columns=None, order=None, limit=None):
        assert table == "location_memo"
        if self.explode:
            raise RuntimeError("database down")
        return list(self.rows)[:limit]

    def upsert_rows(self, table, rows, on_conflict=None):
        assert table == "location_memo" and on_conflict == "place_key"
        if self.explode:
            raise RuntimeError("database down")
        self.upserts.append(list(rows))
        self.rows.extend(rows)


def _isolated_memo(monkeypatch, file_memo=None, db=None):
    import dataforseo
    monkeypatch.setattr(dataforseo, "_MEMO", None)
    monkeypatch.setattr(dataforseo, "_MEMO_DIRTY", set())
    monkeypatch.setattr(dataforseo, "_load_json_file",
                        lambda path: dict(file_memo or {}))
    monkeypatch.setattr(dataforseo, "_save_json_file",
                        lambda path, obj, compact=False: None)
    monkeypatch.setattr(dataforseo, "_memo_db", lambda: db)
    return dataforseo


def test_shared_memo_merges_with_file_winning_conflicts(monkeypatch):
    import dataforseo
    db = _FakeMemoDB([{"place_key": "yaba",
                       "result": _memo_result(111)},
                      {"place_key": "lekki",
                       "result": _memo_result(222)}])
    dataforseo = _isolated_memo(
        monkeypatch, file_memo={"yaba": _memo_result(999)}, db=db)
    monkeypatch.setattr(dataforseo, "_resolve",
                        lambda place: _memo_result(999))
    memo = dataforseo._load_memo()
    # Shared rows fill gaps; the local file wins a direct conflict.
    assert memo["lekki"]["matches"][0]["location_code"] == 222
    assert memo["yaba"]["matches"][0]["location_code"] == 999


def test_only_newly_learned_keys_are_upserted(monkeypatch):
    import dataforseo
    db = _FakeMemoDB()
    dataforseo = _isolated_memo(monkeypatch, db=db)
    monkeypatch.setattr(dataforseo, "_resolve",
                        lambda place: _memo_result(1010294))
    monkeypatch.setattr(dataforseo, "LOGIN", "l")
    monkeypatch.setattr(dataforseo, "PASSWORD", "p")
    dataforseo.lookup_location("Yaba, Lagos")
    dataforseo.lookup_location("Lekki, Lagos")
    assert len(db.upserts) == 2
    assert {r["place_key"] for batch in db.upserts for r in batch} == {
        "yaba lagos", "lekki lagos"}
    # Both are hits now: the steady state writes nothing.
    dataforseo.lookup_location("Yaba, Lagos")
    assert len(db.upserts) == 2


def test_a_db_blip_leaves_file_behaviour_unchanged(monkeypatch):
    import dataforseo
    db = _FakeMemoDB(explode=True)
    dataforseo = _isolated_memo(
        monkeypatch, file_memo={"yaba": _memo_result(999)}, db=db)
    monkeypatch.setattr(dataforseo, "_resolve",
                        lambda place: _memo_result(1010294))
    monkeypatch.setattr(dataforseo, "LOGIN", "l")
    monkeypatch.setattr(dataforseo, "PASSWORD", "p")
    out = dataforseo.lookup_location("Yaba")
    assert out["matches"][0]["location_code"] == 999
    out = dataforseo.lookup_location("Brand New Place")
    assert out["matches"][0]["location_code"] == 1010294
    assert db.upserts == []

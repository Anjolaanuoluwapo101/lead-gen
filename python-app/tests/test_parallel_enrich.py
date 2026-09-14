"""ENRICH + SCORE runs businesses concurrently, STORE stays sequential.

The loop used to walk rows one at a time: crawl a site, score it, store it,
repeat. Every step is network I/O against a different host, so the slowest
site set the pace for all of them. Now workers crawl and judge in parallel
while the database writes stay sequential, in row order, on the caller thread.

Proved here with four businesses whose enrichment each sleeps 0.2s: in series
that is 0.8s minimum, and more than one worker thread must show up.
"""

import threading
import time

import config
import email_scraper
import lead_engine
import llm as llm_layer
import scoring
import supabase_store


def _stubbed_run(keyword="bakery", place="Lagos, Nigeria", **kw):
    """run_campaign with every network call stubbed, as the tests above do.

    Source returns 4 rows with sites, enrich finds one email each, scoring
    computes cleanly, the LLM judges 95/90: deterministic end to end with
    zero network. Extra kwargs pass through to run_campaign (progress_cb).
    """
    import llm as _llm

    def fake_enrich(website, **k):
        return ({"a@biz.com": ["https://biz.example.com"]},
                {"phones_found": []}, [])

    def fake_score(provider, business, intelligence, niche, **k):
        return {"opportunity_score": 95, "confidence_score": 90,
                "reasons": ["has no website"], "gaps": ["no email published"],
                "first_line": "hi"}

    import unittest.mock as _mock
    with _mock.patch.object(lead_engine, "get_source",
                            lambda cfg: _Source(_rows())), \
         _mock.patch.object(supabase_store, "configured", lambda: False), \
         _mock.patch.object(lead_engine, "_enrich_site", fake_enrich), \
         _mock.patch.object(_llm, "score_lead", fake_score), \
         _mock.patch.object(scoring, "compute",
                            lambda row, intel, emails: {
                                "confidence_cap": 100,
                                "presence_class": "thin",
                                "source_agreement": {"score": 1},
                                "activity_recency": {"last_active": None}}), \
         _mock.patch.object(config, "llm_cfg",
                            lambda *a, **k: {"api_key": "test-key"}):
        return lead_engine.run_campaign(
            keyword=keyword, place=place, niche="web design",
            max_results=4, location_code=1010294, **kw)


def test_stage_events_fire_in_order_with_real_counts():
    seen = []
    out = _stubbed_run(
        progress_cb=lambda kind, msg: seen.append((kind, msg)))
    assert out["ok"] is True
    assert [k for k, _ in seen] == ["search", "found", "enrich", "scored"]
    assert seen[0][1] == "Searching for 'bakery' in Lagos, Nigeria"
    assert seen[1][1] == "Found 4 businesses, 4 with websites"
    assert seen[2][1] == "Crawling 4 business sites and scoring them"
    assert seen[3][1] == "Enriched 4, scored 4, qualified 4"


def test_no_callback_behaves_exactly_as_before():
    # The default path every other caller uses: no events, same numbers.
    out = _stubbed_run()
    assert out["ok"] is True
    assert (out["found"], out["enriched"], out["scored"],
            out["qualified"]) == (4, 4, 4, 4)


class _FakeFetcher:
    """Thread-safe fake: canned pages, recorded calls, optional latency."""

    def __init__(self, pages, latency=0.0):
        self.pages = pages
        self.latency = latency
        self.calls = []
        self._lock = threading.Lock()

    def get(self, url, referer=None):
        with self._lock:
            self.calls.append(url)
        if self.latency:
            time.sleep(self.latency)
        return self.pages.get(url, (None, 404))

    def polite_sleep(self):
        pass


def _site_pages():
    # NOTE: example.com is a junk domain to the extractor by design, so the
    # fixture site lives on sitebakery.com instead.
    home = ('<html><body>Reach us at home@sitebakery.com '
            '<a href="/a">a</a> <a href="/b">b</a></body></html>', 200)
    a = ('<html><body>Write to a@sitebakery.com '
         '<a href="/c">c</a></body></html>', 200)
    b = ('<html><body>Write to b@sitebakery.com</body></html>', 200)
    c = ('<html><body>Write to c@sitebakery.com</body></html>', 200)
    return {"https://sitebakery.com": home,
            "https://sitebakery.com/a": a,
            "https://sitebakery.com/b": b,
            "https://sitebakery.com/c": c}


def test_parallel_crawl_finds_everything_a_serial_one_does(monkeypatch):
    monkeypatch.setenv("EMAIL_CRAWL_MIN_GAP", "0")
    for workers in (1, 4):
        out = email_scraper.scrape_website(
            "https://sitebakery.com", max_count=10, max_depth=2,
            fetcher=_FakeFetcher(_site_pages()), crawl_workers=workers)
        assert set(out["emails"]) == {"home@sitebakery.com",
                                      "a@sitebakery.com",
                                      "b@sitebakery.com",
                                      "c@sitebakery.com"}
        assert out["pages_scraped"] == 4
        # Homepage first, always: analysis order is discovery order, not
        # thread finish order.
        assert out["intelligence"]["pages_analyzed"][0].endswith(
            "sitebakery.com")


def test_parallel_crawl_overlaps_fetches(monkeypatch):
    monkeypatch.setenv("EMAIL_CRAWL_MIN_GAP", "0")
    started = time.monotonic()
    email_scraper.scrape_website(
        "https://sitebakery.com", max_count=10, max_depth=1,
        fetcher=_FakeFetcher(_site_pages(), latency=0.2),
        crawl_workers=4)
    elapsed = time.monotonic() - started
    # 3 pages at 0.2s each overlap: well under the 0.6s serial floor.
    assert elapsed < 0.6


def test_a_poison_page_costs_one_page_not_the_crawl(monkeypatch):
    monkeypatch.setenv("EMAIL_CRAWL_MIN_GAP", "0")

    class _Boom(_FakeFetcher):
        def get(self, url, referer=None):
            if url.endswith("/a"):
                raise RuntimeError("parser exploded")
            return super().get(url, referer)

    out = email_scraper.scrape_website(
        "https://sitebakery.com", max_count=10, max_depth=2,
        fetcher=_Boom(_site_pages()), crawl_workers=4)
    assert "b@sitebakery.com" in out["emails"]
    assert any("/a" in e and "fetch failed" in e for e in out["errors"])


def _stored_run(monkeypatch, fail_bulk_leads=False, select_calls=None):
    """run_campaign with store ON and every Supabase call faked in memory.

    Campaign lookup finds nothing (creates camp-1), no prior prospects,
    prospect inserts mint pros-N ids in order. Returns (result, calls) where
    calls records ("insert", table, rowcount) and ("update", table, payload,
    filters_in) in order. select_calls optionally collects (table, kwargs)
    for every select, so tests can assert on query shape.
    """
    calls = []

    def fake_select(*a, **k):
        if select_calls is not None:
            select_calls.append((a[0] if a else None, dict(k)))
        return []

    def fake_insert(table, rows):
        batch = rows if isinstance(rows, list) else [rows]
        if table == "leads" and fail_bulk_leads and len(batch) > 1:
            raise RuntimeError("bulk rejected")
        calls.append(("insert", table, len(batch)))
        if table == "campaigns":
            return [{"id": "camp-1"}]
        return [{"id": f"{table}-{i}"} for i in range(len(batch))]

    def fake_update(table, updates, filters=None, filters_in=None):
        calls.append(("update", table, dict(updates), filters_in))
        return []

    def fake_enrich(website, **k):
        return ({"a@biz.com": ["https://biz.example.com"]},
                {"phones_found": []}, [])

    def fake_score(provider, business, intelligence, niche, **k):
        return {"opportunity_score": 95, "confidence_score": 90,
                "reasons": ["has no website"], "gaps": ["no email published"],
                "first_line": "hi"}

    monkeypatch.setattr(lead_engine, "get_source",
                            lambda cfg: _Source(_rows()))
    monkeypatch.setattr(supabase_store, "configured", lambda: True)
    monkeypatch.setattr(supabase_store, "select_rows", fake_select)
    monkeypatch.setattr(supabase_store, "insert_rows", fake_insert)
    monkeypatch.setattr(supabase_store, "update_rows", fake_update)
    monkeypatch.setattr(lead_engine, "_enrich_site", fake_enrich)
    monkeypatch.setattr(llm_layer, "score_lead", fake_score)
    monkeypatch.setattr(scoring, "compute",
                        lambda row, intel, emails: {
                            "confidence_cap": 100,
                            "presence_class": "thin",
                            "source_agreement": {"score": 1},
                            "activity_recency": {"last_active": None}})
    monkeypatch.setattr(config, "llm_cfg",
                        lambda *a, **k: {"api_key": "test-key"})
    out = lead_engine.run_campaign(
        keyword="bakery", place="Lagos, Nigeria", niche="web design",
        max_results=4, location_code=1010294)
    return out, calls


def test_enrich_site_returns_before_its_timeout(monkeypatch):
    monkeypatch.setattr(
        email_scraper, "scrape_website",
        lambda *a, **k: {"emails": {"a@b.com": ["u"]},
                         "intelligence": {"phones_found": []},
                         "pages_scraped": 1, "errors": []})
    emails, intel, errors = lead_engine._enrich_site(
        "https://x.com", timeout=5)
    assert emails == {"a@b.com": ["u"]}
    assert errors == []


def test_enrich_site_timeout_winds_down_and_keeps_partial(monkeypatch):
    seen = {}

    def slow_crawl(*a, **k):
        seen["stop"] = k.get("stop_event")
        # Simulate a crawl that notices the stop and returns partial data.
        assert k.get("stop_event") is not None
        k["stop_event"].wait(timeout=5)
        return {"emails": {"p@b.com": ["u"]}, "intelligence": {},
                "pages_scraped": 1, "errors": []}

    monkeypatch.setattr(email_scraper, "scrape_website", slow_crawl)
    started = time.monotonic()
    emails, _, errors = lead_engine._enrich_site(
        "https://slow.com", timeout=0.2, stop_grace_s=5)
    elapsed = time.monotonic() - started
    assert seen["stop"].is_set(), "the abandoned crawl must be signalled"
    assert emails == {"p@b.com": ["u"]}
    assert any("exceeded 0.2s" in e and "partial results kept" in e
               for e in errors)
    assert elapsed < 5, "grace wait must bound the timeout path"


def test_enrich_site_timeout_with_nothing_merged_says_skipped(monkeypatch):
    def hung(*a, **k):
        time.sleep(10)

    monkeypatch.setattr(email_scraper, "scrape_website", hung)
    _, _, errors = lead_engine._enrich_site(
        "https://hung.com", timeout=0.2, stop_grace_s=0.2)
    assert any("exceeded 0.2s" in e and "skipped" in e for e in errors)


def test_worker_count_defaults_to_eight_and_env_wins(monkeypatch):
    monkeypatch.delenv("ENRICH_SCORE_WORKERS", raising=False)
    assert lead_engine._enrich_score_workers() == 8
    assert lead_engine.ENRICH_SCORE_WORKERS == 8
    monkeypatch.setenv("ENRICH_SCORE_WORKERS", "3")
    assert lead_engine._enrich_score_workers() == 3
    monkeypatch.setenv("ENRICH_SCORE_WORKERS", "junk")
    assert lead_engine._enrich_score_workers() == 8


def test_store_writes_are_batched_not_per_row(monkeypatch):
    out, calls = _stored_run(monkeypatch)
    assert out["ok"] is True and out["qualified"] == 4
    lead_inserts = [c for c in calls if c[:2] == ("insert", "leads")]
    assert len(lead_inserts) == 1 and lead_inserts[0][2] == 4
    status_updates = [c for c in calls if c[0] == "update"]
    # All four scored 95: one status, one PATCH over four ids.
    assert status_updates == [
        ("update", "prospects", {"status": "qualified"},
         {"id": ["prospects-0", "prospects-1",
                 "prospects-2", "prospects-3"]})]


def test_a_failed_bulk_insert_falls_back_to_per_row(monkeypatch):
    out, calls = _stored_run(monkeypatch, fail_bulk_leads=True)
    assert out["ok"] is True
    lead_inserts = [c for c in calls if c[:2] == ("insert", "leads")]
    assert [c[2] for c in lead_inserts] == [1, 1, 1, 1]
    assert any("bulk lead insert failed but all 4 rows saved on retry" in e
               for e in out["errors"])


def test_dedup_fetch_is_prefiltered_and_bounded(monkeypatch):
    # The dedup scan only ever matches HANDLED rows, so anything else is
    # excluded server-side instead of downloaded and ignored; and the
    # explicit limit replaces the silent PostgREST default cap that used to
    # stop deduplicating old rows past ~1000 prospects with no error.
    select_calls = []
    _stored_run(monkeypatch, select_calls=select_calls)
    prosp = [kw for table, kw in select_calls if table == "prospects"]
    assert prosp, "dedup must read prior prospects"
    assert prosp[0]["filters_in"] == {"status": sorted(lead_engine._HANDLED)}
    assert prosp[0]["limit"] == 5000
    assert prosp[0]["order"] == "created_at.asc"


def test_empty_find_emits_nothing_and_still_returns():
    import unittest.mock as _mock
    with _mock.patch.object(lead_engine, "get_source",
                            lambda cfg: _Source([])), \
         _mock.patch.object(supabase_store, "configured", lambda: False), \
         _mock.patch.object(config, "llm_cfg",
                            lambda *a, **k: {"api_key": "test-key"}):
        seen = []
        out = lead_engine.run_campaign(
            keyword="bakery", place="Nowhere", niche="web design",
            progress_cb=lambda kind, msg: seen.append((kind, msg)))
    assert out["found"] == 0
    # The search still brackets the FIND call even when it comes back empty:
    # an empty result is information, and the trace should show the run
    # looked. Everything after FIND correctly stays silent.
    assert seen == [("search", "Searching for 'bakery' in Nowhere")]


def _rows(n=4):
    return [{
        "business_name": f"Biz {i}",
        "business_page": f"https://biz{i}.example.com",
        "telephone": f"0800000000{i}",
        "category": "bakery",
        "rating": "4.5",
        "review_count": "10",
    } for i in range(n)]


class _Source:
    def __init__(self, rows):
        self._rows = rows

    def find_businesses(self, *a, **k):
        return list(self._rows)


def test_enrich_and_score_overlap_across_businesses(monkeypatch):
    seen_threads = []
    lock = threading.Lock()

    def fake_enrich(website, **kw):
        with lock:
            seen_threads.append(threading.get_ident())
        time.sleep(0.2)
        return ({"a@biz.com": ["https://biz.example.com"]},
                {"phones_found": []}, [])

    def fake_score(provider, business, intelligence, niche, **kw):
        return {"opportunity_score": 95, "confidence_score": 90,
                "reasons": ["has no website"], "gaps": ["no email published"],
                "first_line": "hi"}

    monkeypatch.setattr(lead_engine, "get_source",
                        lambda cfg: _Source(_rows()))
    monkeypatch.setattr(supabase_store, "configured", lambda: False)
    monkeypatch.setattr(lead_engine, "_enrich_site", fake_enrich)
    monkeypatch.setattr(llm_layer, "score_lead", fake_score)
    monkeypatch.setattr(scoring, "compute",
                        lambda row, intel, emails: {
                            "confidence_cap": 100,
                            "presence_class": "thin",
                            "source_agreement": {"score": 1},
                            "activity_recency": {"last_active": None}})
    monkeypatch.setattr(config, "llm_cfg",
                        lambda *a, **k: {"api_key": "test-key"})

    started = time.monotonic()
    out = lead_engine.run_campaign(
        keyword="bakery", place="Lagos, Nigeria", niche="web design",
        max_results=4, location_code=1010294)
    elapsed = time.monotonic() - started

    assert out["ok"] is True
    assert out["found"] == 4
    assert out["enriched"] == 4
    assert out["scored"] == 4
    assert out["qualified"] == 4
    # Order preserved: the parallel phase must not reorder the leads.
    assert [l["business_name"] for l in out["leads"]] == \
        [f"Biz {i}" for i in range(4)]
    # More than one worker did enrichment...
    assert len(set(seen_threads)) > 1
    # ...so the batch finished in less than the 0.8s series would need.
    assert elapsed < 0.65, f"took {elapsed:.2f}s, looks serial"


def test_a_repeated_search_replays_instead_of_rerunning(monkeypatch):
    """An identical second search costs nothing: no engine call, no credit."""
    import agent.tools as T
    from agent.budget import Budget

    calls = []

    def fake_campaign(**kw):
        calls.append(kw)
        return {"ok": True, "campaign_id": "c1", "found": 2,
                "with_website": 2, "qualified": 1, "scored": 2,
                "stored": True, "leads": [], "errors": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget({"max_find_calls": 5}), "r", seller_id="s")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}

    first = tools["find_leads"](keyword="bakery", place="Lagos")
    second = tools["find_leads"](keyword="bakery", place="Lagos")

    assert first["ok"] is True and second["ok"] is True
    assert second["cached"] is True
    assert len(calls) == 1, "the engine ran twice for one search"
    assert ctx.budget.find_calls == 1, "the replay charged credit"


def test_an_empty_search_is_not_cached(monkeypatch):
    """A transient empty must stay retryable within the same run."""
    import agent.tools as T
    from agent.budget import Budget

    calls = []

    def fake_campaign(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return {"ok": True, "campaign_id": "c1", "found": 0,
                    "qualified": 0, "scored": 0, "leads": [], "errors": []}
        return {"ok": True, "campaign_id": "c1", "found": 3,
                "with_website": 3, "qualified": 2, "scored": 3,
                "stored": True, "leads": [], "errors": []}

    monkeypatch.setattr(lead_engine, "run_campaign", fake_campaign)
    ctx = T.RunContext(Budget({"max_find_calls": 5}), "r", seller_id="s")
    tools = {t.tool_name: t for t in T.build_tools(ctx)}

    tools["find_leads"](keyword="bakery", place="Lagos")
    second = tools["find_leads"](keyword="bakery", place="Lagos")

    assert len(calls) == 2, "the retry never ran"
    assert second.get("cached") is not True
    assert second["found"] == 3


def test_one_bad_business_does_not_kill_the_batch(monkeypatch):
    def fake_enrich(website, **kw):
        if "biz1" in website:
            raise RuntimeError("site exploded")
        return ({}, {}, [])

    def fake_score(provider, business, intelligence, niche, **kw):
        if business.get("business_name") == "Biz 1":
            raise RuntimeError("groq 429")
        return {"opportunity_score": 95, "confidence_score": 90,
                "reasons": ["r"], "gaps": ["g"], "first_line": "hi"}

    monkeypatch.setattr(lead_engine, "get_source",
                        lambda cfg: _Source(_rows()))
    monkeypatch.setattr(supabase_store, "configured", lambda: False)
    monkeypatch.setattr(lead_engine, "_enrich_site", fake_enrich)
    monkeypatch.setattr(llm_layer, "score_lead", fake_score)
    monkeypatch.setattr(scoring, "compute",
                        lambda row, intel, emails: {
                            "confidence_cap": 100,
                            "presence_class": "thin",
                            "source_agreement": {"score": 1},
                            "activity_recency": {"last_active": None}})
    monkeypatch.setattr(config, "llm_cfg",
                        lambda *a, **k: {"api_key": "test-key"})

    out = lead_engine.run_campaign(
        keyword="bakery", place="Lagos, Nigeria", niche="web design",
        max_results=4, location_code=1010294)

    # Biz 1's crawl failed (recorded) but it still scored on empty
    # intelligence, exactly as the sequential loop did; its score call then
    # failed, so it lands enriched-but-unscored while the other three qualify.
    # Same degradation as before, not a failed run.
    assert out["scored"] == 3
    assert out["qualified"] == 3
    assert len(out["leads"]) == 4
    assert any("site exploded" in e for e in out["errors"])
    assert any("score failed" in e for e in out["errors"])

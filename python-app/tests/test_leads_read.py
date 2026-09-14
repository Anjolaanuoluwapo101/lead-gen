"""The extraction from app.py must be behaviour-preserving, and the ownership
rule must stay in exactly one place (divergence there is a security bug)."""

import pytest

import leads_read
import supabase_store


# --- flatten_leads / decoded ---------------------------------------------- #

def test_flatten_decodes_json_string_fields():
    out = leads_read.flatten_leads([{"id": "l", "emails": '["a@b.com"]',
                                     "emails_extra": "[]"}])
    assert out[0]["emails"] == ["a@b.com"]
    assert out[0]["emails_extra"] == []


def test_flatten_leaves_unparseable_fields_alone():
    out = leads_read.flatten_leads([{"emails": "not-json"}])
    assert out[0]["emails"] == "not-json"


def test_flatten_lifts_prospect_fields_without_overwriting():
    row = {"top": "keep", "business_name": "Already", "prospect_id": {
        "id": "p1", "business_name": "Nested", "phone": "555"}}
    out = leads_read.flatten_leads([row])[0]
    assert out["business_name"] == "Already"   # existing value wins
    assert out["phone"] == "555"               # blank slot gets filled
    assert out["prospect_id"] == "p1"          # object replaced by the id


def test_flatten_survives_empty_and_none_rows():
    assert leads_read.flatten_leads([{}]) == [{}]
    assert leads_read.flatten_leads([None]) == [{}]
    assert leads_read.flatten_leads([]) == []


def test_decoded_handles_every_shape():
    assert leads_read.decoded('{"k": 1}') == {"k": 1}
    assert leads_read.decoded("bad") == {}
    assert leads_read.decoded("") == {}
    assert leads_read.decoded(None) == {}
    assert leads_read.decoded({"a": 1}) == {"a": 1}


# --- business_from_lead ---------------------------------------------------- #

def test_business_prefers_embedded_prospect():
    lead = {"prospect_id": {"business_name": "Acme", "category": "dentist",
                            "phone": "555"},
            "business_name": "ignored"}
    biz = leads_read.business_from_lead(lead)
    assert biz["business_name"] == "Acme"
    assert biz["category"] == "dentist"
    assert biz["phone"] == "555"


def test_business_falls_back_to_a_placeholder():
    assert leads_read.business_from_lead({})["business_name"] == "this business"


# --- ownership (the security-relevant rule) ------------------------------- #

def test_owns_campaign_requires_ids():
    assert leads_read.owns_campaign(None, "c") is False
    assert leads_read.owns_campaign("s", None) is False


def test_owns_campaign_true_for_exact_owner(monkeypatch):
    monkeypatch.setattr(supabase_store, "select_rows",
                        lambda *a, **k: [{"id": "c", "seller_id": "s1"}])
    assert leads_read.owns_campaign("s1", "c") is True
    assert leads_read.owns_campaign("s2", "c") is False


def test_owns_campaign_unknown_campaign_is_false(monkeypatch):
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    assert leads_read.owns_campaign("s1", "c") is False


def test_owns_campaign_legacy_null_owner_belongs_to_default_only(monkeypatch):
    monkeypatch.setattr(supabase_store, "select_rows",
                        lambda *a, **k: [{"id": "c", "seller_id": None}])
    monkeypatch.setattr(leads_read.lead_engine, "DEFAULT_SELLER_ID", "default-s")
    assert leads_read.owns_campaign("default-s", "c") is True
    assert leads_read.owns_campaign("someone-else", "c") is False


def test_owns_campaign_fails_closed_on_database_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(supabase_store, "select_rows", boom)
    assert leads_read.owns_campaign("s1", "c") is False


# --- read_campaign_niche --------------------------------------------------- #

def test_niche_prefers_rules_then_keyword(monkeypatch):
    monkeypatch.setattr(
        supabase_store, "select_rows",
        lambda *a, **k: [{"niche_rules": '{"niche": "booking"}',
                          "keyword": "dentist", "seller_id": "s1"}])
    assert leads_read.read_campaign_niche("c") == ("booking", "s1")


def test_niche_falls_back_to_keyword(monkeypatch):
    monkeypatch.setattr(
        supabase_store, "select_rows",
        lambda *a, **k: [{"niche_rules": None, "keyword": "dentist",
                          "seller_id": None}])
    assert leads_read.read_campaign_niche("c") == ("dentist", None)


def test_niche_is_quiet_when_campaign_is_missing(monkeypatch):
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    assert leads_read.read_campaign_niche("c") == (None, None)
    assert leads_read.read_campaign_niche(None) == (None, None)


# --- read_lead / read_seller_for_draft ------------------------------------- #

def test_read_lead_keeps_the_prospect_nested(monkeypatch):
    # Deliberately unflattened: the draft path needs the prospect object.
    monkeypatch.setattr(supabase_store, "select_rows",
                        lambda *a, **k: [{"id": "l1",
                                          "prospect_id": {"id": "p1",
                                                          "business_name": "A"}}])
    lead = leads_read.read_lead("l1")
    assert isinstance(lead["prospect_id"], dict)


def test_read_lead_missing_is_none(monkeypatch):
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    assert leads_read.read_lead("nope") is None


def test_read_lead_propagates_database_errors(monkeypatch):
    # Deliberately NOT swallowed, unlike read_campaign_niche/read_seller_for_draft.
    # Returning None here would make "the database is down" indistinguishable
    # from "this lead does not exist", and callers would report a missing lead
    # during an outage. Callers translate the raise into a distinct error.
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(supabase_store, "select_rows", boom)
    with pytest.raises(RuntimeError):
        leads_read.read_lead("l1")


def test_read_seller_separates_identity_from_settings(monkeypatch):
    # settings can carry ENCRYPTED PROVIDER KEYS — it must come back as its own
    # value, never folded into the dict handed to the model.
    monkeypatch.setattr(
        supabase_store, "select_rows",
        lambda *a, **k: [{"id": "s1", "name": "Ann", "brand": "Acme",
                          "settings": '{"llm": {"api_key": "enc:xxx"}}'}])
    seller, settings = leads_read.read_seller_for_draft("s1")
    assert seller["name"] == "Ann"
    assert "settings" not in seller          # never in the model-facing dict
    assert settings["llm"]["api_key"] == "enc:xxx"


def test_read_seller_absent_is_empty_and_none(monkeypatch):
    monkeypatch.setattr(supabase_store, "select_rows", lambda *a, **k: [])
    assert leads_read.read_seller_for_draft("s1") == ({}, None)
    assert leads_read.read_seller_for_draft(None) == ({}, None)

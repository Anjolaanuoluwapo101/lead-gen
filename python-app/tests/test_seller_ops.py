"""seller_ops is the single source of truth for seller/lead writes.

Two classes of test matter here.

1. **The settings leak.** `seller_profile.settings` holds Fernet-encrypted
   provider keys. Four app.py routes used to select that column straight into
   their JSON responses. SAFE_COLUMNS is the fix, and these tests assert on the
   REQUESTED columns and on the serialized response — because the failure mode
   is silent: a leak looks exactly like a success.

2. **The 400/404 branches.** update_seller's render_mode check was dead in
   app.py (the helper coerced the value before validating it), so a typo became
   "auto" instead of an error. A test that only exercises the happy path would
   never have caught that.
"""

import json

import pytest

import lead_engine
import seller_ops


# --------------------------------------------------------------------------- #
# A stand-in for supabase_store that records what was asked for
# --------------------------------------------------------------------------- #
class FakeDB:
    def __init__(self, *, select=None, update=None, insert=None, configured=True):
        self._select = select or {}
        self._update = update or {}
        self._insert = insert or {}
        self._configured = configured
        self.selects = []
        self.updates = []
        self.inserts = []

    def configured(self):
        return self._configured

    def select_rows(self, table, columns="*", filters=None, filters_gte=None,
                    filters_in=None, limit=None, order=None):
        self.selects.append({"table": table, "columns": columns,
                             "filters": filters, "filters_in": filters_in,
                             "limit": limit, "order": order})
        key = (table, tuple(sorted((filters or {}).items())))
        result = self._select.get(key, self._select.get(table, []))
        if callable(result):
            return result(filters, filters_in)
        return result

    def update_rows(self, table, updates, filters):
        self.updates.append({"table": table, "updates": updates,
                             "filters": filters})
        result = self._update.get(table, [])
        if callable(result):
            return result(updates, filters)
        return result

    def insert_rows(self, table, rows):
        self.inserts.append({"table": table, "rows": rows})
        return self._insert.get(table, [{"id": "s-new", **rows}])


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(seller_ops, "supabase_store", fake)
    return fake


SELLER_ROW = {"id": "s1", "email": "a@b.co", "name": "Ada"}


# --------------------------------------------------------------------------- #
# The leak
# --------------------------------------------------------------------------- #
def test_safe_columns_excludes_settings():
    # The whole point. If someone adds it back to make a route "work", this is
    # the alarm.
    assert "settings" not in seller_ops.SAFE_COLUMNS


def test_read_paths_never_request_settings(db):
    db._select = {"seller_profile": [SELLER_ROW]}
    seller_ops.get_seller("s1")
    seller_ops.find_seller_by_email("a@b.co")
    seller_ops.list_sellers()

    for call in db.selects:
        assert call["columns"] != "id,settings"
        assert "settings" not in (call["columns"] or "")
        assert "settings" not in json.dumps(call)


def test_get_seller_config_returns_only_masked_values(db):
    token = "gAAAAABm-ciphertext-should-never-escape"
    db._select = {"seller_profile": [{"id": "s1", "settings": {
        "llm": {"provider": "groq", "model": "llama", "api_key_enc": token},
        "finder": {"provider": "serper", "login_enc": token,
                   "password_enc": token},
    }}]}

    config = seller_ops.get_seller_config("s1")

    assert config["llm"]["provider"] == "groq"
    assert config["llm"]["api_key"] == "********"
    assert config["finder"]["login"] == "********"
    assert config["finder"]["password"] == "********"
    # Not just "the fields look right" — the ciphertext must appear nowhere.
    assert token not in json.dumps(config)


def test_get_seller_config_accepts_settings_as_a_json_string(db):
    db._select = {"seller_profile": [{"id": "s1", "settings": json.dumps(
        {"llm": {"provider": "openai", "api_key_enc": "x"}})}]}
    config = seller_ops.get_seller_config("s1")
    assert config["llm"]["provider"] == "openai"
    assert config["llm"]["api_key"] == "********"


def test_unset_secrets_report_as_none_not_masked(db):
    # A caller must be able to tell "no key stored" from "a key is stored" —
    # otherwise the UI cannot prompt for one.
    db._select = {"seller_profile": [{"id": "s1", "settings": {
        "llm": {"provider": "groq"}, "finder": {}}}]}
    config = seller_ops.get_seller_config("s1")
    assert config["llm"]["api_key"] is None
    assert config["finder"]["login"] is None


def test_malformed_settings_do_not_crash(db):
    db._select = {"seller_profile": [{"id": "s1", "settings": "{not json"}]}
    assert seller_ops.get_seller_config("s1")["llm"]["provider"] is None


# --------------------------------------------------------------------------- #
# create_or_update_seller
# --------------------------------------------------------------------------- #
def test_create_lowercases_the_email(db):
    db._select = {"seller_profile": []}
    out = seller_ops.create_or_update_seller("ADA@Example.COM", name="Ada")
    assert out["created"] is True
    assert db.inserts[0]["rows"]["email"] == "ada@example.com"


def test_create_rejects_a_non_email(db):
    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.create_or_update_seller("not-an-email")
    assert e.value.kind == "bad_request"
    assert db.inserts == []


def test_update_applies_only_supplied_fields(db):
    db._select = {"seller_profile": [SELLER_ROW]}
    db._update = {"seller_profile": [{"id": "s1", "brand": "New"}]}
    out = seller_ops.create_or_update_seller("a@b.co", brand="New")
    assert out["created"] is False and out["updated"] is True
    # A blank field is "the operator didn't type it", not "clear it".
    assert db.updates[0]["updates"]["brand"] == "New"
    assert "name" not in db.updates[0]["updates"]
    assert "title" not in db.updates[0]["updates"]


def test_update_of_an_existing_seller_is_a_noop_when_nothing_is_supplied(db):
    db._select = {"seller_profile": [SELLER_ROW]}
    out = seller_ops.create_or_update_seller("a@b.co")
    assert out == {"created": False, "updated": False, "seller": SELLER_ROW}
    assert db.updates == []      # and no pointless write


def test_reposting_the_same_email_is_idempotent(db):
    db._select = {"seller_profile": [SELLER_ROW]}
    out = seller_ops.create_or_update_seller("A@B.co", name="Ada")
    assert out["seller"]["id"] == "s1"
    assert db.inserts == []


# --------------------------------------------------------------------------- #
# update_seller
# --------------------------------------------------------------------------- #
def test_render_mode_typo_is_rejected_not_coerced(db):
    # The bug this test exists for: render_mode() turns "htm" into "auto", so
    # validating through it made the 400 unreachable.
    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.update_seller("s1", {"render_mode": "htm"})
    assert e.value.kind == "bad_request"
    assert db.updates == []


def test_valid_render_modes_are_accepted(db):
    db._update = {"seller_profile": [SELLER_ROW]}
    for mode in ("html", "js", "auto"):
        db.updates.clear()
        seller_ops.update_seller("s1", {"render_mode": mode})
        assert db.updates[0]["updates"]["render_mode"] == mode


def test_update_with_no_recognised_fields_is_refused(db):
    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.update_seller("s1", {"settings": {"llm": {}}, "nope": 1})
    assert e.value.kind == "bad_request"
    assert db.updates == []


def test_update_reports_not_found_when_the_write_touches_nothing(db):
    db._update = {"seller_profile": []}
    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.update_seller("s1", {"name": "Ada"})
    assert e.value.kind == "not_found"


def test_active_accepts_the_strings_a_form_sends(db):
    db._update = {"seller_profile": [SELLER_ROW]}
    seller_ops.update_seller("s1", {"active": "FALSE"})
    assert db.updates[0]["updates"]["active"] is False


def test_a_phone_sent_as_a_number_does_not_crash(db):
    # n8n forms happily send digits as numbers; str() before strip().
    db._update = {"seller_profile": [SELLER_ROW]}
    out = seller_ops.update_seller("s1", {"phone": 5551234567})
    assert db.updates[0]["updates"]["phone"] == "5551234567"
    assert out is not None


# --------------------------------------------------------------------------- #
# set_resume_text
# --------------------------------------------------------------------------- #
def test_empty_resume_text_is_refused(db):
    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.set_resume_text("s1", "   ")
    assert e.value.kind == "bad_request"


def test_resume_text_is_stored_with_its_length(db):
    db._update = {"seller_profile": [SELLER_ROW]}
    out = seller_ops.set_resume_text("s1", "  ten years of...  ")
    assert out["chars"] == len("ten years of...")
    assert db.updates[0]["updates"]["resume_text"] == "ten years of..."


# --------------------------------------------------------------------------- #
# set_lead_status — the ownership gate
# --------------------------------------------------------------------------- #
def test_an_unknown_status_is_refused(db):
    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.set_lead_status("s1", ["l1"], "maybe")
    assert e.value.kind == "bad_request"


def test_an_empty_lead_list_is_refused(db):
    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.set_lead_status("s1", [], "won")
    assert e.value.kind == "bad_request"


def test_a_seller_may_not_touch_another_sellers_leads(db, monkeypatch):
    fake = FakeDB(select={
        "leads": [{"id": "l1", "campaign_id": "c1"}],
        "campaigns": [{"id": "c1", "seller_id": "someone-else"}],
    })
    monkeypatch.setattr(seller_ops, "supabase_store", fake)

    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.set_lead_status("s1", ["l1"], "won")
    assert e.value.kind == "not_found"
    assert fake.updates == []          # refused BEFORE any write


def test_a_partially_owned_batch_is_refused_whole(db, monkeypatch):
    # All-or-nothing: a half-applied status change is worse than a refusal,
    # because the caller cannot tell which half landed.
    fake = FakeDB(select={
        "leads": [{"id": "l1", "campaign_id": "c1"},
                  {"id": "l2", "campaign_id": "c2"}],
        "campaigns": [{"id": "c1", "seller_id": "s1"},
                      {"id": "c2", "seller_id": "other"}],
    })
    monkeypatch.setattr(seller_ops, "supabase_store", fake)

    with pytest.raises(seller_ops.OpsError):
        seller_ops.set_lead_status("s1", ["l1", "l2"], "won")
    assert fake.updates == []


def test_owned_leads_are_updated(db, monkeypatch):
    fake = FakeDB(
        select={"leads": [{"id": "l1", "campaign_id": "c1"},
                          {"id": "l2", "campaign_id": "c1"}],
                "campaigns": [{"id": "c1", "seller_id": "s1"}]},
        update={"leads": [{"id": "l1", "status": "won"}]})
    monkeypatch.setattr(seller_ops, "supabase_store", fake)

    out = seller_ops.set_lead_status("s1", ["l1", "l2"], "Won")
    assert out == {"updated": 2, "errors": [], "status": "won"}
    assert len(fake.updates) == 2


def test_the_default_bridge_still_owns_null_owner_campaigns(db, monkeypatch):
    # Legacy campaigns predate seller_id. The DEFAULT seller is the single-
    # tenant bridge and must still be able to work them, or the existing n8n
    # flow breaks on every pre-migration campaign.
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", "default-seller")
    fake = FakeDB(
        select={"leads": [{"id": "l1", "campaign_id": "c1"}],
                "campaigns": [{"id": "c1", "seller_id": None}]},
        update={"leads": [{"id": "l1"}]})
    monkeypatch.setattr(seller_ops, "supabase_store", fake)

    assert seller_ops.set_lead_status("default-seller", ["l1"], "contacted")["updated"] == 1


def test_a_non_default_seller_does_not_own_null_owner_campaigns(db, monkeypatch):
    monkeypatch.setattr(lead_engine, "DEFAULT_SELLER_ID", "default-seller")
    fake = FakeDB(select={"leads": [{"id": "l1", "campaign_id": "c1"}],
                          "campaigns": [{"id": "c1", "seller_id": None}]})
    monkeypatch.setattr(seller_ops, "supabase_store", fake)

    with pytest.raises(seller_ops.OpsError):
        seller_ops.set_lead_status("s2", ["l1"], "won")


def test_a_lead_that_does_not_exist_is_not_found(db, monkeypatch):
    fake = FakeDB(select={"leads": [{"id": "l1", "campaign_id": "c1"}],
                          "campaigns": [{"id": "c1", "seller_id": "s1"}]})
    monkeypatch.setattr(seller_ops, "supabase_store", fake)

    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.set_lead_status("s1", ["l1", "ghost"], "won")
    assert e.value.kind == "not_found"


def test_a_duplicated_lead_id_is_deduped(db, monkeypatch):
    fake = FakeDB(
        select={"leads": [{"id": "l1", "campaign_id": "c1"}],
                "campaigns": [{"id": "c1", "seller_id": "s1"}]},
        update={"leads": [{"id": "l1"}]})
    monkeypatch.setattr(seller_ops, "supabase_store", fake)

    assert seller_ops.set_lead_status("s1", ["l1", "l1"], "won")["updated"] == 1


# --------------------------------------------------------------------------- #
# Database-less operation
# --------------------------------------------------------------------------- #
def test_every_op_refuses_cleanly_when_supabase_is_unconfigured(monkeypatch):
    monkeypatch.setattr(seller_ops, "supabase_store", FakeDB(configured=False))
    ops = [
        lambda: seller_ops.get_seller("s1"),
        lambda: seller_ops.list_sellers(),
        lambda: seller_ops.update_seller("s1", {"name": "Ada"}),
        lambda: seller_ops.set_lead_status("s1", ["l1"], "won"),
        lambda: seller_ops.get_seller_config("s1"),
        lambda: seller_ops.create_or_update_seller("a@b.co"),
    ]
    for op in ops:
        with pytest.raises(seller_ops.OpsError) as e:
            op()
        assert e.value.kind == "unavailable"


def test_a_database_error_surfaces_as_upstream_not_a_traceback(monkeypatch):
    class Boom(FakeDB):
        def select_rows(self, *a, **k):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(seller_ops, "supabase_store", Boom())
    with pytest.raises(seller_ops.OpsError) as e:
        seller_ops.get_seller("s1")
    assert e.value.kind == "upstream"
    assert "connection reset" in e.value.message

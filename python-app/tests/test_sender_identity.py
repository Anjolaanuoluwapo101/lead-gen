"""Who the mail is from, per seller.

`SEND_FROM_EMAIL`/`SEND_FROM_NAME` in `.env` make the sender a property of the
BUILD. A seller sending outreach should be the sender, so the question is how
much of that can move per seller -- and the answer is not "all of it":

  * a display name and a Reply-To are NOT authenticated, so a seller sending
    through the platform's account can have both;
  * a From address IS authenticated. Gmail, SES and most providers refuse a From
    the logged-in account may not send as, so a seller-supplied From combined
    with the platform's host/user produces a message that fails at send time --
    loudly, or by being silently rewritten, which is worse because the seller
    never finds out.

So `config.smtp_cfg` honours a seller's `from_email` ONLY when they also brought
their own `host` and `user`. That rule is the reason this file exists; the rest
of it is the precedence chain around it.
"""

import pytest

import config
import draft_send
import send_provider

MINE = {"smtp": {"host": "smtp.gmail.com", "port": 587, "user": "me@gmail.com",
                 "password_enc": None, "from_email": "me@gmail.com"}}
THEIRS = {"smtp": {"host": "smtp.their-isp.com", "port": 2525,
                   "user": "seller@acme.com", "password_enc": None,
                   "from_email": "pitch@acme.com", "from_name": "Acme"}}
PARTIAL = {"smtp": {"from_email": "pitch@acme.com"}}          # no host/user


@pytest.fixture
def env_master(monkeypatch):
    """A platform account, which is what `.env` ships."""
    monkeypatch.setenv("SEND_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "platform@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("SEND_FROM_EMAIL", "platform@gmail.com")
    monkeypatch.setenv("SEND_FROM_NAME", "Lead Engine")
    return "platform@gmail.com"


# --------------------------------------------------------------------------- #
# The credential rule
# --------------------------------------------------------------------------- #
def test_a_seller_from_address_is_ignored_without_their_own_credentials(
        env_master):
    """The load-bearing rule. Not a style preference.

    Using the seller's From on the platform's account is what produces
    `550 From address not verified` on Gmail -- at send time, naming the
    address rather than the setting, on a demo.
    """
    cfg = config.smtp_cfg(PARTIAL)
    assert cfg["from_email"] == "platform@gmail.com"
    assert cfg["own_account"] is False


def test_the_ignored_from_address_is_reported_not_dropped(env_master):
    # A field the operator filled in that does nothing must say so, or they
    # will believe their seller's address is going out.
    cfg = config.smtp_cfg(PARTIAL)
    assert any("From address" in n for n in cfg["notes"]), cfg["notes"]


def test_a_seller_with_their_own_account_keeps_their_from_address(env_master):
    cfg = config.smtp_cfg(THEIRS)
    assert cfg["from_email"] == "pitch@acme.com"
    assert cfg["host"] == "smtp.their-isp.com"
    assert cfg["port"] == 2525
    assert cfg["user"] == "seller@acme.com"
    assert cfg["own_account"] is True
    assert cfg["notes"] == []


def test_half_a_credential_is_not_an_account(env_master):
    # A host with no user cannot authenticate; a user with no host has nowhere
    # to connect. Either alone must fall through to the platform account rather
    # than producing a config that half-works.
    for partial in ({"smtp": {"host": "smtp.acme.com", "from_email": "p@acme.com"}},
                    {"smtp": {"user": "seller@acme.com", "from_email": "p@acme.com"}}):
        cfg = config.smtp_cfg(partial)
        assert cfg["own_account"] is False, partial
        assert cfg["from_email"] == "platform@gmail.com", partial


def test_a_seller_host_without_a_seller_from_still_uses_their_account():
    # Their host/user, no From: send as their own account's user rather than
    # inventing an address. (env_master is deliberately not used here.)
    cfg = config.smtp_cfg({"smtp": {"host": "smtp.acme.com", "user": "s@acme.com"}})
    assert cfg["own_account"] is True
    assert cfg["from_email"] == "" or cfg["from_email"] is None


# --------------------------------------------------------------------------- #
# The parts that ARE safely per-seller
# --------------------------------------------------------------------------- #
def test_a_display_name_is_per_seller_even_on_the_shared_account(env_master):
    # Not authenticated, so no rule applies. It falls back to the seller ROW
    # when their settings do not name one.
    cfg, _ = draft_send.sender_config(
        sender={"id": "s1", "name": "Akin Anjola", "email": "ak@x.com"})
    assert cfg["from_name"] == "Akin Anjola"


def test_the_display_name_chain_is_settings_then_row_then_env(env_master,
                                                              monkeypatch):
    """All three rungs, in order, through the one function that composes them.

    The middle rung is the one that was broken: `smtp_cfg` had already applied
    the env default, so a seller who set nothing in settings kept the platform's
    name and never reached their own profile's.
    """
    seller = {"id": "s1", "name": "Akin Anjola", "brand": "AJ Tech"}
    monkeypatch.setattr(draft_send, "read_settings", lambda sid: {})

    cfg, _ = draft_send.sender_config("s1", sender=seller)
    assert cfg["from_name"] == "Akin Anjola"           # row beats env

    monkeypatch.setattr(draft_send, "read_settings",
                        lambda sid: dict(THEIRS))
    cfg, _ = draft_send.sender_config("s1", sender=seller)
    assert cfg["from_name"] == "Acme"                  # settings beats row

    monkeypatch.setattr(draft_send, "read_settings", lambda sid: {})
    cfg, _ = draft_send.sender_config("s1", sender={"id": "s1"})
    assert cfg["from_name"] == "Lead Engine"           # env is the floor


def test_the_row_falls_back_to_brand_when_there_is_no_name(env_master,
                                                           monkeypatch):
    monkeypatch.setattr(draft_send, "read_settings", lambda sid: {})
    cfg, _ = draft_send.sender_config("s1", sender={"id": "s1", "brand": "AJ Tech"})
    assert cfg["from_name"] == "AJ Tech"


def test_reply_to_is_the_seller_so_replies_reach_the_pitcher(env_master):
    """The point of per-seller sending identity under a shared account.

    The pitch is written and signed by the seller; a reply landing in the
    platform's inbox is a reply nobody answers.
    """
    _cfg, reply_to = draft_send.sender_config(
        sender={"id": "s1", "name": "Akin", "email": "ak@x.com"})
    assert reply_to == "ak@x.com"


def test_the_message_carries_reply_to_but_not_when_it_is_the_from(env_master):
    cfg = config.smtp_cfg(None)          # platform account
    msg = send_provider._build_message(
        to_email="lead@biz.com", subject="s", body_text="b", cfg=cfg,
        reply_to="ak@x.com")
    assert msg["Reply-To"] == "ak@x.com"
    assert msg["From"] == "Lead Engine <platform@gmail.com>"

    same = send_provider._build_message(
        to_email="lead@biz.com", subject="s", body_text="b", cfg=cfg,
        reply_to="platform@gmail.com")
    assert same["Reply-To"] is None, "a Reply-To equal to From is noise"


# --------------------------------------------------------------------------- #
# Precedence and the checks a config makes about itself
# --------------------------------------------------------------------------- #
def test_env_master_is_used_when_the_seller_has_nothing(env_master):
    cfg = config.smtp_cfg({})
    assert (cfg["host"], cfg["user"], cfg["from_email"]) == (
        "smtp.gmail.com", "platform@gmail.com", "platform@gmail.com")
    assert cfg["own_account"] is False


def test_the_default_port_is_used_when_nothing_sets_one(monkeypatch):
    for name in ("SMTP_PORT", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
                 "SEND_FROM_EMAIL", "SEND_FROM_NAME"):
        monkeypatch.delenv(name, raising=False)
    assert config.smtp_cfg({})["port"] == config.SMTP_DEFAULT_PORT
    assert config.smtp_cfg({})["port"] == 587


def test_a_malformed_port_is_corrected_and_reported(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "five-eighty-seven")
    cfg = config.smtp_cfg({})
    assert cfg["port"] == config.SMTP_DEFAULT_PORT
    assert any("not a valid port" in n for n in cfg["notes"])


def test_an_out_of_range_port_is_rejected_too(monkeypatch):
    for bad in ("0", "70000", "-1"):
        monkeypatch.setenv("SMTP_PORT", bad)
        cfg = config.smtp_cfg({})
        assert cfg["port"] == config.SMTP_DEFAULT_PORT, bad


def test_a_from_that_is_not_the_account_is_flagged(env_master):
    """The failure the operator cannot see coming.

    `SEND_FROM_EMAIL` from the shared account, `SMTP_USER` from a seller's:
    Gmail rejects it, and the message names the address, not the setting.
    """
    cfg = config.smtp_cfg({})
    cfg["user"] = "someone-else@gmail.com"
    notes = config.smtp_cfg_notes(cfg)
    assert any("differs from the SMTP account" in n for n in notes), notes


def test_half_a_credential_is_flagged(env_master):
    cfg = config.smtp_cfg({})
    cfg["password"] = None
    assert any("password is missing" in n
               for n in config.smtp_cfg_notes(cfg))


def test_send_email_refuses_when_no_from_address_resolves(monkeypatch):
    # No env master and no seller settings: nothing to send from, and the
    # refusal must name the missing piece rather than failing inside smtplib.
    for name in ("SEND_FROM_EMAIL", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SEND_BACKEND", "smtp")
    with pytest.raises(send_provider.SendRefused) as e:
        send_provider.send_email(to_email="lead@biz.com", subject="s",
                                 body_text="b", cfg=config.smtp_cfg({}))
    assert "From address" in e.value.reason


def test_configured_is_asked_about_a_seller_not_the_build(env_master):
    """`configured()` takes the cfg for the same reason the send does.

    Asking the build-wide question while sending as a particular seller is how
    the gate that allowed a send and the send itself come to disagree.
    """
    assert send_provider.configured(config.smtp_cfg(THEIRS)) is True
    no_from = dict(config.smtp_cfg(THEIRS), from_email=None)
    assert send_provider.configured(no_from) is False
    unhosted = dict(config.smtp_cfg(THEIRS), host=None)
    assert send_provider.configured(unhosted) is False


def test_the_none_backend_refuses_regardless_of_a_perfect_config(env_master,
                                                                monkeypatch):
    monkeypatch.setenv("SEND_BACKEND", "none")
    assert send_provider.configured(config.smtp_cfg(THEIRS)) is False

"""Sending: the provider layer, the file layer, and the route that joins them.

Three properties matter more than the happy path, and each is a way the build
could claim something untrue:

1. **`sent` is written only after a provider returned a message id.** A build
   that marks a draft sent because it called a function is a build whose
   dashboard lies.
2. **A send that fails is `failed`, not `approved`.** We do not know whether the
   message was delivered, so re-offering it invites a duplicate.
3. **`SEND_BACKEND=none` refuses loudly.** The default must be "nothing is
   sent, and here is why" — never a silent no-op.

The provider is stubbed at `send_provider.send_email` in the route tests, which
is the seam that makes "the provider is swappable" a testable claim rather than
an aspiration: nothing below that line is SES-specific or SMTP-specific.
"""

from types import SimpleNamespace

import pytest

import config
import draft_send
import file_store
import lead_engine
import routes_agent
import send_provider
import service_stubs as stubs


SELLER = "seller-1"


@pytest.fixture
def backend(monkeypatch):
    """Set the two switches for one test and put them back after."""

    def _set(send=None, files=None, env=None):
        if send is not None:
            monkeypatch.setenv("SEND_BACKEND", send)
        if files is not None:
            monkeypatch.setenv("FILE_BACKEND", files)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)

    return _set


# --------------------------------------------------------------------------- #
# The switch itself
# --------------------------------------------------------------------------- #
def test_an_unrecognised_send_backend_raises(backend):
    # Same rule as AGENT_BACKEND: a typo must not look like a working setting.
    # `smpt` falling back to `none` would have the app refuse every send while
    # the operator reads a config that says SMTP.
    backend(send="smpt")
    with pytest.raises(ValueError):
        config.send_backend()


def test_an_unrecognised_file_backend_raises(backend):
    backend(files="supabse")
    with pytest.raises(ValueError):
        config.file_backend()


def test_send_defaults_to_none(backend, monkeypatch):
    monkeypatch.delenv("SEND_BACKEND", raising=False)
    assert config.send_backend() == "none"
    assert send_provider.configured() is False


# --------------------------------------------------------------------------- #
# send_provider
# --------------------------------------------------------------------------- #
def test_none_backend_refuses_with_an_actionable_reason(backend):
    backend(send="none")
    with pytest.raises(send_provider.SendRefused) as exc:
        send_provider.send_email(to_email="a@b.com", subject="s", body_text="b")
    assert "SEND_BACKEND" in exc.value.reason


def test_a_bad_address_is_refused_before_the_backend_is_consulted(backend):
    # Refused even with a working backend: this is about the draft, not the
    # configuration, and the message says so.
    backend(send="smtp", env={"SMTP_HOST": "h", "SEND_FROM_EMAIL": "me@x.com"})
    with pytest.raises(send_provider.SendRefused) as exc:
        send_provider.send_email(to_email="", subject="s", body_text="b")
    assert "no recipient" in exc.value.reason


def test_a_backend_with_no_sender_configured_is_refused(backend):
    backend(send="smtp", env={"SMTP_HOST": "h"})
    with pytest.raises(send_provider.SendRefused) as exc:
        send_provider.send_email(to_email="a@b.com", subject="s", body_text="b")
    assert "SEND_FROM_EMAIL" in exc.value.reason


def test_smtp_actually_attaches_the_file(backend, monkeypatch):
    """The whole reason `Content.Raw`/`send_message` is used rather than the
    simple API: the resume has to arrive as an attachment."""
    backend(send="smtp", env={"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
                              "SMTP_USER": "u", "SMTP_PASSWORD": "p",
                              "SEND_FROM_EMAIL": "me@x.com",
                              "SEND_FROM_NAME": "Alex"})
    sent = {}
    server = stubs.FakeSMTPServer(sent)
    monkeypatch.setattr(send_provider.smtplib, "SMTP",
                        lambda *a, **k: server)

    message_id = send_provider.send_email(
        to_email="lead@biz.com", subject="Hello", body_text="Body here",
        attachments=[("resume.pdf", "application/pdf", b"%PDF-1.4 fake")])

    assert message_id
    msg = sent["message"]
    assert msg["To"] == "lead@biz.com"
    assert msg["Subject"] == "Hello"
    assert "Alex" in msg["From"]

    parts = list(msg.iter_attachments())
    assert len(parts) == 1
    assert parts[0].get_filename() == "resume.pdf"
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == b"%PDF-1.4 fake"
    assert server.started_tls is True, "STARTTLS must run before AUTH"


def test_smtp_auth_failure_is_a_send_failure_not_a_crash(backend, monkeypatch):
    backend(send="smtp", env={"SMTP_HOST": "h", "SEND_FROM_EMAIL": "me@x.com",
                              "SMTP_USER": "u", "SMTP_PASSWORD": "wrong"})
    monkeypatch.setattr(send_provider.smtplib, "SMTP",
                        lambda *a, **k: stubs.FakeSMTPServer(
                            {}, fail_auth=True))
    with pytest.raises(send_provider.SendFailed) as exc:
        send_provider.send_email(to_email="a@b.com", subject="s", body_text="b")
    assert "App Password" in str(exc.value)


def test_ses_sends_raw_mime_so_attachments_survive(backend, monkeypatch):
    backend(send="ses", env={"SEND_FROM_EMAIL": "me@x.com"})
    client = stubs.FakeSES()
    monkeypatch.setattr(send_provider, "_ses_client", lambda: client,
                        raising=False)

    send_provider.send_email(
        to_email="lead@biz.com", subject="Hello", body_text="Body",
        attachments=[("resume.pdf", "application/pdf", b"PDF")])

    call = client.calls[0]
    assert "Raw" in call["Content"], "the simple API cannot carry an attachment"
    raw = call["Content"]["Raw"]["Data"]
    assert b"resume.pdf" in raw
    assert call["Destination"]["ToAddresses"] == ["lead@biz.com"]


def test_the_ses_sandbox_error_is_explained_not_passed_through(backend,
                                                               monkeypatch):
    """SES answers an unverified recipient with a rejection that reads like a
    code fault. The operator needs the sentence that names the actual cause."""
    backend(send="ses", env={"SEND_FROM_EMAIL": "me@x.com"})
    monkeypatch.setattr(send_provider, "_ses_client",
                        lambda: stubs.FakeSES(
                            error=Exception("MessageRejected: Email address "
                                            "is not verified")))
    with pytest.raises(send_provider.SendFailed) as exc:
        send_provider.send_email(to_email="a@b.com", subject="s", body_text="b")
    assert "verified identity" in str(exc.value)
    assert "production access" in str(exc.value)


# --------------------------------------------------------------------------- #
# file_store
# --------------------------------------------------------------------------- #
def test_none_backend_stores_nothing_and_says_so(backend):
    backend(files="none")
    assert file_store.available() is False
    assert file_store.put("s/r.pdf", b"data", "application/pdf") is None
    assert file_store.get("s/r.pdf") == (None, None)


def test_local_backend_round_trips(backend, tmp_path):
    backend(files="local", env={"FILE_LOCAL_DIR": str(tmp_path)})
    key = file_store.resume_key("seller-1", "resume.pdf")
    assert file_store.put(key, b"%PDF", "application/pdf") == key
    data, _ = file_store.get(key)
    assert data == b"%PDF"
    file_store.delete(key)
    assert file_store.get(key) == (None, None)


def test_local_backend_refuses_to_escape_its_root(backend, tmp_path):
    """`key` comes out of a database column. A traversal is a traversal whatever
    wrote it, so the check is on the resolved path, not on the input."""
    backend(files="local", env={"FILE_LOCAL_DIR": str(tmp_path)})
    with pytest.raises(ValueError):
        file_store.get("../../../etc/passwd")


def test_a_resume_key_is_prefixed_by_its_owner():
    # The seller id leads the path so a per-seller storage policy is
    # expressible without moving every object later.
    assert file_store.resume_key("abc", "CV.pdf") == "abc/CV.pdf"
    # A filename with a path in it must not become a directory structure.
    assert file_store.resume_key("abc", "../../evil.pdf") == "abc/evil.pdf"


def test_supabase_storage_uses_upsert_so_a_reupload_replaces(backend,
                                                             monkeypatch):
    backend(files="supabase")
    calls = stubs.FakeRequests(status=200, body=b"%PDF")
    monkeypatch.setattr(file_store, "_supabase_creds", lambda: ("http://s", "k"),
                        raising=False)
    monkeypatch.setattr("requests.post", calls.post)
    monkeypatch.setattr("requests.get", calls.get)

    file_store.put("s/r.pdf", b"%PDF", "application/pdf")
    assert calls.posted[0]["headers"]["x-upsert"] == "true"
    data, _ = file_store.get("s/r.pdf")
    assert data == b"%PDF"


# --------------------------------------------------------------------------- #
# draft_send — turning rows into a message
# --------------------------------------------------------------------------- #
def test_the_portfolio_link_is_appended_to_the_body():
    body = draft_send.build_body({"email_body": "Hi there."},
                                 {"portfolio_url": "https://me.dev"})
    assert body == "Hi there.\n\nPortfolio: https://me.dev"


def test_the_portfolio_link_is_not_appended_twice():
    # A compose prompt may already have used the link. A second copy reads as a
    # mistake in the email, which is the one place it cannot be corrected.
    text = "See https://me.dev for more."
    assert draft_send.build_body({"email_body": text},
                                 {"portfolio_url": "https://me.dev"}) == text


def test_no_portfolio_leaves_the_body_alone():
    assert draft_send.build_body({"email_body": "Hi."}, {}) == "Hi."


def test_no_stored_file_means_no_attachment(monkeypatch):
    assert draft_send.attachments_for("s1", {"resume_key": None}) == []
    monkeypatch.setattr(file_store, "get", lambda k: (None, None))
    assert draft_send.attachments_for("s1", {"resume_key": "s1/r.pdf"}) == []


def test_a_storage_outage_does_not_stop_the_pitch(monkeypatch):
    # The resume is an enhancement, not a precondition. Losing the attachment
    # is better than losing the email.
    def boom(key):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(file_store, "get", boom)
    assert draft_send.attachments_for("s1", {"resume_key": "s1/r.pdf"}) == []


def test_the_stored_file_becomes_the_attachment(monkeypatch):
    monkeypatch.setattr(file_store, "get",
                        lambda k: (b"%PDF-1.4", "application/pdf"))
    out = draft_send.attachments_for(
        "s1", {"resume_key": "s1/r.pdf", "resume_filename": "Alex-CV.pdf"})
    assert out == [("Alex-CV.pdf", "application/pdf", b"%PDF-1.4")]

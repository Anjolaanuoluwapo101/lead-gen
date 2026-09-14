#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Send one already-composed email. Swappable, because the vendor is not the point.

WHY THERE IS A SWITCH HERE AND NOT JUST AN SES CALL

Amazon SES was the plan until the account turned out to be in the sandbox with
zero verified identities — where it can deliver to nobody at all, including its
own owner. Rather than make the demo hostage to an AWS support ticket, sending
goes through one function with three implementations:

    none  -- refuse every send, and say why (the DEFAULT)
    ses   -- Amazon SES v2, `Content.Raw` so attachments survive
    smtp  -- any SMTP host

`smtp` is the interesting one: it covers Gmail (app password, ~500/day, any
recipient), Brevo, Mailjet, SMTP2GO and Zoho without a line of code changing.
Choosing a provider becomes an env-var edit, which is the entire reason the
switch exists.

`none` is the default and that is deliberate. A build that cannot send must say
so rather than look configured; an app that quietly drops mail is worse than one
that refuses it.

WHY RAW MIME

The simple SES API takes a subject and a body and nothing else — it cannot carry
an attachment. `Content.Raw` sends the fully built message, which is also what
`smtplib.send_message` wants, so BOTH backends share one `_build_message()`.
The alternative was two message-building paths that would drift.

WHO THE MAIL IS FROM, AND WHY IT IS NOT SIMPLY A SELLER SETTING

Every function here takes an optional `cfg` -- a resolved
`config.smtp_cfg(seller_settings)`, which is `settings.smtp` falling back to the
`SMTP_*` env master. The caller resolves it once and passes the same dict to
`configured()` and to `send_email()`, so the gate that allowed the send and the
send itself cannot disagree about who the sender is.

The subtle half is which parts of it may be per-seller. A display name and a
Reply-To are not authenticated, so a seller sending through the platform account
can set both. A **From address is authenticated**, so it is honoured only when
the seller brought their own host and user -- see `config.smtp_cfg`, which
enforces that and says so in `notes` rather than dropping the value in silence.

WHAT THIS MODULE DOES NOT DO

It does not decide WHETHER to send. Ownership, approval, the concurrency claim
and the recipient check all happen in the route, before this is called, so that
swapping the vendor cannot change who is allowed to send.
"""

import smtplib
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import config


class SendRefused(Exception):
    """Nothing was sent, and why — an expected outcome, not a failure.

    Distinct from SendFailed so the route can tell "this build has no sender
    configured" (a 400 the operator must fix) apart from "the provider rejected
    it" (a 502 the recipient will never see).
    """

    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class SendFailed(Exception):
    """The provider was called and did not accept the message."""


def configured(cfg=None):
    """Is there a sender that could actually send this message?

    `cfg` is a resolved `config.smtp_cfg(...)` for one seller. Omitted means the
    env master alone, which is what a caller with no seller in hand can ask.
    """
    cfg = cfg or config.smtp_cfg(None)
    backend = config.send_backend()
    if backend == "none":
        return False
    if backend == "ses":
        return bool(cfg.get("from_email"))
    if backend == "smtp":
        return bool(cfg.get("host") and cfg.get("from_email"))
    return False


def _build_message(*, to_email, subject, body_text, attachments=(),
                   cfg=None, reply_to=None):
    """One MIME message, shared by both backends.

    Attachments are (filename, content_type, bytes). The content type is split
    rather than passed through because EmailMessage wants maintype/subtype; a
    missing or malformed type falls back to octet-stream instead of producing a
    header that some relays will reject.

    `reply_to` is per-seller and legal under a shared credential in a way that
    the From address is not: Reply-To is not authenticated, so a seller whose
    mail leaves through the platform account can still receive the replies. It
    is only set when it differs from the From address, because a Reply-To equal
    to From is noise every client has to display.
    """
    cfg = cfg or config.smtp_cfg(None)
    msg = EmailMessage()
    msg["Subject"] = subject or "(no subject)"
    from_email = (cfg.get("from_email") or "").strip()
    from_name = (cfg.get("from_name") or "").strip()
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    reply_to = (reply_to or "").strip()
    if reply_to and reply_to.lower() != from_email.lower():
        msg["Reply-To"] = reply_to
    # Set explicitly so the value we record as provider_message_id is one WE
    # chose. SMTP does not return an id, so without this the sent row would
    # have nothing to point at.
    msg["Message-ID"] = make_msgid()
    msg.set_content(body_text or "")

    for filename, content_type, data in attachments:
        maintype, _, subtype = (content_type or "").partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=filename)
    return msg


def send_email(*, to_email, subject, body_text, attachments=(), cfg=None,
               reply_to=None):
    """Send, returning the provider's message id. Raises SendRefused/SendFailed.

    `cfg` is a resolved `config.smtp_cfg(seller_settings)`; omitted means the
    env master alone. The caller resolves it ONCE and passes the same dict to
    `configured()` and here, so the gate that decided this send was allowed and
    the send itself cannot disagree about who the sender is.

    Never returns None: a send that produced no id produced no confirmed
    acceptance, and the caller writes `sent` only on the strength of this
    returning. A silent None here would let `sent` be recorded for a message
    nobody accepted.
    """
    to_email = (to_email or "").strip()
    if not to_email or "@" not in to_email:
        raise SendRefused("this draft has no recipient address")

    cfg = cfg or config.smtp_cfg(None)
    backend = config.send_backend()
    if backend == "none":
        raise SendRefused(
            "sending is switched off in this build (SEND_BACKEND=none), so "
            "nothing was sent. The draft stays approved and is available as "
            "CSV.")
    if not (cfg.get("from_email") or "").strip():
        raise SendRefused(
            f"SEND_BACKEND={backend} is set but there is no From address -- "
            f"neither this seller's settings nor SEND_FROM_EMAIL provides one, "
            f"so there is no sender to send from. "
            + " ".join(config.smtp_cfg_notes(cfg)))

    msg = _build_message(to_email=to_email, subject=subject,
                         body_text=body_text, attachments=attachments,
                         cfg=cfg, reply_to=reply_to)
    if backend == "ses":
        return _send_ses(msg, to_email, cfg)
    if backend == "smtp":
        return _send_smtp(msg, cfg)
    raise SendRefused(f"unknown SEND_BACKEND {backend!r}")


def _ses_client():
    """Separate so tests can substitute one without patching boto3.

    The import is inside the function on purpose: the `none` and `smtp`
    backends are the ones a machine without AWS credentials will use, and they
    should not need boto3 to be importable.
    """
    import boto3
    return boto3.client("sesv2", region_name=config.agentcore_region())


def _send_ses(msg, to_email, cfg=None):
    cfg = cfg or config.smtp_cfg(None)
    client = _ses_client()
    try:
        resp = client.send_email(
            FromEmailAddress=cfg.get("from_email"),
            Destination={"ToAddresses": [to_email]},
            # Raw, because the simple form cannot carry an attachment.
            Content={"Raw": {"Data": msg.as_bytes()}})
    except Exception as exc:
        raise SendFailed(_explain_ses(exc)) from exc
    return resp.get("MessageId") or msg["Message-ID"]


def _explain_ses(exc):
    """Turn SES's two demo-killers into a sentence the operator can act on.

    Both of these are configuration states, not bugs, and both produce an error
    that reads like a code failure if passed through raw.
    """
    text = str(exc)
    if "MessageRejected" in text or "not verified" in text:
        return ("SES refused the message: the sender or the recipient is not a "
                "verified identity. A sandbox account can only send to "
                "addresses verified in that region. Verify the recipient, or "
                "request production access. Details: " + text[:200])
    if "AccessDenied" in text or "not authorized" in text:
        return ("SES refused the call: these credentials lack ses:SendEmail. "
                "Details: " + text[:200])
    return text[:300]


def _send_smtp(msg, cfg=None):
    cfg = cfg or config.smtp_cfg(None)
    host = cfg.get("host")
    port = int(cfg.get("port") or 587)
    user = (cfg.get("user") or "").strip()
    password = cfg.get("password") or ""
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            # Upgrade before authenticating, or the password crosses the wire
            # in clear. Port 465 is implicit-TLS and is not handled here on
            # purpose: 587+STARTTLS is what every provider in the README
            # documents, and a half-supported second mode is worse than one
            # that obviously is not there.
            if port != 25:
                server.starttls()
                server.ehlo()
            if user:
                server.login(user, password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise SendFailed(
            "the SMTP server rejected the username/password. For Gmail this "
            "must be an App Password, not the account password. "
            + str(exc)[:200]) from exc
    except Exception as exc:
        raise SendFailed(f"{type(exc).__name__}: {str(exc)[:250]}") from exc
    return msg["Message-ID"]


def new_idempotency_token():
    """A per-attempt token, for providers that accept one."""
    return str(uuid.uuid4())

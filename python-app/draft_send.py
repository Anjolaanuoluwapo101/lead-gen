#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
One implementation of "send this draft as this seller".

WHY THIS MODULE EXISTS

`routes_agent.py` decides WHO may send. `send_provider.py` decides HOW mail
leaves. Neither of them should know how a seller's resume becomes an
attachment, and putting that in the route would mean the next caller (a retry
job, a CLI) reimplements it — which is how the `resume_key` column ends up
meaning two different things in two places.

So this is the one place that turns a draft row + a seller row into an actual
message:

    body        = the draft's body, plus the portfolio link if there is one
    attachments = the seller's stored resume FILE, if one was ever uploaded

WHAT GETS ATTACHED, AND WHY IT IS THE FILE

The seller's `resume_text` — the extracted text the drafting model reads — is
NOT attached. It is a lossy summary of a formatted document: attaching it would
send the recipient a wall of unformatted text with the seller's name on it. The
file is what a resume is. `file_store.get()` is what makes that possible, and
when no file is stored (FILE_BACKEND=none, or an upload from before the bytes
were kept) the send proceeds with no attachment rather than failing — a resume
is an enhancement to the pitch, not a precondition for it.
"""

import json

import config
import file_store
import send_provider
import supabase_store


def read_sender(seller_id):
    """The seller fields a send needs. Empty dict if unreadable.

    Deliberately a narrow read rather than `select_rows('seller_profile', '*')`:
    the row carries ENCRYPTED PROVIDER KEYS in `settings`, and a function whose
    job is to build an email should not be handed secrets it has no use for.
    """
    if not seller_id:
        return {}
    try:
        rows = supabase_store.select_rows(
            "seller_profile",
            columns=("id,email,name,title,brand,phone,portfolio_url,"
                     "resume_filename,resume_key,resume_content_type"),
            filters={"id": seller_id}, limit=1)
    except Exception:
        return {}
    return rows[0] if rows else {}


def read_settings(seller_id):
    """The seller's `settings` jsonb only, or {}.

    A SECOND read rather than widening `read_sender`, which excludes `settings`
    on purpose. The two callers want different halves of the same row and
    merging them would hand the resume/body builder every encrypted provider
    key the seller owns. This function's only consumer resolves the SMTP block,
    so that is all it is for.
    """
    if not seller_id:
        return {}
    try:
        rows = supabase_store.select_rows(
            "seller_profile", columns="id,settings",
            filters={"id": seller_id}, limit=1)
    except Exception:
        return {}
    if not rows:
        return {}
    raw = rows[0].get("settings")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def sender_config(seller_id=None, sender=None):
    """The resolved SMTP config for one seller, plus who replies should reach.

    Resolve ONCE and pass the same dict to `send_provider.configured()` and
    `send_provider.send_email()`: the gate that decides a send is allowed and
    the send itself must not be able to disagree about the sender, and resolving
    twice is how they would.

    Two per-seller fields are layered on top of `config.smtp_cfg` here, because
    they come from the seller ROW rather than the settings block:

      * display name -- settings.smtp.from_name, else the seller's name/brand,
        else SEND_FROM_NAME. A display name is not authenticated, so this is
        safe under the shared account.
      * Reply-To -- the seller's own email. Also unauthenticated, and the whole
        point of outreach: replies must reach the person who wrote the pitch,
        not the platform account that carried it. Left out when it equals the
        From address, since a Reply-To identical to From is noise.
    """
    seller = sender if sender is not None else read_sender(seller_id)
    cfg = config.smtp_cfg(read_settings(seller_id))
    # Only when the SELLER did not name one. Checking `not cfg["from_name"]`
    # would be wrong: smtp_cfg has already filled in SEND_FROM_NAME by then, so
    # a seller who set nothing would keep the platform's name rather than
    # falling back to their own profile -- which is the case that matters.
    if not cfg.get("from_name_set"):
        row_name = (str((seller or {}).get("name") or "").strip()
                    or str((seller or {}).get("brand") or "").strip())
        if row_name:
            cfg["from_name"] = row_name
    reply_to = str((seller or {}).get("email") or "").strip()
    return cfg, (reply_to or None)


def build_body(draft, sender):
    """The draft's body, with the portfolio link appended if the seller has one.

    Appended at send time rather than stored in `email_body`, so what the human
    approved is what the human saw. Rewriting the stored body would mean the
    editor's "saved revision" no longer matches what actually went out.

    The link is added only when it is not already in the text — a compose prompt
    may well have used it, and appending a second copy reads as a mistake.
    """
    body = (draft.get("email_body") or "").rstrip()
    url = (sender.get("portfolio_url") or "").strip()
    if url and url not in body:
        body = f"{body}\n\nPortfolio: {url}"
    return body


def attachments_for(seller_id, sender):
    """[(filename, content_type, bytes)] — empty when there is nothing to attach.

    Returns an empty list for every "no file" case rather than raising: none of
    them is an error, and the caller's job is the same in each.
    """
    key = (sender or {}).get("resume_key")
    if not key:
        return []
    try:
        data, content_type = file_store.get(key)
    except Exception:
        # A storage outage must not turn "send the pitch" into "send nothing".
        # The draft still goes out; only the attachment is lost.
        return []
    if not data:
        return []
    filename = (sender.get("resume_filename")
                or key.rsplit("/", 1)[-1] or "resume")
    return [(filename,
             content_type or sender.get("resume_content_type")
             or "application/octet-stream",
             data)]


def send(draft, seller_id=None, sender=None, cfg=None, reply_to=None):
    """Send one draft. Returns a result dict, or raises SendRefused/SendFailed.

    The result is what the route writes into the row, so it is built to BE the
    audit trail: which provider carried it, what it was called, and which object
    was attached. A successful send with no record of the attachment would make
    "what did we send this lead?" unanswerable the moment the seller updates
    their resume.

    `sender`/`cfg` are accepted so the caller can resolve them once and reuse
    them; both are read here when omitted, which keeps the single-call path
    (a CLI, a retry job) working without the caller having to know the shape.
    """
    seller_id = seller_id or draft.get("seller_id")
    if sender is None:
        sender = read_sender(seller_id)
    if cfg is None:
        cfg, reply_to = sender_config(seller_id, sender)
    attachments = attachments_for(seller_id, sender)

    message_id = send_provider.send_email(
        to_email=draft.get("to_email"),
        subject=draft.get("subject"),
        body_text=build_body(draft, sender),
        attachments=attachments,
        cfg=cfg, reply_to=reply_to)

    return {
        "provider_message_id": message_id,
        "send_backend": _backend_name(),
        "attachment_key": (sender or {}).get("resume_key") if attachments else None,
        "attachment_name": attachments[0][0] if attachments else None,
    }


def _backend_name():
    """The configured backend, without letting a typo break a completed send.

    The message has already gone out by the time this is called, so a bad
    SEND_BACKEND must not raise here and lose the fact that it succeeded.
    """
    try:
        return config.send_backend()
    except Exception:
        return "unknown"

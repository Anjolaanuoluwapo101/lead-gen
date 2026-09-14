"""Compose ONE outreach draft for ONE lead. The shared middle of two routes.

WHY THIS MODULE EXISTS

Two routes produce a draft:

- `POST /draft` (app.py) — generates and RETURNS it. n8n reads this, its
  response shape is frozen, and nothing is persisted.
- `POST /drafts` (routes_agent.py) — generates and PERSISTS it, for the
  dashboard's review queue.

They differ in exactly two ways: whether the result is saved, and what the
response envelope looks like. Everything between "here is a lead id" and "here
is a subject, body and angle" is identical — the lead read, the campaign niche,
the seller context, the prospect snapshot, the provider resolution, the prompt
call. Duplicating that would mean two prompt-building paths that drift, and the
one that drifts silently is whichever route is used less.

So the middle was lifted here, and both routes call it. app.py's `/draft` keeps
its own seller resolution, ownership gate and response envelope — only its body
changed.

WHAT THIS FUNCTION DOES NOT DECIDE

Seller resolution and the ownership gate stay in the ROUTES, not here. That is
deliberate: this module is handed a `seller_id` and trusts it. A caller that
skips the gate gets a draft composed with another tenant's resume and provider
key, so every caller must gate first. Keeping the gate outside this function
means the gate is visible at each call site rather than buried in a shared
helper nobody re-reads.

The `settings` value returned by `leads_read.read_seller_for_draft` can hold
encrypted provider keys. It is used to build the LLM provider and is never
included in the payload, never handed to the model, and never logged.
"""

import config
import leads_read
import llm
import seller_ops
import supabase_store
from providers import get_llm

PROSPECT_FIELDS = ("business_name", "category", "phone", "website",
                   "locality", "street", "region", "zipcode")

# Fill these from the lead row when the embedded prospect is missing them, so a
# draft still says something concrete rather than "this business".
LEAD_FALLBACK_FIELDS = ("business_name", "category", "website", "locality")


def compose_draft(lead_id, seller_id, *, temperature=0.7, score_model=None):
    """The draft payload for `lead_id`, composed as `seller_id`.

    Returns the dict the routes spread into their responses:
    `{lead_id, seller_id, seller_name, campaign_id, niche, subject, email_body,
    angle}`.

    Raises `seller_ops.OpsError`:
      - `bad_request` — no lead_id
      - `not_found`   — the lead does not exist
      - `upstream`    — a read failed, or the model call failed
    """
    lead_id = str(lead_id or "").strip()
    if not lead_id:
        raise seller_ops.OpsError("bad_request", "lead_id is required")

    lead = leads_read.read_lead(lead_id)
    if not lead:
        raise seller_ops.OpsError("not_found", "lead not found")

    campaign_id = lead.get("campaign_id")
    # A missing niche is not an error — the caller falls back to the seller's
    # own positioning, which is why read_campaign_niche returns (None, None)
    # rather than raising.
    niche, _campaign_seller_id = leads_read.read_campaign_niche(campaign_id)

    seller, settings = leads_read.read_seller_for_draft(seller_id)

    business = _business_from(lead)
    intelligence = leads_read.decoded(lead.get("intelligence"))
    weakness = str(lead.get("weakness") or "").strip()
    first_line = str(lead.get("first_line") or "").strip()
    # A lead with no recorded weakness still needs the model to know what is
    # being sold, or the email has no angle at all.
    if not weakness and (business.get("category") or niche):
        weakness = (f"Category: {business.get('category') or '?'}. "
                    f"We're selling: {niche or '?'}.")

    try:
        provider = get_llm(config.llm_cfg(settings, model=score_model))
    except Exception as exc:
        raise seller_ops.OpsError("upstream",
                                  f"could not build a provider: {exc}")

    try:
        out = llm.draft_email(
            provider, business=business, intelligence=intelligence,
            niche=niche, weakness=weakness, first_line=first_line,
            seller=seller, temperature=float(temperature or 0.7))
    except Exception as exc:
        raise seller_ops.OpsError("upstream", f"draft failed: {exc}")

    return {
        "lead_id": lead_id,
        "seller_id": seller_id,
        "seller_name": (seller.get("name") or seller.get("brand")) or None,
        "campaign_id": campaign_id,
        "niche": niche,
        **(out or {}),
    }


def _business_from(lead):
    """The prospect snapshot handed to the model.

    The embedded `prospect_id` object is the good source; older rows only have
    the lead's own columns, so those fill the gaps. Empty values are dropped
    rather than sent as nulls — the prompt reads better without them.
    """
    prospect = lead.get("prospect_id")
    if isinstance(prospect, dict):
        business = {k: prospect.get(k) for k in PROSPECT_FIELDS}
        business = {k: v for k, v in business.items() if v}
    else:
        business = {"business_name": "this business"}
    for key in LEAD_FALLBACK_FIELDS:
        if not business.get(key):
            business[key] = lead.get(key)
    return business


def first_email(lead):
    """The first usable address on a lead, or None.

    `emails` is stored as an encoded jsonb column and has been seen as a raw
    string on older rows, so both shapes are handled.
    """
    raw = (lead or {}).get("emails")
    if isinstance(raw, str):
        raw = leads_read.decoded(raw)
    if not isinstance(raw, list):
        return None
    for addr in raw:
        addr = str(addr or "").strip()
        if "@" in addr:
            return addr
    return None


def read_lead(lead_id):
    """Kept here so routes_agent does not need a second import for one call."""
    return leads_read.read_lead(lead_id)


__all__ = ["compose_draft", "first_email", "read_lead", "supabase_store"]

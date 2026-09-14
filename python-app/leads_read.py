"""Shared lead-read helpers.

Extracted from app.py so the Flask app and the Strands agent read leads through
ONE definition. Without this, the agent would carry its own copy of the column
list and the ownership rule -- and divergence in the ownership rule is a
security bug, not a cosmetic one.

Moved verbatim from app.py: LEAD_COLUMNS, DRAFT_LEAD_COLUMNS, flatten_leads,
decoded, owns_campaign. app.py keeps aliases so its call sites are untouched.
"""

import json

import lead_engine
import supabase_store

# Columns n8n needs for outreach + to render the lead. Business contact fields
# (name/phone/website/…) live on the linked PROSPECT, so we embed them via the
# FK and lift them to the lead's top level for a flat, n8n-friendly shape.
LEAD_COLUMNS = ("id,campaign_id,digital_presence,opportunity_score,"
                "confidence_score,source_agreement,score,qualified,status,"
                "emails,emails_extra,first_line,weakness,last_active_at,"
                "created_at,prospect_id(id,business_name,phone,website,category,"
                "locality,street,region,zipcode)")

DRAFT_LEAD_COLUMNS = (
    "id,campaign_id,prospect_id(id,business_name,phone,website,category,"
    "locality,street,region,zipcode),weakness,first_line,intelligence,emails,"
    "digital_presence,opportunity_score,qualified,created_at")


def flatten_leads(rows):
    """Decode jsonb/text fields PostgREST returns as strings, and lift the
    embedded prospect object's contact fields up to the lead top level (so n8n
    sees business_name/phone/website without digging into a nested object)."""
    out = []
    for r in rows:
        r = dict(r or {})
        for k in ("emails", "emails_extra"):
            v = r.get(k)
            if isinstance(v, str) and v:
                try:
                    r[k] = json.loads(v)
                except (json.JSONDecodeError, ValueError):
                    pass
        prosp = r.get("prospect_id")
        if isinstance(prosp, dict):
            for k in ("business_name", "phone", "website", "category",
                      "locality", "street", "region", "zipcode"):
                if k in prosp and r.get(k) in (None, ""):
                    r[k] = prosp[k]
            r["prospect_id"] = prosp.get("id")
        out.append(r)
    return out


def decoded(obj):
    if isinstance(obj, str):
        try:
            return json.loads(obj)
        except (json.JSONDecodeError, ValueError):
            return {}
    return obj or {}


def read_lead(lead_id):
    """One lead row with its embedded prospect (prospect_id stays a dict, so the
    caller can lift business fields itself). None when the lead is absent.

    Deliberately NOT flattened: the draft path needs the nested prospect object.
    Use flatten_leads() for the n8n-facing flat shape."""
    rows = supabase_store.select_rows(
        "leads", columns=DRAFT_LEAD_COLUMNS,
        filters={"id": lead_id}, limit=1)
    return rows[0] if rows else None


def business_from_lead(lead):
    """Clean prospect snapshot for the LLM: the embedded prospect's fields when
    present, else the lead's own, with blanks dropped."""
    prospect = (lead or {}).get("prospect_id")
    if isinstance(prospect, dict):
        business = {k: prospect.get(k) for k in
                    ("business_name", "category", "phone", "website",
                     "locality", "street", "region", "zipcode")}
        business = {k: v for k, v in business.items() if v}
    else:
        business = {"business_name": "this business"}
    for k in ("business_name", "category", "website", "locality"):
        if not business.get(k):
            business[k] = (lead or {}).get(k)
    return business


def read_campaign_niche(campaign_id):
    """(niche, seller_id) for a campaign. Niche prefers the stored
    niche_rules.niche, falling back to the campaign keyword. Returns
    (None, None) when the campaign is unknown or unreadable — a missing niche
    is not an error, the caller falls back to the seller's own."""
    if not campaign_id:
        return None, None
    try:
        rows = supabase_store.select_rows(
            "campaigns", columns="id,keyword,niche_rules,seller_id",
            filters={"id": campaign_id}, limit=1)
    except Exception:
        return None, None
    if not rows:
        return None, None
    rules = decoded(rows[0].get("niche_rules"))
    niche = rules.get("niche") or rows[0].get("keyword")
    return niche, rows[0].get("seller_id")


def read_seller_for_draft(seller_id):
    """(seller_context, settings) for a seller.

    seller_context is the identity the model may see (name/title/brand/phone/
    resume/portfolio). `settings` can hold ENCRYPTED PROVIDER KEYS — it is
    returned for the caller to build an LLM provider with, and must NEVER be
    handed to the model or echoed into a tool result.
    """
    if not seller_id:
        return {}, None
    try:
        rows = supabase_store.select_rows(
            "seller_profile",
            columns=("id,email,name,title,brand,phone,resume_text,"
                     "portfolio_text,render_mode,settings"),
            filters={"id": seller_id}, limit=1)
    except Exception:
        return {}, None
    if not rows:
        return {}, None
    seller = {k: rows[0].get(k) for k in
              ("name", "title", "brand", "phone",
               "resume_text", "portfolio_text")}
    return seller, decoded(rows[0].get("settings"))


def owns_campaign(seller_id, campaign_id):
    """True when `campaign_id` belongs to `seller_id`. A legacy NULL-owner
    campaign counts as owned only by the DEFAULT single-tenant bridge (so a
    pre-backfill install keeps surfacing its own rows). Unknown campaign ->
    False."""
    if not seller_id or not campaign_id:
        return False
    try:
        rows = supabase_store.select_rows(
            "campaigns", columns="id,seller_id",
            filters={"id": campaign_id}, limit=1)
    except Exception:
        return False
    if not rows:
        return False
    owner = rows[0].get("seller_id")
    if owner is not None:
        return str(owner) == seller_id
    return seller_id == (lead_engine.DEFAULT_SELLER_ID or "").strip()

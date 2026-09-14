"""Seller + lead operations, shared by the Flask API and the Strands agent tools.

WHY THIS MODULE EXISTS

These operations used to live inline in app.py's route functions. The agent
needs the same abilities (create a seller, set a resume, fetch a portfolio,
change a lead's status), and copying the logic into agent/tools.py would create
two sets of validation rules that drift apart — the binder would accept a
render_mode the tool rejected, or vice versa. So the rules live here once, and
both surfaces call them.

This mirrors what leads_read.py already does for the read path.

WHY THE FUNCTIONS RAISE INSTEAD OF RETURNING HTTP

The two callers want different failure shapes: a Flask route wants a status
code, and an agent tool wants {"ok": False, "error": ...} that the model can
read and react to. So these functions raise `OpsError` carrying a semantic
`kind`, and each caller translates. Keeping the HTTP vocabulary out of here is
what lets the agent reuse it at all.

SECURITY: THE `settings` COLUMN

`seller_profile.settings` holds the seller's provider API keys, encrypted with
Fernet (at_rest.py). It is NOT in SAFE_COLUMNS, and no read path in this module
returns it. Four routes previously selected it into their responses; ciphertext
is still secret material, and for a tool it is worse, because everything a tool
returns is copied into the model's context window.

Reading the keys for the purpose of USING them is a different thing and still
works: config.llm_cfg()/finder_cfg() (config.py) decrypt in memory and hand back
only what an adapter needs, and leads_read.read_seller_for_draft() keeps the
decrypted settings inside the drafting path. Neither returns them to a caller.

WHAT THE AGENT MAY NOT DO

There is deliberately no set_provider_key() here. The agent reads untrusted
web pages (scraped business sites, email bodies), so a hostile page can put
instructions in front of the model. Giving the model a tool that rewrites
credentials turns that into a live attack path: a page could talk the agent
into repointing the seller's account. Keys are set by a human via the Flask
API. The agent can read the MASKED view (which provider, whether a key is set)
and nothing more.
"""

import hashlib
from datetime import datetime, timezone

import identity
import leads_read
import supabase_store

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
SELLER_RENDER_MODES = {"html", "js", "auto"}
LEAD_STATUSES = {"new", "contacted", "replied", "won", "lost"}

SELLER_PATCHABLE = ("name", "title", "brand", "niche", "phone",
                    "portfolio_url", "render_mode", "active")

# NOTE: no `settings`. See the module docstring.
SAFE_COLUMNS = ("id,email,name,title,brand,niche,phone,resume_text,"
                "portfolio_url,portfolio_text,render_mode,active,"
                "created_at,updated_at")

LIST_COLUMNS = "id,email,name,brand,title,render_mode,active,created_at"


class OpsError(Exception):
    """A failed operation, with a semantic kind the caller maps to its own
    vocabulary (HTTP status, or an agent tool's error string)."""

    def __init__(self, kind, message):
        self.kind = kind          # 'bad_request' | 'not_found' | 'unavailable' | 'upstream'
        self.message = message
        super().__init__(message)


# --------------------------------------------------------------------------- #
# Coercion helpers (moved verbatim from app.py so behaviour does not shift)
# --------------------------------------------------------------------------- #
def text(value):
    """Any scalar -> trimmed string, or None. n8n forms may send numbers (a
    phone typed as digits), so never call .strip() on the raw value."""
    return None if value is None else (str(value).strip() or None)


def render_mode(value, default="auto"):
    v = str(value or "").strip().lower()
    return v if v in SELLER_RENDER_MODES else default


def maybe_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def mask_settings(settings):
    """A seller's settings as safe to return: provider/model readable, every
    stored secret reduced to a 'set' flag. Never the token, never plaintext.

    Takes the RAW decoded settings dict (with *_enc fields)."""
    settings = settings or {}
    llm = settings.get("llm") or {}
    finder = settings.get("finder") or {}
    return {
        "llm": {
            "provider": llm.get("provider"),
            "model": llm.get("model"),
            "base_url": llm.get("base_url"),
            "api_key": "********" if llm.get("api_key_enc") else None,
        },
        "finder": {
            "provider": finder.get("provider"),
            "login": "********" if finder.get("login_enc") else None,
            "password": "********" if finder.get("password_enc") else None,
        },
    }


def _require_db():
    if not supabase_store.configured():
        raise OpsError("unavailable", "Supabase not configured")


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Sellers
# --------------------------------------------------------------------------- #
def create_or_update_seller(email, name=None, title=None, brand=None, niche=None,
                            phone=None, render_mode_value=None, render_mode_given=False):
    """Find-or-create by email (the natural key the n8n web form uses).

    Re-posting the same email always yields the same seller_id, so a form submit
    can safely be repeated. On an EXISTING seller, any field the caller actually
    supplied is applied; fields left blank are NOT cleared, since a form only
    sends what the operator typed.
    """
    email = str(email or "").strip().lower()
    if not email or "@" not in email:
        raise OpsError("bad_request", "a valid email is required")
    _require_db()

    supplied = {"name": text(name), "title": text(title), "brand": text(brand),
                "niche": text(niche), "phone": text(phone)}

    try:
        existing = supabase_store.select_rows(
            "seller_profile", columns=SAFE_COLUMNS,
            filters={"email": email}, limit=1)
    except Exception as e:
        raise OpsError("upstream", str(e))

    if existing:
        updates = {k: v for k, v in supplied.items() if v}
        if render_mode_given:
            updates["render_mode"] = render_mode(render_mode_value)
        if not updates:
            return {"created": False, "updated": False, "seller": existing[0]}
        updates["updated_at"] = _now()
        try:
            updated = supabase_store.update_rows(
                "seller_profile", updates, {"id": existing[0]["id"]})
        except Exception as e:
            raise OpsError("upstream", str(e))
        return {"created": False, "updated": True,
                "seller": (updated or existing)[0]}

    row = {"email": email, **supplied,
           "render_mode": render_mode(render_mode_value)}
    try:
        created = supabase_store.insert_rows("seller_profile", row)
    except Exception as e:
        raise OpsError("upstream", str(e))
    return {"created": True, "updated": False, "seller": (created or [{}])[0]}


def update_seller(seller_id, fields):
    """Patch profile fields. Only SELLER_PATCHABLE scalars are accepted —
    config/settings and resume/portfolio have their own operations."""
    updates = {}
    for k in SELLER_PATCHABLE:
        if k not in fields:
            continue
        v = fields[k]
        if k == "render_mode":
            # Validate the RAW value: render_mode() coerces anything unknown to
            # "auto", so routing through it here would swallow the typo a 400
            # exists to catch (render_mode="htm" would silently become auto).
            v = str(v or "").strip().lower()
            if v not in SELLER_RENDER_MODES:
                raise OpsError("bad_request", "render_mode must be html|js|auto")
        elif k == "active":
            v = maybe_bool(v)
            if v is None:
                continue
        elif v is None:
            continue
        else:
            v = text(v)
        updates[k] = v

    if not updates:
        raise OpsError("bad_request", "no valid fields to update")

    updates["updated_at"] = _now()
    _require_db()
    try:
        updated = supabase_store.update_rows(
            "seller_profile", updates, {"id": seller_id})
    except Exception as e:
        raise OpsError("upstream", str(e))
    if not updated:
        raise OpsError("not_found", "seller not found")
    return updated[0]


def get_seller(seller_id):
    _require_db()
    try:
        rows = supabase_store.select_rows(
            "seller_profile", columns=SAFE_COLUMNS,
            filters={"id": seller_id}, limit=1)
    except Exception as e:
        raise OpsError("upstream", str(e))
    if not rows:
        raise OpsError("not_found", "seller not found")
    return rows[0]


def find_seller_by_email(email):
    email = str(email or "").strip().lower()
    if not email:
        raise OpsError("bad_request", "email is required")
    _require_db()
    try:
        rows = supabase_store.select_rows(
            "seller_profile", columns=SAFE_COLUMNS,
            filters={"email": email}, limit=1)
    except Exception as e:
        raise OpsError("upstream", str(e))
    if not rows:
        raise OpsError("not_found", "seller not found")
    return rows[0]


def list_sellers(active_only=True):
    _require_db()
    filters = {"active": "true"} if active_only else None
    try:
        return supabase_store.select_rows(
            "seller_profile", columns=LIST_COLUMNS,
            filters=filters, order="created_at.desc")
    except Exception as e:
        raise OpsError("upstream", str(e))


# --------------------------------------------------------------------------- #
# Resume / portfolio
# --------------------------------------------------------------------------- #
def set_resume_text(seller_id, text_value, filename=None):
    """Store already-extracted resume text.

    Deliberately takes TEXT, not a file. File parsing (docx/pdf) stays on the
    Flask upload routes; the agent has no way to receive a binary upload, and
    teaching the model to base64 a file would be worse than useless.
    """
    value = (text_value or "").strip()
    if not value:
        raise OpsError("bad_request", "resume text is empty")
    _require_db()
    updates = {"resume_text": value, "updated_at": _now()}
    if filename:
        updates["resume_filename"] = str(filename).strip()
    try:
        updated = supabase_store.update_rows(
            "seller_profile", updates, {"id": seller_id})
    except Exception as e:
        raise OpsError("upstream", str(e))
    if not updated:
        raise OpsError("not_found", "seller not found")
    return {"chars": len(value), "seller": updated[0]}


def fetch_portfolio(seller_id, url, render_mode_value=None):
    """Scrape a seller's portfolio site into visible text and store it.

    portfolio.py is imported here, not at module scope: it pulls the heavy
    anti-bot HTTP stack (curl_cffi, cloudscraper), and nothing else in this
    module needs it.
    """
    url = str(url or "").strip()
    if not url:
        raise OpsError("bad_request", "url is required")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _require_db()

    try:
        sellers = supabase_store.select_rows(
            "seller_profile", columns="id,render_mode",
            filters={"id": seller_id}, limit=1)
    except Exception as e:
        raise OpsError("upstream", str(e))
    if not sellers:
        raise OpsError("not_found", "seller not found")

    mode = (render_mode_value if str(render_mode_value or "").strip().lower()
            in SELLER_RENDER_MODES else None)
    mode = mode or sellers[0].get("render_mode") or "auto"

    import portfolio
    try:
        fetched = portfolio.fetch_portfolio(url, mode)
    except Exception as e:
        raise OpsError("upstream", f"portfolio fetch failed: {e}")
    if not fetched.get("ok"):
        raise OpsError("upstream", fetched.get("error") or "portfolio fetch failed")

    try:
        supabase_store.update_rows(
            "seller_profile",
            {"portfolio_url": url, "portfolio_text": fetched.get("text", ""),
             "updated_at": _now()},
            {"id": seller_id})
    except Exception as e:
        raise OpsError("upstream", str(e))

    return {
        "portfolio_url": url,
        "render_mode_used": fetched.get("render_mode"),
        "rendered_js": fetched.get("rendered"),
        "page_title": fetched.get("page_title"),
        "chars": len(fetched.get("text", "")),
        "notes": fetched.get("notes", []),
    }


# --------------------------------------------------------------------------- #
# Provider config (READ-ONLY, masked)
# --------------------------------------------------------------------------- #
def get_seller_config(seller_id):
    """The masked view of a seller's provider config. Read-only by design —
    see the module docstring for why there is no setter here."""
    _require_db()
    try:
        rows = supabase_store.select_rows(
            "seller_profile", columns="id,settings",
            filters={"id": seller_id}, limit=1)
    except Exception as e:
        raise OpsError("upstream", str(e))
    if not rows:
        raise OpsError("not_found", "seller not found")
    # `settings` may arrive as a JSON string (PostgREST) or a dict; decoded()
    # normalises both and degrades to {} on malformed JSON.
    return mask_settings(leads_read.decoded(rows[0].get("settings")))


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #
def set_lead_status(seller_id, lead_ids, status):
    """Flip the lifecycle status of one or more leads, all-or-nothing.

    Ownership is enforced before any write: a seller may only change leads whose
    campaign they own. If ANY target lead is not owned, the whole batch is
    refused rather than partially applied, so a caller can never end up with a
    half-updated set it cannot reason about.
    """
    import lead_engine          # local: avoids a circular import at module load

    status = str(status or "").strip().lower()
    if status not in LEAD_STATUSES:
        raise OpsError("bad_request", f"status must be one of {sorted(LEAD_STATUSES)}")
    if isinstance(lead_ids, str):
        lead_ids = [lead_ids]
    if not isinstance(lead_ids, list) or not lead_ids:
        raise OpsError("bad_request", "lead_ids must be a uuid string or list")
    _require_db()

    unique_ids = list(dict.fromkeys(str(x) for x in lead_ids))

    try:
        lrows = supabase_store.select_rows(
            "leads", columns="id,campaign_id", filters_in={"id": unique_ids})
    except Exception as e:
        raise OpsError("upstream", str(e))
    if len(lrows) != len(unique_ids):
        raise OpsError("not_found",
                       "one or more leads not found or not owned by this seller")

    campaign_ids = sorted({str(r["campaign_id"]) for r in lrows
                           if r.get("campaign_id")})
    if not campaign_ids:
        raise OpsError("not_found", "leads have no campaign owner to verify")

    try:
        crow = supabase_store.select_rows(
            "campaigns", columns="id,seller_id", filters_in={"id": campaign_ids})
    except Exception as e:
        raise OpsError("upstream", str(e))

    default_seller = (lead_engine.DEFAULT_SELLER_ID or "").strip()
    owned = set()
    for c in crow:
        owner = c.get("seller_id")
        if owner is not None:
            if str(owner) == str(seller_id):
                owned.add(str(c["id"]))
        elif str(seller_id) == default_seller:
            owned.add(str(c["id"]))   # DEFAULT bridge owns legacy NULL-owner rows
    if not set(campaign_ids).issubset(owned):
        raise OpsError("not_found",
                       "one or more leads not found or not owned by this seller")

    updated, errors = 0, []
    for lid in unique_ids:
        try:
            rows = supabase_store.update_rows(
                "leads", {"status": status, "updated_at": _now()}, {"id": lid})
            updated += 1 if rows is not None else 0
        except Exception as e:
            errors.append(f"{lid}: {e}")
    return {"updated": updated, "errors": errors, "status": status}


# --------------------------------------------------------------------------- #
# Hand-added businesses ("I already know who I want to pitch")
# --------------------------------------------------------------------------- #
# WHY A CAMPAIGN IS INVENTED RATHER THAN SKIPPED
#
# A campaign IS a search: `campaigns.keyword` and `campaigns.location` are both
# `not null`, and `target_key` is a hash of seller|keyword|place|niche so that
# re-running the same target reuses one row. A NAMED BUSINESS has no keyword and
# no location, so it cannot be expressed as one of those -- there is nothing to
# hash.
#
# Leaving the prospect's `campaign_id` null is not an escape either. Ownership
# runs through the campaign: `leads_read.owns_campaign` reads
# `campaigns.seller_id`, and every read/write route gates on it. An ownerless
# prospect is a row no route can authorise, which in a multi-tenant app is
# either invisible or an open door.
#
# So one "Direct adds" campaign per seller, found-or-created by a synthetic but
# STABLE target_key (hashed from the seller id alone), and every hand-added
# business lands in it. Ownership keeps working and no schema changes.
DIRECT_CAMPAIGN_NAME = "Direct adds"


def direct_target_key(seller_id):
    """Stable target_key for a seller's hand-added businesses.

    Deliberately derived from the seller id ONLY. If it included the business
    being added, every add would mint a new campaign and the seller's list would
    fill with one-business campaigns.
    """
    return hashlib.sha1(
        f"direct|{seller_id or ''}".encode("utf-8")).hexdigest()


def ensure_direct_campaign(seller_id):
    """The seller's Direct-adds campaign id, creating it on first use."""
    _require_db()
    if not seller_id:
        raise OpsError("bad_request", "seller_id is required")
    target_key = direct_target_key(seller_id)
    try:
        existing = supabase_store.select_rows(
            "campaigns", columns="id",
            filters={"target_key": target_key, "seller_id": seller_id}, limit=1)
    except Exception as e:
        raise OpsError("unavailable", f"campaign lookup failed: {e}")
    if existing and existing[0].get("id"):
        return existing[0]["id"]

    created = supabase_store.insert_rows("campaigns", {
        "name": DIRECT_CAMPAIGN_NAME,
        # Not a real search, and labelled so nothing downstream mistakes it for
        # one. The niche_rules block keeps the same self-describing shape a
        # normal campaign has.
        "keyword": "direct",
        "location": "direct",
        "target_key": target_key,
        "seller_id": seller_id,
        "niche_rules": {"niche": "", "source": "direct", "direct_add": True},
    })
    cid = (created or [{}])[0].get("id")
    if not cid:
        raise OpsError("upstream", "could not create the Direct adds campaign")
    return cid


def add_direct_prospect(seller_id, row, source="dataforseo"):
    """Store ONE hand-added business as a prospect. Returns a summary dict.

    Inserts the prospect and stops. It does NOT crawl, score or draft: those
    need the orchestrator and a run, and a run is what this feature exists to
    avoid -- there is no search to bound and nothing to cancel. The business
    appears in the seller's list and the normal machinery takes over whenever
    they ask it to.

    Deduplication is by `identity.dedup_key` within the seller's Direct-adds
    campaign, so adding the same business twice re-uses the first row instead of
    stacking duplicates in front of the seller.
    """
    _require_db()
    if not row or not (row.get("business_name") or "").strip():
        raise OpsError("bad_request",
                       "the lookup returned no business to add")
    campaign_id = ensure_direct_campaign(seller_id)
    key = identity.dedup_key(row)

    try:
        existing = supabase_store.select_rows(
            "prospects", columns="id,status",
            filters={"campaign_id": campaign_id, "dedup_key": key}, limit=1)
    except Exception as e:
        raise OpsError("unavailable", f"prospect lookup failed: {e}")
    if existing and existing[0].get("id"):
        return {"prospect_id": existing[0]["id"], "campaign_id": campaign_id,
                "created": False, "status": existing[0].get("status")}

    payload = {
        "campaign_id": campaign_id,
        "source": source,
        "business_name": row.get("business_name", ""),
        "category": row.get("category", ""),
        "phone": row.get("telephone", ""),
        "website": row.get("business_page", ""),
        "street": row.get("street", ""),
        "locality": row.get("locality", ""),
        "region": row.get("region", ""),
        "zipcode": row.get("zipcode", ""),
        "rating": _num_or_none(row.get("rating")),
        "review_count": _int_or_none(row.get("review_count")),
        "place_id": row.get("place_id", ""),
        "maps_url": row.get("maps_url", ""),
        "listing_url": row.get("listing_url") or None,
        "status": "discovered",
        "dedup_key": key,
        "raw_payload": row,
    }
    try:
        created = supabase_store.insert_rows("prospects", payload)
    except Exception as e:
        raise OpsError("upstream", f"could not add the business: {e}")
    pid = (created or [{}])[0].get("id")
    if not pid:
        raise OpsError("upstream", "could not add the business")
    return {"prospect_id": pid, "campaign_id": campaign_id,
            "created": True, "status": "discovered"}


def _num_or_none(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def _int_or_none(v):
    try:
        return int(float(v)) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None

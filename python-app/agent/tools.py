"""The agent's hands: Strands tools wrapping the existing engine.

Two rules hold this file together.

1. **Every tool returns a COMPACT dict, never `leads[]`.** lead_engine returns a
   full lead list; handing that back would blow the context window on the
   second call. find_leads returns counts + a 5-lead digest.
2. **charge() runs at the TOP of every tool, before any work**, so a refused
   call costs nothing. The one exception is the LLM calls the engine makes
   internally during find_leads — those have already happened by the time we
   see them, so they are RECORDED (budget.record_llm_calls) rather than
   prevented, and the cap acts as a circuit breaker for the next call.

The agent's model is separate from the `providers/` registry used by the engine
(Bedrock/SigV4 vs api-key). These tools never construct the agent's model.
"""

from strands import tool

import config
import lead_engine
import leads_read
import llm as llm_layer
import seller_ops
import supabase_store
from agent import run_store
from agent.budget import BudgetExceeded, TerminalReason
from providers import get_llm

# How many leads the digest shows, and how much of each weakness string.
DIGEST_LIMIT = 5
WEAKNESS_CHARS = 200


class RunContext:
    """Per-run state shared by every tool bound to this instance.

    `campaign_id` is mutable: find_leads may create a campaign, and the reads
    that follow need to target it.

    `campaign_ids` records EVERY campaign this run created, in order. It exists
    because `campaign_id` alone is not enough to find a run's output: a run
    makes several find calls, each creating its own campaign, and `campaign_id`
    only ever names the most recent one. A run was observed drafting three
    emails against its first campaign and then reporting its last, empty one —
    so anyone using the report to look up the run's results found nothing.
    Keep the mutable `campaign_id` (that is the "where do I read next" pointer)
    and treat `campaign_ids` as the durable record of what was touched.
    """

    def __init__(self, budget, run_id, seller_id=None, campaign_id=None,
                 pinned_location_code=None, pinned_location_name=None,
                 scrutiny=None, niche=None, single_search=False):
        self.budget = budget
        self.run_id = run_id
        self.seller_id = seller_id
        self.campaign_id = campaign_id
        self.campaign_ids = [campaign_id] if campaign_id else []
        # Run options chosen on the dashboard: how strictly to judge (strict |
        # balanced | lenient), what the seller pitches (overrides the model's
        # own reading of the goal), and whether the run gets exactly one
        # search before it must draft from what that search returned.
        self.scrutiny = scrutiny
        self.niche = niche
        self.single_search = bool(single_search)
        # The dashboard picker's pinned search area, when the run carries one.
        # find_leads defaults to it, so the model cannot wander off to a
        # neighbouring city or invent a place from whole cloth.
        self.pinned_location_code = pinned_location_code
        self.pinned_location_name = pinned_location_name
        # Compact find_leads answers by (keyword, place, code, niche, count).
        # A repeat search replays the stored answer instead of re-running a
        # minutes long campaign over results the dedup would discard anyway.
        self.find_cache = {}
        # Progress markers the orchestrator reads to decide TARGET_MET / idle
        # turns. Updated by the tools, never by the model.
        self.qualified = 0
        self.drafts = 0
        # Engine warnings collected by find_leads (dev-default niche, missing
        # provider...). Deduplicated there; the orchestrator reports them.
        self.warnings = []
        # Per-run read caches (Phase 2.2): every draft/score call used to
        # re-read ownership + campaign niche + the seller row (4-5 SELECTs per
        # draft, identical answers). All three are immutable within a run —
        # campaigns never change owner or niche_rules, and a seller profile
        # edit mid-run only affects LATER runs' personalization, never sends
        # (sending re-reads the live row). Leads and drafts themselves are
        # NEVER cached: those rows move under the run's own feet.
        self._owns_cache = {}
        self._niche_cache = {}
        self._seller_cache = None

    def progress_marker(self):
        """A cheap fingerprint of 'did anything happen this turn'."""
        return (self.budget.find_calls, self.budget.llm_calls,
                self.qualified, self.drafts)


def _refused(exc):
    """The clean refusal a tool returns when a cap refuses it. The model sees
    this and stops proposing that action — no traceback."""
    return {"ok": False, "error": "budget_exceeded",
            "cap": exc.cap, "detail": exc.detail}


def _digest(leads, limit=DIGEST_LIMIT):
    """Rank by opportunity_score and keep the top few, trimmed for the context
    window. Contains NO lead ids — call read_leads_tool for the persisted rows
    (with ids) that drafting needs."""
    ranked = sorted(leads or [],
                    key=lambda x: -(x.get("opportunity_score") or 0))
    out = []
    for lead in ranked[:limit]:
        weakness = str(lead.get("weakness") or "")
        out.append({
            "business_name": lead.get("business_name") or lead.get("name"),
            "website": lead.get("website"),
            "locality": lead.get("locality"),
            "opportunity_score": lead.get("opportunity_score"),
            "confidence_score": lead.get("confidence_score"),
            "qualified": bool(lead.get("qualified")),
            "weakness": weakness[:WEAKNESS_CHARS] or None,
        })
    return out


def _clock_refusal(ctx):
    """A refusal dict if the run has outlived its wall clock, else None.

    Read-only tools use this instead of charge_*: they cost nothing, so they
    must not raise, but they should still stop working once the run is over.
    """
    try:
        ctx.budget.check_wall_clock()
    except BudgetExceeded as exc:
        return _refused(exc)
    return None


def _load_lead(lead_id):
    """Return (lead, refusal_dict).

    read_lead deliberately raises on a database error rather than returning
    None, so that "the DB is down" and "no such lead" stay distinguishable.
    Tools must not leak that as a traceback, so it is translated here into a
    refusal the model can read — with a DIFFERENT error code than not_found.
    """
    try:
        return leads_read.read_lead(lead_id), None
    except Exception as exc:
        return None, {"ok": False, "error": "read_failed",
                      "message": str(exc)[:300]}


def _owns(ctx, campaign_id):
    """Does this run's seller own the campaign, cached per run.

    Ownership is immutable (campaigns never change seller), so the first
    answer stands for the whole run instead of re-querying per draft, per
    read, per summary. A falsy campaign id is never owned.
    """
    if not campaign_id:
        return False
    if campaign_id not in ctx._owns_cache:
        ctx._owns_cache[campaign_id] = bool(
            leads_read.owns_campaign(ctx.seller_id, campaign_id))
    return ctx._owns_cache[campaign_id]


def _campaign_niche(ctx, campaign_id):
    """The campaign's (niche, source), cached per run.

    niche_rules are written once at campaign creation and never edited, so
    like ownership this is read once per campaign per run.
    """
    if campaign_id not in ctx._niche_cache:
        ctx._niche_cache[campaign_id] = leads_read.read_campaign_niche(
            campaign_id)
    return ctx._niche_cache[campaign_id]


def _draft_seller(ctx):
    """The (seller, settings) pair drafting needs, read once per run.

    See RunContext: safe to cache because nothing the agent writes depends
    on a mid-run profile edit (sends re-read the live row at send time).
    """
    if ctx._seller_cache is None:
        ctx._seller_cache = leads_read.read_seller_for_draft(ctx.seller_id)
    return ctx._seller_cache


def _first_email(lead):
    """The best available address for a lead, or None. `emails` may arrive as a
    real list or as the JSON string PostgREST hands back; decoded() covers both."""
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


def _ops_call(fn, *args, **kwargs):
    """Run a seller_ops operation and translate OpsError into a refusal.

    seller_ops raises so the Flask layer can pick an HTTP status; a tool has no
    status to pick, and a traceback would end the run. So the semantic `kind`
    becomes the error code, which the model can actually act on — `not_found`
    means "stop asking", `upstream` means "retry later".
    """
    try:
        # Call exactly once: these are writes, so a "peek then call" would
        # double-apply the update.
        result = fn(*args, **kwargs)
    except seller_ops.OpsError as exc:
        return {"ok": False, "error": exc.kind, "message": exc.message}
    except Exception as exc:
        return {"ok": False, "error": "failed", "message": str(exc)[:300]}
    if isinstance(result, dict):
        return {"ok": True, **result}
    return {"ok": True, "result": result}


def _scoped_seller(ctx):
    """The run's seller id, or a refusal.

    Fails CLOSED. Every seller-scoped tool reads its seller from the run
    context rather than from its own arguments, so the model cannot name a
    different tenant — there is no seller_id parameter for it to fill in. A run
    with no seller is not "the default tenant"; it is a misconfigured run, and
    letting it through would hand it whatever row it asked for.
    """
    sid = (ctx.seller_id or "").strip() if isinstance(ctx.seller_id, str) else ctx.seller_id
    if not sid:
        return None, {"ok": False, "error": "no_seller",
                      "message": "This run has no seller bound to it, so "
                                 "seller-scoped operations are refused."}
    return sid, None


def build_tools(ctx):
    """Bind a RunContext to the tool set and return the list for Agent(tools=...)."""

    @tool
    def find_leads(keyword: str, place: str, niche: str = "",
                   max_results: int = 20, location_code: int = 0,
                   scrutiny: str = "") -> dict:
        """Search for businesses matching a keyword in a place, then enrich,
        score and store them. Returns COUNTS plus a short digest of the top few
        leads — never the full list.

        When this run carries a pinned search area, its code is already filled
        in: keep it, and vary the keyword when results are thin. Only widen the
        place when the pinned area itself is exhausted, and then only to its
        parent area, never to a different place. Each call costs one find
        credit. Repeating an identical search replays the stored answer for
        free instead of re-running the campaign. In single search mode the
        second search is refused outright: draft from what the first returned.
        """
        # The pin wins over anything the model types: a picked code is exact,
        # a typed place is a guess. Without a pin the search resolves the place
        # from its name exactly as before. Same for the niche: the seller's own
        # words beat the model's reading of the goal.
        code = location_code or ctx.pinned_location_code or None
        try:
            code = int(code) if code not in ("", None) else None
        except (TypeError, ValueError):
            code = None
        if code is not None and code <= 0:
            code = None
        scr = str(scrutiny or ctx.scrutiny or "").strip().lower() or None
        nch = str(ctx.niche or niche or "").strip() or None
        cache_key = (str(keyword or "").strip().lower(),
                     str(place or "").strip().lower(), code,
                     str(nch or "").strip().lower(),
                     int(max_results or 20), scr)
        hit = ctx.find_cache.get(cache_key)
        if hit is not None:
            # No work happened, so no credit is charged and no LLM usage is
            # re-recorded: replaying the stored answer is free by construction.
            # A copy, so the model cannot mutate the cached copy for later.
            return {**hit, "cached": True}
        if ctx.single_search and ctx.budget.find_calls >= 1:
            # One search per run, by the seller's choice. Not a cap to work
            # around and not charged: the way forward is drafting, and the
            # prompt says so where this error is documented.
            return {"ok": False, "error": "single_search",
                    "message": "This run allows one search, which is spent. "
                               "Draft from the businesses it returned."}
        try:
            ctx.budget.charge_find()
        except BudgetExceeded as exc:
            return _refused(exc)

        def _emit(kind, message):
            # Stage events from the engine (searching, found, enriching,
            # scored) land in the live trace as they happen. Informational
            # only: a Supabase blip here must never fail the find itself.
            try:
                run_store.append_event(ctx.run_id, kind, message)
            except Exception:
                pass

        try:
            res = lead_engine.run_campaign(
                keyword=keyword, place=place, niche=nch,
                max_results=int(max_results or 20),
                location_code=code, scrutiny=scr,
                seller_id=ctx.seller_id, progress_cb=_emit)
        except Exception as exc:
            return {"ok": False, "error": "find_failed",
                    "message": str(exc)[:300]}

        if res.get("campaign_id"):
            ctx.campaign_id = res["campaign_id"]
            # Append, don't just assign: this run now owns another campaign.
            # Dedup because run_campaign may return an existing campaign for a
            # repeat keyword/place rather than creating a new one.
            if res["campaign_id"] not in ctx.campaign_ids:
                ctx.campaign_ids.append(res["campaign_id"])
        if res.get("qualified") is not None:
            ctx.qualified = int(res.get("qualified") or 0)
        # The engine scored leads internally; those calls already happened.
        ctx.budget.record_llm_calls(res.get("scored") or 0)

        answer = {
            "ok": bool(res.get("ok")),
            "campaign_id": res.get("campaign_id"),
            "keyword": res.get("keyword"),
            "place": res.get("place"),
            "location_code": code,
            "found": res.get("found"),
            "with_website": res.get("with_website"),
            "qualified": res.get("qualified"),
            "stored": res.get("stored"),
            "duplicates": res.get("duplicates"),
            "digest": _digest(res.get("leads")),
            "digest_truncated_to": DIGEST_LIMIT,
            "errors": (res.get("errors") or [])[:3],
            # Engine warnings (a dev-default niche, a missing provider) used
            # to die here: counted nowhere, shown nowhere. They are the
            # difference between "the run found nothing" and "the run was
            # judging against the wrong niche", so they travel to the report
            # and the live trace below instead.
            "warnings": list(res.get("warnings") or [])[:5],
        }
        for warning in answer["warnings"]:
            if warning and warning not in ctx.warnings:
                ctx.warnings.append(str(warning)[:300])
                if len(ctx.warnings) > 10:
                    ctx.warnings.pop(0)
                try:
                    run_store.append_event(ctx.run_id, "warning", warning)
                except Exception:
                    pass  # the trace is informational; never fail a find for it
        # Only a search that found something is worth replaying. An empty
        # answer may be transient, and caching it would turn one empty run
        # into a permanent one for the rest of this run.
        if res.get("found"):
            ctx.find_cache[cache_key] = answer
        return answer

    @tool
    def read_leads_tool(campaign_id: str = "", min_opportunity: int = 0,
                        limit: int = 25) -> dict:
        """List stored leads for a campaign, best first. THIS is where lead ids
        come from — use them for drafting. Returns a compact list, not full rows.
        Defaults to the campaign from the most recent find."""
        refused = _clock_refusal(ctx)
        if refused:
            return refused
        cid = (campaign_id or "").strip() or ctx.campaign_id
        if not cid:
            return {"ok": False, "error": "no_campaign",
                    "message": "Run find_leads first, or pass a campaign_id."}
        if not _owns(ctx, cid):
            return {"ok": False, "error": "not_found"}
        try:
            rows = supabase_store.select_rows(
                "leads", columns=leads_read.LEAD_COLUMNS,
                filters={"campaign_id": cid}, limit=int(limit or 25))
        except Exception as exc:
            return {"ok": False, "error": "read_failed",
                    "message": str(exc)[:300]}
        rows = leads_read.flatten_leads(rows)
        floor = int(min_opportunity or 0)
        rows = [r for r in rows if (r.get("opportunity_score") or 0) >= floor]
        rows.sort(key=lambda r: -(r.get("opportunity_score") or 0))
        return {
            "ok": True,
            "campaign_id": cid,
            "count": len(rows),
            "leads": [{
                "lead_id": r.get("id"),
                "business_name": r.get("business_name"),
                "website": r.get("website"),
                "opportunity_score": r.get("opportunity_score"),
                "qualified": bool(r.get("qualified")),
                "status": r.get("status"),
                "email": _first_email(r),
            } for r in rows[:int(limit or 25)]],
        }

    @tool
    def campaign_summary(campaign_id: str = "") -> dict:
        """Counts for a campaign: how many leads, how many qualified, by status.
        Cheap — use it to decide whether to keep searching."""
        refused = _clock_refusal(ctx)
        if refused:
            return refused
        cid = (campaign_id or "").strip() or ctx.campaign_id
        if not cid:
            return {"ok": False, "error": "no_campaign"}
        if not _owns(ctx, cid):
            return {"ok": False, "error": "not_found"}
        try:
            rows = supabase_store.rpc("campaign_summary", {
                "p_campaign_id": cid, "p_seller_id": ctx.seller_id})
        except Exception as exc:
            return {"ok": False, "error": "summary_failed",
                    "message": str(exc)[:300]}
        return {"ok": True, "campaign_id": cid, "summary": rows}

    @tool
    def score_niche_fit(lead_id: str, niche_hints: str = "") -> dict:
        """Re-score ONE lead's fit for the seller's niche with the LLM, and
        return opportunity/confidence plus the stated reasons. Costs one LLM
        call. Use it when you doubt a score, not for every lead."""
        try:
            ctx.budget.charge_llm()
        except BudgetExceeded as exc:
            return _refused(exc)
        lead, refusal = _load_lead(lead_id)
        if refusal:
            return refusal
        if not lead:
            return {"ok": False, "error": "not_found"}
        if not _owns(ctx, lead.get("campaign_id")):
            return {"ok": False, "error": "not_found"}
        niche, _ = _campaign_niche(ctx, lead.get("campaign_id"))
        seller, settings = _draft_seller(ctx)
        try:
            provider = get_llm(config.llm_cfg(settings))
            scored = llm_layer.score_lead(
                provider,
                business=leads_read.business_from_lead(lead),
                intelligence=leads_read.decoded(lead.get("intelligence")),
                niche=niche or "", extra_hints=niche_hints or "")
        except Exception as exc:
            return {"ok": False, "error": "score_failed",
                    "message": str(exc)[:300]}
        return {
            "ok": True, "lead_id": lead_id,
            "opportunity_score": scored.get("opportunity_score"),
            "confidence_score": scored.get("confidence_score"),
            "score": scored.get("score"),
            "reasons": (scored.get("reasons") or [])[:5],
            # Evidence and gaps are returned side by side rather than folded
            # into one list, because the agent reads this to decide what to
            # pitch and the two answer different questions: `reasons` is why
            # the score landed there (praise, for a strong lead), `gaps` is
            # what the business lacks. Losing that distinction is what put
            # "strong digital presence" into a column named `weakness`.
            "gaps": (scored.get("gaps") or [])[:5],
            "first_line": scored.get("first_line"),
            "niche": niche,
            "seller_name": (seller.get("name") or seller.get("brand")),
        }

    @tool
    def draft_outreach(lead_id: str, extra_hints: str = "") -> dict:
        """Write a personalized outreach email for ONE lead and SAVE it as a
        draft awaiting human approval. Costs one LLM call.

        This does NOT send anything. It returns the draft id and its subject —
        approve it separately. Read the draft's own subject/angle and judge
        whether it is specific enough; if it is generic, call revise_draft.
        """
        try:
            ctx.budget.charge_llm()
        except BudgetExceeded as exc:
            return _refused(exc)
        lead, refusal = _load_lead(lead_id)
        if refusal:
            return refusal
        if not lead:
            return {"ok": False, "error": "not_found"}
        campaign_id = lead.get("campaign_id")
        if not _owns(ctx, campaign_id):
            return {"ok": False, "error": "not_found"}

        niche, _ = _campaign_niche(ctx, campaign_id)
        seller, settings = _draft_seller(ctx)
        weakness = str(lead.get("weakness") or "").strip()
        first_line = str(lead.get("first_line") or "").strip()
        business = leads_read.business_from_lead(lead)
        if not weakness and (business.get("category") or niche):
            weakness = (f"Category: {business.get('category') or '?'}. "
                        f"We're selling: {niche or '?'}.")

        try:
            provider = get_llm(config.llm_cfg(settings))
            out = llm_layer.draft_email(
                provider,
                business=business,
                intelligence=leads_read.decoded(lead.get("intelligence")),
                niche=niche or "", weakness=weakness, first_line=first_line,
                seller=seller, extra_hints=extra_hints or "")
        except Exception as exc:
            return {"ok": False, "error": "draft_failed",
                    "message": str(exc)[:300]}

        try:
            draft = run_store.create_draft(
                lead_id=lead_id, campaign_id=campaign_id,
                seller_id=ctx.seller_id, run_id=ctx.run_id,
                subject=out.get("subject"), email_body=out.get("email_body"),
                angle=out.get("angle"), to_email=_first_email(lead))
        except Exception as exc:
            # Most likely idx_drafts_lead_live: a live draft already exists.
            return {"ok": False, "error": "draft_not_saved",
                    "message": str(exc)[:300]}
        ctx.drafts += 1
        return {
            "ok": True,
            "draft_id": draft.get("id"),
            "lead_id": lead_id,
            "business_name": business.get("business_name"),
            "subject": out.get("subject"),
            "angle": out.get("angle"),
            "to_email": _first_email(lead),
            "status": draft.get("status"),
            "note": "Saved as a draft. Nothing has been sent.",
        }

    @tool
    def revise_draft(draft_id: str, feedback: str) -> dict:
        """Rewrite an existing draft using specific feedback (e.g. 'mention the
        missing booking page'). Costs one LLM call. Returns the new subject and
        angle; the revision counter increments so the edit is traceable."""
        try:
            ctx.budget.charge_llm()
        except BudgetExceeded as exc:
            return _refused(exc)
        draft = run_store.get_draft(draft_id)
        if not draft:
            return {"ok": False, "error": "not_found"}
        if ctx.seller_id and draft.get("seller_id") \
                and str(draft["seller_id"]) != str(ctx.seller_id):
            return {"ok": False, "error": "not_found"}
        lead, refusal = _load_lead(draft.get("lead_id"))
        if refusal:
            return refusal
        if not lead:
            return {"ok": False, "error": "not_found"}
        niche, _ = _campaign_niche(ctx, lead.get("campaign_id"))
        seller, settings = _draft_seller(ctx)
        try:
            provider = get_llm(config.llm_cfg(settings))
            out = llm_layer.draft_email(
                provider,
                business=leads_read.business_from_lead(lead),
                intelligence=leads_read.decoded(lead.get("intelligence")),
                niche=niche or "",
                weakness=str(lead.get("weakness") or "").strip(),
                first_line=str(lead.get("first_line") or "").strip(),
                seller=seller,
                extra_hints=(f"Revise the previous draft. Feedback: {feedback}"))
        except Exception as exc:
            return {"ok": False, "error": "revise_failed",
                    "message": str(exc)[:300]}
        updated = run_store.update_draft(
            draft_id, subject=out.get("subject"),
            email_body=out.get("email_body"), angle=out.get("angle"),
            bump_revision=True)
        return {
            "ok": True, "draft_id": draft_id,
            "subject": out.get("subject"), "angle": out.get("angle"),
            "revision": updated.get("revision"),
        }

    # --- seller profile ---------------------------------------------------- #

    @tool
    def seller_profile() -> dict:
        """The seller this run works for: name, brand, niche, title, phone,
        portfolio_url. Read it before drafting when you need positioning or a
        signature. No arguments — a run only ever sees its own seller."""
        sid, refused = _scoped_seller(ctx)
        if refused:
            return refused
        try:
            row = seller_ops.get_seller(sid)
        except seller_ops.OpsError as exc:
            return {"ok": False, "error": exc.kind, "message": exc.message}
        except Exception as exc:
            return {"ok": False, "error": "failed", "message": str(exc)[:300]}

        # resume_text/portfolio_text run to tens of thousands of characters and
        # would crowd out everything else in the context window. Report whether
        # each is present and how big, never the body.
        profile = {k: v for k, v in row.items()
                   if k not in ("resume_text", "portfolio_text")}
        profile["has_resume"] = bool(row.get("resume_text"))
        profile["resume_chars"] = len(row.get("resume_text") or "")
        profile["has_portfolio_text"] = bool(row.get("portfolio_text"))
        profile["portfolio_chars"] = len(row.get("portfolio_text") or "")
        return {"ok": True, "seller": profile}

    @tool
    def update_seller_profile(name: str = "", title: str = "", brand: str = "",
                              niche: str = "", phone: str = "",
                              portfolio_url: str = "") -> dict:
        """Update this seller's profile fields. Send ONLY the fields you want to
        change; blanks are ignored, never used to clear a value. Costs nothing.

        Use it when the operator tells you something durable about themselves
        (a new brand name, a corrected niche) — not to record run findings."""
        sid, refused = _scoped_seller(ctx)
        if refused:
            return refused
        fields = {k: v for k, v in
                  (("name", name), ("title", title), ("brand", brand),
                   ("niche", niche), ("phone", phone),
                   ("portfolio_url", portfolio_url)) if str(v or "").strip()}
        if not fields:
            return {"ok": False, "error": "bad_request",
                    "message": "No fields supplied — nothing to update."}
        try:
            row = seller_ops.update_seller(sid, fields)
        except seller_ops.OpsError as exc:
            return {"ok": False, "error": exc.kind, "message": exc.message}
        except Exception as exc:
            return {"ok": False, "error": "failed", "message": str(exc)[:300]}
        # Echo back only what changed. The row also carries resume_text and
        # portfolio_text, which must not land in the context window.
        return {"ok": True, "updated": sorted(fields), "seller_id": row.get("id")}

    @tool
    def set_resume_text(resume_text: str) -> dict:
        """Replace the seller's stored resume with this text. Pass the resume
        CONTENT, not a filename or a URL — use fetch_portfolio for a URL.

        The text is used verbatim when drafting, so keep it a resume: if you are
        condensing, say so in your reply to the operator."""
        sid, refused = _scoped_seller(ctx)
        if refused:
            return refused
        try:
            out = seller_ops.set_resume_text(sid, resume_text)
        except seller_ops.OpsError as exc:
            return {"ok": False, "error": exc.kind, "message": exc.message}
        except Exception as exc:
            return {"ok": False, "error": "failed", "message": str(exc)[:300]}
        # Not _ops_call: it would flatten `seller` (the whole updated row,
        # including the resume you just sent) into the context window.
        return {"ok": True, "chars": out.get("chars"),
                "seller_id": (out.get("seller") or {}).get("id")}

    @tool
    def fetch_portfolio(url: str) -> dict:
        """Fetch a portfolio/homepage URL and store its visible text as the
        seller's portfolio, so later drafts can reference real work. Returns
        page metadata and a character count, NOT the page text — it is far too
        long for this context. Takes a minute or two on JS-heavy sites.

        Costs no budget credit, but it does hit the network, so do not repeat it
        for a URL already stored (seller_profile tells you portfolio_url)."""
        sid, refused = _scoped_seller(ctx)
        if refused:
            return refused
        return _ops_call(seller_ops.fetch_portfolio, sid, url)

    @tool
    def provider_config() -> dict:
        """Which LLM/finder providers this seller has configured, and WHETHER a
        credential is set — never the credential itself. Use it to explain a
        failure ("no LLM key is set") without guessing.

        There is no tool to change these. API keys are set by a person through
        the dashboard, deliberately: this agent reads untrusted web pages, and a
        tool that rewrites credentials would let a page rewrite them."""
        sid, refused = _scoped_seller(ctx)
        if refused:
            return refused
        return _ops_call(seller_ops.get_seller_config, sid)

    @tool
    def set_lead_status(lead_ids: list, status: str) -> dict:
        """Set the lifecycle status of one or more leads you already found. One
        of: new, contacted, replied, won, lost.

        This records what the OPERATOR tells you happened ("mark those three as
        contacted") — it does not send anything and it is not how you record
        your own progress. The whole batch is refused if any lead is not this
        seller's, so a partial success is not possible."""
        sid, refused = _scoped_seller(ctx)
        if refused:
            return refused
        if isinstance(lead_ids, str):
            lead_ids = [lead_ids]
        return _ops_call(seller_ops.set_lead_status, sid, lead_ids, status)

    return [find_leads, read_leads_tool, campaign_summary,
            score_niche_fit, draft_outreach, revise_draft,
            seller_profile, update_seller_profile, set_resume_text,
            fetch_portfolio, provider_config, set_lead_status]

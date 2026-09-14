#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Shared LLM TASK layer (transport-agnostic).

score_lead() and draft_email() live here. They are pure functions of a PROVIDER
(an instance of providers.LLMProvider) plus data: they build the prompts and
parse the JSON reply, and the ONLY provider interaction is provider.chat(...).
That keeps the task logic identical across Groq/OpenAI/Anthropic/etc. — swapping
a provider is selecting a different adapter in config, never editing this file.

Formerly groq_ai.py; that module is now a thin shim that builds a default Groq
(OpenAI-compatible) provider from env and delegates here, so existing CLI usage
and imports keep working.
"""

import json

# --------------------------------------------------------------------------- #
# SCORE prompts
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You are a lead-qualification analyst for a B2B local-business outreach "
    "service. You receive (a) a business listing, (b) scraped website "
    "intelligence, and (c) a DETERMINISTIC EVIDENCE breakdown computed by code "
    "(digital-presence class, evidence sub-scores, confidence_cap). Your job is "
    "to interpret that evidence against the niche service being sold -- you do "
    "NOT invent the evidence numbers. Decide two separate 0-100 scores:\n"
    "  - opportunity_score: how valuable a prospect this business is for the "
    "niche (fits, plausibly LACKS what's sold, worth pitching). This may be high "
    "even when the data is thin.\n"
    "  - confidence_score: how sure we can actually be of the facts about this "
    "business. Mirror the evidence: never exceed the provided confidence_cap, "
    "and keep it LOW when the crawl is thin or the listing unverified.\n"
    "Corroboration across independent sources (source_agreement.count > 0, e.g. "
    "website + several resolved social/sameAs profiles) and a recent "
    "last-active date (activity_recency.score high) are legitimate reasons to "
    "raise confidence_score -- but never above the provided confidence_cap, and "
    "never when the underlying crawl is thin.\n"
    "Return STRICT JSON only, no prose, with exactly these keys: "
    "opportunity_score (int 0-100), confidence_score (int 0-100), reasons "
    "(array of short strings citing concrete evidence and sub-scores), gaps "
    "(array of short strings naming what this business plausibly LACKS that "
    "the niche service would supply), "
    "first_line (a short personalized outreach opening that references a "
    "concrete detail from the data).\n"
    "reasons and gaps are DIFFERENT and must not overlap. reasons is the "
    "evidence behind your two scores, and for a strong lead that evidence "
    "reads as praise -- \"strong digital presence (presence=100)\" is a "
    "reason. gaps is what the outreach email will be built around, so every "
    "entry must be a defect or an absence the service addresses. The same "
    "fact can be one or the other depending on which way it points: \"3 "
    "social profiles and 6 phone numbers found, but no email address "
    "published\" is a gap. If the evidence supports no honest gap, return an "
    "empty array -- an invented gap writes an email that insults a business "
    "for a problem it does not have.\n"
    "CHECK THE LISTING FIELDS IN BUSINESS FIRST, before inferring a gap from "
    "the scrape. They are Google's own record of the business, so a gap drawn "
    "from one is a fact the recipient can verify in seconds, while a gap "
    "inferred from a crawl is a guess that may be wrong (the site may be "
    "JS-rendered, or the feature may sit behind a login). In order of "
    "strength: is_claimed=false means the owner has not verified the listing, "
    "so its hours, photos and phone are Google's guesses that the owner cannot "
    "edit -- that is a gap. An absent or null is_claimed means you DO NOT "
    "KNOW, so never write that the listing is unclaimed. book_online_url "
    "absent, work_hours/hours empty, a missing website, a very low "
    "review_count, and a high rating with almost no reviews are all gaps of "
    "the same kind. Prefer these to anything you would have to infer."
)


def build_prompt(business, intelligence, niche, extra_hints="", breakdown=None):
    biz = json.dumps(business or {}, indent=2, ensure_ascii=False)
    info = json.dumps(intelligence or {}, indent=2, ensure_ascii=False)[:6000]
    evidence = json.dumps(breakdown or {}, indent=2, ensure_ascii=False)[:4000]
    return (
        "NICHE SERVICE WE SELL: "
        f"{niche}\n\n"
        f"BUSINESS (from the finder):\n{biz}\n\n"
        f"WEBSITE INTELLIGENCE (scraped):\n{info}\n\n"
        f"DETERMINISTIC EVIDENCE (computed by code -- treat as ground truth for "
        f"confidence):\n{evidence}\n\n"
        f"ADDITIONAL CONTEXT:\n{extra_hints}\n\n"
        "Score how good a prospect this business is and draft a first line. "
        "Output JSON only."
    )


# --------------------------------------------------------------------------- #
# DRAFT prompts
# --------------------------------------------------------------------------- #
_DRAFT_SYSTEM = (
    "You ghostwrite a short, personal, non-generic B2B cold email from ONE "
    "human seller to ONE local business. You are NOT a mass-mailer: the message "
    "must read like a specific person who actually looked at the business and "
    "speaks to the business's OWN situation -- never 'as one of our clients...' "
    "or 'we noticed your website'. Keep it under ~180 words, plain and human.\n"
    "Inputs you receive:\n"
    "  - WHAT WE SELL (one line): the concrete service/product and who it is for.\n"
    "  - THE BUSINESS (prospect): name, category, location, website, what they "
    "seem to be / do.\n"
    "  - WHY THEY MAY NEED THIS (their likely gap / fit signals, e.g. they may "
    "lack online booking). Use this to open with THEIR situation, not ours.\n"
    "  - WHO THE SELLER IS: name/title/company, plus (optional) resume "
    "credentials and (optional) portfolio work they can cite. Only cite the "
    "resume/portfolio when it gives you a concrete, credible, RELEVANT detail "
    "-- otherwise lean on the seller's title and stay specific to the business. "
    "Never invent credentials that are not in the resume/portfolio, and never "
    "fabricate facts about the business beyond the inputs.\n"
    "Return STRICT JSON only, no prose, exactly these keys:\n"
    "  - subject: a specific, curiosity/benefit-driven subject line referencing "
    "the business's context (not generic). Max ~9 words.\n"
    "  - email_body: the full ready-to-send email, greeting + a short personal "
    "open about the business + one concrete value statement tied to the "
    "business's gap + a low-friction single question/ask + a one-line "
    "signature with the seller's real name/title/company. Do not add placeholders "
    "like [Your Name].\n"
    "  - angle: one short sentence explaining the single hook/angle you chose "
    "and why it fits THIS business (the reasoning a human would give)."
)


def _seller_line(seller):
    """A one-line human identity for the seller, without exposing resume dumps."""
    if not seller:
        return "Unknown sender."
    parts = [p for p in ((seller.get("name") or "").strip(),
                         (seller.get("title") or "").strip(),
                         (seller.get("brand") or "").strip()) if p]
    return " ".join(parts) or "Unknown sender."


def build_draft_prompt(business, intelligence, niche, weakness="", first_line="",
                       seller=None, extra_hints=""):
    biz = json.dumps(business or {}, indent=2, ensure_ascii=False)[:4000]
    info = json.dumps(intelligence or {}, indent=2, ensure_ascii=False)[:5000]
    resume = (seller or {}).get("resume_text", "") or ""
    portfolio = (seller or {}).get("portfolio_text", "") or ""
    if resume:
        resume = ("RESUME (sender's own, cite concrete details if relevant):\n"
                  + resume.strip()[:2000])
    else:
        resume = "(sender provided no resume)"
    if portfolio:
        portfolio = ("PORTFOLIO / PAST WORK (cite concrete work if relevant):\n"
                     + portfolio.strip()[:1600])
    else:
        portfolio = "(sender provided no portfolio)"
    gap = (weakness or "").strip()
    if first_line:
        gap = (gap + "\nEarlier opener: " + first_line) if gap else \
            "Earlier opener: " + first_line
    # The caution is not decoration and not only for prompt quality. Rows scored
    # before `gaps` existed have the old reason list sitting in `weakness`, and
    # scoring evidence reads as praise -- "strong digital presence (presence=
    # 100)" is a compliment telling the model the opposite of what this heading
    # claims. A drafter handed that and asked for an email does not report the
    # contradiction; it resolves it by inventing a defect, which is how a lead
    # scored STRONG got an email saying its site needs fixing. Rows are only
    # re-scored on a new run, so the legacy shape stays reachable and the prompt
    # has to survive it.
    if not gap:
        gap = ("(no specific gap flagged — infer a plausible but honest one from "
               "the business intelligence, or keep the email to a helpful offer)")
    else:
        gap += ("\n\n(If any line above is a compliment rather than a gap -- it "
                "describes something the business already does well -- then no "
                "gap was flagged: infer a plausible but honest one, or keep the "
                "email to a helpful offer. Do NOT treat a strength as a defect.)")
    return (
        f"WHAT WE SELL: {niche}\n\n"
        f"THE BUSINESS (prospect):\n{biz}\n\n"
        f"WEBSITE INTELLIGENCE (scraped):\n{info}\n\n"
        f"WHY THEY MAY NEED THIS (what they plausibly LACK — build the email "
        f"around one of these, and never around a strength):\n{gap}\n\n"
        f"WHO THE SELLER IS:\n{_seller_line(seller)}\n\n"
        f"{resume}\n\n{portfolio}\n\n"
        f"ADDITIONAL CONTEXT:\n{extra_hints}\n\n"
        "Write the email and subject now. Output JSON only."
    )


# --------------------------------------------------------------------------- #
# Tolerant JSON reply parsing (shared by score + draft)
# --------------------------------------------------------------------------- #
def _first_json(text):
    """Extract the first balanced top-level JSON object from `text`, tolerating
    stray prose/whitespace around it. Returns the parsed dict or raises."""
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object in model reply: {text[:200]!r}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except (json.JSONDecodeError, ValueError) as e:
                    raise ValueError(
                        f"Could not parse JSON from model reply: "
                        f"{text[start:i + 1][:300]!r}") from e
    raise ValueError(f"Unbalanced JSON in model reply: {text[:300]!r}")


def _str_list(parsed, key):
    """A model's list-of-short-strings field, coerced into one.

    Shared by `reasons` and `gaps` because they have identical shape tolerance
    requirements: the key may be absent (older prompt, different provider), a
    bare string (a model that ignored "array"), or not a list at all.

    `None` is dropped rather than stringified. `str(None)` is `"None"`, which
    is truthy, so a model that emits `["a real reason", null]` -- a common way
    to leave a slot empty -- used to yield the literal string "None" as an
    entry. That string was then shown to the drafter, and for `gaps` it would
    be shown under "WHY THEY MAY NEED THIS".
    """
    value = (parsed or {}).get(key)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [s for s in (str(x).strip() for x in value
                        if x is not None and not isinstance(x, bool)) if s]


def _parse_score(content):
    def _clamp_int(parsed, key, default=0):
        try:
            v = int(parsed.get(key, default) or default)
        except (TypeError, ValueError):
            v = default
        return max(0, min(100, v))

    parsed = _first_json(content)
    reasons = _str_list(parsed, "reasons")
    # `gaps` is a separate key for the same reason `reasons` exists: they answer
    # different questions, and collapsing them into one field is what put
    # "strong digital presence" in a column named `weakness`, under a prompt
    # heading asking for a defect. A model that predates this key (or ignores
    # it) returns no gaps, and an empty list is the honest reading of that --
    # the drafter has a fallback for "no gap flagged", and it does not involve
    # inventing one.
    gaps = _str_list(parsed, "gaps")
    opportunity = _clamp_int(parsed, "opportunity_score",
                             _clamp_int(parsed, "score", 0))
    confidence = _clamp_int(parsed, "confidence_score")
    return {
        "opportunity_score": opportunity,
        "confidence_score": confidence,
        "score": opportunity,               # backward-compat alias
        "reasons": reasons,
        "gaps": gaps,
        "first_line": str(parsed.get("first_line", "") or "").strip(),
    }


def _parse_draft(content):
    parsed = _first_json(content)
    return {
        "subject": str(parsed.get("subject", "") or "").strip(),
        "email_body": str(parsed.get("email_body", "") or "").strip(),
        "angle": str(parsed.get("angle", "") or "").strip(),
    }


# --------------------------------------------------------------------------- #
# Task entry points (take a provider instance)
# --------------------------------------------------------------------------- #
def score_lead(provider, business, intelligence, niche, extra_hints="",
               temperature=0.2, breakdown=None):
    """Judge how good a prospect `business` is for `niche`, using a PROVIDER
    (providers.LLMProvider). Returns opportunity/confidence/reasons/first_line.
    breakdown (from scoring.compute) is the deterministic evidence the model
    interprets; callers should clamp confidence to its confidence_cap."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",
         "content": build_prompt(business, intelligence, niche,
                                 extra_hints, breakdown)},
    ]
    content = provider.chat(messages, temperature=temperature, json_mode=True)
    return _parse_score(content)


def draft_email(provider, business=None, intelligence=None, niche=None,
                weakness="", first_line="", seller=None, extra_hints="",
                temperature=0.7):
    """Generate a personalized outreach email + subject + angle for ONE lead on
    behalf of ONE seller, using a PROVIDER. See the plan/doc for the arg shapes."""
    messages = [
        {"role": "system", "content": _DRAFT_SYSTEM},
        {"role": "user",
         "content": build_draft_prompt(business, intelligence, niche,
                                       weakness, first_line,
                                       seller=seller, extra_hints=extra_hints)},
    ]
    content = provider.chat(messages, temperature=temperature, json_mode=True)
    return _parse_draft(content)

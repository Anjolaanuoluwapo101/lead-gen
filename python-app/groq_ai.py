#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Server-side LLM scoring pass (the optional "LLM" stage in the lead pipeline).

Calls Groq's OpenAI-compatible endpoint to decide whether a scraped/enriched
business "fits" a niche weakness you sell a fix for, and to draft a
personalized outreach first line. This is a normal HTTPS call from Python — no
browser, no third-party account popup — so it runs inside n8n/Flask like the
other engines.

Credentials: GROQ_API_KEY in the environment or a .env file next to this script.

Usage
-----
    python groq_ai.py --json \
      --business '{"business_name":"Swish Dental","category":"Dentist","website":"https://www.swishsmiles.com","telephone":"+15126476045"}' \
      --intelligence '{"jsonld":[...],"nav_links":[...],"social_links":{...}}' \
      --niche "a service provider that adds online booking to dental clinics"

Output: JSON with qualified/score/reasons/first_line.
"""

import argparse
import json
import os
import re
import sys

import requests

# --------------------------------------------------------------------------- #
# .env loader (no extra dependency) — same convention as dataforseo.py
# --------------------------------------------------------------------------- #
def _load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

HERE = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(HERE, ".env"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

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
    "(array of short strings citing concrete evidence and sub-scores), "
    "first_line (a short personalized outreach opening that references a "
    "concrete detail from the data)."
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


def score_lead(business, intelligence, niche, extra_hints="", model=None,
               temperature=0.2, breakdown=None):
    """Return a dict with opportunity_score/confidence_score/reasons/first_line.

    breakdown (from scoring.compute) is the deterministic evidence the model
    interprets. The returned confidence is the model's proposal; callers should
    clamp it to breakdown['confidence_cap'] before persisting.
    """
    if not API_KEY:
        raise RuntimeError(
            "Missing GROQ_API_KEY. Add it to .env (see .env.example).")

    payload = {
        "model": model or MODEL,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user",
             "content": build_prompt(business, intelligence, niche,
                                     extra_hints, breakdown)},
        ],
    }
    resp = requests.post(
        f"{BASE_URL}/chat/completions", headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }, json=payload, timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _parse_score(content)


def _parse_score(content):
    """
    Parse the model's JSON answer tolerantly. The free tier sometimes wraps the
    object in prose or emits stray tokens, so fall back to slicing out the first
    balanced JSON object before giving up. Also normalize `reasons` to a clean
    list of strings and `score` to an int so the DB never sees model noise.
    """
    def try_load(text):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

    parsed = try_load(content)
    if parsed is None:
        # find the first '{' ... matching '}' on the top level
        start = content.find("{")
        if start != -1:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(content)):
                c = content[i]
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
                        parsed = try_load(content[start:i + 1])
                        break
        if parsed is None:
            raise ValueError(f"Could not parse JSON from model reply: "
                             f"{content[:300]!r}")

    # Normalize the fields we persist / display.
    reasons = parsed.get("reasons")
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        reasons = []
    reasons = [r for r in (str(x).strip() for x in reasons) if r]

    def _clamp_int(key, default=0):
        try:
            v = int(parsed.get(key, default) or default)
        except (TypeError, ValueError):
            v = default
        return max(0, min(100, v))

    opportunity = _clamp_int("opportunity_score",
                             _clamp_int("score", 0))
    confidence = _clamp_int("confidence_score")

    return {
        "opportunity_score": opportunity,
        "confidence_score": confidence,
        "score": opportunity,               # backward-compat alias
        "reasons": reasons,
        "first_line": str(parsed.get("first_line", "") or "").strip(),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Groq LLM lead-qualification scoring.")
    ap.add_argument("--business", required=True,
                    help="JSON: business fields from the finder")
    ap.add_argument("--intelligence", default="{}",
                    help="JSON: website intelligence from /email")
    ap.add_argument("--niche", required=True,
                    help="the niche service you sell, e.g. 'adds online "
                         "booking to dental clinics'")
    ap.add_argument("--extra-hints", default="")
    ap.add_argument("--breakdown", default=None,
                    help="JSON: deterministic evidence from scoring.compute()")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        business = json.loads(args.business)
        intelligence = json.loads(args.intelligence or "{}")
        breakdown = json.loads(args.breakdown) if args.breakdown else None
    except json.JSONDecodeError as e:
        print(f"ERROR: could not parse JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        result = score_lead(business, intelligence, args.niche,
                            args.extra_hints, model=args.model,
                            breakdown=breakdown)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json or True:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

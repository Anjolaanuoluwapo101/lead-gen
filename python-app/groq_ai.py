#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Backward-compatible shim for the LLM layer.

The task logic (prompt building + parsing + score_lead/draft_email) now lives in
llm.py and takes a PROVIDER. This module keeps the OLD call signature for CLI
usage and any code not yet migrated: it builds a default Groq (OpenAI-compatible)
provider from the GROQ_* env vars and delegates to llm.

New code should prefer providers.get_llm(cfg) + llm.score_lead(provider, ...) so
the provider is resolvable per-seller.
"""

import json
import os
import sys

import llm
from providers import get_llm


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
# Fallback for the deployed runtime: .env lives OUTSIDE this directory
# because AgentCore's CodeZip packager copies codeLocation wholesale and
# does NOT exclude .env (only .git/.venv/__pycache__/node_modules are
# skipped). Secrets must never be inside the packaged directory.
_load_dotenv(os.path.join(HERE, os.pardir, ".env"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _default_provider(model=None):
    if not os.environ.get("GROQ_API_KEY", "").strip():
        raise RuntimeError(
            "Missing GROQ_API_KEY. Add it to .env (see .env.example).")
    return get_llm({
        "provider": "groq",
        "api_key": os.environ.get("GROQ_API_KEY", "").strip(),
        "base_url": os.environ.get("GROQ_BASE_URL") or None,
        "model": model or os.environ.get("GROQ_MODEL") or None,
    })


def score_lead(business, intelligence, niche, extra_hints="", model=None,
               temperature=0.2, breakdown=None):
    return llm.score_lead(_default_provider(model), business, intelligence,
                          niche, extra_hints=extra_hints,
                          temperature=temperature, breakdown=breakdown)


def draft_email(business=None, intelligence=None, niche=None, weakness="",
                first_line="", seller=None, extra_hints="", model=None,
                temperature=0.7):
    return llm.draft_email(_default_provider(model), business=business,
                           intelligence=intelligence, niche=niche,
                           weakness=weakness, first_line=first_line,
                           seller=seller, extra_hints=extra_hints,
                           temperature=temperature)


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Groq LLM lead-qualification scoring (back-compat shim).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_score = sub.add_parser("score")
    p_score.add_argument("--business", required=True)
    p_score.add_argument("--intelligence", default="{}")
    p_score.add_argument("--niche", required=True)
    p_score.add_argument("--extra-hints", default="")
    p_score.add_argument("--breakdown", default=None)
    p_score.add_argument("--model", default=None)
    p_draft = sub.add_parser("draft")
    p_draft.add_argument("--business", default="{}")
    p_draft.add_argument("--intelligence", default="{}")
    p_draft.add_argument("--niche", required=True)
    p_draft.add_argument("--weakness", default="")
    p_draft.add_argument("--first-line", default="")
    p_draft.add_argument("--extra-hints", default="")
    p_draft.add_argument("--model", default=None)
    args = ap.parse_args()

    try:
        if args.cmd == "score":
            result = score_lead(
                json.loads(args.business),
                json.loads(args.intelligence or "{}"),
                args.niche, args.extra_hints, model=args.model,
                breakdown=(json.loads(args.breakdown) if args.breakdown else None))
        else:
            result = draft_email(
                business=json.loads(args.business or "{}"),
                intelligence=json.loads(args.intelligence or "{}"),
                niche=args.niche, weakness=args.weakness,
                first_line=args.first_line, extra_hints=args.extra_hints,
                model=args.model)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

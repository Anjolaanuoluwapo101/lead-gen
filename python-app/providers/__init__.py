#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Provider registry: turns a resolved provider config dict into a live adapter.

Config comes from config.py (per-seller settings with env-master fallback). The
registry stays DUMB about where creds came from — it just builds the adapter.
Adding a new provider = one entry in the maps below + (if a genuinely different
transport) one adapter class under providers/.
"""

from .base import LLMProvider, BusinessSource   # noqa: F401 (re-export)
from .llm_openai import OpenAICompatible
from .llm_anthropic import Anthropic
from .source_dataforseo import DataForSEO

# --- LLM providers --------------------------------------------------------- #
# Which transport each name uses, plus sane defaults when the seller's config
# omits base_url/model. 'transport' selects the adapter class.
_LLM_META = {
    "groq":       {"transport": "openai", "base_url": "https://api.groq.com/openai/v1",
                   "model": "openai/gpt-oss-120b"},
    "openai":     {"transport": "openai", "base_url": "https://api.openai.com/v1",
                   "model": "gpt-4o-mini"},
    "openrouter": {"transport": "openai",
                   "base_url": "https://openrouter.ai/api/v1",
                   "model": "openai/gpt-4o-mini"},
    "anthropic":  {"transport": "anthropic", "base_url": "https://api.anthropic.com",
                   "model": "claude-3-5-haiku-latest"},
}

_LLM_DEFAULT = "groq"


def llm_provider_names():
    return set(_LLM_META)


def get_llm(cfg):
    """Build an LLMProvider from a resolved config dict:
    {provider?, api_key, base_url?, model?}. `provider` picks transport +
    defaults; explicit base_url/model in cfg win."""
    provider = (cfg or {}).get("provider") or _LLM_DEFAULT
    meta = _LLM_META.get(provider, _LLM_META[_LLM_DEFAULT])
    api_key = (cfg or {}).get("api_key")
    base_url = (cfg or {}).get("base_url") or meta["base_url"]
    model = (cfg or {}).get("model") or meta["model"]
    common = {"api_key": api_key, "base_url": base_url, "model": model}
    if meta["transport"] == "anthropic":
        return Anthropic(**common)
    return OpenAICompatible(**common)


# --- Business sources ------------------------------------------------------ #
_FINDER_DEFAULT = "dataforseo"


def source_provider_names():
    return {"dataforseo"}


def get_source(cfg):
    """Build a BusinessSource from {provider?, login?, password?}."""
    provider = (cfg or {}).get("provider") or _FINDER_DEFAULT
    if provider == "dataforseo":
        return DataForSEO(login=(cfg or {}).get("login"),
                          password=(cfg or {}).get("password"))
    raise ValueError(f"unknown business source provider: {provider}")

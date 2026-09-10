#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Resolve a seller's provider CONFIG from their stored settings + env master keys.

This is the "config-in, stateless adapter" glue. Every module that calls an
outside provider goes through here to decide WHAT it should call, then hands the
result to providers.get_llm()/get_source() to build the live adapter.

Precedence (user option 3 — per-seller key OR env-master fallback):
  1. A per-seller secret stored encrypted in settings (decrypted in memory).
  2. If none / not decryptable, the platform master env key for that provider.

settings jsonb shape (secrets are Fernet tokens, never plaintext):
{
  "llm":    {"provider":"groq", "model":"…", "base_url":null, "api_key_enc":null},
  "finder": {"provider":"dataforseo", "login_enc":null, "password_enc":null}
}
"""

import json
import os

import at_rest  # Fernet secrets-at-rest (module named at_rest to avoid shadowing stdlib secrets)

DEFAULT_LLM = "groq"
DEFAULT_SOURCE = "dataforseo"

# Which env var holds the MASTER key for each LLM provider (fallback when a
# seller has no key of their own). base_url/model fall back to registry defaults.
_MASTER_LLM_ENV = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _as_dict(settings):
    if isinstance(settings, dict):
        return settings
    if isinstance(settings, str) and settings:
        try:
            parsed = json.loads(settings)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def llm_cfg(settings=None, provider=None, model=None):
    """Resolve an LLM provider config dict: {provider, api_key, base_url, model}.
    `settings` is the seller's settings jsonb (or None for env-master only).
    Optional explicit provider/model overrides (e.g. a campaign's score_model)."""
    s = _as_dict(settings).get("llm") or {}
    name = (provider or s.get("provider") or DEFAULT_LLM)
    env_key = _MASTER_LLM_ENV.get(name)

    api_key = None
    enc = s.get("api_key_enc")
    if enc:
        try:
            api_key = at_rest.decrypt(enc)
        except ValueError:
            api_key = None  # undecryptable -> fall through to env master
    if not api_key and env_key:
        api_key = os.environ.get(env_key, "").strip() or None
    return {
        "provider": name,
        "api_key": api_key,
        "base_url": s.get("base_url") or None,
        "model": model or s.get("model") or None,
    }


def source_cfg(settings=None):
    """Resolve a business-source config dict: {provider, login, password}."""
    s = _as_dict(settings).get("finder") or {}
    name = s.get("provider") or DEFAULT_SOURCE

    def _dec(enc):
        if not enc:
            return None
        try:
            return at_rest.decrypt(enc)
        except ValueError:
            return None

    login = _dec(s.get("login_enc"))
    password = _dec(s.get("password_enc"))
    if not login:
        login = os.environ.get("DATAFORSEO_LOGIN", "").strip() or None
    if not password:
        password = os.environ.get("DATAFORSEO_PASSWORD", "").strip() or None
    return {"provider": name, "login": login, "password": password}

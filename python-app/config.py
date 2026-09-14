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
  "finder": {"provider":"dataforseo", "login_enc":null, "password_enc":null},
  "smtp":   {"host":null, "port":587, "user":null, "password_enc":null,
             "from_email":null, "from_name":null}
}
"""

import json
import os

import at_rest  # Fernet secrets-at-rest (module named at_rest to avoid shadowing stdlib secrets)

DEFAULT_LLM = "groq"
DEFAULT_SOURCE = "dataforseo"

# Where POST /runs hands a run's work. See agent_backend() below.
AGENT_BACKENDS = ("local", "agentcore")


def agent_backend():
    """Which executor owns a run started from the dashboard.

    local      -- an in-process daemon thread (default; needs nothing deployed)
    agentcore  -- POST /invocations on the deployed runtime in us-west-2

    An unrecognised value RAISES rather than falling back to local. A typo'd
    AGENT_BACKEND=agentcore would otherwise quietly demo the local path while
    the operator believes they are watching AWS -- a wrong answer to the one
    question the switch exists to answer.
    """
    value = (os.getenv("AGENT_BACKEND") or "local").strip().lower()
    if value not in AGENT_BACKENDS:
        raise ValueError(
            f"AGENT_BACKEND must be one of {list(AGENT_BACKENDS)}, got {value!r}")
    return value


def agentcore_runtime_arn():
    """The deployed runtime to invoke, or None when it has not been configured."""
    return (os.getenv("AGENTCORE_RUNTIME_ARN") or "").strip() or None


def agentcore_region():
    return (os.getenv("AGENTCORE_REGION")
            or os.getenv("AWS_REGION") or "us-west-2").strip()


# --------------------------------------------------------------------------- #
# Sending, and where uploaded files live. Both are swappable for the same
# reason the agent backend is: the demo must not be hostage to one vendor, and
# the honest refusal has to be a first-class setting rather than an accident.
# --------------------------------------------------------------------------- #
SEND_BACKENDS = ("none", "ses", "smtp")
FILE_BACKENDS = ("none", "supabase", "s3", "local")


def _switch(env_name, allowed, default):
    """Read a backend switch, refusing an unrecognised value.

    Same rule as agent_backend(): a typo RAISES. `SEND_BACKEND=smpt` silently
    falling back to `none` would make the app refuse every send while the
    operator reads a log that says SMTP is configured — a wrong answer to the
    exact question the switch exists to answer.
    """
    value = (os.getenv(env_name) or default).strip().lower()
    if value not in allowed:
        raise ValueError(
            f"{env_name} must be one of {list(allowed)}, got {value!r}")
    return value


def send_backend():
    """none      -- refuse every send and say why (the default: nothing is sent
                   until someone deliberately turns it on)
    ses       -- Amazon SES v2, raw MIME so attachments work
    smtp      -- any SMTP host: Gmail app-password, Brevo, Mailjet, SMTP2GO…
    """
    return _switch("SEND_BACKEND", SEND_BACKENDS, "none")


def file_backend():
    """none      -- store nothing; the extracted text still lands in the row
    supabase  -- Supabase Storage (default when creds are present)
    s3        -- any S3-compatible bucket
    local     -- the filesystem, for a machine with no cloud at all
    """
    return _switch("FILE_BACKEND", FILE_BACKENDS, "supabase")


SMTP_DEFAULT_PORT = 587


def _smtp_env(name):
    return (os.getenv(name) or "").strip()


def smtp_cfg(settings=None):
    """Resolve the SMTP config a send should use: one seller, env as the master.

    Same precedence as llm_cfg and source_cfg -- a seller MAY bring their own
    account, and when they have not, the env master sends on their behalf:

        settings.smtp.{host,port,user,password_enc} -> SMTP_{HOST,PORT,USER,PASSWORD}

    THE ONE RULE THAT IS NOT LIKE THE OTHERS, and the reason this is not a copy
    of source_cfg: **`from_email` follows the CREDENTIAL, not the seller.**
    Gmail, SES and most providers refuse a From the authenticated account is not
    allowed to send as. A seller-supplied `from_email` combined with the env
    account's host/user therefore produces a message that fails at send time --
    loudly on Gmail ("550 From address not verified"), or by silently rewriting
    the header on providers that permit it, which is worse because the seller
    never learns their address did not go out.

    So the seller's `from_email` is honoured ONLY when the seller also supplied
    their own `host` and `user`. When it is ignored it is reported in `notes`
    rather than dropped in silence: a field the operator filled in that does
    nothing must say so.

    `from_name` has no such constraint (display names are not authenticated) and
    is therefore always taken from the seller when set.
    """
    s = _as_dict(settings).get("smtp") or {}

    def _dec(enc):
        if not enc:
            return None
        try:
            return at_rest.decrypt(enc)
        except ValueError:
            return None          # undecryptable -> fall through to env master

    # The seller's own account, if they gave one. Both halves are required: a
    # host without a user cannot authenticate, and a user without a host has
    # nowhere to connect.
    seller_host = str(s.get("host") or "").strip()
    seller_user = str(s.get("user") or "").strip()
    own_account = bool(seller_host and seller_user)

    notes = []
    seller_from = str(s.get("from_email") or "").strip()
    if seller_from and not own_account:
        notes.append(
            "this seller set a From address but no SMTP host/user of their own, "
            "so it was ignored -- mail goes out from the shared account's From "
            "address. Providers reject a From the authenticated account is not "
            "allowed to send as.")

    port = (str(s.get("port") or "").strip() or _smtp_env("SMTP_PORT")
            or str(SMTP_DEFAULT_PORT))
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        notes.append(f"SMTP port {port!r} is not a valid port; using "
                     f"{SMTP_DEFAULT_PORT}.")
        port = SMTP_DEFAULT_PORT

    seller_name = str(s.get("from_name") or "").strip()
    return {
        "host": (seller_host or _smtp_env("SMTP_HOST")) or None,
        "port": port,
        "user": (seller_user or _smtp_env("SMTP_USER")) or None,
        "password": (_dec(s.get("password_enc"))
                     or os.getenv("SMTP_PASSWORD") or None),
        "from_email": ((seller_from if own_account else None)
                       or _smtp_env("SEND_FROM_EMAIL") or None),
        "from_name": seller_name or _smtp_env("SEND_FROM_NAME") or None,
        # Whether the SELLER named it, as opposed to the env default filling
        # in. The caller needs the distinction because there is one rung
        # between the two that this function cannot see: the seller's own
        # `name`/`brand` on their profile row. Without this flag the env
        # default has already won by the time the caller looks, so a seller
        # whose settings are silent shows the platform's name instead of their
        # own -- which is the whole thing per-seller identity is for.
        # Precedence is settings.smtp.from_name > seller row > SEND_FROM_NAME.
        "from_name_set": bool(seller_name),
        "own_account": own_account,
        "notes": notes,
    }


def smtp_cfg_notes(cfg):
    """The warnings a resolved config can raise about itself.

    Both fail at send time in a way that reads like a code error rather than a
    setting:

      * From != account. Gmail refuses to send as an address the authenticated
        account does not own, and the rejection names the address rather than
        the setting that is wrong.
      * Half a credential. A user with no password (or the reverse) means the
        login cannot succeed, and the server's reply does not point at the
        missing half.
    """
    notes = list(cfg.get("notes") or [])
    from_email = (cfg.get("from_email") or "").strip().lower()
    user = (cfg.get("user") or "").strip().lower()
    if from_email and user and from_email != user:
        notes.append(
            f"SEND_FROM_EMAIL ({cfg.get('from_email')}) differs from the SMTP "
            f"account ({cfg.get('user')}). Most providers reject a From the "
            f"account does not own; use the same address unless you know this "
            f"account may send as another.")
    if bool(cfg.get("user")) != bool(cfg.get("password")):
        missing = "password" if cfg.get("user") else "user"
        notes.append(f"an SMTP {missing} is missing, so the login cannot "
                     f"succeed.")
    return notes


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

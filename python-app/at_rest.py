#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Encryption-at-rest for per-seller provider credentials (API keys, DataForSEO
login/password) that are stored in seller_profile.settings.

IMPORTANT (not hashing): an API key has to be USED to call the provider, so it
cannot be one-way hashed. Instead it is encrypted with Fernet (AES-128-CBC +
HMAC) under a MASTER key that lives ONLY in the environment
(CRED_ENCRYPTION_KEY). The database stores a Fernet token; the plaintext exists
only in memory for the few seconds of the API call.

Usage / behaviour:
  * If CRED_ENCRYPTION_KEY is unset, everything degrades gracefully:
      encrypt() -> raises KeyNotConfigured (callers return 503 on write);
      decrypt() -> raises KeyNotConfigured (callers fall back to env master).
    Non-secret settings are unaffected.
  * If the stored token was made under a different key (or is corrupt),
    decrypt() raises ValueError (callers treat it as "no usable seller secret"
    and fall back to env master rather than crashing a run).

Generate a key for .env once:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # -> CRED_ENCRYPTION_KEY=<that value>
"""

import os

from cryptography.fernet import Fernet, InvalidToken


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


class KeyNotConfigured(RuntimeError):
    """Raised when no CRED_ENCRYPTION_KEY is set but one is required."""


def _fernet():
    key = (os.environ.get("CRED_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise KeyNotConfigured(
            "CRED_ENCRYPTION_KEY is not set. Generate one and add it to .env "
            "(see at_rest.py header).")
    return Fernet(key.encode("ascii"))


def key_configured():
    """True when a master key exists (so callers can encrypt/write secrets)."""
    try:
        _fernet()
        return True
    except KeyNotConfigured:
        return False


def encrypt(plaintext):
    """Return a Fernet token for `plaintext`. Raises KeyNotConfigured if no
    master key is set."""
    if plaintext is None:
        return None
    plaintext = str(plaintext)
    if plaintext == "":
        return None
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token):
    """Return the plaintext for a Fernet `token`. Returns None for empty/None.
    Raises KeyNotConfigured if no master key; raises ValueError if the token is
    unreadable (wrong key / corrupt) — callers treat that as 'no usable secret'
    and fall back to the env master key."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("stored credential is not decryptable "
                         "(wrong CRED_ENCRYPTION_KEY or corrupt token)") from e


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Encrypt/decrypt a value under CRED_ENCRYPTION_KEY.")
    ap.add_argument("mode", choices=["encrypt", "decrypt"])
    ap.add_argument("value")
    args = ap.parse_args()
    try:
        out = encrypt(args.value) if args.mode == "encrypt" else decrypt(args.value)
    except Exception as e:
        print(f"ERROR: {e}", file=os.sys.stderr)
        os.sys.exit(1)
    print(out)


if __name__ == "__main__":
    main()

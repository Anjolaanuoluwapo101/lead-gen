"""Who is calling, and which seller do they own.

TWO CALLERS, TWO CREDENTIALS

The API has two kinds of client and they cannot share one scheme.

- **A browser** logs in as a person. It gets a Supabase Auth JWT and sends
  `Authorization: Bearer <token>`.
- **n8n** is a machine. Five workflows hardcode `http://127.0.0.1:5000` and send
  no credential at all. It gets `X-Service-Token`, a static secret standing for
  the default tenant.

Both end up as a `seller_profile.id`, because every route in the app is already
scoped to exactly one seller. That collapse is the whole point of this module:
`_resolve_read_seller` was written (see the comment above it in app.py) so that
auth could later be injected here without redesigning a single read.

WHY THE JWT IS NOT DECODED HERE

`GET {SUPABASE_URL}/auth/v1/user` with the anon key asks Supabase to tell us who
this is. The alternative — decoding the JWT locally and trusting its claims —
means we own the signing algorithm, the expiry check, and the revocation story.
This way Supabase stays the source of truth and there is one code path, one
network call, and no way to get algorithm confusion wrong. The cost is an HTTP
round trip, which the 60s cache below makes irrelevant.

WHY AUTH_REQUIRED DEFAULTS TO FALSE

Defaulting to `true` would break all five n8n workflows silently — they would
start getting 401s and nothing in the workflow says why. The default is
therefore permissive, and turning it on is a deliberate act. That is a weaker
production posture and the README says so plainly rather than pretending
otherwise. The wiring is real either way; only the enforcement is deferred.

WHY AN EMAIL IS ENOUGH TO FIND THE SELLER

`seller_profile.email` is already `unique` (supabase-schema.sql) and is already
the natural key the n8n form find-or-creates on. A Supabase Auth user's verified
email is therefore a join key we already had — no `user_id` column, no
migration, no backfill. Signup is "create the auth user, then find-or-create the
profile for their email", which is a function that already existed.
"""

import hashlib
import os
import threading
import time

import requests

import lead_engine
import seller_ops

# A dashboard polls /runs/<id>/events every 5 seconds. Without a cache that is a
# Supabase Auth round trip per poll per user. 60s is short enough that a revoked
# session dies quickly and long enough that polling is free.
CACHE_TTL_S = 60
VERIFY_TIMEOUT_S = 5

# sha256(token) -> (expires_at, user_dict_or_None)
_cache = {}
_cache_lock = threading.Lock()

# A sentinel, because None is a legitimate CACHED value here: it means "this
# token was checked and refused". Returning None for both a miss and a cached
# refusal would re-ask Supabase on every request from a logged-out browser.
_MISS = object()


class AuthError(Exception):
    """A credential problem the caller must fix. Carries the HTTP status so
    routes do not each re-derive whether a missing token is 401 or 400."""

    def __init__(self, message, status=401):
        self.message = message
        self.status = status
        super().__init__(message)


def _env(name, default=""):
    return str(os.environ.get(name) or default).strip()


def auth_required():
    return _env("AUTH_REQUIRED", "false").lower() in ("1", "true", "yes", "on")


def service_token():
    return _env("SERVICE_TOKEN")


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key, _MISS)
        if hit is _MISS:
            return _MISS
        expires_at, value = hit
        if expires_at < time.monotonic():
            _cache.pop(key, None)
            return _MISS
        return value


def _cache_put(key, value):
    with _cache_lock:
        _cache[key] = (time.monotonic() + CACHE_TTL_S, value)


def clear_cache():
    """Drop every cached verification. For tests, and for a logout if we ever
    need one to be immediate rather than up to 60s late."""
    with _cache_lock:
        _cache.clear()


def _token_key(token):
    # The token IS the credential; hashing it means a cache dump or a log line
    # can never leak a usable session.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token, client=None):
    """Ask Supabase who this token belongs to.

    Returns the user dict (with `id` and `email`) or None. Only a 200 counts:
    a 401 means expired or forged, and anything else means we could not tell —
    and "could not tell" must never be treated as "valid".
    """
    token = (token or "").strip()
    if not token:
        return None

    key = _token_key(token)
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached

    url = _env("SUPABASE_URL").rstrip("/")
    anon = _env("SUPABASE_ANON_KEY")
    if not url or not anon:
        raise AuthError(
            "Supabase Auth is not configured: SUPABASE_URL and "
            "SUPABASE_ANON_KEY must both be set to verify a bearer token.",
            status=503)

    get = client.get if client is not None else requests.get
    try:
        resp = get(f"{url}/auth/v1/user",
                   headers={"apikey": anon, "Authorization": f"Bearer {token}"},
                   timeout=VERIFY_TIMEOUT_S)
    except Exception as exc:
        # A network failure is NOT an invalid token. Refusing to guess here is
        # what stops a Supabase blip from logging every user out.
        raise AuthError(f"could not verify token: {exc}", status=503)

    if resp.status_code != 200:
        _cache_put(key, None)
        return None

    try:
        user = resp.json() or {}
    except ValueError:
        user = {}
    if not user.get("id"):
        _cache_put(key, None)
        return None

    # Cache a bare dict, not the response object, so a caller cannot mutate what
    # the next caller receives.
    result = {"id": user.get("id"), "email": (user.get("email") or "").strip().lower()}
    _cache_put(key, result)
    return result


def bearer_token(headers):
    raw = (headers or {}).get("Authorization") or ""
    if raw[:7].lower() != "bearer ":
        return ""
    return raw[7:].strip()


def seller_for_user(user):
    """The seller_profile.id for a verified auth user, created on first sight.

    Signup creates the auth user; this creates the profile. Doing it lazily on
    first authenticated request means there is no separate signup endpoint to
    keep in sync, and a user who signs up but never calls the API simply has no
    profile yet rather than a half-built one.

    The email->id mapping is cached for CACHE_TTL_S like verifications: a
    dashboard polls every 5s, and without this each poll re-read the profile
    row. Note create_or_update_seller with no fields supplied is already
    read-only (it returns the existing row without writing), so this cache
    removes a SELECT per poll, not a write — but at 12 polls a minute per tab
    the SELECTs were the load. Misses are never cached (a poll racing signup
    must see the profile the moment it exists), and clear_cache() drops these
    entries with the token verifications.
    """
    email = (user or {}).get("email") or ""
    if not email or "@" not in email:
        # A Supabase user can legitimately have no email (phone/oauth). We have
        # no way to map them to a seller, so say so instead of inventing one.
        raise AuthError("this account has no email address, so it cannot be "
                        "matched to a seller profile", status=403)
    key = "seller:" + email
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    try:
        out = seller_ops.create_or_update_seller(email)
    except seller_ops.OpsError as exc:
        # Upstream/unavailable is not the user's fault; bad_request is.
        raise AuthError(exc.message, status=400 if exc.kind == "bad_request" else 503)
    sid = ((out or {}).get("seller") or {}).get("id")
    if sid:
        _cache_put(key, sid)
    return sid


def resolve_seller(data=None, headers=None, client=None):
    """The caller's seller id, or None when nothing identifies them.

    Resolution order — most specific credential first:

      1. `Authorization: Bearer <jwt>`  -> verified user -> their profile
      2. `X-Service-Token`              -> the default tenant (n8n)
      3. body `seller_id`               -> DEFAULT_SELLER_ID (the old bridge)

    Path 3 is the pre-auth behaviour and is left intact on purpose: it is what
    keeps the five existing n8n workflows running. It is also the path that
    makes the API unauthenticated when AUTH_REQUIRED is false, which the README
    states rather than hides.

    A malformed or expired bearer token is REFUSED even when auth is not
    required — silently falling through to the default tenant would turn a
    logged-out browser into a different tenant's session.
    """
    headers = headers or {}
    data = data or {}

    token = bearer_token(headers)
    if token:
        user = verify_token(token, client=client)
        if not user:
            raise AuthError("invalid or expired token", status=401)
        return seller_for_user(user)

    # The service token is checked BEFORE the enforcement gate, and the order is
    # load-bearing: n8n presents a service token and nothing else, so gating
    # first would 401 every one of its five workflows the moment AUTH_REQUIRED
    # is turned on — which is the opposite of what a machine credential is for.
    if _service_token_ok(headers.get("X-Service-Token"), service_token()):
        return lead_engine.DEFAULT_SELLER_ID or None

    if auth_required():
        raise AuthError("authentication required", status=401)

    sid = str(data.get("seller_id") or "").strip()
    return sid or (lead_engine.DEFAULT_SELLER_ID or None)


def _service_token_ok(presented, expected):
    """True only when a service token was configured AND matches.

    An unset SERVICE_TOKEN must not match an unset header — that would make the
    default tenant reachable by sending nothing, which is the exact opposite of
    what the header is for.
    """
    presented = str(presented or "").strip()
    return bool(expected) and bool(presented) and presented == expected


def is_operator(headers):
    """True when the caller is the platform rather than a tenant.

    A valid service token means "act on behalf of any seller" — that is what
    makes n8n able to work a form submitted with any seller's email. A bearer
    token never is: a browser is always exactly one tenant.
    """
    return _service_token_ok((headers or {}).get("X-Service-Token"),
                             service_token())


# The two scopes a caller can have over a seller row. `resolve_seller` answers
# "who is calling"; these answer "what may they touch" — a different question,
# and the one the /seller/<id>/* routes were never asking.
SELF = "self"          # a tenant, over their own row
ANY = "any"            # the platform, over any row


def scope_for(headers):
    """`ANY` for the platform, `SELF` for a tenant.

    Deliberately not folded into resolve_seller: routes that only read the
    caller's own row (/leads, /runs) do not care which scope produced the id,
    and widening that function's return type would have changed three call
    sites for no gain.
    """
    return ANY if is_operator(headers) else SELF


def authorize_seller(seller_id, data=None, headers=None, client=None):
    """The caller's seller id — but only if `seller_id` is theirs to touch.

    This is the missing half of `resolve_seller`. That function answers "which
    seller is calling", which is sufficient when the route's subject IS the
    caller (every read route, and /seller/me). It is NOT sufficient for
    `/seller/<seller_id>/*`, where the subject comes from the URL and is
    therefore the caller's to choose. Those routes need the two compared, and
    before this existed they simply were not.

    With AUTH_REQUIRED=false the whole API is open by design (see the module
    docstring), so the comparison is skipped and n8n keeps working with no
    service token. Turning the flag on is what makes this bite — which is the
    point: the enforcement was always meant to be deferred to one switch, not
    to be absent.
    """
    headers = headers or {}
    own = resolve_seller(data, headers, client=client)

    if scope_for(headers) == ANY:
        return own
    if not auth_required():
        return own

    if not own or str(seller_id) != str(own):
        # Worded like leads_read.owns_campaign's refusal, and deliberately
        # ambiguous about which half failed: answering "that seller exists but
        # is not yours" would confirm the id, and ids are enumerable.
        raise AuthError("seller not found or not yours", status=403)
    return own

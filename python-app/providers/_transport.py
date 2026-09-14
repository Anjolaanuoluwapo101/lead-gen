"""Shared HTTP transport for the LLM adapters: one Session, patient retries.

LLM POSTs are retried deliberately, unlike Supabase writes (which fail loudly
rather than risk inserting twice): a retried judge call may bill twice, but
an unretried 429 fails the whole business batch with "score failed" on every
row. backoff_factor=2 with Retry-After respected, so a rate limit degrades
into latency instead of a hammering loop. Connection reuse saves a TLS+TCP
handshake on every one of the dozens of judge calls per campaign.
"""

import threading

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:
    Retry = None

_lock = threading.Lock()
_SESSION = None


def llm_session():
    """Process-wide Session for LLM chat calls. Thread-safe to share: the
    pool underneath is, and no per-call state lives on the Session."""
    global _SESSION
    if _SESSION is None:
        with _lock:
            if _SESSION is None:
                s = requests.Session()
                if Retry is not None:
                    retry = Retry(
                        total=3, backoff_factor=2,
                        status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods=frozenset(["POST", "GET"]),
                        respect_retry_after_header=True)
                    adapter = HTTPAdapter(pool_connections=20,
                                          pool_maxsize=20, max_retries=retry)
                    s.mount("https://", adapter)
                    s.mount("http://", adapter)
                _SESSION = s
    return _SESSION

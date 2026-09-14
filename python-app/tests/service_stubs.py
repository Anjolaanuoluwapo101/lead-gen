"""Fakes for the outbound half of the send path.

Kept in one module rather than repeated per test file because three separate
tests need to stand in for a service that must never be called for real in a
test run: SMTP, SES, and Supabase Storage. A fake that talks to a live provider
would send mail or upload resumes during `pytest`, and `conftest.py`'s network
guard turns that into a failure rather than a surprise — these are how you get
past it honestly.
"""

import smtplib


class FakeSMTPServer:
    """Enough of smtplib.SMTP to observe what would have been sent.

    Records the message object rather than its bytes so a test can ask the MIME
    structure directly — "did the resume arrive as an attachment?" is the
    question that matters, and it is much clearer against `iter_attachments()`
    than against a byte string.
    """

    def __init__(self, sink, fail_auth=False):
        self.sink = sink
        self.fail_auth = fail_auth
        self.started_tls = False
        self.logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        return (250, b"ok")

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        if self.fail_auth:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.logged_in = True

    def send_message(self, msg):
        self.sink["message"] = msg
        return {}


class FakeSES:
    """Records send_email calls; optionally raises one."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"MessageId": "ses-message-id-1"}


class FakeRequests:
    """Stand-in for `requests` in file_store's Supabase Storage calls.

    `status` drives the response code so the 404-means-absent branch is
    reachable without a network, which is the branch that decides whether a
    seller with no stored file is an error or an ordinary state.
    """

    def __init__(self, status=200, body=b""):
        self.status = status
        self.body = body
        self.posted = []
        self.getted = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.posted.append({"url": url, "headers": headers or {}, "data": data})
        return self._response()

    def get(self, url, headers=None, timeout=None):
        self.getted.append({"url": url, "headers": headers or {}})
        return self._response()

    def delete(self, url, headers=None, timeout=None):
        return self._response()

    def _response(self):
        return SimpleResponse(self.status, self.body)


class SimpleResponse:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "replace") if content else ""
        self.headers = {}

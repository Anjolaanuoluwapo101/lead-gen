"""The dashboard pages.

Pages are thin shells, so there is little to test — but two things here are
worth pinning, and both are the kind of bug that only shows up when a human is
watching:

1. **The pages render with no database configured.** A page that 500s because a
   query failed would break the demo at exactly the wrong moment, and these
   pages make no queries. This is asserted, not assumed.
2. **The browser plumbing calls the routes that actually exist, with the
   methods they actually accept.** This was wrong on the first pass in several
   places — `/seller/by-email` is GET-with-a-query, config is PATCH, the resume
   upload takes base64 bytes rather than text. Every one of those would have
   been a silent 405 or 400 in the browser and nothing else would have caught
   it, because the pages contain no Python.
"""

import os
import re
import shutil
import subprocess
import tempfile
import uuid

import pytest

import app as flask_app

RUN_ID = "11111111-2222-3333-4444-555555555555"

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

# `/runs/<id>` is one URL with two representations: JSON for the API, this page
# for a browser. The HTML tests must ask for HTML explicitly, because the
# default (`*/*`, what curl sends) resolves to JSON on purpose.
HTML = {"Accept": "text/html"}

PAGES = [("/", {}), ("/login", {}), ("/signup", {}), ("/settings", {}),
         ("/prospects", {}), (f"/runs/{RUN_ID}", HTML)]


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


# --------------------------------------------------------------------------- #
# The pages render
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,headers", PAGES)
def test_every_page_renders(client, path, headers):
    r = client.get(path, headers=headers)
    assert r.status_code == 200, path
    assert b"<html" in r.data.lower()


@pytest.mark.parametrize("path,headers", PAGES)
def test_pages_render_with_no_database_configured(client, path, headers,
                                                  monkeypatch):
    # THE POINT OF A THIN SHELL. If any page ever starts querying, this fails.
    def boom(*a, **k):
        raise AssertionError(f"{path} queried the database: {a}")

    monkeypatch.setattr("supabase_store.select_rows", boom, raising=False)
    assert client.get(path, headers=headers).status_code == 200


def test_the_run_page_carries_its_run_id(client):
    body = client.get(f"/runs/{RUN_ID}", headers=HTML).get_data(as_text=True)
    assert f'"{RUN_ID}"' in body


def test_a_browser_gets_the_page_and_a_script_gets_json(client, monkeypatch):
    """One URL, two representations — and the DEFAULT must be JSON.

    A script is the common case for an API endpoint, and a script that
    unexpectedly receives HTML fails in a confusing way ("invalid JSON" rather
    than "wrong format").
    """
    import routes_agent
    import agent.run_store as run_store

    monkeypatch.setattr(run_store, "get_run", lambda rid: None)
    # A malformed id keeps the JSON branch away from the database entirely.
    html = client.get("/runs/not-a-uuid", headers=HTML)
    assert html.status_code == 200 and b"<html" in html.data.lower()

    js = client.get("/runs/not-a-uuid")
    assert js.status_code == 404
    assert js.get_json()["ok"] is False


def test_a_malformed_run_id_is_404_not_a_500(client, monkeypatch):
    # `runs.id` is a uuid column: Postgres answers 22P02, which would surface as
    # a 500 for a URL anyone can type by hand.
    import supabase_store
    called = []
    monkeypatch.setattr(supabase_store, "select_rows",
                        lambda *a, **k: called.append(a) or [])
    assert client.get("/runs/not-a-uuid").status_code == 404
    assert called == [], "a malformed id reached the database"


def test_a_malformed_draft_id_is_404_not_a_500(client, monkeypatch):
    import supabase_store
    called = []
    monkeypatch.setattr(supabase_store, "select_rows",
                        lambda *a, **k: called.append(a) or [])
    assert client.get("/drafts/not-a-uuid", json={}).status_code == 404
    assert called == [], "a malformed id reached the database"


def test_the_anon_key_is_exposed_but_the_service_token_is_not(client,
                                                              monkeypatch):
    # The anon key is publishable by design and the login form cannot work
    # without it. A service token in a page would be a real leak, so this test
    # asserts the boundary rather than trusting that nobody adds one later.
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-public-key")
    monkeypatch.setenv("SERVICE_TOKEN", "svc-do-not-leak")
    body = client.get("/login").get_data(as_text=True)
    assert "anon-public-key" in body
    assert "svc-do-not-leak" not in body


def test_ui_config_reports_whether_auth_is_enforced(client, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    assert client.get("/ui/config").get_json()["auth_required"] is True
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    assert client.get("/ui/config").get_json()["auth_required"] is False


def test_the_poll_interval_is_served_to_the_page(client):
    # The page and the server must agree on the cadence; hardcoding it in two
    # places is how they drift.
    assert client.get("/ui/config").get_json()["poll_ms"] == 5000


def _run_page_source(client):
    return client.get(
        "/runs/11111111-2222-3333-4444-555555555555",
        headers={"Accept": "text/html"}).get_data(as_text=True)


def test_the_trace_speaks_plain_words_not_kinds(client):
    # The event trace used to print raw kinds ("turn") and database ordinals
    # as the trail. KIND_LABEL translates every known kind, SKIP_KIND keeps
    # heartbeats out of the narrative, and the trail reads created_at.
    body = _run_page_source(client)
    for label in ("Sent to the runner", "Goal", "Search area", "Searching",
                  "Search results", "Checking businesses",
                  "Shortlist update", "Warning", "Finished"):
        assert label in body
    assert "SKIP_KIND" in body and "renderEvent" in body


def test_the_trace_trail_is_a_time_not_a_sequence_number(client):
    # e.seq in the trail looked like a step count and started at 2. Gone.
    body = _run_page_source(client)
    assert "A.esc(e.seq)" not in body
    assert "eventTime(e)" in body


def test_the_trace_accumulates_instead_of_replacing(client):
    # Each poll carries only what is new; replacing innerHTML collapsed the
    # story to the latest batch while the header promised an order.
    body = _run_page_source(client)
    assert "dataset.live" in body


# --------------------------------------------------------------------------- #
# The pages call routes that exist
# --------------------------------------------------------------------------- #
def _routes():
    return {(r.rule, m) for r in flask_app.app.url_map.iter_rules()
            for m in r.methods}


def _api_url_args(body):
    """The first argument of every `A.api(...)` call, as SOURCE TEXT.

    Not `re.findall(r'A\\.api\\(\\s*"([^"]+)"')`, which is what this used to be
    and which only captured the FIRST string literal. The pages build most
    paths by concatenation —

        A.api("/drafts/" + encodeURIComponent(id) + "/send", ...)

    — so that regex captured `"/drafts/"`, matched the real `/drafts` rule, and
    passed. A mutation that changed that call to `"/draft/"` was NOT caught: the
    guard was checking a prefix, and the prefix was fine. Every interpolated
    path in both pages had the same blind spot.

    So the whole argument is captured, by scanning to the first top-level comma
    and tracking string state — because `A.api(url, { body: {} })` has a comma
    inside the object literal that must not end the argument.
    """
    out = []
    for match in re.finditer(r"A\.api\(", body):
        i, depth, chars = match.end(), 0, []
        while i < len(body):
            ch = body[i]
            if ch in "\"'":
                end = body.index(ch, i + 1)
                chars.append(body[i:end + 1])
                i = end + 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                break
            chars.append(ch)
            i += 1
        out.append("".join(chars).strip())
    return out


def _as_path(expr):
    """Turn a JS url expression into a comparable path.

    `"/drafts/" + encodeURIComponent(id) + "/send"` -> `/drafts/{id}/send`

    Quote-aware, by scanning rather than by regex substitution. A regex that
    replaced bare identifiers with `{id}` also rewrote the identifiers INSIDE
    the string literals — `"/drafts/"` became `"/{id}/"` — so every path
    collapsed to `/{id}/{id}` and nothing matched. The literals are the part
    being checked; they have to survive.
    """
    out, i = [], 0
    while i < len(expr):
        ch = expr[i]
        if ch in "\"'":
            end = expr.index(ch, i + 1)
            out.append(expr[i + 1:end])
            i = end + 1
        elif ch == "+" or ch.isspace():
            i += 1
        else:
            # Anything that is not a literal is an interpolated value.
            depth = 0
            while i < len(expr) and (depth or expr[i] not in '+"\''):
                if expr[i] == "(":
                    depth += 1
                elif expr[i] == ")":
                    depth -= 1
                i += 1
            out.append("{id}")
    return "".join(out).split("?")[0].replace("//", "/")


def _assert_calls_real_routes(body, page):
    paths = {_as_path(e) for e in _api_url_args(body)}
    assert paths, f"no api() calls found — did {page} change shape?"
    rules = {r.rule for r in flask_app.app.url_map.iter_rules()}
    for path in paths:
        assert any(_matches(path, rule) for rule in rules), \
            f"{page} calls {path!r}, which no route serves"


def test_the_settings_page_calls_only_real_routes(client):
    """Every URL in settings.html must match a registered rule.

    The JS is not executed by tests, so a typo'd path is invisible until a user
    clicks it.
    """
    _assert_calls_real_routes(client.get("/settings").get_data(as_text=True),
                              "settings.html")


def test_the_run_page_calls_only_real_routes(client):
    """The same guard as the settings page, for the page with the most calls.

    Run.html now posts to /drafts/<id>/send as well as approve, reject, export
    and cancel. The send button is the one place in this app that cannot be
    undone, so a typo'd path there failing at click time — in front of a
    reviewer — is exactly what this catches.
    """
    _assert_calls_real_routes(
        client.get("/runs/" + RUN_ID, headers=HTML).get_data(as_text=True),
        "run.html")


def test_the_dashboard_calls_only_real_routes(client):
    """The third page, for completeness: `/runs`, `/runs/list`, `/drafts/list`.

    Short, but it is where a run is STARTED, and `/runs` is one POST away from
    being the first thing a reviewer touches.
    """
    _assert_calls_real_routes(client.get("/").get_data(as_text=True),
                              "dashboard.html")


def test_the_run_page_offers_a_send_button(client):
    """The button exists, and it is disabled unless the draft is approved.

    Sending is irreversible — `idx_drafts_sent_once` makes `sent` final — so it
    must not look available for a draft a human has not approved.
    """
    body = client.get("/runs/" + RUN_ID, headers=HTML).get_data(as_text=True)
    assert 'id="send"' in body
    assert 'A.el("send").disabled = current.status !== "approved"' in body
    # And it is confirmed, not one-click.
    assert "window.confirm" in body


def _matches(path, rule):
    """Compare a literal path against a Flask rule, treating <x> as any segment."""
    a = [p for p in path.split("/") if p]
    b = [p for p in rule.split("/") if p]
    if len(a) != len(b):
        return False
    for got, want in zip(a, b):
        if want.startswith("<") and want.endswith(">"):
            continue
        if got != want:
            return False
    return True


def test_the_by_email_lookup_is_a_get(client):
    """`/seller/by-email` is GET with ?email=. Posting a body to it is a 405.

    This is asserted against the app rather than the template so that changing
    the route's method to satisfy the template would fail here instead of
    silently breaking n8n, which also uses it.
    """
    assert ("/seller/by-email", "GET") in _routes()
    assert ("/seller/by-email", "POST") not in _routes()


def test_the_config_route_is_a_patch_not_a_post(client):
    assert ("/seller/<seller_id>/config", "PATCH") in _routes()
    assert ("/seller/<seller_id>/config", "POST") not in _routes()


def test_the_settings_page_uses_patch_for_config(client):
    body = client.get("/settings").get_data(as_text=True)
    # A POST to a PATCH-only route is a 405 in the browser and nowhere else.
    assert re.search(r'/config"\s*,\s*\{\s*method:\s*"PATCH"', body), \
        "settings.html must PATCH /config"


def test_the_resume_upload_sends_base64_not_text(client):
    # The route extracts text server-side from raw bytes (pdf/docx included).
    # Sending `resume_text` would 400 with "filename and content_base64 are
    # required" — a message that does not mention the field the UI sent.
    body = client.get("/settings").get_data(as_text=True)
    assert "content_base64" in body
    assert "resume_text:" not in body


def test_the_masked_secret_is_never_loaded_into_an_input(client, monkeypatch):
    """A masked value must be a placeholder, never a value.

    If "********" were rendered into the input's value, saving the form without
    touching it would overwrite the real key with eight asterisks — and the
    seller would never see an error, just a provider that stopped working.
    """
    body = client.get("/settings").get_data(as_text=True)
    for m in re.finditer(r'<input[^>]*id="[^"]*-secret"[^>]*>', body):
        assert "value=" not in m.group(0)
    # The JS must only send a secret when one was typed.
    assert re.search(r'secret\.value\.trim\(\)', body)


def test_the_dashboard_links_to_a_real_run_route(client):
    body = client.get("/").get_data(as_text=True)
    assert '/runs/' in body
    assert ("/runs/<run_id>", "GET") in _routes()


def test_the_prospects_page_calls_only_real_routes(client):
    """The single business page posts to two routes and both must exist.

    `/prospects/direct` is the one that costs money (a billable DataForSEO
    lookup per call) and `/location` is the one that stops a typed place being
    sent to DataForSEO verbatim. A typo in either fails at click time only.
    """
    _assert_calls_real_routes(client.get("/prospects").get_data(as_text=True),
                              "prospects.html")


def test_the_workspace_nav_reaches_the_prospects_page(client):
    """The page is linked, not just routable.

    It was reached by hand-editing the URL for a while, which is how a page
    ends up shipped and invisible. Asserted from the dashboard rather than from
    the page itself, because the link lives in base.html's drawer.
    """
    body = client.get("/").get_data(as_text=True)
    assert 'href="/prospects"' in body


def _node():
    return shutil.which("node")


@pytest.mark.parametrize("path,headers", PAGES)
def test_every_rendered_script_parses(client, path, headers):
    """Run the page's inline JavaScript through `node --check`.

    A syntax error anywhere in a page's script means the ENTIRE script never
    runs: no error appears, the page simply sits there. That is the same shape
    as the duplicate id bug one screen up, and it is invisible to every other
    test here, because nothing in this suite executes JavaScript.

    Checked on the RENDERED output rather than the template source, because the
    templates contain Jinja expressions (`var RUN_ID = {{ run_id | tojson }};`)
    that are only valid JavaScript after rendering.

    Skipped when node is absent, so the suite still runs on a machine without
    it. The check is worth having where it can run.
    """
    if not _node():
        pytest.skip("node is not installed")
    html = client.get(path, headers=headers).get_data(as_text=True)
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html,
                        re.S)
    assert blocks, f"{path} rendered no inline script at all"
    for i, js in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(js)
            name = fh.name
        try:
            done = subprocess.run([_node(), "--check", name],
                                  capture_output=True, text=True)
        finally:
            os.unlink(name)
        assert done.returncode == 0, (
            f"{path} script block {i} does not parse, so none of the page's "
            f"JavaScript runs:\n{done.stderr}")


# --------------------------------------------------------------------------- #
# Copy rules
# --------------------------------------------------------------------------- #
def _visible_source(path):
    """Template source with the parts a user never sees removed.

    Jinja comments, `//` line comments and `/* */` blocks are stripped. The
    rule this feeds is about copy on the page, and every remaining em dash in
    the templates is in one of those three, so leaving them in would make the
    test fail on prose nobody reads.
    """
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    src = re.sub(r"\{#.*?#\}", " ", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", " ", src)
    src = re.sub(r"(?m)^\s*\*.*$", " ", src)
    return src


@pytest.mark.parametrize("name", sorted(os.listdir(TEMPLATES)))
def test_no_template_puts_a_dash_on_the_page(name):
    """No em dash (`—`), no en dash (`–`), anywhere a user can read one.

    A flat style rule, and flat style rules come back the moment nobody is
    looking. Hyphens are not checked here: they are inseparable from class
    names, ids and `data-` attributes, so the recurring compound word
    ("wall-clock", "self-service") is caught by review rather than by this.
    """
    if not name.endswith(".html"):
        pytest.skip("only templates carry user facing copy")
    path = os.path.join(TEMPLATES, name)
    found = sorted({m.group(0) for m in
                    re.finditer("[—–]", _visible_source(path))})
    assert not found, (
        f"{name} shows a dash to the user: {found}. Rewrite the sentence "
        f"rather than swapping in a different dash.")


# --------------------------------------------------------------------------- #
# One id, one element
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,headers", PAGES)
def test_no_page_has_a_duplicate_element_id(client, path, headers):
    """A repeated id is invisible in review and breaks the page at runtime.

    `getElementById` returns the FIRST match in document order, so a second
    element with the same id is silently unreachable — and, worse, the first
    one receives whatever the script writes to it. On the dashboard the section
    heading and the table body were both `id="runs"`, so every rendered row
    went into a plain <div> (outside any table) while the real <tbody> sat on
    "Loading…" forever. The API was returning 200 the entire time, which is why
    nothing else caught it: there is no error, just a page that never fills in.

    Asserted per rendered PAGE rather than per template file, because the
    collision can just as easily be between a child template and base.html.
    """
    html = client.get(path, headers=headers).get_data(as_text=True)
    ids = re.findall(r'\sid="([^"]+)"', html)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"{path} repeats these element ids: {dupes}"

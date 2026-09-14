"""The human-facing pages: signup, login, dashboard, run detail, settings.

WHY THESE ARE THIN SHELLS

Every page here renders a template and nothing else. None of them query the
database, and none of them decide what a seller may see. The data comes from the
JSON API (`app.py` + `routes_agent.py`) called from the browser with the user's
bearer token — the same routes n8n uses, with the same ownership gates.

That is deliberate. A server-rendered dashboard would need its own copy of
"which campaigns belong to this seller", and a second copy of an authorization
rule is a second place for it to be wrong. One implementation, two clients.

WHY THE ANON KEY IS SAFE TO EMBED

`SUPABASE_ANON_KEY` is injected into the page. It is *designed* to be public —
it identifies the project, not the user, and every request made with it is still
subject to Supabase's row-level security. It cannot read a seller's data on its
own. The keys that must never reach a browser are the service key and the
seller's stored provider keys; neither appears here.

WHY THE TOKEN LIVES IN sessionStorage

Same-origin only, no CORS, so no reason to send it anywhere. sessionStorage
rather than localStorage means closing the tab ends the session, which is the
right default for a demo and costs nothing.

WHY POLLING AND NOT SSE

`/runs/<id>` polls `/runs/<id>/events?after=<seq>` every 5 seconds. Server-sent
events would stream the same information through a second code path, a second
set of failure modes, and a connection to keep alive per viewer. The spec cut
SSE for exactly this reason; `after` makes each poll incremental, so the wasted
bytes are a few hundred per tick.
"""

import os

from flask import jsonify, render_template

POLL_INTERVAL_MS = 5000


def _env(name, default=""):
    return str(os.environ.get(name) or default).strip()


def page_config():
    """The values every page needs. Read per-request so a .env change or a test
    override is picked up without restarting."""
    return {
        "supabase_url": _env("SUPABASE_URL").rstrip("/"),
        # Publishable by design — see the module docstring.
        "supabase_anon_key": _env("SUPABASE_ANON_KEY"),
        "poll_ms": POLL_INTERVAL_MS,
        "auth_required": _env("AUTH_REQUIRED", "false").lower() in
                         ("1", "true", "yes", "on"),
    }


def register_ui_routes(app):

    @app.route("/")
    def ui_dashboard():
        return render_template("dashboard.html", **page_config())

    @app.route("/login")
    def ui_login():
        return render_template("login.html", **page_config())

    @app.route("/signup")
    def ui_signup():
        return render_template("signup.html", **page_config())

    # NOTE: there is deliberately no `/runs/<id>` route here. That URL is owned
    # by routes_agent.register_agent_routes, which serves JSON to scripts and
    # renders run.html to a browser from the same view. Registering it twice
    # would not raise — Flask keeps whichever came first and silently drops the
    # other — so the page would simply never render, with nothing to say why.

    @app.route("/prospects")
    def ui_prospects():
        """Adding ONE business the seller already knows about.

        The form posts to /prospects/direct in routes_agent, which is the route
        n8n also calls. Same reasoning as everywhere else in this module: one
        implementation of "what may this seller add", two clients.
        """
        return render_template("prospects.html", **page_config())

    @app.route("/settings")
    def ui_settings():
        return render_template("settings.html", **page_config())

    @app.route("/ui/config")
    def ui_config():
        """The same values as JSON, for the JS that needs them before a render."""
        return jsonify(page_config())

    return app

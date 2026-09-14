"""The extraction is only real if app.py's call sites resolve to the SHARED
implementation.

app.py aliases these names at import time. A later `def` at module level would
rebind the name and silently undo the extraction �?" the aliases would exist but
be dead. These tests are the alarm for that; without them the failure is
invisible until the two copies drift and the ownership rule diverges.
"""

import os

import app
import leads_read
import seller_ops


def test_aliases_are_the_shared_objects():
    assert app._flatten_leads is leads_read.flatten_leads
    assert app._decoded is leads_read.decoded
    assert app._seller_owns_campaign is leads_read.owns_campaign
    assert app.LEAD_COLUMNS is leads_read.LEAD_COLUMNS
    assert app._DRAFT_LEAD_COLUMNS is leads_read.DRAFT_LEAD_COLUMNS


def test_seller_aliases_are_the_shared_objects():
    assert app._SELLER_COLUMNS is seller_ops.SAFE_COLUMNS
    assert app._SELLER_PATCHABLE is seller_ops.SELLER_PATCHABLE
    assert app.SELLER_RENDER_MODES is seller_ops.SELLER_RENDER_MODES
    assert app._render_mode is seller_ops.render_mode
    assert app._maybe_bool is seller_ops.maybe_bool


def _req_lines(name):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, os.pardir, name), encoding="utf-8") as fh:
        return [ln.strip() for ln in fh
                if ln.strip() and not ln.startswith("#")]


def test_web_requirements_mirror_dev_minus_playwright():
    # Dockerfile.web installs requirements-web.txt. If someone adds a package
    # to requirements.txt and forgets the web list, the image silently ships
    # without it and the route that needs it 500s only in production.
    dev = _req_lines("requirements.txt")
    web = _req_lines("requirements-web.txt")
    assert "playwright" in dev and "playwright" not in web
    assert sorted(web) == sorted(p for p in dev if p != "playwright"), (
        "requirements-web.txt drifted from requirements.txt: mirror every "
        "change except playwright")


def test_app_seller_columns_carry_no_encrypted_settings():
    # The routes answer with whatever this constant names. If `settings` ever
    # reappears here, every seller route starts returning Fernet ciphertext.
    assert "settings" not in app._SELLER_COLUMNS


def test_the_functions_live_in_leads_reads_namespace():
    # Identity alone would pass for a copy-pasted duplicate bound to the same
    # name; the defining module proves there is only one definition.
    assert app._flatten_leads.__module__ == "leads_read"
    assert app._decoded.__module__ == "leads_read"
    assert app._seller_owns_campaign.__module__ == "leads_read"
    assert app._render_mode.__module__ == "seller_ops"
    assert app._maybe_bool.__module__ == "seller_ops"


def test_app_route_helpers_are_still_local():
    # These are app-specific and must NOT have been swept into leads_read.
    assert app._resolve_read_seller.__module__ == "app"
    assert not hasattr(leads_read, "_resolve_read_seller")

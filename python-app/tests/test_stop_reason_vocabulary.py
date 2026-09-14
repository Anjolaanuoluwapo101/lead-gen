"""The stop reason is the product's main claim. It must survive the UI.

`terminal_reason` is written by `agent/budget.py` and read by two browser
templates that keep their own copy of the vocabulary — they have to, because
each page renders the reason as a sentence rather than a status word. Two
copies of a vocabulary is two places for it to drift, and it already had:

  * both templates listed the CAP NAMES (`max_find_calls`, `max_llm_calls`,
    `max_spend_usd`, `max_turns`), which `TerminalReason` never writes;
  * the three values it writes MOST often (`budget_exhausted`, `max_attempts`,
    `timeout`) were missing from both.

The visible effect was that the commonest stop of all — budget_exhausted —
fell through to the generic "This run has finished." and the page showed no
stop reason at all. The one thing the README says a run always has.

These tests read the templates as TEXT rather than rendering them, because the
map is JavaScript: rendering would need a JS engine, and the property under
test (does every value have a sentence?) is a property of the source.
"""

import os
import re

import pytest

from agent.budget import TerminalReason

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

# Both pages keep their own map, deliberately — they are separate documents
# with separate prose. tests assert they stay in sync with the backend and,
# implicitly, with each other.
PAGES = ("run.html", "dashboard.html")


def _why_block(page):
    """The `var WHY = { ... };` initialiser, as source text."""
    with open(os.path.join(TEMPLATES, page), encoding="utf-8") as fh:
        source = fh.read()
    match = re.search(r"var WHY = \{(.*?)\};", source, re.S)
    assert match, f"{page} no longer defines a `var WHY = {{...}}` map"
    return match.group(1)


def _why_keys(page):
    """The quoted keys of the map. A key without a quoted string would be a
    syntax error in the browser, so requiring quotes is also a light check that
    the map is still valid JS."""
    return set(re.findall(r"(\w+)\s*:\s*[\"']", _why_block(page)))


@pytest.mark.parametrize("page", PAGES)
def test_every_terminal_reason_has_a_sentence(page):
    missing = sorted(set(TerminalReason.ALL) - _why_keys(page))
    assert not missing, (
        f"{page}'s WHY map has no entry for {missing}. Every value "
        f"TerminalReason writes must resolve to prose, or the run page shows "
        f"no stop reason at all -- which is the product's whole claim.")


@pytest.mark.parametrize("page", PAGES)
def test_the_map_invents_no_reasons(page):
    extra = sorted(_why_keys(page) - set(TerminalReason.ALL))
    assert not extra, (
        f"{page}'s WHY map explains {extra}, which TerminalReason never "
        f"writes. A map that answers for values the backend cannot produce "
        f"reads as coverage while the real ones fall through.")


@pytest.mark.parametrize("page", PAGES)
def test_the_cap_names_are_not_used_as_reasons(page):
    """The original bug, pinned as its own test.

    The cap names are Budget's `bounds` keys, not stop reasons. They are
    plausible-looking, which is exactly why they were used by mistake — so the
    guard has to name them explicitly rather than rely on the set comparison
    above being read carefully.
    """
    keys = _why_keys(page)
    for cap in ("max_find_calls", "max_llm_calls", "max_spend_usd", "max_turns"):
        assert cap not in keys, (
            f"{page} uses the budget cap {cap!r} as a stop reason. Caps are "
            f"never written to terminal_reason; the run reports "
            f"budget_exhausted instead.")


def test_the_two_pages_agree_on_the_vocabulary():
    """Separate prose, identical keys.

    They are allowed to word things differently — the dashboard is a table
    cell and the run page is a sentence — but a key present in one and absent
    in the other means one of the two pages cannot explain some run.
    """
    run, dash = _why_keys("run.html"), _why_keys("dashboard.html")
    assert run == dash, (
        f"run.html only: {sorted(run - dash)}; "
        f"dashboard.html only: {sorted(dash - run)}")


def test_terminal_reason_all_is_not_empty():
    """Guard the guard: if ALL were ever emptied, every test above would pass
    vacuously against an empty map."""
    assert len(TerminalReason.ALL) >= 5
    assert len(set(TerminalReason.ALL)) == len(TerminalReason.ALL)

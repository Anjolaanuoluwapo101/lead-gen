"""Bounds must hold regardless of what the model decides. These tests are the
evidence for that claim — the prompt is not the enforcement mechanism."""

import pytest

from agent.budget import (DEFAULT_BOUNDS, Budget, BudgetExceeded,
                          TerminalReason)


class Clock:
    """Injectable time so wall-clock behaviour is tested without sleeping."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_defaults_are_not_mutated_by_a_run():
    before = dict(DEFAULT_BOUNDS)
    Budget()
    assert DEFAULT_BOUNDS == before


def test_unknown_bound_is_rejected():
    # A typo'd cap that silently does nothing is the exact failure this module
    # exists to prevent.
    with pytest.raises(ValueError):
        Budget({"max_find_call": 3})


def test_find_cap_refuses_before_the_work():
    b = Budget({"max_find_calls": 2})
    b.charge_find()
    b.charge_find()
    with pytest.raises(BudgetExceeded) as e:
        b.charge_find()
    assert e.value.cap == TerminalReason.BUDGET_EXHAUSTED
    assert e.value.detail["counter"] == "find_calls"
    assert b.find_calls == 2  # a refused charge does not consume


def test_llm_cap_counts_and_refuses():
    b = Budget({"max_llm_calls": 3})
    for _ in range(3):
        b.charge_llm()
    with pytest.raises(BudgetExceeded):
        b.charge_llm()
    assert b.llm_calls == 3


def test_spend_cap_binds_when_unit_cost_is_high():
    b = Budget({"max_llm_calls": 100, "estimated_unit_cost_usd": 0.5,
                "max_estimated_spend_usd": 1.0})
    b.charge_llm()
    b.charge_llm()
    with pytest.raises(BudgetExceeded) as e:
        b.charge_llm()
    assert e.value.detail["counter"] == "estimated_spend_usd"
    assert e.value.detail["estimate"] is True


def test_the_spend_cap_binds_at_the_combined_ceiling():
    # The real worst case. Spend is priced over ALL model calls -- the tool
    # calls bounded by max_llm_calls plus the driver's one-per-turn calls
    # bounded by max_agent_turns -- so the ceiling is their SUM.
    # Pricing off llm_calls alone made this assertion false in the safe-looking
    # direction: it understated spend by every turn call, and the $1.00 cap
    # could never fire. If someone changes a default above, this test is what
    # tells them what they just did to the effective ceiling.
    b = Budget()
    ceiling = (b.bounds["max_llm_calls"] + b.bounds["max_agent_turns"]) * \
        b.bounds["estimated_unit_cost_usd"]
    assert ceiling > b.bounds["max_estimated_spend_usd"], \
        "the spend cap can never fire, so it is decoration rather than a cap"


def test_turn_calls_are_priced_even_though_llm_calls_stays_tool_only():
    # The live-run bug, in miniature: three turns plus one drafting call is
    # FOUR model calls. Reporting llm_calls=1 priced one of them.
    b = Budget({"max_agent_turns": 99})
    for _ in range(3):
        b.charge_turn()
    b.charge_llm()
    assert b.llm_calls == 1, "max_llm_calls must still bound TOOL calls only"
    assert b.model_calls == 4
    assert b.estimated_spend_usd == round(
        4 * b.bounds["estimated_unit_cost_usd"], 6)


def test_wall_clock_uses_time_since_construction():
    c = Clock()
    b = Budget({"max_wall_clock_s": 10}, clock=c)
    c.t = 9.0
    b.charge_find()  # still inside
    c.t = 10.1
    with pytest.raises(BudgetExceeded) as e:
        b.charge_find()
    assert e.value.cap == TerminalReason.TIMEOUT


def test_turn_cap():
    b = Budget({"max_agent_turns": 2})
    b.charge_turn()
    b.charge_turn()
    with pytest.raises(BudgetExceeded) as e:
        b.charge_turn()
    assert e.value.cap == TerminalReason.MAX_ATTEMPTS


def test_record_llm_calls_cannot_refuse():
    # Work already done outside a reservation must still be COUNTED (otherwise
    # estimated_spend_usd is a lie), and the cap becomes a circuit breaker for
    # the next call.
    b = Budget({"max_llm_calls": 2})
    b.record_llm_calls(5)
    assert b.llm_calls == 5
    assert b.exhausted() == TerminalReason.BUDGET_EXHAUSTED


def test_target_met_boundary():
    b = Budget({"target_qualified": 10})
    assert b.target_met(9) is False
    assert b.target_met(10) is True
    assert b.target_met(None) is False


def test_snapshot_is_labelled_an_estimate():
    snap = Budget().snapshot()
    assert snap["estimate"] is True
    assert "caps" in snap and snap["find_calls"] == 0


def test_terminal_reasons_are_unique():
    assert len(TerminalReason.ALL) == len(set(TerminalReason.ALL))


# --------------------------------------------------------------------------- #
# The wall-clock cap must fire BEFORE any platform ceiling does
# --------------------------------------------------------------------------- #
AGENTCORE_SYNC_CEILING_S = 900      # AgentCore's 15-minute synchronous limit


def test_wall_clock_fires_before_the_agentcore_ceiling():
    """The cap has to be ours, not the platform's.

    With AGENT_BACKEND=agentcore a run is one synchronous HTTP request, and
    AgentCore kills it at 15 minutes. If max_wall_clock_s were equal to that, a
    long run would be destroyed by the platform at the same instant our own cap
    came due — and the platform's version is a 504 with NO terminal_reason.
    That is precisely the outcome the whole bounds design exists to prevent, so
    the margin is the feature, not a tuning detail.

    Raising this to 900 or above should fail here rather than at demo time.
    """
    assert DEFAULT_BOUNDS["max_wall_clock_s"] < AGENTCORE_SYNC_CEILING_S


def test_the_margin_is_big_enough_to_record_the_stop():
    """Not just "less than" — there has to be room to serialise the report and
    answer the request after the cap fires. A 1-second margin would technically
    satisfy the test above and still lose the reason."""
    margin = AGENTCORE_SYNC_CEILING_S - DEFAULT_BOUNDS["max_wall_clock_s"]
    assert margin >= 60, f"only {margin}s of headroom before the platform 504s"


def test_the_cap_is_accessible_to_the_dashboard_without_importing_budget():
    """routes_agent stores DEFAULT_BOUNDS verbatim on the run row, so the
    bound a run ACTUALLY ran under is recoverable from `runs.bounds` even after
    this default changes."""
    assert "max_wall_clock_s" in DEFAULT_BOUNDS

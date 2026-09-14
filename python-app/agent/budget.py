"""Hard bounds for an agent run — enforced here, in code, never by the prompt.

The whole design rests on one claim: the model cannot exceed these caps no
matter what it decides, because the caps are counters in Python that raise
before any work happens. Do not restate this as "the prompt tells it to stop."
That would be false, and it is the first thing a judge probes.

Cost is an ESTIMATE. lead_engine reports no cost data at all, so
`estimated_spend_usd` is `model_calls * estimated_unit_cost_usd`, where
`model_calls` is tool calls plus one per agent turn. Never present it as
measured.
"""

import time

# Kept in sync with supabase-schema-agent.sql (runs.terminal_reason) and
# agent/run_store.finish_run. A run that stops always says why.
# NOTE on the two cost caps. Spend is priced over model_calls -- tool calls
# (max_llm_calls) PLUS the driver's one-per-turn calls (max_agent_turns) -- so
# with these defaults the worst case is (60 + 24) x $0.012 = $1.008, and
# max_estimated_spend_usd = $1.00 is what actually stops a maxed-out run, one
# call before its counters run out. That is the honest reading of "$1.00 cap".
# It was NOT true while spend was priced off llm_calls alone: that ignored all
# 24 turn calls, ceiling $0.72, and the $1.00 cap could never fire.
# test_the_spend_cap_binds_at_the_combined_ceiling pins this arithmetic; if you
# change a default above, that test is what tells you what you just did.
DEFAULT_BOUNDS = {
    "target_qualified":        10,
    "max_find_calls":           6,
    "max_llm_calls":           60,
    "estimated_unit_cost_usd": 0.012,   # ESTIMATE — never measured
    "max_estimated_spend_usd":  1.00,   # see note above: not the binding cap
    # Deliberately BELOW agentcore's 15-minute (900s) synchronous ceiling. If
    # they were equal, a run that ran long would be killed by the platform at
    # the same moment our own cap came due -- and the platform's version is a
    # 504 with no recorded reason, which is the exact outcome this module
    # exists to make impossible. 780 leaves ~2min of headroom to serialise the
    # report and answer. See agent/budget.py's test for the invariant.
    "max_wall_clock_s":        780,
    "max_agent_turns":          24,
}


class TerminalReason:
    """Why a run ended. The single source of truth for this vocabulary."""

    TARGET_MET = "target_met"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MAX_ATTEMPTS = "max_attempts"
    TIMEOUT = "timeout"
    NO_RESULTS = "no_results"
    ERROR = "error"
    USER_CANCELLED = "user_cancelled"

    ALL = (TARGET_MET, BUDGET_EXHAUSTED, MAX_ATTEMPTS, TIMEOUT, NO_RESULTS,
           ERROR, USER_CANCELLED)


class BudgetExceeded(Exception):
    """Raised by Budget.charge_* BEFORE the work happens.

    Tools catch this and return {"ok": False, "error": "budget_exceeded",
    "reason": ...} so the model sees a clean refusal rather than a traceback.
    """

    def __init__(self, cap, detail=None):
        self.cap = cap
        self.detail = detail or {}
        super().__init__(f"budget exceeded: {cap} ({self.detail})")


class Budget:
    """Counters for one run.

    `clock` is injectable so wall-clock behaviour is testable without sleeping.
    """

    def __init__(self, bounds=None, *, clock=time.monotonic):
        # Per-run overrides win, but an unknown key is an error rather than a
        # silent no-op: a typo'd cap that quietly does nothing is exactly the
        # failure this module exists to prevent.
        unknown = set(bounds or {}) - set(DEFAULT_BOUNDS)
        if unknown:
            raise ValueError(f"unknown bound(s): {sorted(unknown)}")
        self.bounds = {**DEFAULT_BOUNDS, **(bounds or {})}
        self._clock = clock
        self.started_at = clock()
        self.find_calls = 0
        self.llm_calls = 0
        self.agent_turns = 0

    # --- measured state ---------------------------------------------------- #

    @property
    def elapsed_s(self):
        return self._clock() - self.started_at

    @property
    def model_calls(self):
        """Every model call this run caused. `llm_calls` is NOT that number.

        The two counters measure different things and neither is redundant:
        `llm_calls` counts calls charged by TOOLS (draft_outreach, revise_draft,
        the scorer), while `agent_turns` counts the driver's own loop -- one
        model call per turn. A live run made three turn calls and one drafting
        call and reported llm_calls=1, so pricing off llm_calls alone
        understated what Bedrock was actually asked for. Kept as a separate
        property rather than folded into llm_calls because `max_llm_calls` is
        the cap tools check, and redefining it would silently tighten that cap.
        """
        return self.llm_calls + self.agent_turns

    @property
    def estimated_spend_usd(self):
        """ESTIMATE: model calls x unit cost. lead_engine reports no real cost.

        Counts turn calls as well as tool calls -- see model_calls.
        """
        return round(self.model_calls * float(self.bounds["estimated_unit_cost_usd"]), 6)

    def snapshot(self):
        """The `progress` jsonb persisted mid-run so the dashboard can poll."""
        return {
            "find_calls": self.find_calls,
            "llm_calls": self.llm_calls,
            "agent_turns": self.agent_turns,
            "model_calls": self.model_calls,
            "estimated_spend_usd": self.estimated_spend_usd,
            "elapsed_s": round(self.elapsed_s, 1),
            "caps": {
                "max_find_calls": self.bounds["max_find_calls"],
                "max_llm_calls": self.bounds["max_llm_calls"],
                "max_estimated_spend_usd": self.bounds["max_estimated_spend_usd"],
                "max_wall_clock_s": self.bounds["max_wall_clock_s"],
                "max_agent_turns": self.bounds["max_agent_turns"],
            },
            "estimate": True,   # never let the UI imply this is measured
        }

    # --- charges (call these BEFORE doing the work) ------------------------ #

    def check_wall_clock(self):
        """Raise if the run has outlived max_wall_clock_s. Checked before each
        turn and inside tools, so a long external call cannot overrun by much."""
        if self.elapsed_s > self.bounds["max_wall_clock_s"]:
            raise BudgetExceeded(
                TerminalReason.TIMEOUT,
                {"elapsed_s": round(self.elapsed_s, 1),
                 "max_wall_clock_s": self.bounds["max_wall_clock_s"]})

    def charge_find(self, n=1):
        """Reserve `n` finder calls."""
        self.check_wall_clock()
        if self.find_calls + n > self.bounds["max_find_calls"]:
            raise BudgetExceeded(
                TerminalReason.BUDGET_EXHAUSTED,
                {"counter": "find_calls", "used": self.find_calls,
                 "requested": n, "cap": self.bounds["max_find_calls"]})
        self.find_calls += n
        return self.find_calls

    def charge_llm(self, n=1):
        """Reserve `n` LLM calls, then re-check the estimated spend cap."""
        self.check_wall_clock()
        if self.llm_calls + n > self.bounds["max_llm_calls"]:
            raise BudgetExceeded(
                TerminalReason.BUDGET_EXHAUSTED,
                {"counter": "llm_calls", "used": self.llm_calls,
                 "requested": n, "cap": self.bounds["max_llm_calls"]})
        spend = (self.model_calls + n) * float(self.bounds["estimated_unit_cost_usd"])
        if spend > self.bounds["max_estimated_spend_usd"]:
            raise BudgetExceeded(
                TerminalReason.BUDGET_EXHAUSTED,
                {"counter": "estimated_spend_usd", "used": self.estimated_spend_usd,
                 "requested": round(spend, 6),
                 "cap": self.bounds["max_estimated_spend_usd"], "estimate": True})
        self.llm_calls += n
        return self.llm_calls

    def record_llm_calls(self, n):
        """Count LLM calls ALREADY made outside a charge_llm() reservation.

        lead_engine.run_campaign scores leads internally, so those calls happen
        inside find_leads where no cap could refuse them in advance. This
        increments unconditionally because the spend has already occurred —
        understating it would make estimated_spend_usd a lie.

        Effect: the cap becomes a CIRCUIT BREAKER for the next call rather than
        a gate on this one. That distinction is the honest one; do not describe
        it as prevention.
        """
        if n and int(n) > 0:
            self.llm_calls += int(n)
        return self.llm_calls

    def charge_turn(self):
        """Count one agent turn against max_agent_turns."""
        self.check_wall_clock()
        if self.agent_turns + 1 > self.bounds["max_agent_turns"]:
            raise BudgetExceeded(
                TerminalReason.MAX_ATTEMPTS,
                {"counter": "agent_turns", "used": self.agent_turns,
                 "cap": self.bounds["max_agent_turns"]})
        self.agent_turns += 1
        return self.agent_turns

    # --- stopping conditions ---------------------------------------------- #

    def target_met(self, qualified):
        """True once enough qualified leads exist to call the goal achieved."""
        return int(qualified or 0) >= int(self.bounds["target_qualified"])

    def exhausted(self):
        """The stop reason if the run cannot continue, else None. Lets the
        driver check without catching an exception."""
        try:
            self.check_wall_clock()
        except BudgetExceeded as e:
            return e.cap
        if self.find_calls >= self.bounds["max_find_calls"]:
            return TerminalReason.BUDGET_EXHAUSTED
        if self.llm_calls >= self.bounds["max_llm_calls"]:
            return TerminalReason.BUDGET_EXHAUSTED
        if self.estimated_spend_usd >= self.bounds["max_estimated_spend_usd"]:
            return TerminalReason.BUDGET_EXHAUSTED
        if self.agent_turns >= self.bounds["max_agent_turns"]:
            return TerminalReason.MAX_ATTEMPTS
        return None

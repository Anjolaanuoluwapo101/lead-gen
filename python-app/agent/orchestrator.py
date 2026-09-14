"""Drive one agent run to a RECORDED stop.

The model chooses strategy. This module decides when the run ENDS, using
counters the model cannot influence. That split is the entire claim: if the
loop asked the model whether it was finished, the caps would be advice rather
than limits — and advising a model is not bounding it.

Every exit path records a terminal_reason. A run that ends without saying why
is a bug, not a rounding error.
"""

from agent import run_store
from agent.budget import Budget, BudgetExceeded, TerminalReason
from agent.prompts import STRATEGIST_SYSTEM_PROMPT
from agent.tools import RunContext, build_tools

# The `us.` prefix is mandatory on this account: bare model IDs are rejected
# because the model is only reachable via a cross-region inference profile.
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Consecutive turns that change nothing before we call it a dead end. Two, so a
# single turn spent reading existing leads is not mistaken for giving up.
IDLE_TURNS_BEFORE_GIVING_UP = 2


def build_strands_agent(ctx, model_id=None, region_name="us-west-2"):
    """The real model-backed agent.

    Imported lazily so this module — and its tests — do not require strands or
    bedrock_agentcore to be installed. The stop logic below is the part worth
    testing, and it must not be coupled to a 200MB SDK to be runnable.
    """
    from strands import Agent
    from strands.models import BedrockModel

    model = BedrockModel(model_id=model_id or DEFAULT_MODEL_ID,
                         region_name=region_name)
    return Agent(model=model, system_prompt=STRATEGIST_SYSTEM_PROMPT,
                 tools=build_tools(ctx))


class Orchestrator:
    """One run. Construct, then call run().

    agent_factory(ctx) -> callable(prompt) is injectable so the bounds can be
    exercised without a model. That injection is what makes
    test_orchestrator_stop_reasons.py possible at all.
    """

    def __init__(self, *, seller_id, goal, bounds=None, trigger="dashboard",
                 campaign_id=None, agent_factory=None, model_id=None,
                 region_name="us-west-2", cancel_event=None, run_id=None,
                 location_code=None, location_name=None,
                 scrutiny=None, niche=None, single_search=None):
        self.budget = Budget(bounds)
        self.goal = goal
        # The seller's pinned search area from the dashboard picker, if any.
        # In memory only: the runs table has no column for it (no migration),
        # so it is recorded on the trace as a `place` event and read back from
        # there. When absent the agent resolves the place from the goal prose
        # exactly as before.
        try:
            self.location_code = int(location_code) \
                if location_code not in ("", None) else None
        except (TypeError, ValueError):
            self.location_code = None
        if self.location_code is not None and self.location_code <= 0:
            self.location_code = None
        self.location_name = str(location_name or "").strip() or None
        # Run options from the dashboard. Scrutiny is one of strict | balanced
        # | lenient; anything else falls back to the engine default rather
        # than failing the run (the HTTP layer 400s junk first, so this is a
        # backstop, not the validator). Niche overrides the model's reading of
        # the goal when the seller stated one. Single search defaults ON for a
        # pinned run — one area, one search — and stays off otherwise, so older
        # callers keep the multi search behaviour they were built against.
        scr = str(scrutiny or "").strip().lower()
        self.scrutiny = scr if scr in ("strict", "balanced", "lenient") else None
        self.niche = str(niche or "").strip() or None
        if single_search is None:
            single_search = self.location_code is not None
        self.single_search = bool(single_search)
        # Optional threading.Event set by POST /runs/<id>/cancel. The route
        # cannot reach into a running loop, so the loop asks instead — which
        # also keeps cancellation testable without a thread.
        self._cancel = cancel_event
        self._model_id = model_id
        self._region = region_name
        self._factory = agent_factory or (
            lambda ctx: build_strands_agent(ctx, model_id=model_id,
                                            region_name=region_name))

        if run_id:
            # The caller already created the row so it could answer POST /runs
            # with an id before any work started. Creating a second row here
            # would leave an orphan `running` run that never finishes.
            self.run_id = run_id
        else:
            run = run_store.create_run(
                seller_id=seller_id, campaign_id=campaign_id, trigger=trigger,
                goal=goal, bounds=self.budget.bounds)
            self.run_id = run.get("id")
        if not self.run_id:
            # Without a run id there is nothing to attach events or a stop
            # reason to, so refuse rather than run blind and unrecorded.
            raise RuntimeError(
                "could not create a run row — is supabase-schema-agent.sql "
                "applied and Supabase configured?")

        self.ctx = RunContext(self.budget, self.run_id, seller_id, campaign_id)
        self.ctx.pinned_location_code = self.location_code
        self.ctx.pinned_location_name = self.location_name
        self.ctx.scrutiny = self.scrutiny
        self.ctx.niche = self.niche
        self.ctx.single_search = self.single_search
        self._agent = None

    @property
    def agent(self):
        if self._agent is None:
            self._agent = self._factory(self.ctx)
        return self._agent

    # --- the loop ---------------------------------------------------------- #

    def run(self):
        """Drive to a stop and close the run. Never raises for run failures —
        it reports them, because an unrecorded crash is the worst outcome."""
        run_store.append_event(self.run_id, "start", self.goal)
        if self.location_code:
            run_store.append_event(
                self.run_id, "place",
                self.location_name or str(self.location_code),
                {"location_code": self.location_code,
                 "location_name": self.location_name})
        try:
            reason = self._drive()
        except Exception as exc:
            return self._finish(TerminalReason.ERROR,
                                error=f"{type(exc).__name__}: {exc}")
        return self._finish(reason)

    def _cancelled(self):
        """Has this run been asked to stop?

        Two sources, because the run has two possible homes:

          * an in-process Event, set by POST /runs/<id>/cancel in THIS process
            -- the local daemon-thread path, where a round-trip to the
            database would be pure overhead;
          * the `cancel_requested` column, set by a DIFFERENT process -- the
            AgentCore path, where the cancel arrives at Flask but the loop is
            running in us-west-2 and no local object can reach it.

        Same granularity either way: both are checked between turns, so work
        already in flight (an LLM call, a find_leads) still finishes. Cancel
        has never interrupted mid-call, and this does not pretend otherwise.
        """
        if self._cancel is not None:
            return self._cancel.is_set()
        if not self.run_id:
            return False
        return run_store.is_cancel_requested(self.run_id)

    def _drive(self):
        """Loop until a cap, the target, or a dead end. Always returns a reason.

        Order matters: the goal is checked FIRST so a satisfied run never spends
        another credit, and the caps are checked BEFORE the turn is charged so a
        refusal costs nothing.
        """
        idle = 0
        while True:
            # Cancellation is checked FIRST, ahead of the goal test and ahead of
            # any charge: a user who presses stop should not pay for one more
            # turn while we work out that they already got what they wanted.
            if self._cancelled():
                return TerminalReason.USER_CANCELLED

            if self.budget.target_met(self.ctx.qualified):
                return TerminalReason.TARGET_MET

            reason = self.budget.exhausted()
            if reason:
                return reason

            try:
                self.budget.charge_turn()
            except BudgetExceeded as exc:
                return exc.cap

            before = self.ctx.progress_marker()
            run_store.append_event(self.run_id, "turn",
                                   f"turn {self.budget.agent_turns}")
            self.agent(self._prompt())
            run_store.update_progress(self.run_id, self._progress(),
                                      self.budget.estimated_spend_usd)

            if self.ctx.progress_marker() == before:
                idle += 1
                if idle >= IDLE_TURNS_BEFORE_GIVING_UP:
                    return TerminalReason.NO_RESULTS
            else:
                idle = 0

    def _prompt(self):
        """The goal plus the live budget state, so the model can spend what is
        left sensibly. Informational only — it is not what stops the run."""
        b = self.budget.bounds
        prompt = (
            f"{self.goal}\n\n"
            f"Budget: {self.budget.find_calls}/{b['max_find_calls']} searches, "
            f"{self.budget.llm_calls}/{b['max_llm_calls']} LLM calls, "
            f"{self.ctx.qualified}/{b['target_qualified']} qualified leads, "
            f"{self.ctx.drafts} drafts written.\n")
        if self.location_code:
            where = self.location_name or str(self.location_code)
            prompt += (
                f"Pinned search area: {where} "
                f"(location_code {self.location_code}). Pass this code on "
                f"every find_leads call and do not search anywhere else. "
                f"If results are thin, vary the keyword, not the place.\n")
        if self.scrutiny:
            prompt += (
                f"Shortlist strictness: {self.scrutiny}. Judge every business "
                f"at that tier.\n")
        if self.single_search:
            prompt += (
                "Single search mode: this run allows one find_leads call. A "
                "refusal with error single_search is final, not a cap to work "
                "around: draft from what the search returned.\n")
        return prompt + "Continue, or stop and report what you achieved."

    # --- closing ----------------------------------------------------------- #

    def _progress(self):
        """The `progress` jsonb the dashboard polls DURING a run.

        `budget.snapshot()` covers the counters Budget owns. The lead/draft
        counters belong to RunContext, which Budget deliberately knows nothing
        about -- so they are added here, by the one object that holds both.

        Without this the run page's "Qualified" and "Drafts" tiles and its
        progress bar read keys that never existed: snapshot() has no
        `qualified`, `drafts` or `target_qualified`, so those rendered "—" and
        the bar divided by a target of zero. The tiles beside them (Searches,
        LLM calls) worked, which is what made the gap look like styling rather
        than a wrong source. `report` has the same numbers, but report is only
        written when the run ENDS -- a live progress bar cannot read it.
        """
        return {
            **self.budget.snapshot(),
            "qualified": self.ctx.qualified,
            "drafts": self.ctx.drafts,
            "target_qualified": self.budget.bounds["target_qualified"],
            # Every campaign this run created so far, so the run page can list
            # prospects WHILE the run is still going. Report carries the same
            # list, but report only lands at the end; without this the
            # prospects table would sit empty until the run stops, which is
            # exactly when nobody is watching anymore.
            "campaign_ids": list(self.ctx.campaign_ids),
        }

    def _finish(self, reason, error=None):
        status = {TerminalReason.TARGET_MET: "succeeded",
                  TerminalReason.ERROR: "failed"}.get(reason, "stopped")
        progress = self._progress()
        report = self._report(reason)
        run_store.append_event(self.run_id, "stop", reason, progress)
        run_store.finish_run(
            self.run_id, status=status, terminal_reason=reason, report=report,
            error=error, progress=progress,
            estimated_spend_usd=self.budget.estimated_spend_usd)
        return {
            "ok": reason == TerminalReason.TARGET_MET,
            "run_id": self.run_id,
            "status": status,
            "terminal_reason": reason,
            "progress": progress,
            "report": report,
            "error": error,
        }

    def _report(self, reason):
        return {
            "terminal_reason": reason,
            # Engine warnings the run collected (a dev-default niche, a
            # missing provider...). Without these the report can only say what
            # happened, never that the setup was wrong — the exact silence
            # that once scored bakeries as dental clinics with no word
            # anywhere.
            "warnings": list(self.ctx.warnings),
            # `campaign_id` is the LAST campaign touched — kept for compatibility
            # and for "where to read next". `campaign_ids` is every campaign this
            # run created, in order, and is what to use when looking up the run's
            # output: drafts are not guaranteed to live in the last one. A live
            # run drafted into its first campaign and reported its last, empty
            # one, so a caller trusting `campaign_id` alone found no drafts.
            "campaign_id": self.ctx.campaign_id,
            "campaign_ids": list(self.ctx.campaign_ids),
            "qualified": self.ctx.qualified,
            "drafts": self.ctx.drafts,
            "find_calls": self.budget.find_calls,
            "llm_calls": self.budget.llm_calls,
            "estimated_spend_usd": self.budget.estimated_spend_usd,
            "estimate": True,   # never imply this was measured
            "elapsed_s": round(self.budget.elapsed_s, 1),
        }

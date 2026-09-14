-- =====================================================================
-- Lead Engine — AGENT schema (Strands agent on Bedrock AgentCore).
-- Run this in the Supabase SQL editor (Dashboard > SQL Editor > New query >
-- Run). Safe to run more than once: every statement is idempotent.
-- Matches the columns written by python-app/agent/run_store.py.
--
-- Additive only. Nothing here alters campaigns / prospects / leads /
-- seller_profile, so the existing n8n workflows are unaffected.
-- =====================================================================

-- ---------------------------------------------------------------------
-- runs — one row per agent run: the logbook that makes a stop explainable.
-- `terminal_reason` is the answer to "why did it stop?" and is the single
-- most load-bearing field for the demo. Vocabulary (not enforced — see note),
-- kept in sync with the design spec §2.2 and agent/budget.py:
--   target_met | budget_exhausted | max_attempts | timeout | no_results |
--   error | user_cancelled
-- It is deliberately free text: the reason set will grow, and a failed CHECK
-- on insert would break a run mid-demo for no safety gain.
-- ---------------------------------------------------------------------
create table if not exists public.runs (
  id                  uuid primary key default gen_random_uuid(),
  campaign_id         uuid references public.campaigns(id) on delete set null,
  seller_id           uuid references public.seller_profile(id) on delete set null,
  trigger             text not null default 'dashboard',  -- dashboard | agentcore | n8n | manual
  status              text not null default 'running'
                      check (status in ('running','succeeded','failed','stopped')),
  terminal_reason     text,                    -- see vocabulary above; null while running
  goal                text,                    -- the operator's ask, verbatim
  bounds              jsonb not null default '{}',   -- the caps this run was started with
  progress            jsonb not null default '{}',   -- live counters (find_calls, llm_calls, ...)
  -- ESTIMATE, not a measurement: lead_engine reports no cost data at all, so
  -- this is llm_calls * estimated_unit_cost_usd. Label it as an estimate in
  -- any UI or README — presenting it as measured would be false.
  estimated_spend_usd numeric(10,4) not null default 0,
  report              jsonb,                   -- the bounded digest returned to the caller
  error               text,
  -- A cancel REQUEST, not an outcome. The orchestrator reads it between turns,
  -- so a run stopped this way still ends as status='stopped' with
  -- terminal_reason='user_cancelled' -- the same terminal state the in-process
  -- Event produces. It is a column rather than a status value because `status`
  -- is constrained below and has no 'cancelling' member: there was nowhere to
  -- put it, and the request must be visible to a DIFFERENT process when the
  -- run is executing on AgentCore rather than in Flask.
  cancel_requested    boolean not null default false,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  finished_at         timestamptz
);

-- For databases created before cancel_requested existed. `create table if not
-- exists` above is a no-op on an existing table, so this is what actually adds
-- the column there; on a fresh database it is a harmless no-op.
alter table public.runs
  add column if not exists cancel_requested boolean not null default false;

-- ---------------------------------------------------------------------
-- run_events — append-only trace, so the dashboard can show progress by
-- polling instead of holding a streaming connection open.
-- (run_id, seq) is unique: seq is assigned by the writer and makes the
-- trace replayable in order.
-- ---------------------------------------------------------------------
create table if not exists public.run_events (
  id         uuid primary key default gen_random_uuid(),
  run_id     uuid not null references public.runs(id) on delete cascade,
  seq        int  not null,
  kind       text not null,          -- start | find | score | draft | stop | error
  message    text,
  payload    jsonb,
  created_at timestamptz not null default now(),
  constraint run_events_run_seq_key unique (run_id, seq)
);

-- ---------------------------------------------------------------------
-- drafts — emails the agent wrote, awaiting a human decision.
-- The agent NEVER sends. Only an explicit approve action moves a row to
-- 'sending' and then 'sent'.
--
-- Live statuses (draft|approved|sending) hold the "one live draft per lead"
-- slot; 'failed' and 'rejected' release it so a replacement can be written.
-- ---------------------------------------------------------------------
create table if not exists public.drafts (
  id                  uuid primary key default gen_random_uuid(),
  lead_id             uuid not null references public.leads(id) on delete cascade,
  campaign_id         uuid references public.campaigns(id) on delete set null,
  seller_id           uuid references public.seller_profile(id) on delete set null,
  -- The run that WROTE this draft. Nullable: a human can compose a draft from
  -- the dashboard with no agent run behind it, and rows predating this column
  -- keep NULL. It exists because campaign_id is the wrong join for "what did
  -- THIS run produce?" -- a run drafts against leads it found, and the human
  -- route reuses campaigns -- so the run page was showing the seller's whole
  -- draft library under every run. See routes_ui/run.html's draft fetch.
  run_id              uuid references public.runs(id) on delete set null,
  subject             text,
  email_body          text,
  angle               text,                    -- the LLM's stated reason for this pitch
  to_email            text,                    -- resolved at draft time; re-checked before send
  status              text not null default 'draft'
                      check (status in ('draft','approved','sending','sent',
                                        'failed','rejected')),
  revision            int not null default 1,  -- bumped when a human edits the body
  approved_by         text,                    -- Supabase Auth user id / email, once auth lands
  sent_at             timestamptz,
  provider_message_id text,                    -- SES/SMTP id, for reconciling a send
  send_error          text,
  -- What was actually attached, and by which provider. Recorded per draft
  -- rather than read back off the seller at display time: a seller may replace
  -- their resume after a send, and this row must still say what went out.
  attachment_key      text,                    -- object key in file_store
  attachment_name     text,                    -- filename the recipient saw
  send_backend        text,                    -- none | ses | smtp, as configured
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

-- For databases created before drafts.run_id existed. Must run BEFORE the
-- index block below: `create index ... on drafts(run_id)` fails outright if the
-- column is not there yet, and on an existing table the `create table if not
-- exists` above is a no-op that adds nothing. Existing rows keep NULL, which is
-- correct -- we cannot know after the fact which run wrote them, and a guessed
-- run id is worse than an absent one.
alter table public.drafts
  add column if not exists run_id uuid references public.runs(id) on delete set null;

-- For databases created before sending existed. `attachment_key` is where the
-- seller's resume FILE lives (see file_store.py), recorded on the draft so the
-- audit trail says what went out with which message -- the seller's resume can
-- change after a send, and "what was attached" has to stay answerable.
-- `send_backend` records which provider carried it, because the provider is
-- swappable and a row that does not say is a row that cannot be reconciled.
alter table public.drafts
  add column if not exists attachment_key  text,
  add column if not exists attachment_name text,
  add column if not exists send_backend    text;

-- ---------------------------------------------------------------------
-- Indexes — common reads only.
-- ---------------------------------------------------------------------
create index if not exists idx_runs_campaign      on public.runs(campaign_id);
create index if not exists idx_runs_seller_recent on public.runs(seller_id, created_at desc);
create index if not exists idx_run_events_run     on public.run_events(run_id, seq);
create index if not exists idx_drafts_campaign    on public.drafts(campaign_id, status);
create index if not exists idx_drafts_seller      on public.drafts(seller_id, status);
-- The run page's read: "the drafts this run produced."
create index if not exists idx_drafts_run         on public.drafts(run_id);

-- One LIVE draft per lead: never two competing drafts awaiting review.
-- Partial, so the historical rows (sent/rejected/failed) don't collide.
create unique index if not exists idx_drafts_lead_live
  on public.drafts(lead_id)
  where status in ('draft','approved','sending');

-- Backstop against double-send: at most one 'sent' row per lead. This is the
-- database refusing to let the same business be emailed twice, independent of
-- whatever the application logic believes.
create unique index if not exists idx_drafts_sent_once
  on public.drafts(lead_id)
  where status = 'sent';

-- ---------------------------------------------------------------------
-- Row Level Security — defence in depth on the three NEW tables.
--
-- The app talks to Supabase with the SERVICE-ROLE key, which bypasses RLS
-- entirely, so enabling this changes nothing for the app. It only closes the
-- gap where the anon/authenticated key could otherwise read or write these
-- tables directly over PostgREST. No policies are defined, which means
-- deny-all for anything that is not service-role.
--
-- CAVEAT: if the dashboard ever reads these tables from the browser with an
-- anon key, it will get zero rows until a policy is added. Today the
-- dashboard is served by Flask (service-role), so this is safe.
-- ---------------------------------------------------------------------
alter table public.runs       enable row level security;
alter table public.run_events enable row level security;
alter table public.drafts     enable row level security;

-- ---------------------------------------------------------------------
-- append_run_event — one-round-trip trace append (Phase 2.1).
--
-- run_store.append_event prefers this over read-max-then-insert (two round
-- trips per event). The database assigns max(seq)+1 inside the INSERT, so a
-- poll-heavy run halves its trace writes. Race semantics are UNCHANGED from
-- the old path: one writer per run by design, and an interleaved second
-- writer still hits the (run_id, seq) unique constraint loudly.
--
-- The application treats a missing function as "not migrated yet" and falls
-- back to the legacy path, so applying this is optional and never a flag
-- day: run this block in the Supabase SQL editor (or re-run this whole
-- file; every statement is IF NOT EXISTS / OR REPLACE safe) and the faster
-- path activates on its own. Nothing here runs itself.
-- ---------------------------------------------------------------------
create or replace function public.append_run_event(
  p_run_id uuid, p_kind text, p_message text, p_payload jsonb)
returns table (id uuid, run_id uuid, seq integer, kind text, message text,
               payload jsonb, created_at timestamptz)
language sql as $$
  insert into public.run_events(run_id, seq, kind, message, payload)
  select p_run_id, coalesce(max(seq), 0) + 1, p_kind, p_message, p_payload
  from public.run_events where run_id = p_run_id
  returning id, run_id, seq, kind, message, payload, created_at;
$$;

-- ---------------------------------------------------------------------
-- Quick sanity check — expect 3 empty tables.
-- ---------------------------------------------------------------------
-- select 'runs' as t, count(*) from public.runs
-- union all select 'run_events', count(*) from public.run_events
-- union all select 'drafts',     count(*) from public.drafts;

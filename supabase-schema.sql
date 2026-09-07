-- =====================================================================
-- Lead Engine schema — run this in the Supabase SQL editor (Dashboard >
-- SQL Editor > New query > Run). Safe to run once.
-- Matches the columns written by python-app/lead_engine.py + supabase_store.py.
-- =====================================================================

-- One config per agency/client niche. Location + keyword are the run inputs.
create table if not exists public.campaigns (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  keyword     text not null,               -- e.g. 'dentist'
  location    text not null,               -- e.g. 'Austin, TX'
  niche_rules jsonb default '{}',          -- niche description / scoring hints
  target_key  text not null default '',    -- sha1(keyword|location|niche): stable per-target id (re-runs reuse this campaign)
  status      text default 'active',       -- config-object status
  active      boolean default true,
  created_at  timestamptz default now()
);

-- Every business the finder returned (even unqualified -> audit trail).
create table if not exists public.prospects (
  id           uuid primary key default gen_random_uuid(),
  campaign_id  uuid references public.campaigns(id) on delete cascade,
  source       text,                        -- 'dataforseo' | 'google_maps' ...
  business_name text,
  category     text,
  phone        text,
  website      text,
  street text, locality text, region text, zipcode text,
  rating numeric, review_count int,
  place_id     text,
  maps_url     text,
  listing_url  text,
  raw_payload  jsonb,                       -- full row from the finder
  -- Phase 2: lifecycle stage + dedup bookkeeping.
  status        text default 'discovered',  -- discovered|validating|presence_checked|enriched|scored|qualified|lead|dismissed|duplicate
  dedup_key     text,                       -- identity fingerprint (see identity.py)
  duplicate_of  uuid,                       -- when a re-run re-sees a business, points at the canonical prospect
  created_at   timestamptz default now()
);

-- One row per enriched + scored business; links back to its prospect.
create table if not exists public.leads (
  id           uuid primary key default gen_random_uuid(),
  prospect_id  uuid references public.prospects(id) on delete cascade,
  campaign_id  uuid references public.campaigns(id) on delete cascade,
  emails       jsonb default '[]',          -- from /email
  emails_extra text[],                      -- phones found on site
  intelligence jsonb default '{}',          -- merged /email intelligence
  score        int not null default 0,      -- retained = opportunity (back-compat)
  qualified    boolean not null default false,
  weakness     text,                        -- why this lead fits the niche
  first_line   text,                        -- optional outreach opener
  -- Phase 1: split score into opportunity + confidence, plus the evidence.
  digital_presence  text,                   -- OFF_GRID|SOCIAL_ONLY|WEBSITE_ONLY|WEBSITE_AND_SOCIAL|STRONG_DIGITAL_PRESENCE
  opportunity_score int,                    -- how valuable a prospect (LLM, evidence-based)
  confidence_score  int,                    -- how sure we can be (clamped to data quality)
  score_breakdown   jsonb,                  -- deterministic evidence sub-scores + signals
  status       text default 'new',          -- new | contacted | replied | won | lost
  created_at   timestamptz default now()
);

-- Phase 1 migration for databases created before these columns existed
-- (idempotent -- safe to run on every deploy / schema refresh).
alter table public.leads
  add column if not exists digital_presence  text,
  add column if not exists opportunity_score int,
  add column if not exists confidence_score  int,
  add column if not exists score_breakdown   jsonb;

-- Phase 3 -- namespaced corroboration/recency evidence (idempotent, nullable).
-- These let an operator filter "corroborated across N channels" / "active
-- recently" without parsing score_breakdown jsonb. They are additive: they never
-- rewrite opportunity_score / confidence_score or the confidence_cap.
alter table public.leads
  add column if not exists source_agreement int,   -- 0-100 multi-source strength
  add column if not exists last_active_at  timestamptz;  -- best-known activity date (often null)

-- ---------------------------------------------------------------------------
-- Phase 2 -- campaign-as-config + prospect lifecycle + dedup (idempotent).
-- Partial unique index (WHERE target_key <> '') so historical rows that predate
-- target_key (all blank) never collide with real per-target keys.
-- ---------------------------------------------------------------------------
alter table public.campaigns
  add column if not exists target_key text not null default '',
  add column if not exists status     text default 'active';

alter table public.prospects
  add column if not exists status       text default 'discovered',
  add column if not exists dedup_key    text,
  add column if not exists duplicate_of uuid;

create unique index if not exists idx_campaigns_target
  on public.campaigns(target_key) where target_key <> '';
create index if not exists idx_prospects_campaign_status
  on public.prospects(campaign_id, status);
create index if not exists idx_prospects_duplicate_of
  on public.prospects(duplicate_of);

-- Helpful indexes for the common reads (leads by campaign, qualified first).
create index if not exists idx_prospects_campaign on public.prospects(campaign_id);
create index if not exists idx_leads_campaign    on public.leads(campaign_id);
create index if not exists idx_leads_qualified   on public.leads(campaign_id, qualified);
create index if not exists idx_leads_opportunity on public.leads(campaign_id, opportunity_score desc);

-- Quick sanity check — expect 3 empty tables.
select 'campaigns' as t, count(*) from public.campaigns
union all select 'prospects', count(*) from public.prospects
union all select 'leads', count(*) from public.leads;

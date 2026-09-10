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

-- =====================================================================
-- Campaign summary -- efficient server-side aggregation.
-- Backs POST /campaigns/summary so the engine never pulls the whole leads
-- table just to tally counts. Run this block in the Supabase SQL editor.
-- =====================================================================
create or replace function public.campaign_summary(p_campaign_id uuid default null)
returns table (
  campaign_id      uuid,
  name             text,
  keyword          text,
  location         text,
  niche            text,
  created_at       timestamptz,
  total_leads      bigint,
  qualified        bigint,
  ready            bigint,
  status_new       bigint,
  status_contacted bigint,
  status_replied   bigint,
  status_won       bigint,
  status_lost      bigint
)
language sql
stable
as $$
  select
    c.id,
    c.name,
    c.keyword,
    c.location,
    c.niche_rules ->> 'niche',
    c.created_at,
    count(l.id),
    count(l.id) filter (where l.qualified),
    count(l.id) filter (where l.status = 'new'),
    count(l.id) filter (where l.status = 'new'),
    count(l.id) filter (where l.status = 'contacted'),
    count(l.id) filter (where l.status = 'replied'),
    count(l.id) filter (where l.status = 'won'),
    count(l.id) filter (where l.status = 'lost')
  from public.campaigns c
  left join public.leads l on l.campaign_id = c.id
  where p_campaign_id is null or c.id = p_campaign_id
  group by c.id
  order by c.created_at desc;
$$;

grant execute on function public.campaign_summary(uuid) to anon, authenticated, service_role;

-- =====================================================================
-- Phase 4 -- multi-tenant sellers (see docs/multi-tenant-plan.md).
-- One row per seller. Campaigns belong to a seller via campaigns.seller_id;
-- leads/prospects inherit the seller through their campaign (no column needed
-- on them). Run this whole block in the Supabase SQL editor.
-- =====================================================================

create table if not exists public.seller_profile (
  id           uuid primary key default gen_random_uuid(),
  email        text not null,                 -- natural key -> find-or-create from n8n form
  name         text,                          -- person's display name (used in draft signature)
  title        text,                          -- e.g. 'Founder', 'Sales Director'
  brand        text,                          -- business/sender brand the emails go out as
  phone        text,
  -- Resume (uploaded by the seller). We store the EXTRACTED text, not the file.
  resume_text     text,
  resume_filename text,
  -- Portfolio website. We store the scraped/extracted output.
  portfolio_url   text,
  portfolio_text  text,
  -- Tri-state render toggle: html (static only) | js (always JS-render) | auto (detect).
  -- Defaults to 'auto'; set explicitly at seller creation from the web form.
  render_mode   text not null default 'auto'
                check (render_mode in ('html','js','auto')),
  -- Per-seller provider creds + behaviour overrides that today live in env
  -- (see env tiers in the plan doc). Empty object = fall back to env defaults.
  -- e.g. {"groq": {...}, "dataforseo": {...}, "scrutiny": {...}, "confidence_floor": ...}
  settings      jsonb not null default '{}',
  active        boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint seller_profile_email_key unique (email)
);

-- Give every existing + new campaign an owning seller (nullable now so old
-- rows keep working; the engine falls back to DEFAULT_SELLER_ID when null).
alter table public.campaigns
  add column if not exists seller_id uuid references public.seller_profile(id);

create index if not exists idx_campaigns_seller on public.campaigns(seller_id);
create index if not exists idx_seller_profile_email on public.seller_profile(email);

-- =====================================================================
-- Seller read-scoping: campaign_summary now also scopes to ONE seller.
-- Adds a second (uuid) overload; keep the single-arg one above for any
-- existing callers. Backs POST /campaigns/summary {seller_id, campaign_id?}.
-- Run this block in the Supabase SQL editor.
-- =====================================================================
create or replace function public.campaign_summary(
  p_campaign_id uuid default null,
  p_seller_id   uuid default null
)
returns table (
  campaign_id      uuid,
  name             text,
  keyword          text,
  location         text,
  niche            text,
  created_at       timestamptz,
  total_leads      bigint,
  qualified        bigint,
  ready            bigint,
  status_new       bigint,
  status_contacted bigint,
  status_replied   bigint,
  status_won       bigint,
  status_lost      bigint
)
language sql
stable
as $$
  select
    c.id,
    c.name,
    c.keyword,
    c.location,
    c.niche_rules ->> 'niche',
    c.created_at,
    count(l.id),
    count(l.id) filter (where l.qualified),
    count(l.id) filter (where l.status = 'new'),
    count(l.id) filter (where l.status = 'new'),
    count(l.id) filter (where l.status = 'contacted'),
    count(l.id) filter (where l.status = 'replied'),
    count(l.id) filter (where l.status = 'won'),
    count(l.id) filter (where l.status = 'lost')
  from public.campaigns c
  left join public.leads l on l.campaign_id = c.id
  where (p_campaign_id is null or c.id = p_campaign_id)
    and (p_seller_id is null or c.seller_id = p_seller_id)
  group by c.id
  order by c.created_at desc;
$$;

grant execute on function public.campaign_summary(uuid, uuid) to anon, authenticated, service_role;

-- ---------------------------------------------------------------------
-- Optional, recommended backfill: claim legacy campaigns that were created
-- before seller_id existed (their owner column is NULL) for the DEFAULT
-- single-tenant seller, so the seller-scoped reads below still surface them.
-- Set <DEFAULT_SELLER_ID> to the value in your .env, then run.
-- ---------------------------------------------------------------------
-- update public.campaigns
--    set seller_id = '<DEFAULT_SELLER_ID>'
--  where seller_id is null;

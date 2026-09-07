# Lead Engine — Product Architecture (Brainstorm, Rebuilt)

> Rebuilt from the original brainstorm session. This is the *theoretical* design
> doc for how the lead-generation product is intended to work. It is product-first,
> not node-first: we define what a sellable "qualified lead" is and the data shape
> behind it. Every n8n workflow we build later simply feeds this schema.

---

## 1. The vision

A **configurable lead-generation engine** an operator (agency or B2B seller) uses to
produce lists of businesses that *look like* they need a service — enriched,
reachable, and scored — rather than raw, unsorted business names.

The same engine serves many niches. Location and keyword are always customer inputs.
We target **US businesses first**, price in **USD**, and stay **region-agnostic** by
design (a future customer in the UK/elsewhere should work too, with the right proxy).

### The core product truth
> Agencies don't pay for *lists*. They pay for leads that are **enriched**,
> **scored**, and **reachable** — prospects already likely to need their client's
> service. The enrichment + scoring is the moat. The finders are commodities.

Our existing `email_scraper.py` already extracts the signals that power enrichment
and scoring (CMS platform, social links, phones, nav-link intent, schema.org data).
That is the seed of the valuable product, not the YellowPages/Google Maps finders.

---

## 2. Target customer (who we sell to)

Primary personas, both consuming the same engine:

1. **Lead-gen agencies / resellers** — run lead-gen for many client niches. Need
   multi-niche, per-client configuration, white-label output.
2. **B2B sellers targeting local businesses** — sell web design, SEO, booking
   software, payment processing, etc., to local businesses. Want a list of
   businesses that *lack* the thing they sell (old site, no socials, no online
   booking) plus contact info to pitch.

### The opportunity in one line
Give a niche-seller a weekly list of N businesses that **already have a
weakness-fitting-problem signal** + verified phone/email/website, so their outreach
converts instead of spam-crashing.

---

## 3. The "Qualified Lead" definition (the core)

A lead is *qualified* when it passes a configurable score threshold. Scoring inputs
(per customer's niche), examples:

| Signal | Example rule | Source |
|--------|-------------|--------|
| Fit (is this the right kind of business?) | category ∈ target set | maps/yp `category` |
| Has a website | website present | maps/yp `business_page` |
| **Weakness signal** | site CMS is outdated, no socials, no booking/nav intent | `/email` intelligence |
| Reachable | phone OR email present | maps/yp + `/email` |
| Freshness | recently active / not a dead listing | listing presence |

A lead row is not "good" until it carries: contact, a website URL, and enough
intelligence to write a *personalized first line*. Everything else is a prospect
record, not a qualified lead.

---

## 4. The pipeline (find → enrich → score → store → deliver)

```
            ┌─────────────────────────────────────────────────────────┐
 CUSTOMER   │  CONFIG:  service/keyword  +  location  +  niche rules  │
 INPUT      └─────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────▼──────────────────────┐
  1 FIND│  Google Maps  (maps engine)                │ business list
        │  YellowPages   (US, proxy required)        │ name/category/phone/web
        └─────────────────────┬──────────────────────┘
                              │ each business has a website URL
        ┌─────────────────────▼──────────────────────┐
  2 ENRICH│ email engine per site  (/email)          │ emails + phones +
        │ crawl site, extract intelligence           │ CMS + socials +
        │                                            │ nav-intent signals
        └─────────────────────┬──────────────────────┘
                              │
        ┌─────────────────────▼──────────────────────┐
  3 SCORE│ rule / LLM pass                            │ qualified = True/False
        │ "does this site show the weakness we sell  │ + score + reason
        │  a fix for?" (old CMS, no socials, no      │ + personalized first line
        │  booking, thin content…)                   │
        └─────────────────────┬──────────────────────┘
                              │
        ┌─────────────────────▼──────────────────────┐
  4 STORE│ Supabase: leads table + raw scrapes       │
        └─────────────────────┬──────────────────────┘
                              │
        ┌─────────────────────▼──────────────────────┐
  5 DELIVER│ scheduled report / web dashboard / CSV  │ agency-readable output
           │ export → customer outreach              │
        └────────────────────────────────────────────┘
```

### Step-by-step responsibilities
1. **Find** — produce candidate businesses for `keyword in place`. Cheap, wide net.
   Engine: `/googlemaps` primary, `/yellowpages` secondary (US-only, proxy).
2. **Enrich** — for each candidate with a website, run `/email` on the homepage to
   pull emails, phones, socials, CMS, nav intent, schema.org. Narrows reachable set.
3. **Score** — keep only leads matching the niche weakness. Optional LLM pass writes
   a personalized outreach first-line using the intelligence.
4. **Store** — persist qualified leads + scoring reasons (audit / re-run-ability).
5. **Deliver** — surface as a client-facing artifact (dashboard/CSV/email), NOT raw
   scrapes.

---

## 5. How the existing code maps to each stage

| Existing engine | File | Stage | Notes / gaps |
|-----------------|------|-------|--------------|
| Flask HTTP wrapper | `python-app/app.py` | all | `POST /yellowpages`, `/googlemaps`, `/places`, `/email`. Internal only (127.0.0.1:5000). |
| Google Maps finder | `python-app/google_maps.py` | FIND | richest dataset; Selenium/Chromium; **LOCAL-DEV ONLY** — headless scraping needs a one-time human consent pass on a residential IP; not hostable as a service |
| **DataForSEO Maps finder** | `python-app/dataforseo.py` | FIND | **hostable replacement.** Licensed Google Maps data via API. No browser/consent. `POST /places`. ~$0.002/call. See §5b. |
| YellowPages finder | `python-app/yellow_pages.py` | FIND | US-only traffic; **requires US proxy** from non-US IP |
| Website intelligence | `python-app/email_scraper.py` | ENRICH | emails + content-intelligence (CMS/socials/nav/jsonld/phones). **Core asset.** |
| Anti-blocking layer | shared in scrapers | all | curl_cffi TLS impersonation → cloudscraper → requests; proxy rotation; retries. |

**Gaps (not yet built):**
- No n8n workflow exists / was never confirmed.
- No Supabase schema / integration written.
- No scoring rules or LLM enrichment node.
- No delivery / customer-facing layer.
- No config-per-customer model (multi-tenant).

### §5b — Hostable FIND (how the product actually ships)

`google_maps.py` (Selenium) is fine for local dev but **cannot be the hosted backbone**: Google shows a consent/CAPTCHA wall to fresh/headless/cloud-IP browsers, and clearing it needs a human + a residential IP that don't exist in a container. So the shipped product uses a **licensed data source for FIND**:

- **`dataforseo.py` + `POST /places`** hits DataForSEO's Google Maps live endpoint (`/v3/serp/google/maps/live/advanced`) with `keyword` + a location name. It returns the **same row shape as `google_maps.py`** (name, phone, website, category, rating, review_count, address, place_id, hours), so the n8n pipeline / downstream code doesn't care which finder ran.
- Auth is DataForSEO account `login`+`password` via HTTP Basic, read from `.env` (`DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD`); `.env.example` is committed.
- Cost ~$0.002/call on prepaid credits (no card required to start; PayPal accepted; credits never expire).
- Why this over scraping Google: **legal, stable, runs in any container** — it de-risks the one commodity step and leaves the moat (enrichment + scoring + delivery) intact.

**Result:** a customer's n8n workflow points at `/places` (hostable) or `/googlemaps` (local dev) — identical downstream behavior.

---

## 6. Supabase schema (proposed)

```sql
-- One config per agency/client niche. Location + keyword are inputs.
create table campaigns (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid,              -- agency/reseller user
  name        text not null,
  keyword     text not null,     -- e.g. 'dentist'
  location    text not null,     -- e.g. 'Austin, TX'
  niche_rules jsonb default '{}',-- scoring thresholds / weakness signals
  active      boolean default true,
  created_at  timestamptz default now()
);

-- Every business the finder returned (even unqualified → audit trail).
create table prospects (
  id           uuid primary key default gen_random_uuid(),
  campaign_id  uuid references campaigns(id) on delete cascade,
  source       text,             -- 'google_maps' | 'yellowpages'
  business_name text,
  category     text,
  phone        text,
  website      text,
  street, locality, region, zipcode text,
  rating numeric, review_count int,
  listing_url  text,
  maps_url     text,
  place_id     text,
  raw_payload  jsonb,            -- full row from the engine
  created_at   timestamptz default now()
);

-- One row per qualified lead; a prospect can qualify for many campaigns later.
create table leads (
  id           uuid primary key default gen_random_uuid(),
  prospect_id  uuid references prospects(id) on delete cascade,
  campaign_id  uuid references campaigns(id) on delete cascade,
  emails       jsonb default '[]',          -- from /email
  emails_extra text[],                      -- phones found on site
  intelligence jsonb default '{}',          -- merged /email intelligence
  score        int not null default 0,
  qualified    boolean not null default false,
  weakness     text,                        -- why this lead fits the niche
  first_line   text,                        -- optional LLM outreach opener
  status       text default 'new',          -- new | contacted | replied | won | lost
  created_at   timestamptz default now()
);
```

Key idea: **prospects** (raw finder output) and **leads** (qualified, enriched) are
separate tables. This keeps the cheap raw net separate from the valuable scored
subset, and lets re-scoring without losing history.

---

## 7. n8n workflow blueprint (to build later)

```
[ Schedule / Manual / Webhook trigger ]
        │
        ▼
[ Set campaign config (keyword, location, API url/port) ]
        │
        ▼
[ HTTP POST → /googlemaps ]  ──►  [ Split/loop over rows with a website ]
        │                                      │
        ▼                                      ▼ each site
[ (optional) HTTP POST → /yellowpages ]   [ HTTP POST → /email (enrich) ]
                                                     │
                                                     ▼
                                        [ Merge enriched intelligence into row ]
                                                     │
                                                     ▼
                                        [ Score rows (rules + optional LLM node) ]
                                                     │
                                                     ▼
                                        [ Insert into Supabase: prospects + leads ]
                                                     │
                                                     ▼
                                        [ Generate report / notify agency ]
```

Notes for when we build it:
- Flask binds `127.0.0.1:5000`; n8n is in the same container, so call
  `http://127.0.0.1:5000/...` directly. Confirm Flask starts (see gap: port 7860 vs 5000).
- `/email` is slow (crawl + sleeps) — batch and keep `max_depth` small; use n8n's
  queue/batching so one slow enrichment doesn't block the whole campaign.
- Always pass a region-appropriate proxy for geo-blocked sources (esp. YellowPages).

---

## 8. Compliance & risk (read before commercializing)

- **Google Maps & YellowPages scraping violates their ToS.** Data resale also has
  legal exposure (GDPR in EU, CAN-SPAM in US for outreach).
- Engine is a legitimate B2B market-research tool; but a *commercial* product
  reselling scraped contact data needs care: use consented/opted-in outreach,
  restrict to business contacts, honor opt-outs, consider a compliant data layer.
- Recommendation: design the *delivery* product as "qualified business-market
  research you use to run your own outreach," not "bulk contact lists to sell."

---

## 9. Open decisions (to resolve as we build)

1. **Scoring engine:** pure n8n rules vs. an LLM node vs. both (LLM for
   weakness-detection + first-line, rules for pass/fail gates). Cost tradeoff.
2. **Delivery surface:** what does the agency actually open? A web dashboard, a
   CSV, an emailed report? (Decides the last pipeline stage's shape.)
3. **Multi-tenant config:** how per-campaign config reaches n8n (Supabase rows →
   webhook → workflow).
4. **Finder choice:** `/places` (DataForSEO, hostable) vs `/googlemaps` (local
   dev only). Product ships on `/places`; cost per lead needs a real-world number.
5. **Legal posture:** how we position and market the product (see §8). `/places`
   is licensed; scraped finders are not.

---

## 10. Next steps

1. **Finder is proven** — `/googlemaps` works locally (with a one-time consent
   pass); `/places` works headlessly/hostably via DataForSEO. ✅ (2026-09)
2. **Stand up Supabase** schema (§6) — the contracts everything else writes to.
3. **Build one end-to-end n8n workflow** (places → enrich → score → store →
   export) for a single demo niche, e.g. *dentists with no online booking in
   Austin, TX*. Workflow calls `/places` for FIND.
4. Pick the delivery surface + first customer/pilot for the demo niche.

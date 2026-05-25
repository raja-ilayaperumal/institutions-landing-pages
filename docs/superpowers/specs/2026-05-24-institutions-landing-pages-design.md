# Clema Institutions Landing Pages — Data Pipeline Design

**Status:** Draft v1.1 — pending user approval (research funding bundle added 2026-05-24)
**Date:** 2026-05-24
**Owner:** Raja (clema GTM)
**Scope:** Data pipeline only. A sibling project will render the actual landing pages by consuming `landing.mv_institution_page`.

---

## 1. Goal

Build a robust, reproducible scraping system that fills a new `landing.*` schema in the existing `ipeds` Postgres database with everything a consumer frontend needs to render one rich SEO landing page per institution (~6,072 institutions today; the SEO research targets the full set). The pipeline must:

- Take public, free, legal data sources.
- Discover the right URL per `(institution, source_type)` automatically — institutional websites are inconsistent; we cannot hand-curate 6k × 9 source URLs.
- Extract typed, validated rows with full provenance (source URL, fetched-at, parser version, raw payload pointer, confidence).
- Store raw fetched bytes so we can re-parse without re-fetching when parsers improve.
- Run end-to-end on 5 pilot institutions first, then scale to all 6k+ using the same code path.

## 2. Non-goals (deferred or owned elsewhere)

- Page rendering / Next.js / Astro / HTML output — owned by a sibling Clema project.
- IR Person directory (~20k pages), Association pages, Author pages — future brainstorms, not this spec.
- Real-time data; pipeline is batch (initial full crawl + scheduled refreshes).
- Authentication, paywalled sources, scraping behind logins.
- Long-tail manual overrides for individual institutions (we'll allow them via DB inserts but not build UI).

## 3. Inputs we already have

| Asset | What it provides |
|---|---|
| `ipeds` schema (242 tables, 2012-2025) | Institution directory, enrollment, completions, graduation, finance, staff, salaries, derived `drv_*` views |
| `score_card.fact_institution_yearly` | admission rate, SAT/ACT percentiles, demographics, **net price by income band** (matches Block 02 exactly), Pell rate, completion rate, **earnings 6/8/10yr median+mean**, debt, repayment |
| `pseo.fact_earnings` / `fact_flows` | Post-secondary employment outcomes by state |
| `ipeds_data_dictionary.*` | Variable labels, value codes, surveys (turns integer codes into human strings) |
| `assets/all-institutions.csv` | 6,050 active postsec institutions (unitid, name, state, type, year) — pilot seed |
| `assets/seo-research/*.docx` | URL structure, schema.org targets, 12 page-block definitions, meta templates, keyword priorities |

**Implication:** Blocks 01–07 of the SEO page spec (admissions, tuition/cost, graduation, enrollment/demographics, programs, faculty, peers) are **almost entirely covered by existing DB tables**. New scraping is only needed for: Block 00 logo/brand, Block 08 notable alumni, Block 09 IR office/team, Block 10 CDS link, Block 11 jobs, plus documents library (factbook, strategic plan, glossary, data dictionary). A **new research-funding block** (NSF HERD R&D expenditures + federal awards from NIH/NSF/USAspending) is added to v1 beyond the SEO doc's original 12 blocks, since it's critical for R1/R2 institutions and not derivable from existing IPEDS/Scorecard data.

## 4. Architecture

### 4.1 Four-stage pipeline per `(unitid, source_type)`

```
DISCOVER  →  VALIDATE  →  EXTRACT  →  STORE
```

| Stage | Job | Inputs | Outputs | Tech |
|---|---|---|---|---|
| **1. Discover** | Find candidate URL(s) for this source | institution root, source-type config | rows in `landing.discovered_urls` (with `relevance_score`, `discovery_method`) | API call (BrandFetch, Wikipedia REST), or `site:webaddr` search (SerpAPI / Google CSE / DDG), or `crawl4ai` `BestFirstCrawlingStrategy` with `KeywordRelevanceScorer` + `FilterChain` |
| **2. Validate** | Pick the ONE canonical URL, reject false positives | candidates from stage 1 | one current row in `landing.canonical_urls` (with `confidence`, `llm_reasoning`) | Claude Haiku LLM-as-judge with cached system prompt |
| **3. Extract** | Pull typed rows from canonical URL | canonical URL | raw bytes in `landing.raw_payloads`, typed rows in per-source content table | `crawl4ai` `AsyncWebCrawler` + `LLMExtractionStrategy` w/ Pydantic schema for unstructured, rule-based parsers for structured (API JSON, RSS, CSV, PDF text) |
| **4. Store** | Write typed rows + refresh the page view | extract output | rows in `landing.<source>` table; eventually `landing.mv_institution_page` refresh | SQLAlchemy via `ProvenanceWriter` |

Each stage is a Celery task. Failure at any stage is isolated and retried independently. The `run_id` (UUID) ties all stage rows together for a single pipeline run.

### 4.2 Per-source Celery queues

| Queue | Concurrency | Rate limit | Sources |
|---|---|---|---|
| `q_api` | 20 | source-specific | brandfetch, wikipedia, scorecard refresh, research_herd, research_nih, research_nsf, research_usaspending |
| `q_search` | 5 | 1 req/sec/domain | site: search providers |
| `q_crawl` | 4 | 1 req/2 sec/domain | crawl4ai deep crawls of institution sites |
| `q_extract_llm` | 8 | Anthropic rate | LLM extraction & judge calls |
| `q_pdf` | 4 | n/a | PDF download + text extraction |

Queue assignment lives in `landing.source_types.default_queue`. Override per task via Celery's `task_routes`.

### 4.3 Pilot vs full crawl

Pilot is **not** a separate code path. It's a `--pilot` flag on the orchestrator CLI that filters seed work to `landing.institutions WHERE pilot_cohort = TRUE`. The 5 pilot institutions are marked at seed time. After pilot acceptance, drop the flag and the same Celery tasks run against all 6k.

## 5. Database schema (`landing.*` in `ipeds` DB)

Full DDL in section 9 below. Structure:

- **Group A — Master & lookups**: `institutions` (thin master, FK to `ipeds.institutions_2024`), `source_types`
- **Group B — Pipeline operational**: `discovered_urls`, `canonical_urls`, `raw_payloads`, `scrape_runs`
- **Group C — Content tables (one per source/page block)**: `brand_assets`, `wiki_summaries`, `ir_offices`, `ir_contacts`, `documents`, `cds_links`, `jobs`, `notable_alumni`, `peer_groups`, `research_expenditures`, `research_awards`, plus phase-2 stubs (`grants`, `rfps`, `accreditation_dapip`, `athletics_eada`, `cdr`)
- **Group D — Page-ready views**: `mv_research_funding_summary` (per-institution rollup of `research_awards`) and `mv_institution_page` (one row per unitid aggregating IPEDS + score_card + all landing content as JSONB arrays). Migration order matters — `mv_research_funding_summary` must exist before `mv_institution_page` joins it.

Three universal rules:

1. **Master is thin** — `landing.institutions` stores only what IPEDS doesn't (slug, normalized URL, robots, pilot flag, enabled flag). Everything else joined live in the materialized view.
2. **Provenance footer on every content row** — `source_url`, `fetched_at`, `raw_payload_id`, `parser_version`, `confidence`. Enforced by `ProvenanceWriter` (the only allowed write path).
3. **Algorithm versioning on derived data** — `peer_groups.algorithm` + `algorithm_version` are part of the PK so multiple peer algorithms can coexist.

## 6. Source types and connectors (v1 scope)

| source_type | Stage 1 discover | Stage 3 extract | Table(s) written | Page blocks served |
|---|---|---|---|---|
| `brandfetch` | API call: `brandfetch.com/{domain}` | JSON → typed | `brand_assets` | Block 00 hero |
| `wikipedia` | REST search by institution name → page title | REST summary + infobox parse | `wiki_summaries`, `notable_alumni` | Block 00 about, Block 08 alumni |
| `ir_page` | crawl4ai BFS from root, scored on `["institutional research","office of ir","ir office"]` | LLM judge → page summary | `ir_offices` | Block 09 |
| `ir_team` | Inherit ir_page URL → BFS one hop scored on `["staff","team","people","director","analyst"]` | LLM extract `[{name, title, email, phone}]` | `ir_contacts` | Block 09 |
| `factbook` | site search `site:{webaddr} factbook` + crawl4ai PDF link discovery | PDF download → text extract → store | `documents` (`doc_type='factbook'`) | (linked from page) |
| `cds` | site search `site:{webaddr} "common data set"` + year regex | URL only, no body extract | `cds_links` | Block 10 |
| `strategic_plan` | site search `site:{webaddr} "strategic plan"` | PDF download + LLM 3-sentence summary | `documents` (`doc_type='strategic_plan'`) | (linked from page) |
| `jobs` | HigherEdJobs API + institution careers page crawl | LLM categorize `ir` vs `other` | `jobs` | Block 11 |
| `peers` | DB-only — nearest neighbors by Carnegie + state + control (algorithm v1: `carnegie_state_control_v1`) | n/a (algorithm) | `peer_groups` | Block 07 |
| `research_herd` | NSF HERD CSV download | CSV parse → typed row per (unitid, fiscal_year) | `research_expenditures` | Research block (new) |
| `research_nih` | NIH RePORTER POST API filtered by `org_name`/`uei` | JSON → award rows | `research_awards` (source_system='nih_reporter') | Research block |
| `research_nsf` | NSF Awards Search API filtered by `AwardeeName`/`AwardeeUEI` | JSON → award rows | `research_awards` (source_system='nsf_awards') | Research block |
| `research_usaspending` | USAspending.gov POST API filtered by `recipient_uei` | JSON → award rows | `research_awards` (source_system='usaspending') | Research block |

The four research-funding source_types all join exactly on `ipeds.institutions_2024.ueis` (Unique Entity Identifier) — federal awards systems all use UEI as the canonical key, so matching is exact, not fuzzy. Where UEI is null in IPEDS, we fall back to name-search via API and require LLM-judge confidence ≥ 0.9.

Phase 2 source_types are reserved in `landing.source_types` and have content tables stubbed (Group C — Future stubs in section 9) but no connector code yet: `grants` (Grants.gov), `rfps` (SAM.gov), `accreditation_dapip` (DAPIP database), `athletics_eada`, `cdr`, `glossary`, `data_dictionary`. The Leadership, Safety/Compliance, and Catalog source bundles considered during brainstorming are explicitly deferred to a later phase per user decision.

## 7. Pilot scope and acceptance criteria

### 7.1 Cohort

| unitid | institution | Carnegie | Control | Why this one |
|---|---|---|---|---|
| 166683 | MIT | R1 (15) | Private NP (2) | Gold-standard site, rich IR + factbook + CDS, low scrape risk |
| 228778 | UT Austin | R1 (15) | Public (1) | Sprawling decentralized public; tests deep crawl + URL discovery |
| 100654 | Alabama A&M | Doctoral/Prof (18), HBCU | Public (1) | HBCU-specific data path; smaller IR footprint |
| 225423 | Houston Community College | Associate's (1) | Public (1) | Community college tests CDS/factbook absence handling |
| 168342 | Williams College | Bac Arts & Sciences (21) | Private NP (2) | Liberal arts; IR often inside Provost; tests categorization |

Marked via `landing.institutions.pilot_cohort = TRUE`.

### 7.2 Acceptance criteria (pilot is "done" when ALL hold)

1. `landing.mv_institution_page` returns 5 rows, one per pilot unitid.
2. For each `(unitid, source_type)` pair (5 × 13 = 65 combinations across the v1 source_types — 9 web/doc + research_herd + research_nih + research_nsf + research_usaspending):
   - Stage 4 wrote ≥1 row to the content table, **or**
   - `landing.scrape_runs` has a terminal row with `status='skipped'` and a documented `error_class` (e.g. `NO_CANDIDATE_URL_FOUND`, `LLM_LOW_CONFIDENCE`, `ROBOTS_DISALLOWED`).
3. Manual eyeball QA on a one-page printout per institution: logo correct? top-3 IR contacts plausible (or documented absent)? at least 1 CDS or factbook link real? peers Carnegie-sensible?
4. Total LLM cost for the pilot ≤ $5 (full crawl forecast: $30–60).
5. `landing.raw_payloads` populated so Stage 3 can be re-run without re-fetching.

## 8. Repository structure

```
institutions-landing-pages/
├── README.md
├── CLAUDE.md                        # project memory for Claude (clean & focused)
├── pyproject.toml                   # deps: crawl4ai, sqlalchemy, alembic, celery[redis], anthropic,
│                                    #       httpx, pydantic, structlog, click, pdfplumber
├── .env / .env.example              # POSTGRES_*, REDIS_URL, ANTHROPIC_API_KEY, BRANDFETCH_API_KEY,
│                                    # HIGHEREDJOBS_*, SERPAPI_KEY, NIH_REPORTER_*, NSF_AWARDS_*,
│                                    # USASPENDING_*
├── docker-compose.yml               # postgres + redis (Celery broker) — local only
├── alembic.ini
├── alembic/
│   ├── env.py                       # points at landing schema
│   └── versions/
│       ├── 0001_landing_master_lookups.py
│       ├── 0002_landing_pipeline_ops.py
│       ├── 0003_landing_content_tables.py
│       ├── 0004_landing_research_funding.py
│       ├── 0005_landing_mv_research_summary.py     # must precede mv_institution_page
│       └── 0006_landing_mv_institution_page.py
├── docs/
│   ├── superpowers/specs/
│   │   └── 2026-05-24-institutions-landing-pages-design.md
│   └── runbooks/
│       ├── add_new_source.md
│       └── pilot_then_full_crawl.md
├── src/landing_scraper/
│   ├── config.py                    # pydantic Settings (env-driven)
│   ├── db/
│   │   ├── engine.py                # SQLAlchemy engine + session factory
│   │   ├── models.py                # ORM matching schema (or raw SQL helpers)
│   │   └── writer.py                # ProvenanceWriter — only sanctioned content-table write path
│   ├── core/
│   │   ├── crawler.py               # crawl4ai AsyncWebCrawler wrapper, browser pool, robots cache
│   │   ├── llm.py                   # Anthropic client + prompt caching + cost ledger
│   │   ├── search.py                # SerpAPI (v1 chosen provider for site: queries)
│   │   └── pdf.py                   # pdfplumber/pypdf text extraction
│   ├── connectors/                  # ONE file per source_type, all subclass BaseConnector
│   │   ├── base.py
│   │   ├── brandfetch.py
│   │   ├── wikipedia.py
│   │   ├── ir_page.py
│   │   ├── ir_team.py
│   │   ├── factbook.py
│   │   ├── cds.py
│   │   ├── strategic_plan.py
│   │   ├── jobs.py
│   │   ├── research_herd.py         # NSF HERD survey CSV ingestion
│   │   ├── research_nih.py          # NIH RePORTER API
│   │   ├── research_nsf.py          # NSF Awards Search API
│   │   └── research_usaspending.py  # USAspending.gov API
│   ├── pipelines/
│   │   ├── celery_app.py            # per-source queue routing
│   │   ├── tasks.py                 # @app.task wrappers per stage per source
│   │   └── orchestrator.py          # click CLI: pilot-run, full-run, requeue, re-extract
│   ├── peers/
│   │   └── carnegie_state_control.py
│   └── views/
│       ├── refresh_research_summary.py
│       └── refresh_mv.py            # REFRESH MATERIALIZED VIEW CONCURRENTLY
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/sample_pages/       # saved HTML/PDF golden inputs
├── scripts/
│   ├── seed_institutions.py
│   ├── seed_source_types.py
│   └── pilot_run.py
└── ops/
    ├── celery_worker.sh
    └── celery_beat.sh
```

Top-level highlights:

- `alembic/versions/000{1..4}_*.py` — migrations create the schema in 4 atomic steps so each can be reviewed independently.
- `src/landing_scraper/connectors/<source>.py` — one file per source, all subclass `BaseConnector(ABC)` with `discover/validate/extract/store`.
- `src/landing_scraper/db/writer.py:ProvenanceWriter` — the only sanctioned write path to content tables. Enforces presence of `source_url + fetched_at + raw_payload_id + parser_version`.
- `src/landing_scraper/core/llm.py` — Anthropic wrapper with prompt caching, model defaults (`claude-haiku-4-5-20251001` for judge, `claude-sonnet-4-6` for hard extract), cost ledger flowing into `scrape_runs.cost_usd`.
- `src/landing_scraper/pipelines/orchestrator.py` — Click CLI: `pilot-run`, `full-run`, `requeue <run_id>`, `re-extract --since <date>`.
- `scripts/seed_institutions.py` — loads `assets/all-institutions.csv` into `landing.institutions`, joins to `ipeds.institutions_2024` for `webaddr` / `opeid` / `ein`, computes `slug`, marks the 5 pilot unitids.
- `tests/fixtures/sample_pages/` — saved HTML/PDF golden inputs so parser unit tests run offline.

## 9. Full DDL

```sql
CREATE SCHEMA IF NOT EXISTS landing;

-- =========================================================
-- GROUP A — MASTER & LOOKUPS
-- =========================================================

CREATE TABLE landing.institutions (
  unitid                 INTEGER PRIMARY KEY,
  slug                   TEXT NOT NULL UNIQUE,
  opeid                  TEXT,
  ein                    TEXT,
  ueis                   TEXT,
  webaddr_raw            TEXT,
  webaddr_normalized     TEXT,
  canonical_root_url     TEXT,
  robots_txt_url         TEXT,
  robots_txt_fetched_at  TIMESTAMPTZ,
  enabled                BOOLEAN NOT NULL DEFAULT TRUE,
  pilot_cohort           BOOLEAN NOT NULL DEFAULT FALSE,
  last_full_crawl_at     TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_landing_inst_slug  ON landing.institutions(slug);
CREATE INDEX ix_landing_inst_pilot ON landing.institutions(pilot_cohort) WHERE pilot_cohort;

CREATE TABLE landing.source_types (
  source_type           TEXT PRIMARY KEY,
  description           TEXT NOT NULL,
  default_queue         TEXT NOT NULL,
  refresh_cadence_days  INT,
  is_active             BOOLEAN NOT NULL DEFAULT TRUE
);

-- =========================================================
-- GROUP B — PIPELINE OPERATIONAL
-- =========================================================

CREATE TABLE landing.discovered_urls (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_type      TEXT    NOT NULL REFERENCES landing.source_types(source_type),
  url              TEXT    NOT NULL,
  discovery_method TEXT    NOT NULL,
  relevance_score  NUMERIC,
  depth            INTEGER,
  discovered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  run_id           UUID NOT NULL,
  UNIQUE (unitid, source_type, url, run_id)
);
CREATE INDEX ix_disc_inst_src ON landing.discovered_urls(unitid, source_type);

CREATE TABLE landing.canonical_urls (
  unitid          INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_type     TEXT    NOT NULL REFERENCES landing.source_types(source_type),
  url             TEXT    NOT NULL,
  confidence      NUMERIC NOT NULL,
  llm_reasoning   TEXT,
  chosen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  superseded_at   TIMESTAMPTZ,
  PRIMARY KEY (unitid, source_type, chosen_at)
);
CREATE INDEX ix_canon_current ON landing.canonical_urls(unitid, source_type) WHERE superseded_at IS NULL;

CREATE TABLE landing.raw_payloads (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_type      TEXT    NOT NULL REFERENCES landing.source_types(source_type),
  url              TEXT    NOT NULL,
  content_type     TEXT,
  http_status      INTEGER,
  body_text        TEXT,
  body_html        TEXT,
  body_blob_path   TEXT,
  body_sha256      CHAR(64) NOT NULL,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  fetcher          TEXT NOT NULL,
  run_id           UUID NOT NULL
);
CREATE INDEX ix_raw_dedupe ON landing.raw_payloads(unitid, source_type, body_sha256);
CREATE INDEX ix_raw_latest ON landing.raw_payloads(unitid, source_type, fetched_at DESC);

CREATE TABLE landing.scrape_runs (
  run_id           UUID PRIMARY KEY,
  unitid           INTEGER REFERENCES landing.institutions(unitid),
  source_type      TEXT REFERENCES landing.source_types(source_type),
  stage            TEXT NOT NULL,
  status           TEXT NOT NULL,
  started_at       TIMESTAMPTZ NOT NULL,
  finished_at      TIMESTAMPTZ,
  duration_ms      INTEGER,
  error_class      TEXT,
  error_message    TEXT,
  rows_written     INTEGER,
  llm_tokens_in    INTEGER,
  llm_tokens_out   INTEGER,
  cost_usd         NUMERIC(10,5)
);
CREATE INDEX ix_runs_inst ON landing.scrape_runs(unitid, source_type, started_at DESC);

-- =========================================================
-- GROUP C — CONTENT TABLES (v1)
-- =========================================================

CREATE TABLE landing.brand_assets (
  unitid           INTEGER PRIMARY KEY REFERENCES landing.institutions(unitid),
  logo_url         TEXT,
  logo_url_dark    TEXT,
  logo_icon_url    TEXT,
  colors_primary   TEXT,
  colors_secondary TEXT,
  color_palette    JSONB,
  fonts            JSONB,
  source_url       TEXT,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL
);

CREATE TABLE landing.wiki_summaries (
  unitid            INTEGER PRIMARY KEY REFERENCES landing.institutions(unitid),
  wikipedia_url     TEXT,
  wikidata_qid      TEXT,
  short_description TEXT,
  extract_summary   TEXT,
  founded_year      INTEGER,
  motto             TEXT,
  endowment_usd     BIGINT,
  president_name    TEXT,
  source_url        TEXT,
  fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id    BIGINT REFERENCES landing.raw_payloads(id),
  parser_version    TEXT NOT NULL
);

CREATE TABLE landing.peer_groups (
  unitid            INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  peer_unitid       INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  rank              SMALLINT NOT NULL,
  similarity_score  NUMERIC,
  algorithm         TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (unitid, algorithm, algorithm_version, peer_unitid),
  CHECK (unitid <> peer_unitid)
);
CREATE INDEX ix_peers_lookup ON landing.peer_groups(unitid, algorithm, algorithm_version, rank);

CREATE TABLE landing.ir_offices (
  unitid           INTEGER PRIMARY KEY REFERENCES landing.institutions(unitid),
  office_name      TEXT,
  page_url         TEXT,
  page_summary     TEXT,
  parent_division  TEXT,
  source_url       TEXT,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL,
  confidence       NUMERIC
);

CREATE TABLE landing.ir_contacts (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  name             TEXT NOT NULL,
  title            TEXT,
  email            TEXT,
  phone            TEXT,
  linkedin_url     TEXT,
  ordering         SMALLINT,
  source_url       TEXT,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL,
  confidence       NUMERIC,
  UNIQUE (unitid, name, title)
);
CREATE INDEX ix_ircontacts_inst ON landing.ir_contacts(unitid);

CREATE TABLE landing.documents (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  doc_type         TEXT NOT NULL,
  title            TEXT,
  year             SMALLINT,
  doc_url          TEXT NOT NULL,
  mime_type        TEXT,
  page_count       INTEGER,
  storage_path     TEXT,
  extracted_text   TEXT,
  source_url       TEXT,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL,
  confidence       NUMERIC,
  UNIQUE (unitid, doc_type, year, doc_url)
);
CREATE INDEX ix_docs_inst_type ON landing.documents(unitid, doc_type, year DESC);

CREATE TABLE landing.cds_links (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  year             SMALLINT NOT NULL,
  cds_url          TEXT NOT NULL,
  is_pdf           BOOLEAN,
  source_url       TEXT,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL,
  UNIQUE (unitid, year)
);

CREATE TABLE landing.jobs (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  title            TEXT NOT NULL,
  department       TEXT,
  category         TEXT,
  posted_at        DATE,
  expires_at       DATE,
  apply_url        TEXT NOT NULL,
  source_board     TEXT,
  raw_description  TEXT,
  source_url       TEXT,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL,
  UNIQUE (unitid, apply_url)
);
CREATE INDEX ix_jobs_inst_cat ON landing.jobs(unitid, category, posted_at DESC);

CREATE TABLE landing.notable_alumni (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  name             TEXT NOT NULL,
  field            TEXT,
  short_bio        TEXT,
  wikipedia_url    TEXT,
  source_url       TEXT,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL,
  UNIQUE (unitid, name)
);

-- Research funding (v1, added in v1.1)
CREATE TABLE landing.research_expenditures (
  unitid                  INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  fiscal_year             SMALLINT NOT NULL,
  total_rd_expend_usd     BIGINT,
  federal_rd_usd          BIGINT,
  state_local_rd_usd      BIGINT,
  institutional_rd_usd    BIGINT,
  business_rd_usd         BIGINT,
  nonprofit_rd_usd        BIGINT,
  other_rd_usd            BIGINT,
  by_field                JSONB,       -- {life_sciences, physical_sciences, engineering, ...} in USD
  rank_national           INTEGER,     -- HERD-provided rank
  source_url              TEXT,
  fetched_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id          BIGINT REFERENCES landing.raw_payloads(id),
  parser_version          TEXT NOT NULL,
  PRIMARY KEY (unitid, fiscal_year)
);

CREATE TABLE landing.research_awards (
  id                      BIGSERIAL PRIMARY KEY,
  unitid                  INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_system           TEXT NOT NULL,          -- 'nih_reporter' | 'nsf_awards' | 'usaspending'
  external_id             TEXT NOT NULL,          -- award/grant ID at source
  agency                  TEXT,                   -- 'NIH','NSF','DOE','NASA','DoD','HHS','ED', etc.
  program_or_mechanism    TEXT,                   -- 'R01','NSF CAREER','SBIR Phase II', etc.
  title                   TEXT,
  pi_name                 TEXT,
  pi_orcid                TEXT,
  amount_total_usd        BIGINT,                 -- full award amount
  amount_obligated_usd    BIGINT,                 -- obligated to date (USAspending)
  fiscal_year             SMALLINT,
  start_date              DATE,
  end_date                DATE,
  abstract                TEXT,
  source_url              TEXT,
  fetched_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id          BIGINT REFERENCES landing.raw_payloads(id),
  parser_version          TEXT NOT NULL,
  UNIQUE (source_system, external_id)
);
CREATE INDEX ix_research_awards_inst    ON landing.research_awards(unitid, fiscal_year DESC);
CREATE INDEX ix_research_awards_pi      ON landing.research_awards(pi_name);
CREATE INDEX ix_research_awards_agency  ON landing.research_awards(unitid, agency, fiscal_year DESC);

-- Pre-aggregated rollup so mv_institution_page reads one row
CREATE MATERIALIZED VIEW landing.mv_research_funding_summary AS
SELECT
  unitid,
  COUNT(*)                                                                          AS award_count_total,
  COUNT(*) FILTER (WHERE fiscal_year >= EXTRACT(YEAR FROM CURRENT_DATE)::INT - 3)   AS award_count_last_3y,
  SUM(amount_total_usd)                                                             AS amount_total_all_time_usd,
  SUM(amount_total_usd) FILTER (WHERE fiscal_year >= EXTRACT(YEAR FROM CURRENT_DATE)::INT - 3) AS amount_total_last_3y_usd,
  MAX(end_date)                                                                     AS latest_award_end,
  jsonb_object_agg(source_system, sub_count) FILTER (WHERE source_system IS NOT NULL) AS by_source
FROM (
  SELECT unitid, source_system, fiscal_year, amount_total_usd, end_date,
         COUNT(*) OVER (PARTITION BY unitid, source_system) AS sub_count
  FROM landing.research_awards
) s
GROUP BY unitid;
CREATE UNIQUE INDEX ix_mv_research_summary ON landing.mv_research_funding_summary(unitid);

-- Future stubs (tables exist so schema doesn't churn later; no v1 connector)
CREATE TABLE landing.grants (
  id BIGSERIAL PRIMARY KEY,
  unitid INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_system TEXT, external_id TEXT, agency TEXT, title TEXT, amount_usd BIGINT,
  award_date DATE, end_date DATE, status TEXT, source_url TEXT,
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL,
  UNIQUE (source_system, external_id)
);
CREATE TABLE landing.rfps (
  id BIGSERIAL PRIMARY KEY,
  unitid INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_system TEXT, external_id TEXT, title TEXT, due_date DATE, status TEXT,
  apply_url TEXT, source_url TEXT, fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL,
  UNIQUE (source_system, external_id)
);
CREATE TABLE landing.accreditation_dapip (
  unitid INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  accreditor_name TEXT NOT NULL, program_name TEXT, status TEXT,
  date_first DATE, date_last_action DATE, source_url TEXT,
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL,
  PRIMARY KEY (unitid, accreditor_name, program_name)
);
CREATE TABLE landing.athletics_eada (
  unitid INTEGER PRIMARY KEY REFERENCES landing.institutions(unitid),
  total_athletes INTEGER, men_athletes INTEGER, women_athletes INTEGER,
  total_revenue_usd BIGINT, total_expenses_usd BIGINT, year SMALLINT,
  source_url TEXT, fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL
);
CREATE TABLE landing.cdr (
  unitid INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  year SMALLINT NOT NULL, cdr_rate NUMERIC(5,2),
  num_borrowers INTEGER, num_defaulted INTEGER,
  source_url TEXT, fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL,
  PRIMARY KEY (unitid, year)
);

-- =========================================================
-- GROUP D — PAGE-READY MATERIALIZED VIEW
-- =========================================================

CREATE MATERIALIZED VIEW landing.mv_institution_page AS
SELECT
  i.unitid,
  li.slug,
  i.instnm                         AS institution_name,
  i.city, i.stabbr, i.zip,
  i.webaddr, i.opeid, i.ein, i.ueis,
  cc.label                         AS carnegie_label,
  ctrl.label                       AS control_label,
  loc.label                        AS locale_label,
  i.hbcu, i.tribal, i.hospital, i.medical, i.landgrnt,
  sc.admission_rate, sc.sat_avg,
  sc.tuition_in_state, sc.tuition_out_of_state,
  sc.avg_net_price_public, sc.avg_net_price_private,
  sc.net_price_public_0_30k, sc.net_price_public_30_48k,
  sc.net_price_public_48_75k, sc.net_price_public_75_110k,
  sc.net_price_public_110k_plus,
  sc.pell_grant_rate, sc.federal_loan_rate,
  sc.completion_rate_overall, sc.completion_rate_4yr_150pct,
  sc.earnings_6yr_median, sc.earnings_10yr_median,
  sc.median_debt_completers,
  sc.demo_white, sc.demo_black, sc.demo_hispanic, sc.demo_asian,
  to_jsonb(b.*)                    AS brand,
  to_jsonb(w.*)                    AS wiki,
  to_jsonb(ir.*)                   AS ir_office,
  COALESCE(jsonb_agg(DISTINCT to_jsonb(ic.*))  FILTER (WHERE ic.id  IS NOT NULL), '[]') AS ir_contacts,
  COALESCE(jsonb_agg(DISTINCT to_jsonb(d.*))   FILTER (WHERE d.id   IS NOT NULL), '[]') AS documents,
  COALESCE(jsonb_agg(DISTINCT to_jsonb(cds.*)) FILTER (WHERE cds.id IS NOT NULL), '[]') AS cds_links,
  COALESCE(jsonb_agg(DISTINCT to_jsonb(j.*))   FILTER (WHERE j.id   IS NOT NULL), '[]') AS jobs,
  COALESCE(jsonb_agg(DISTINCT to_jsonb(na.*))  FILTER (WHERE na.id  IS NOT NULL), '[]') AS notable_alumni,
  COALESCE(jsonb_agg(DISTINCT to_jsonb(pg.*))  FILTER (WHERE pg.peer_unitid IS NOT NULL), '[]') AS peers,
  to_jsonb(rfs.*)                  AS research_summary,
  COALESCE(jsonb_agg(DISTINCT to_jsonb(rx.*))  FILTER (WHERE rx.fiscal_year IS NOT NULL), '[]') AS research_expenditures,
  GREATEST(b.fetched_at, w.fetched_at, ir.fetched_at) AS landing_last_updated_at
FROM landing.institutions li
JOIN ipeds.institutions_2024 i        USING (unitid)
LEFT JOIN ipeds.carnegie_codes cc     ON cc.code = i.c21basic
LEFT JOIN ipeds.control_codes  ctrl   ON ctrl.code = i.control
LEFT JOIN ipeds.sector_codes   loc    ON loc.code = i.locale
LEFT JOIN score_card.fact_institution_yearly sc
       ON sc.institution_id = i.unitid
      AND sc.academic_year_id = (
            SELECT MAX(academic_year_id)
            FROM score_card.fact_institution_yearly
            WHERE institution_id = i.unitid
          )
LEFT JOIN landing.brand_assets   b    USING (unitid)
LEFT JOIN landing.wiki_summaries w    USING (unitid)
LEFT JOIN landing.ir_offices     ir   USING (unitid)
LEFT JOIN landing.ir_contacts    ic   USING (unitid)
LEFT JOIN landing.documents      d    USING (unitid)
LEFT JOIN landing.cds_links      cds  USING (unitid)
LEFT JOIN landing.jobs           j    USING (unitid)
LEFT JOIN landing.notable_alumni na   USING (unitid)
LEFT JOIN landing.peer_groups    pg   ON pg.unitid = i.unitid
                                      AND pg.algorithm = 'carnegie_state_control_v1'
LEFT JOIN landing.mv_research_funding_summary rfs USING (unitid)
LEFT JOIN landing.research_expenditures      rx   USING (unitid)
GROUP BY i.unitid, li.slug, cc.label, ctrl.label, loc.label,
         sc.*, b.*, w.*, ir.*, rfs.*;

CREATE UNIQUE INDEX ix_mv_inst_page  ON landing.mv_institution_page(unitid);
CREATE INDEX        ix_mv_inst_slug  ON landing.mv_institution_page(slug);
```

## 10. Operational concerns

- **Robots.txt**: cached per-institution in `landing.institutions.robots_txt_*`. crawl4ai wrapper respects it by default; we never override.
- **Rate limiting**: per-domain via crawl4ai's built-in throttler; `q_crawl` worker concurrency=4 with 1 req / 2 sec / domain.
- **Idempotency**: `raw_payloads.body_sha256` is the dedupe key. If a re-crawl returns the same SHA, stage 3 skips re-extraction (the previously written content rows remain valid).
- **Re-extract without re-fetch**: bumping `parser_version` in a connector and running `orchestrator re-extract --since <date>` re-runs Stage 3 on stored payloads only.
- **Cost ceiling**: `core/llm.py` writes per-task `cost_usd` to `scrape_runs`. A daily Celery beat task aggregates last-24h cost and emits a warning if it exceeds the `LLM_DAILY_COST_WARN_USD` env var (default: $10). Pilot's hard ceiling is $5 total (acceptance criterion 4).
- **Manual overrides**: insert directly into `landing.canonical_urls` (with high confidence and `llm_reasoning='manual override'`) to force the chosen URL for a specific (unitid, source_type). No code change required.
- **Refresh cadence**: `landing.source_types.refresh_cadence_days` drives a Celery beat job that requeues stale `(unitid, source_type)` pairs nightly.
- **Materialized view refresh**: `landing.mv_institution_page` refreshed `CONCURRENTLY` after each batch write, or at most once per hour.

## 11. Open questions resolved during brainstorming

- ✅ Scope is Institution Landing Page only; IR Person/Association/Authors pages are separate future projects.
- ✅ This repo builds the data pipeline only; frontend rendering is a sibling project.
- ✅ Tech stack: Python + crawl4ai (Playwright-backed) + Celery + Redis + Postgres.
- ✅ DB approach: new `landing` schema inside existing `ipeds` DB.
- ✅ Pilot 5 institutions: MIT, UT Austin, Alabama A&M, Houston CC, Williams.
- ✅ LLM-assisted parsing accepted (Approach C); Anthropic Haiku for judge, Sonnet for hard extract.

## 12. Known risks / things we accept

- **Long-tail IR pages will fail** even with LLM extraction at, say, ~5–15% of institutions. We accept this and surface "no IR contacts found" in the materialized view rather than failing the pipeline.
- **PDF factbooks vary wildly** in structure; v1 only stores them + extracted text. Field-level extraction (enrollment table → rows) is Phase 2.
- **Wikipedia coverage is uneven**; HBCUs and community colleges have thinner pages. We accept partial coverage.
- **Jobs freshness drifts**; nightly refresh is "good enough" for SEO purposes — we are not building a job board.
- **BrandFetch is a third-party SaaS**; failure to fetch logo doesn't block the page. Fallback is `null` in `brand_assets`.

## 13. Changelog

- **v1.1 (2026-05-24)** — Added research-funding bundle to v1 scope after deeper source research:
  - New source_types: `research_herd`, `research_nih`, `research_nsf`, `research_usaspending`
  - New content tables: `landing.research_expenditures`, `landing.research_awards`
  - New rollup: `landing.mv_research_funding_summary`
  - `mv_institution_page` now joins both, exposing `research_summary` (JSONB) and `research_expenditures` (JSONB array)
  - Leveraged `ipeds.institutions_2024.ueis` (UEI) as the canonical join key into all four federal awards systems
  - Phase 2 bundles (Leadership/Governance, Safety/Compliance, Catalog/Programs) reviewed and explicitly deferred
- **v1.0 (2026-05-24)** — Initial design: 4-stage pipeline, 9 source_types (brandfetch, wikipedia, ir_page, ir_team, factbook, cds, strategic_plan, jobs, peers), `landing.*` schema in `ipeds` DB, pilot on 5 institutions (MIT, UT Austin, Alabama A&M, Houston CC, Williams).

---

End of design.

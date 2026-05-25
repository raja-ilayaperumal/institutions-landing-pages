# Clema Institutions Landing Pages — Data Pipeline

This repo is a **data pipeline only** (no frontend). It scrapes public sources and fills a `landing.*` schema in the existing `ipeds` Postgres DB. A separate Clema project renders the actual 6k+ institution landing pages by reading from `landing.mv_institution_page`.

## Authoritative documents

- **Page-by-page data blueprint** (every field + source mapping; the single source of truth for what data goes on the page): `docs/data-blueprint.md`
- **System design spec** (architecture, pipeline stages, schema): `docs/superpowers/specs/2026-05-24-institutions-landing-pages-design.md`
- **Original SEO research** (raw): `assets/seo-research/*.docx`
- **Seed list** (6,050 active postsec institutions, 2024): `assets/all-institutions.csv`
- **Sample scraped outputs** (verified end-to-end): `data/mit_sample.json`, `data/aamu_sample.json`
- Whiteboard photo (original vision): provided in chat history (not in repo)

If anything in this file conflicts with the spec, the spec wins. Update both together.

## Local environment

- Postgres: `localhost:5432`, user `rajailayaperumal`, db `ipeds` (see `.env`)
- Existing schemas to read from: `ipeds.*` (242 tables, 2012-2025 history), `score_card.*` (already has admissions, demographics, net price by income band, completion, earnings, repayment), `pseo.*` (earnings/flows), `ipeds_data_dictionary.*` (variable labels)
- New schema we own: `landing.*` (created via Alembic in this repo)
- Redis: required for Celery broker — `docker-compose up` brings up Postgres + Redis for local

## Architecture in one paragraph

Four-stage pipeline per `(unitid, source_type)`: **Discover** (crawl4ai `BestFirstCrawlingStrategy` from institution root, or API call, or `site:` search) → **Validate** (LLM-as-judge with Haiku, picks one canonical URL) → **Extract** (crawl4ai `LLMExtractionStrategy` with Pydantic schema, or rule-based for structured sources) → **Store** (typed row in per-source content table + raw payload archived for re-parse). Per-source Celery queues prevent slow IR scrapes from starving fast BrandFetch calls. Pilot vs full crawl is just a `--pilot` flag — same code path.

## Key code conventions

- **One connector per file** in `src/landing_scraper/connectors/` (`brandfetch.py`, `ir_team.py`, ...). All subclass `BaseConnector` and implement `discover/validate/extract/store`.
- **All content-table writes go through `db/writer.py:ProvenanceWriter`** — never direct INSERT. Every row must carry `source_url`, `fetched_at`, `raw_payload_id`, `parser_version`.
- **Raw payloads are the time machine** — `landing.raw_payloads` stores HTML/markdown/PDF bytes with `body_sha256`. Re-parse without re-fetch when improving parsers.
- **LLM model defaults**: `claude-haiku-4-5-20251001` for Stage 2 judge, `claude-sonnet-4-6` for Stage 3 hard extraction. Always enable prompt caching via `core/llm.py` wrapper.
- **Pilot cohort flag**: `landing.institutions.pilot_cohort = TRUE` for the 5 institutions in the pilot. `pilot_run.py` filters on it. Full run drops the filter.

## Pilot institutions (validate before scaling to 6k)

| unitid | institution | why |
|---|---|---|
| 166683 | MIT | private R1, gold-standard site |
| 228778 | UT Austin | public R1, sprawling decentralized site |
| 100654 | Alabama A&M | HBCU, smaller IR footprint |
| 225423 | Houston Community College | community college, thin web presence |
| 168342 | Williams College | private liberal arts, IR inside Provost |

## What lives where

| Concern | Location |
|---|---|
| DB models / migrations | `alembic/versions/`, `src/landing_scraper/db/` |
| Scraper core (crawler, llm, search, pdf) | `src/landing_scraper/core/` |
| Per-source pipelines | `src/landing_scraper/connectors/<source>.py` |
| Celery tasks + orchestrator CLI | `src/landing_scraper/pipelines/` |
| Peer algorithm | `src/landing_scraper/peers/` |
| Materialized view refresh | `src/landing_scraper/views/` |
| Seeders | `scripts/seed_*.py`, `scripts/pilot_run.py` |
| Tests + fixtures | `tests/`, `tests/fixtures/sample_pages/` |

## Source types (v1, after v1.1 additions)

Web/document scraping: `brandfetch`, `wikipedia`, `ir_page`, `ir_team`, `factbook`, `cds`, `strategic_plan`, `jobs`.

DB-only / computed: `peers` (Carnegie+state+control nearest neighbors).

Federal research-funding APIs (join on `ipeds.institutions_2024.ueis` — UEI is exact, no fuzzy match needed): `research_herd` (NSF HERD R&D expenditures), `research_nih` (NIH RePORTER awards), `research_nsf` (NSF Awards Search), `research_usaspending` (USAspending federal awards).

Phase 2 stubs already in schema: `grants`, `rfps`, `accreditation_dapip`, `athletics_eada`, `cdr`, `notable_alumni`, `glossary`, `data_dictionary`. Deferred bundles considered but not in v1: Leadership/Governance (Trustees, Pres/Provost/CFO, 990 forms), Safety/Compliance (Clery, HCM, OCR), Catalog/Programs (course catalog, mission, press kit, dashboards).

## What NOT to do here

- No frontend code, no Next.js, no page rendering. That's a sibling project.
- Don't INSERT into `landing.*` content tables outside `ProvenanceWriter` — code review will reject.
- Don't add a connector without a row in `landing.source_types` and an Alembic migration if it needs new columns.
- Don't bypass crawl4ai for scraping HTML — we want one HTTP path with one set of rate-limit/robots/cache rules.
- Don't write to existing `ipeds.*` / `score_card.*` schemas — read-only.

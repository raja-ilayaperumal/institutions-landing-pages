# Scraping Process — The Robust Pipeline

**Status:** v1 (2026-05-24)
**Pairs with:** `docs/data-blueprint.md` (what data goes on each page)

This is the single canonical description of how data flows from raw sources
into the page-ready `landing.mv_institution_page`. Every connector, every
target, every render follows this same shape.

---

## Phase 1 — Static Data (no scraping; pure DB joins)

For every field the page can answer from **federal data that's already in the
local Postgres** (IPEDS + College Scorecard + PSEO), we DO NOT scrape. We
pull at materialized-view refresh time.

| Field bucket | Source schema/tables | How |
|---|---|---|
| Identity, location, accreditor flags, web addresses | `ipeds.institutions_2024` | direct join in `mv_institution_page` |
| Carnegie, control, sector, state, region labels | `ipeds.carnegie_codes`, `control_codes`, `sector_codes`, `state_codes` | lookup joins; canonicalized in `landing.institutions` at seed time |
| Admissions, SAT/ACT, demographics, net price, completion, earnings, debt, repayment | `score_card.fact_institution_yearly` | latest year via `LATERAL` subquery |
| Graduation rate, retention, Pell graduation | `ipeds.drv_graduation_rates`, `ipeds.fall_enrollment_retention` | scalar subqueries in renderer when Scorecard is null |
| Enrollment, completions, faculty, salaries | `ipeds.fall_enrollment_*`, `ipeds.completions_by_program_*`, `ipeds.staff_*` | aggregate/join in `mv_institution_page` |
| Federal research awards | `landing.research_awards` (populated by NIH/NSF/USAspending API connectors) | rolled up in `mv_research_funding_summary` |
| IPEDS DFR comparison group | `landing.peer_groups WHERE algorithm='dfr_v1'` | scraped once from `nces.ed.gov/ipeds/dfr/{year}/ReportHTML.aspx?unitId=...` |

**Refresh cadence:** materialized view refreshed after every batch write
(`REFRESH MATERIALIZED VIEW CONCURRENTLY`).

---

## Phase 2 — External Data (per-target scraping pipeline)

Everything that's NOT in the federal DB — IR pages, factbooks, CDS links,
strategic plans, data dictionaries, glossaries, IR team rosters — runs through
this **4-stage pipeline**:

```
DISCOVER  ──►  VALIDATE  ──►  EXTRACT  ──►  PERSIST
```

### 2.1 Define what you want first (TargetSpec)

Every scrapable doc type is a `TargetSpec` row in `src/landing_scraper/scraper/targets.py`:

```python
TargetSpec(
    name="factbook",
    search_queries=('"factbook"',),
    crawl_keywords=("factbook", "fact book", "fact sheet"),
    url_patterns_positive=(r"/factbook", r"\.pdf$"),
    wellknown_paths=("/factbook", "/ir/factbook", "/oir/factbook"),
    wellknown_subdomains=(),
    validator_system="...",   # LLM judge prompt with required JSON shape
    extractor_system="...",   # LLM structured-extraction prompt with field schema
)
```

The `extractor_system` prompt declares EXACTLY which fields to extract —
this is "understand what information to get" made executable.

### 2.2 DISCOVER — find candidate URLs (5 channels, sitemap-first)

The pipeline runs discovery channels in TWO phases to respect search quota:

**Phase A — Free channels (run in parallel):**

1. **`sitemap`** — fetch `{root}/sitemap.xml`, parse all `<loc>` URLs, keep
   those matching the target's `url_patterns_positive`. This is FIRST in
   priority because the institution itself tells us where things live.
   * For sitemap-indexes: follow up to 3 child sitemaps.
   * Found URL pattern is institution-specific evidence (e.g., MIT's IR pages
     live at `ir.mit.edu/...`, AAMU's at `/research-economic-development/...`).
2. **`wellknown`** — probe known URL conventions in parallel:
   * Subdomain probes: `https://ir.{domain}/`, `https://oir.{domain}/`, …
   * Path probes off both `https://{domain}` and the canonical root:
     `/institutional-research`, `/oir`, `/ir/factbook`, etc.
   * For path-style IR landings (`.../institutional-research.html`), we strip
     the filename and probe sibling paths under the parent directory
     (`.../staff.html`, `.../team.html`).
   * 200 OK = strong evidence; 403/503 from a *subdomain* probe is still
     treated as a candidate (anti-bot pages exist — crawl4ai will fetch).
3. **`deep_crawl`** — crawl4ai `BestFirstCrawlingStrategy` from the
   institution root, scored by `crawl_keywords`. Last resort; only when the
   above two are thin.

**Phase B — Paid search (only if Phase A is thin):**

4. **`search`** — Tavily / Google CSE / DDG `site:<domain> <query>` for each
   `search_queries` template. Only runs if Phase A yielded < 2 strong candidates.
   Each provider tried in order, first one to succeed wins.
5. **`open_search`** — non-`site:` fallback (e.g., `"MIT" "office of institutional research"`).
   Only when both `site:` search and free channels are empty.

**Composite scoring** merges all channels:
* +3.0 if `wellknown` returned 200
* +2.0 + (rank bonus) if `search` returned it
* +1.0 if `sitemap` returned it
* +1.0 per `url_patterns_positive` regex match
* +0.5 per cross-channel agreement (multiple channels = more trust)
* `url_patterns_negative` (news articles, login pages, image files) =
  candidate dropped entirely.

**Persisted to:** `landing.crawl_log` (every candidate + score + channel +
run_id) for audit.

### 2.3 VALIDATE — LLM-as-judge picks one

Top-K candidates (default 5) are bundled into a single prompt with their URL +
title + snippet, sent to the target's `validator_system`. The LLM (Haiku /
gpt-4o-mini) returns:

```json
{ "best_index": 1-5 or 0 if none match,
  "confidence": 0.0-1.0,
  "reason": "short explanation" }
```

* `best_index = 0` → record `LLM_REJECTED` in `landing.scraper_runs`, stop.
* Otherwise → chosen URL is recorded in `landing.canonical_urls` with
  `confidence` and `llm_reasoning`.

### 2.4 EXTRACT — fetch + LLM structured extraction

1. **Fetch** the canonical URL via `crawl4ai` (Playwright-backed; handles JS
   rendering + Cloudflare/anti-bot). PDFs go through `httpx + pdfplumber`.
2. **Save raw payload** to `landing.raw_payloads` (HTML/markdown +
   `body_sha256` for dedupe). This is the time-machine: future parser
   improvements can re-run extraction without re-fetching.
3. **Persist file** via `db.document_store.save()` →
   `data/docs/<doc_type>/<unitid>_<year>_<title-stub>_<sha8>.<ext>`.
4. **LLM structured extract** using the target's `extractor_system` prompt.
   The prompt specifies the exact JSON shape required.

### 2.5 PERSIST — write typed rows with provenance

Every content-table row carries:
* `source_url` — the canonical URL we extracted from
* `fetched_at` — when
* `raw_payload_id` — pointer to the raw HTML in `landing.raw_payloads`
* `parser_version` — bump to trigger re-extract without re-fetch
* `confidence` — from the validator

Plus the typed structured fields (office_name, page_url, etc.) into the
target table (`landing.ir_pages`, `landing.ir_documents`, etc.).

The dispatch from PipelineResult → table is in `src/landing_scraper/scraper/pipeline.py:_write_content`.

### 2.6 Quota & cost discipline

* **Sitemap, wellknown, deep_crawl, fetch, LLM** = no external quota cost
  beyond the LLM tokens. Run freely.
* **Tavily / Google CSE** = external quota. Run only when free channels
  thin. Single high-precision query per target. ~$0.001 per Tavily call.
* **LLM judge** = ~$0.0005 (Haiku/4o-mini). Run on top-K, never on the long
  tail.
* **LLM extract** = ~$0.005-0.05 depending on page size and tier (gpt-4o for
  hard, 4o-mini for simple). Run once per chosen URL.

For the 10-institution pilot the total LLM cost was **$1.13** — IR-page,
IR-team, factbook, CDS, strategic_plan, data_dictionary, data_definition,
glossary, plus DFR + research API connectors.

---

## Phase 3 — Render

Single template: `scripts/render_page.py` reads one row from
`landing.mv_institution_page`, opens auxiliary queries for IPEDS-derived
graduation rates + peer details + ir_page office contact, emits HTML.

Documents grouped by category with year prominently displayed:

```
Documents & Reports
├── IPEDS Data Feedback Report
│     2024  |  IPEDS Data Feedback Report 2024  |  open  |  [file]
├── Factbook
│     2023  |  Factbook 2023               |  open  |  [file]
├── Common Data Set
│     2024  |  Common Data Set 2024        |  open  |  [link only]
├── Data Dictionary  …
├── Data Definitions  …
├── Glossary  …
└── Strategic Plan  …
```

---

## Quality expectations per data type

| Target | Typical success rate | When it fails |
|---|---|---|
| `dfr_report` | ~100% | Only if NCES has no DFR for that year |
| `wikipedia` | ~95% | Tiny / new institutions |
| `research_*` (NIH/NSF/USAspending) | 100% if UEI present | UEI null in IPEDS (rare) |
| `brandfetch` | ~80% | Free-tier API rate limits |
| `ir_page` | ~75% | Institution buries IR deep + no sitemap entry |
| `ir_team` | ~50% | Institution doesn't publish individual staff (real gap, not bug) |
| `factbook` | ~30% | Institution doesn't publish a public factbook (community colleges often don't) |
| `cds` | ~40% | Institution doesn't participate in CDS (MIT did — many R1s don't) |
| `strategic_plan` | ~40% | Strategic plan lives on president's office, not IR |
| `data_dictionary` / `data_definition` / `glossary` | ~30-50% | Only larger institutions publish these |

When a target "fails" it's almost always because the institution genuinely
doesn't publish that document, not because the scraper couldn't find it. We
record `LLM_REJECTED` so it's auditable.

---

## When something doesn't work

1. **Check `landing.crawl_log`** — see every candidate URL we considered
   with its channel + score.
2. **Check `landing.canonical_urls`** — see what we chose (and what
   confidence).
3. **Check `landing.scraper_runs`** — see which stage failed with what
   error_class.
4. **Check `landing.raw_payloads`** — read the actual fetched HTML.
5. **If the candidate URL is correct but extraction is wrong** — bump
   `parser_version` in the connector and re-run `pipeline.extract()` only.
   No re-fetch needed.
6. **If no candidate URL was correct** — extend the target's
   `wellknown_paths` / `wellknown_subdomains` with the patterns that should
   have worked.

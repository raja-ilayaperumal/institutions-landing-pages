# Institution Landing Pages — Data Blueprint

**Status:** v1 (2026-05-24)
**Owner:** Raja
**Consolidates:** `assets/seo-research/Institutional_page (URL, Schema, Keywords, Structure).docx` + the source-map docx
**Pairs with:** `docs/superpowers/specs/2026-05-24-institutions-landing-pages-design.md` (architecture)

This is the **single source of truth for what data appears on every page and where each field comes from**. The design spec answers "how is the system built"; this doc answers "for each field on the page, what do I read or scrape, and into which table does it land".

---

## 0. URL & internal linking architecture

| Page type | URL pattern | Examples |
|---|---|---|
| **Hub** | `clema.ai/institutions/` | one page total |
| **State hub** | `clema.ai/institutions/[state-slug]/` | `/institutions/texas/`, `/institutions/california/`, `/institutions/new-york/` (~50 pages) |
| **Carnegie hub** | `clema.ai/institutions/[carnegie-slug]/` | `/institutions/research-1/`, `/institutions/research-2/`, `/institutions/research-colleges-universities/` (~33 Carnegie classes) |
| **Leaf (institution)** | `clema.ai/institutions/[institution-slug]/` | `/institutions/university-of-texas-austin/`, `/institutions/mit/`, `/institutions/stanford-university/` (~6,072 pages) |

**Linking rules:**
- Every leaf page links *up* to its State hub AND its Carnegie hub.
- Each State hub links sideways to every Carnegie hub represented in that state.
- Each Carnegie hub links sideways to top-N State hubs by institution count.

## 1. SEO templates

### 1.1 Title and meta description (per page type)

| Page type | Title template | Meta description template |
|---|---|---|
| Institution leaf | `[Institution Name] — Enrollment, Tuition & Outcomes Data [Year]` | `Key federal data for [Institution Name]: enrollment, graduation rate, tuition cost, and peer benchmarks. Updated [Year] from all the federal data sources.` |
| State hub | `Colleges & Universities in [State] — Enrollment & Outcomes Data | Clema` | `[N] institutions in [State]. Browse enrollment, graduation rates, tuition, and IPEDS data for every college and university. Updated [Year].` |
| Carnegie hub | `[Carnegie Label] Institutions — Federal Data & Peer Benchmarks | Clema` | `[N] [Carnegie Label] institutions. Compare enrollment, graduation rates, tuition, and IPEDS outcomes data. Updated [Year] from Carnegie Classifications.` |

### 1.2 H1 variants for the institution leaf (programmatic generation)

| Condition | H1 template |
|---|---|
| Default | `[Institution Name]: Enrollment, Tuition & Outcomes Data [Year]` |
| Community college (Carnegie `Associate's`) | `[Institution Name]: Enrollment, Cost & Outcomes Data [Year]` |
| Research 1 | `[Institution Name]: Research University Data, Outcomes & Peers [Year]` |

### 1.3 Schema.org markup
Every page emits JSON-LD as `https://schema.org/Dataset`. Required properties per page: `name`, `description`, `url`, `keywords`, `creator`, `sourceOrganization`, `temporalCoverage` (year), `license`, `variableMeasured` (array — one entry per metric block).

### 1.4 Target keyword families
Each leaf page targets the following keyword families (substitute institution short-name like `mit`):

- `<inst> graduation rate`
- `<inst> ranking`
- `<inst> common data set` / `<inst> common data set pdf`
- `<inst> enrollment` / `<inst> total enrollment` / `<inst> undergraduate enrollment`
- `<inst> cost` / `<inst> tuition` / `<inst> net price`
- Demographic long-tail: `<inst> class of 2028 black enrollment percentage`, `<inst> class of 2028 black enrollment 5%`, `<inst> class of 2028 black enrollment drop`, etc.

The page must rank on these — every keyword family corresponds to one of the blocks below.

---

## 2. Source legend (used in every field table)

| Tag | Meaning |
|---|---|
| `IPEDS` | Existing `ipeds.*` schema in the local DB (242 tables, 2012-2025) |
| `SCORECARD` | Existing `score_card.fact_institution_yearly` (and friends) |
| `PSEO` | Existing `pseo.*` schema (state-level earnings) |
| `IPEDS_DICT` | `ipeds_data_dictionary.*` — label/value-code lookups |
| `WIKI` | Wikipedia REST API or HTML parse (per institution) |
| `BRANDFETCH` | brandfetch.io API |
| `HERD` | NSF HERD CSV (one-time download) |
| `NIH` | NIH RePORTER API |
| `NSF` | NSF Awards Search API |
| `USASPENDING` | USAspending.gov POST API (joins on UEI) |
| `SCRAPE_HTML` | scrape institution website (HTML page) — discovery via search + crawl4ai |
| `SCRAPE_PDF` | scrape institution website (PDF download) |
| `SCRAPE_API` | scrape third-party API (e.g. HigherEdJobs) |
| `LLM` | derived via LLM extraction/summarization from another source's raw payload |
| `DERIVED` | computed in our code from other fields (rank, peers, slug) |
| `MANUAL` | requires human curation (no automated source) |
| `GAP` | no current source — open question |

Where multiple tags appear, the first is primary, others are fallback.

---

## 3. Institution leaf page — all 12 blocks, all fields

Order matches keyword volume priority from the SEO research. Every field below appears on the page; every field maps to exactly one source.

### Block 00 — Hero / Identity

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| Institution name | IPEDS | `ipeds.institutions_2024.instnm` | |
| City | IPEDS | `ipeds.institutions_2024.city` | |
| State (abbr + full) | IPEDS + IPEDS_DICT | `institutions_2024.stabbr` + lookup table | |
| ZIP | IPEDS | `institutions_2024.zip` | |
| Control (Public / Private NP / For-profit) | IPEDS + IPEDS_DICT | `institutions_2024.control` + `ipeds.control_codes` | |
| Carnegie classification label + slug | IPEDS + IPEDS_DICT | `institutions_2024.c21basic` + `ipeds.carnegie_codes` | drives `/research-1/` hub slug |
| HBCU flag | IPEDS | `institutions_2024.hbcu` | |
| HSI flag | SCORECARD | `fact_institution_yearly.is_hispanic_serving` | not in raw IPEDS |
| Tribal flag | IPEDS | `institutions_2024.tribal` | |
| Hospital / Medical / Land-grant flags | IPEDS | `institutions_2024.hospital`, `.medical`, `.landgrnt` | |
| Accreditor name(s) | DAPIP | (Phase 2) scrape DAPIP for `unitid` | basic accreditor also in `ipeds.institutions_2024` derived |
| Logo image URL | BRANDFETCH | `landing.brand_assets.logo_url` | |
| Brand color (primary/secondary) | BRANDFETCH | `landing.brand_assets.colors_primary`, `.colors_secondary` | |
| Brand fonts | BRANDFETCH | `landing.brand_assets.fonts` | |
| Web address | IPEDS | `institutions_2024.webaddr` | normalized to `landing.institutions.canonical_root_url` |
| Phone | IPEDS | `institutions_2024.gentele` | |
| Chief executive name + title | IPEDS | `institutions_2024.chfnm`, `.chftitle` | |
| OPEID, EIN, UEI | IPEDS | `.opeid`, `.ein`, `.ueis` | UEI is the join key to USAspending |
| Data source line ("Federal data from IPEDS [Year]. Last updated [date].") | DERIVED | render at page build | |

**Web scraping needed for Block 00:** None of the fundamentals (BrandFetch is API, not a scrape; logo is an image URL).

### Block 01 — Admissions Data

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| Acceptance rate (%) | SCORECARD → IPEDS | `fact_institution_yearly.admission_rate` → `ipeds.drv_admissions.dvadm01` | Scorecard cleaner |
| Total applicants | IPEDS | `ipeds.admissions.applcn` (or equivalent) | |
| Total admitted | IPEDS | `ipeds.admissions.admssn` | derived rate sanity-checks Scorecard |
| Admissions yield (%) | DERIVED | enrolled / admitted | |
| SAT middle 50th (reading 25th/75th, math 25th/75th, writing 25th/75th) | SCORECARD | `fact_institution_yearly.sat_reading_25th/75th`, `.sat_math_25th/75th`, `.sat_writing_25th/75th` | |
| SAT average | SCORECARD | `.sat_avg` | |
| ACT middle 50th (cumulative, english, math) | SCORECARD | `.act_cumulative_25th/75th`, `.act_english_25th/75th`, `.act_math_25th/75th` | |
| GPA requirement / average | IPEDS | `admissions.satvr25`/`admssn_gpa` (where reported) | sparse — show only if present |
| First-time, full-time freshmen enrolled | IPEDS | `admissions.enrlt` | |

**Web scraping needed for Block 01:** None (all federal data).

### Block 02 — Tuition & Cost

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| In-state tuition & fees | SCORECARD → IPEDS | `fact_institution_yearly.tuition_in_state` → `ipeds.cost1.tuition1`/`cost_tuition_fees` | |
| Out-of-state tuition & fees | SCORECARD → IPEDS | `.tuition_out_of_state` → `ipeds.cost1.tuition2` | |
| Required fees | IPEDS | `cost_tuition_fees.fee1`/`fee2` | |
| Room & board (on-campus / off-campus) | IPEDS | `ipeds.cost1.rmbrdoncmp`, `.rmbrdoffcmp` | |
| Books & supplies | IPEDS | `ipeds.cost1.bookssupplies` | |
| Total cost of attendance (in-state / out-of-state) | DERIVED | tuition + fees + room/board + books | |
| Net price by income band — $0–30k | SCORECARD | `fact_institution_yearly.net_price_public_0_30k` / `.net_price_private_0_30k` | matches SEO doc spec exactly |
| Net price by income band — $30–48k | SCORECARD | `.net_price_*_30_48k` | |
| Net price by income band — $48–75k | SCORECARD | `.net_price_*_48_75k` | |
| Net price by income band — $75–110k | SCORECARD | `.net_price_*_75_110k` | |
| Net price by income band — $110k+ | SCORECARD | `.net_price_*_110k_plus` | |
| Average net price overall | SCORECARD | `.avg_net_price_public` / `.avg_net_price_private` | |
| % students receiving federal financial aid | SCORECARD | `.federal_loan_rate` (proxy) + IPEDS SFA | |
| % students receiving Pell grants | SCORECARD | `.pell_grant_rate` | |
| Average institutional grant aid | IPEDS | `ipeds.financial_aid.iagrnt_a` (avg institutional grant amount) | |
| Tuition trend (last 5 yrs) | IPEDS | `institutions_<year>` series joined | |

**Web scraping needed for Block 02:** None. Optional later: scrape the institution's "Net Price Calculator" landing URL (a single link, not the calculator itself) — Phase 2 enhancement.

### Block 03 — Graduation & Outcomes

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| 6-year graduation rate (150% time) — overall | IPEDS → SCORECARD | `ipeds.drv_graduation_rates.dvgr15` / `graduation_rates_2024` → `fact_institution_yearly.completion_rate_overall` | |
| 4-year graduation rate (100% time) | IPEDS | `ipeds.drv_graduation_rates.dvgr12` / `graduation_rates_2024.grrtot12` | |
| 200% time graduation rate | IPEDS | `ipeds.graduation_rates_200pct` | |
| Retention rate — full-time students | IPEDS | `ipeds.fall_enrollment_retention.ret_pcf` | |
| Retention rate — part-time students | IPEDS | `ipeds.fall_enrollment_retention.ret_pcp` | |
| Pell grant recipient graduation rate | IPEDS | `ipeds.graduation_rates_pell_ssl.pgr*` | |
| Non-Pell graduation rate | IPEDS | `ipeds.graduation_rates_pell_ssl.npgr*` | |
| Transfer-out rate | IPEDS | `ipeds.outcome_measures_2024.om_*` | |
| 8-year completion (Outcome Measures) | IPEDS | `outcome_measures_2024` | richer signal than 6-yr for non-traditional |
| Peer-group avg comparison (graduation) | DERIVED | avg over `landing.peer_groups` for institution | |
| State avg comparison (graduation) | DERIVED | avg over IPEDS institutions in same state + control | |
| Median earnings 6/8/10 years after enrollment | SCORECARD | `fact_institution_yearly.earnings_6yr_median`/`.earnings_8yr_median`/`.earnings_10yr_median` | block straddles to outcomes |
| Median debt of completers / non-completers | SCORECARD | `.median_debt_completers`, `.median_debt_non_completers` | |
| Repayment rate (1/3/5/7 yr) | SCORECARD | `.repayment_1yr_rate`, etc. | |
| Cohort Default Rate (CDR) | CDR | (Phase 2) `landing.cdr` table | Federal Student Aid CDR CSV |

**Web scraping needed for Block 03:** None.

### Block 04 — Enrollment & Demographics

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| Total headcount enrollment (fall) | IPEDS | `ipeds.fall_enrollment_2024.eftotlt` | |
| Undergraduate enrollment | IPEDS | `ipeds.fall_enrollment_2024.efugtot` (or `drv_fall_enrollment`) | |
| Graduate enrollment | IPEDS | `ipeds.fall_enrollment_2024.efgrtot` | |
| Full-time vs part-time split | IPEDS | `ipeds.fall_enrollment_2024.eftotlft`/`eftotlpt` | |
| 12-month unduplicated headcount | IPEDS | `ipeds.enrollment_12month_2024.ftugentn` | |
| Online-only students (% enrolled exclusively in distance ed) | IPEDS | `ipeds.fall_enrollment_distance_2024.efdetot` / `efdeexc` | |
| Male / female breakdown | IPEDS | `fall_enrollment_2024.eftotlw`/`eftotlm` (or `efrace*`) | |
| Race/ethnicity breakdown (all IPEDS categories) | IPEDS + SCORECARD | `fall_enrollment_2024.efaiant`/`efbkaat`/`efhispt`/`efasiat`/`efnhpit`/`efwhitt`/`ef2morat`/`efnralt`/`efunknt` and `fact_institution_yearly.demo_*` | both sources for cross-check |
| First-generation student % | SCORECARD | `fact_institution_yearly.firstgen` (where reported) | sparse — show only if present |
| Pell recipient % (in-cohort trend) | SCORECARD | `.pell_grant_rate` over years | |
| Age distribution | IPEDS | `ipeds.fall_enrollment_age_2024` | |
| State of residence (in-state vs out-of-state breakdown) | IPEDS | `ipeds.fall_enrollment_residence_2024` | for first-time freshmen |
| Enrollment by major / CIP | IPEDS | `ipeds.fall_enrollment_major_2024` | |
| Class-of-YYYY demographic snapshots (e.g. "Class of 2028 Black enrollment") | DERIVED | per-year, per-race join of `fall_enrollment_2024.efbkaat` for first-time freshmen | required for keyword long-tail |

**Web scraping needed for Block 04:** None.

### Block 05 — Academic Programs & Degrees

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| Degree levels offered (Cert / Assoc / Bach / Mast / Doct) | IPEDS | `institutions_2024.hloffer`, `.ugoffer`, `.groffer`, `.hdegofr1` | |
| Top CIP program areas by completions (last year) | IPEDS | `ipeds.completions_by_program_2024.cipcode` aggregated | |
| Number of distinct programs offered | IPEDS | `ipeds.programs_offered` (or count distinct `cipcode` in completions) | |
| Online programs offered (flag + count) | IPEDS | `ipeds.programs_offered.distance` | |
| Degree levels offered by program | IPEDS | `ipeds.completions_by_program_2024.awlevel` | |
| Field-of-study earnings (per program → earnings) | SCORECARD | `score_card.fact_field_of_study_*` | |
| Field-of-study debt | SCORECARD | `score_card.fact_field_of_study_*` | |
| Program-level accreditation (e.g. ABET, AACSB) | DAPIP | (Phase 2) scrape DAPIP per `unitid` | |
| Catalog / Bulletin URL | SCRAPE_HTML | scrape root site for `/catalog`, `/bulletin`, `/programs` | Phase 2 |

**Web scraping needed for Block 05:**
- (Phase 2) `catalog_url` — single URL discovered via site:search `catalog OR bulletin OR programs`.

### Block 06 — Faculty & Staff

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| Student-to-faculty ratio | IPEDS | `ipeds.institutions_2024.stufacr` (if in HD) or derived from EAP | |
| Total instructional staff (FTE) | IPEDS | `ipeds.staff_instructional_summary` | |
| Full-time vs part-time faculty | IPEDS | `ipeds.staff_instructional_summary.ftft`/`.ptft` | |
| Faculty by rank (Prof / Assoc / Asst / Instructor / Lecturer) | IPEDS | `ipeds.staff_instructional_detail_2024` | |
| Faculty by tenure status | IPEDS | `ipeds.staff_instructional_detail_2024` | |
| % faculty with terminal degree | IPEDS | derive from `staff_instructional_detail_2024` (PhD field) | sparse |
| Average faculty salary by rank | IPEDS | `ipeds.salaries_instructional` | |
| New faculty hires | IPEDS | `ipeds.staff_new_hires_2024` | |
| Non-instructional staff | IPEDS | `ipeds.salaries_noninstructional`, `ipeds.staff_occupation` | |
| Employees assigned (FTE) | IPEDS | `ipeds.employees_assigned` | |

**Web scraping needed for Block 06:** None.

### Block 07 — Rankings & Peers

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| Carnegie classification (with link to Carnegie hub) | IPEDS | (see Block 00) | |
| IPEDS peer group (auto-generated from Carnegie + state + control) | DERIVED | `landing.peer_groups WHERE algorithm='carnegie_state_control_v1'` | |
| 2–5 named peer institutions (with links) | DERIVED | top-5 from `peer_groups` | |
| State rank — graduation rate (within control type) | DERIVED | rank over `score_card.fact_institution_yearly.completion_rate_overall` partitioned by state+control | |
| State rank — retention rate (within control type) | DERIVED | rank over IPEDS retention partitioned | |
| State rank — net price | DERIVED | rank over Scorecard net price | |
| Carnegie-class rank (graduation, retention, net price) | DERIVED | rank over Scorecard partitioned by Carnegie | |
| US News rank, THE rank, QS rank | GAP / MANUAL | (deferred — scraping ranking sites is TOS-risky) | |
| Wikidata QID (for linked-data SEO) | WIKI | `landing.wiki_summaries.wikidata_qid` | |

**Web scraping needed for Block 07:** None for v1 (algorithmic peers only). Third-party rankings deferred.

### Block 08 — Notable Alumni

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| Notable alumni list (name, field, brief identifier) | WIKI → MANUAL | `landing.notable_alumni` populated by `connectors/wikipedia.py` "Notable alumni" section parse; fall back to manual curation for institutions with thin Wikipedia | |
| Per-alumnus link (Wikipedia URL) | WIKI | `notable_alumni.wikipedia_url` | |
| Short bio | WIKI | `notable_alumni.short_bio` | first sentence of the list item |

**Web scraping needed for Block 08:**
- **WIKI: alumni section parse** — already implemented in `connectors/wikipedia.py`. Falls back to 0 entries when Wikipedia has no "Notable alumni" section.

### Block 09 — IR Team Resources

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| IR office name (e.g. "Office of Institutional Research") | SCRAPE_HTML + LLM | `landing.ir_offices.office_name` | extracted from IR landing page |
| IR office page URL | SCRAPE_HTML | `landing.ir_offices.page_url` | |
| IR office 1-sentence summary | LLM | `landing.ir_offices.page_summary` | LLM summary of IR page |
| Parent division (Provost / Strategic Planning / IT) | LLM | `landing.ir_offices.parent_division` | inferred from page content |
| IR team member list (name, title, email, phone) | SCRAPE_HTML + LLM | `landing.ir_contacts.*` rows | from IR landing + `/staff` or `/team` sub-page |
| LinkedIn URLs (for IR team) | SCRAPE_HTML / GAP | optional; scrape from team page or skip | |

**Web scraping needed for Block 09 (top priority):**
- **`ir_page`** — discover canonical IR landing URL.
  - Discovery: `site:<registrable_domain> "institutional research"` via Tavily/Google CSE/DDG, plus crawl4ai `BestFirstCrawlingStrategy` from root scored on `["institutional research", "office of ir", "ir office", "institutional effectiveness", "institutional analytics", "institutional planning"]`.
  - Validation: Claude Haiku LLM judge — "is this the official IR office landing page?"
  - Store: `landing.ir_offices`.
- **`ir_team`** — depends on `ir_page` URL.
  - Discovery: fetch IR landing, take same-domain links matching `["staff", "team", "people", "directory", "personnel"]`; fetch top 3.
  - Extraction: Claude Sonnet structured extract: `{members: [{name, title, email, phone}], office_name, summary}`.
  - Store: `landing.ir_contacts` (one row per member) + update `landing.ir_offices`.

### Block 10 — Common Data Set [Year]

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| CDS URL (most recent year) | SCRAPE_HTML | `landing.cds_links.cds_url` | |
| CDS year | DERIVED | year regex over the URL/title | |
| Is-PDF flag | DERIVED | URL suffix | |
| Historical CDS URLs (last 5 yrs) | SCRAPE_HTML | additional rows in `landing.cds_links` | |

**Web scraping needed for Block 10:**
- **`cds`** — `site:<registrable_domain> "common data set"`. URL-only — no body extraction needed (the CDS file is just a deliverable link). Confidence high if URL matches `/cds`, `/common-data-set`, or `cds_<year>.pdf`. Store: `landing.cds_links`.

### Block 11 — Jobs at [Institution Name]

| Field | Source | Concrete reference | Notes |
|---|---|---|---|
| List of current IR openings (title + apply URL) | SCRAPE_HTML + LLM + SCRAPE_API | `landing.jobs WHERE category='ir'` | |
| List of other openings (title + apply URL) | SCRAPE_HTML + LLM + SCRAPE_API | `landing.jobs WHERE category='other'` | |
| Posted date / expires date | SCRAPE_API | when source provides them | |
| Source board (HigherEdJobs / institution site / LinkedIn) | DERIVED | `jobs.source_board` | |
| Static fallback link to careers page | SCRAPE_HTML | `landing.jobs.source_url` (the careers landing) | shown when no open IR roles |

**Web scraping needed for Block 11:**
- **`jobs`** — two-tracked:
  - **Track A — Institution careers page**: `site:<registrable_domain> careers OR jobs OR employment`; pick top result; parse all anchor links with text 5-150 chars; LLM categorize each title as `ir` vs `other` using prompt above.
  - **Track B — HigherEdJobs API**: filter by institution name; tag results as `category='ir'` for IR titles via the same LLM categorizer. *(Needs `HIGHEREDJOBS_API_KEY`.)*

### Final CTA block

| Element | Source |
|---|---|
| "Cite this page" button → BibTeX / APA generator | DERIVED — generated at render time from `landing.mv_institution_page` |
| "Share this as a report" → PDF export | DERIVED |
| "Last updated" | `landing.mv_institution_page.landing_last_updated_at` |

---

## 4. Additional v1 page block — Research Funding (added after SEO doc)

Not in the original SEO doc but added in design v1.1. Drives R1/R2 institution credibility and matches the keyword family `"<inst> research funding" / "<inst> NIH grants" / "<inst> NSF awards"`.

| Field | Source | Concrete reference |
|---|---|---|
| Total R&D expenditures (FY-latest) | HERD | `landing.research_expenditures.total_rd_expend_usd` |
| Federal R&D share | HERD | `.federal_rd_usd` / `.total_rd_expend_usd` |
| R&D by field (life sciences, physical sciences, engineering, etc.) | HERD | `.by_field` JSONB |
| National R&D rank | HERD | `.rank_national` |
| Total federal award $ (last 3 FY, all agencies) | USASPENDING | `landing.mv_research_funding_summary.amount_total_last_3y_usd` |
| Award count (last 3 FY) | USASPENDING | `.award_count_last_3y` |
| Top 5 PIs by award $ | DERIVED | over `landing.research_awards.pi_name` |
| Top 5 individual awards (title, agency, $) | DERIVED | over `landing.research_awards ORDER BY amount_total_usd DESC LIMIT 5` |
| Agency breakdown ($ by NIH / NSF / DOE / NASA / DoD / etc.) | DERIVED | over `landing.research_awards.agency` |
| Recent NIH/NSF award titles (last 12 months) | NIH + NSF | `landing.research_awards WHERE source_system IN ('nih_reporter','nsf_awards') AND start_date >= now()-interval '12 months'` |

**Web scraping needed for research funding:** None — all API-based via UEI.

---

## 5. State hub page — fields

URL: `clema.ai/institutions/[state-slug]/`

| Section | Field | Source |
|---|---|---|
| H1 | `Colleges & Universities in [State]: Enrollment, Tuition & Outcomes Data [Year]` | DERIVED |
| Intro | Total institutions in state | DERIVED (count from IPEDS) |
| Intro | Breakdown by Carnegie class | DERIVED |
| Intro | Breakdown by control (public / private NP / for-profit) | DERIVED |
| Summary stats | Median graduation rate (in state) | DERIVED over IPEDS + Scorecard |
| Summary stats | Median tuition (in-state, public) | DERIVED |
| Summary stats | Total enrollment in state | DERIVED |
| Table | All institutions in state: name, city, Carnegie class, graduation rate, tuition, linked | DERIVED |
| Filters | Carnegie class, control, city | client-side over the table |
| Internal links | Carnegie hubs for each class represented | DERIVED |
| CTA | "Browse IR job openings at [State] institutions" | DERIVED + `landing.jobs` aggregate |
| CTA | "See how Clema supports IR teams across [State]" | static |

**Web scraping needed for state hub:** None (pure DB aggregation over IPEDS + landing.jobs).

---

## 6. Carnegie hub page — fields

URL: `clema.ai/institutions/[carnegie-slug]/`

| Section | Field | Source |
|---|---|---|
| H1 | `[Carnegie Label]: Federal Data & Peer Benchmarks [Year]` (variants per Carnegie class) | DERIVED |
| Definition | 2–3 sentence description of this Carnegie class | MANUAL (one-time content authoring per class) |
| Key thresholds | Research spending + doctorate-production requirements for the class | MANUAL or scraped from ACE Carnegie site (one-time) |
| Summary stats | Count of institutions in class | DERIVED |
| Summary stats | Median graduation rate, median tuition | DERIVED |
| Table | All institutions in class: name, state, graduation rate, tuition, enrollment, linked | DERIVED |
| Filters | State, control | client-side |
| Internal links | Top State hubs by institution count in this class | DERIVED |
| CTA | "See how Clema supports IR teams at [Carnegie class] institutions" | static |
| CTA | "Browse IR job openings at [Carnegie class] universities" | DERIVED + `landing.jobs` aggregate |

**Web scraping needed for Carnegie hub:** Only the one-time class-definition copy (manual authoring, ~33 classes).

---

## 7. Hub root page (`clema.ai/institutions/`)

| Section | Source |
|---|---|
| Total institutions count | DERIVED (count from `landing.institutions WHERE enabled`) |
| 50 state hub links | DERIVED |
| ~33 Carnegie hub links | DERIVED |
| Featured institutions (e.g. R1s, HBCUs) | DERIVED |
| Search-by-name autocomplete | DERIVED (over `landing.institutions.slug`) |

---

## 8. Consolidated list — every web-scrape target (for the engineering team)

Sorted by priority. Each entry has: discovery, extraction, store, blockers.

### Tier A — institutional-website scrapes (v1)

| # | source_type | Page block | Discovery | Extraction | Store | Status | Needs |
|---|---|---|---|---|---|---|---|
| 1 | `ir_page` | Block 09 | site:search `"institutional research"` + crawl4ai BFS scored on IR keywords | Claude Haiku judge on top candidate | `landing.ir_offices` | ✅ built | ANTHROPIC_API_KEY, TAVILY for better discovery |
| 2 | `ir_team` | Block 09 | follow IR page → same-domain links matching team/staff/people/directory; fetch top 3 | Claude Sonnet structured extract `{members[], office_name, summary}` | `landing.ir_contacts`, update `landing.ir_offices` | ✅ built | ANTHROPIC_API_KEY |
| 3 | `factbook` | linked from page | site:search `factbook OR "fact book"`; scrape landing pages for PDF links matching `factbook` | PDF download + pdfplumber text extract; LLM optional table extract (Phase 2) | `landing.documents` (`doc_type='factbook'`) | ✅ built | ANTHROPIC for judge |
| 4 | `cds` | Block 10 | site:search `"common data set"`; year regex over URL | URL only | `landing.cds_links` | ✅ built | better search provider |
| 5 | `strategic_plan` | linked from page | site:search `"strategic plan"`; PDF link discovery | PDF download + Claude Haiku 3-sentence summary | `landing.documents` (`doc_type='strategic_plan'`) | ✅ built | ANTHROPIC for summary |
| 6 | `jobs` | Block 11 | site:search `careers OR jobs OR employment`; parse anchors on top result | Claude Haiku categorize `ir` vs `other`; HigherEdJobs API as Track B | `landing.jobs` | ✅ built (Track A) | ANTHROPIC, HIGHEREDJOBS_API_KEY |

### Tier B — third-party APIs (v1)

| # | source_type | Page block | Source | Status | Needs |
|---|---|---|---|---|---|
| 7 | `brandfetch` | Block 00 | brandfetch.io API by domain | ✅ built | BRANDFETCH_API_KEY |
| 8 | `wikipedia` | Block 00, Block 08 | Wikipedia REST search + summary + page HTML parse | ✅ built (verified end-to-end) | none |
| 9 | `research_herd` | Research block | NSF HERD CSV (one-time download) | ✅ built (awaiting CSV) | HERD CSV at `assets/herd/herd_latest.csv` |
| 10 | `research_nih` | Research block | NIH RePORTER POST API | ✅ built (verified: MIT 867 awards $52M) | none |
| 11 | `research_nsf` | Research block | NSF Awards Search API + strict post-filter | ✅ built (verified: MIT 40 awards $23M) | none |
| 12 | `research_usaspending` | Research block | USAspending POST API joined on UEI | ✅ built (verified: MIT $1.06B in 50 awards) | none |

### Tier C — deferred to Phase 2

| # | source_type | Page block | Source | Why deferred |
|---|---|---|---|---|
| 13 | `accreditation_dapip` | Block 00 (accreditor names), Block 05 (program accred) | DAPIP web scrape (no API) | adds breadth not depth — initial accreditor name already in IPEDS |
| 14 | `athletics_eada` | (new athletics block) | EADA CSV download | not in the SEO doc's 12 blocks |
| 15 | `cdr` | Block 03 (debt context) | Federal Student Aid CDR CSV | optional, low-priority |
| 16 | `notable_alumni` (curated) | Block 08 | manual curation for thin-Wiki institutions | scale problem; defer |
| 17 | `catalog_url` | Block 05 | site:search `catalog OR bulletin OR programs` | nice-to-have |
| 18 | `mission_statement` | Block 00 | site:search `mission OR vision OR about` | nice-to-have |
| 19 | `board_of_trustees` | (new governance block) | site:search `trustees OR regents OR board` + LLM extract | high-GTM but deferred |
| 20 | `leadership_team` | (new governance block) | site:search `leadership OR president OR provost OR administration` + LLM extract | high-GTM but deferred |
| 21 | `irs_990` | (new financials block) | ProPublica Nonprofit Explorer API | high-value but deferred |
| 22 | `clery_act` | (new safety block) | OPE Campus Security tool + institution ASR PDFs | high-traffic but deferred |
| 23 | `dashboards` | (linked from page) | site:search `dashboard OR tableau OR analytics` | useful for IR-team audience |
| 24 | `press_kit` | (linked from page) | site:search `press kit OR media kit` | nice-to-have |
| 25 | `social_handles` | (footer) | scrape footer of root page for x.com/facebook.com/linkedin.com/youtube.com links | nice-to-have |
| 26 | `grants_gov` | (RFPs block) | Grants.gov API | for procurement-oriented audience |
| 27 | `sam_gov` | (RFPs block) | SAM.gov API | for procurement-oriented audience |

---

## 9. Per-institution scrape budget (v1)

What hits a single institution's website end-to-end:

| Stage | Pages fetched | Why |
|---|---|---|
| `brandfetch` | 0 from inst site (API only) | |
| `wikipedia` | 0 from inst site | |
| `ir_page` discovery | 1 site:search request + (worst case) crawl4ai BFS of up to 15 pages | `q_crawl` rate-limited 1 req / 2 sec / domain |
| `ir_page` extraction | 0 (judge runs on the search snippet) | |
| `ir_team` | 1 (IR landing) + up to 3 (team sub-pages) = **4 pages** | |
| `factbook` | 1 site:search + up to 2 landing pages + 1 PDF download | |
| `cds` | 1 site:search, 0 fetches | URL-only |
| `strategic_plan` | 1 site:search + up to 2 landing + 1 PDF | |
| `jobs` | 1 site:search + 1 careers landing fetch | |
| `research_*` | 0 from inst site | |

**Total per institution:** ≤ ~25 page fetches on the institution's site (worst case with deep crawl). Throttled at 1 req / 2 sec → ≤ 50s per institution. Across 6,072 institutions at concurrency=4 → ~21 hours for one full pass.

**LLM cost per institution (with Anthropic key):**
- `ir_page` judge: 1 Haiku call (~$0.0005)
- `ir_team` extract: 1 Sonnet call (~$0.005-0.01)
- `factbook` judge: 1 Haiku call (~$0.0005)
- `strategic_plan` summary: 1 Haiku call (~$0.001)
- `jobs` categorize: 1 Haiku call (~$0.001)
- **Total: ~$0.01-0.015 per institution → $60-90 per full crawl of 6,072 institutions**

---

## 10. Known data gaps (no current source)

| Field | SEO doc reference | Why missing | Resolution |
|---|---|---|---|
| Notable alumni for institutions with no Wikipedia article | Block 08 | Wikipedia coverage of community colleges + small institutions is thin | accept partial; manual curation for marquee institutions |
| US News / THE / QS rank | Block 07 | TOS-risky to scrape rankings sites | manual entry; or link out only |
| First-generation student % | Block 04 | Scorecard publishes for some institutions only | show conditionally; don't synthesize |
| GPA average | Block 01 | IPEDS reports inconsistently | show conditionally |
| % faculty with terminal degree | Block 06 | IPEDS HR survey is partial | show conditionally |
| Accreditor *self-attribution* per page | Block 00 | DAPIP scrape is Phase 2 | use IPEDS basic accreditor in v1 |
| Athletics (EADA) | (not in original 12 blocks) | EADA CSV ingestion deferred | Phase 2 if user wants the block |
| Real-time tuition (live calculator) | Block 02 | institutions update outside IPEDS reporting cycle | accept lag of 1-2 academic years |

---

## 11. Cross-doc references

- **Architecture / pipeline design:** `docs/superpowers/specs/2026-05-24-institutions-landing-pages-design.md`
- **Original SEO research:** `assets/seo-research/Institutional_page (URL, Schema, Keywords, Structure).docx` (page-block spec) + `Institutional_page (URL, Schema, Keywords, Structure) (1).docx` (source-map)
- **Project memory for Claude:** `CLAUDE.md`
- **Sample scraped data (verified):** `data/mit_sample.json`, `data/aamu_sample.json`

---

## 12. Changelog

- **v1 (2026-05-24)** — first consolidated blueprint. Inlines both DOCX research docs, adds explicit per-field source mapping, calls out exact web-scrape targets and budget. Aligned with design spec v1.1 (research-funding block included).

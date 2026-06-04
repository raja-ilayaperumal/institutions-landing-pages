# Stats Narratives Pipeline Step — Design

**Status:** draft (2026-06-04)
**Owner:** Raja
**Pairs with:** `docs/data-blueprint.md` (field/source mapping), `docs/superpowers/specs/2026-05-24-institutions-landing-pages-design.md` (architecture)

## 1. Goal

Generate a few lines of factual "storytelling" prose per institution describing how its
key stats have trended, for rendering on the landing page (see the live `Enrollment Trends`
and `Graduation Trends` blocks on `clema.ai/institutions/...`). The prose is **deterministic
template** filled with verified figures — no LLM, no scraping, no quota risk.

Two grains are required:
- **`(unitid, data_year)`** — a durable per-year stats store that grows as new IPEDS/Scorecard
  years load.
- **`(unitid, section, data_year)`** — one narrative per section per year, so the frontend can
  request "graduation narrative for 2023" and render that year's page with that year's prose.

### Sections (v1)
`enrollment`, `admissions`, `graduation_outcomes`, `tuition_cost`.

### Decisions locked during brainstorming
- Generation: **deterministic template** (free, reproducible at 6k scale; matches screenshots).
- Output: **prose + the figures it cites** (so the frontend can drive cards/arrows from the
  same verified numbers).
- Year labels: **latest-available current year; baseline = latest year ≤ current−10, else
  earliest available.** No hardcoded 2025/2014 — labels always match the data on hand.
- Storage: **real tables via `ProvenanceWriter`** (not a SQL view) — re-template without
  re-reading sources, keep the provenance pattern, easy to extend.

## 2. Data sources (best-source-per-cell, per year)

Local DB (`clema_landing`) holds: `ipeds.*` (single 2024 snapshot tables **plus** multi-year
`drv_*` tables for `data_year` 2021–2024), `score_card.fact_institution_yearly` (1996–2023,
keyed on `institution_id` = IPEDS `unitid`), `score_card.dim_*`.

| Section | Metric | Primary source | Fallback |
|---|---|---|---|
| enrollment | total / undergrad / graduate | `ipeds.drv_fall_enrollment.enrtot/efug/efgrad` (per `data_year`) | `score_card.student_size` |
| enrollment | online (exclusively distance) | `ipeds.fall_enrollment_distance_2024` (current year only locally) | null |
| admissions | acceptance_rate | `score_card.admission_rate` (1996–2023, deep) | `ipeds.drv_admissions` |
| admissions | sat_avg, act_cumulative_25th/75th | `score_card.sat_avg/act_cumulative_*` | null |
| graduation_outcomes | grad_rate_150 (overall) | `score_card.completion_rate_overall` (deep) | `ipeds.drv_graduation_rates` |
| graduation_outcomes | retention_rate_ft, transfer_out_rate | `ipeds.fall_enrollment_retention` / `outcome_measures_2024` (best-effort, nullable) | null |
| graduation_outcomes | earnings_10yr_median, median_debt_completers | `score_card.earnings_10yr_median/median_debt_completers` | null |
| tuition_cost | tuition_in_state / out_of_state | `score_card.tuition_in_state/out_of_state` (deep) | null |
| tuition_cost | avg_net_price, pell_grant_rate | `score_card.avg_net_price_public/private`, `.pell_grant_rate` | null |

All columns nullable; a year row exists if **any** metric is present. Scorecard rates are
0–1 fractions → normalized to percentages on write. Grad enrollment derives as
`total − undergrad` when only the pair is present (per the IPEDS enrollment-mapping memory).

## 3. Table 1 — `landing.institution_stats_yearly` (durable per-year store)

One row per `(unitid, data_year)`. PK `(unitid, data_year)`. Idempotent upsert.

```
unitid                int        not null
data_year             smallint   not null
-- enrollment
total_enrollment      integer
undergrad_enrollment  integer
graduate_enrollment   integer
online_enrollment     integer
-- admissions
acceptance_rate       numeric(5,2)   -- percent
sat_avg               integer
act_cumulative_25th   integer
act_cumulative_75th   integer
-- graduation & outcomes
grad_rate_150         numeric(5,2)   -- percent
retention_rate_ft     numeric(5,2)   -- percent
transfer_out_rate     numeric(5,2)   -- percent
earnings_10yr_median  integer
median_debt_completers integer
-- tuition & cost
tuition_in_state      integer
tuition_out_of_state  integer
avg_net_price         integer
pell_grant_rate       numeric(5,2)   -- percent
-- faculty (future-friendly, nullable in v1)
student_faculty_ratio numeric(5,1)
-- provenance
sources               jsonb      -- {column: source_system} per-field provenance
parser_version        text       not null
fetched_at            timestamptz not null default now()
primary key (unitid, data_year)
```

Populated by `scripts/build_stats_yearly.py` — pure DB read/merge (no HTTP, no LLM). Runs
`INSERT ... ON CONFLICT (unitid, data_year) DO UPDATE`. Re-runnable; cheap.

## 4. Table 2 — `landing.institution_narratives` (`(unitid, section, year)` grain)

```
unitid          int        not null
section         text       not null   -- enrollment|admissions|graduation_outcomes|tuition_cost
data_year       smallint   not null   -- the "current" year the narrative is about
prose           text       not null
figures         jsonb      not null   -- {current_year, current_value, baseline_year,
                                      --  baseline_value, pct_change, direction, unit, metric}
headline_metric text       not null
parser_version  text       not null
generated_at    timestamptz not null default now()
primary key (unitid, section, data_year)
```

`direction ∈ {increase, decrease, flat}`. `pct_change` null when no baseline (single year).

## 5. Narrative generation — `narratives` connector (DERIVED, sibling of `peers`)

A DB-only connector (no `discover/validate/extract` HTTP). For each institution:

1. Read all `institution_stats_yearly` rows.
2. For each section: current = latest year where the section's headline metric is non-null;
   baseline = latest year ≤ current−10 with that metric, else earliest available.
3. Compute `pct_change = round((cur−base)/base * 100, 2)` and `direction`.
4. Fill the section template; emit one narrative row **per year that has the headline metric**
   (each compared to its own baseline), so the table is queryable by year.

### Templates (headline metric per section)
- **enrollment** (`total_enrollment`): *"The {cur} total students population is {N:,} at
  {Institution}, which is {|pct|}% {increase/decrease} compared to {base} enrollment. Discover
  how the size has changed by gender, school level, and enrollment type by year."*
- **admissions** (`acceptance_rate`): *"The {cur} acceptance rate is {x}% at {Institution},
  which is {|pct|}% {increase/decrease} compared to {base}. Explore admissions selectivity,
  test scores, and yield by year."*
- **graduation_outcomes** (`grad_rate_150`): *"The {cur} graduation rate is {x}% at
  {Institution}, which is {|pct|}% {increase/decrease} compared to {base} rate. Discover trends
  of graduation, transfer-out, and retention rate by year, plus the student-to-faculty ratio."*
- **tuition_cost** (`tuition_in_state`): *"The {cur} in-state tuition is ${N:,} at
  {Institution}, which is {|pct|}% {increase/decrease} compared to {base}. See how tuition, net
  price, and fees have changed by year."*

**Graceful degradation:** single year → drop the comparison clause ("The {cur} ... is {N} at
{Institution}.") + fixed second sentence. Headline metric missing for all years → no row for
that section (frontend renders the "−" placeholder).

Institution name normalization (strip IPEDS-verbose suffixes) reused from
`utils/institutions.py` for the `{Institution}` token.

## 6. MV change — `landing.mv_institution_page`

Add two `jsonb` columns (migration recreates the MV):
- `stats_yearly` — array of per-year metric rows, ascending by year (drives "by year" charts).
- `narratives` — `{ section: { latest_year: int, by_year: { "<year>": {prose, figures} } } }`.

## 7. Pipeline wiring

- `source_types` row `narratives` (`is_active = true`).
- Connector registered in the orchestrator registry; `db/dispatch.py` routes
  `source_type=="narratives"` → `writer.write_narratives(...)`; `db/writer.py` adds
  `write_narratives` (upsert into `institution_narratives`, provenance-stamped).
- **Run order:** `build_stats_yearly` (stage 1) → `narratives` connector (stage 2) → MV refresh
  (stage 3). Pilot 5 first, then full 6k. No per-source queue needed (DB-only, fast).

## 8. Migration

One Alembic migration: create `institution_stats_yearly`, create `institution_narratives`,
insert `source_types('narratives', true)`, drop+recreate `mv_institution_page` with the two new
columns (+ unique index for `REFRESH CONCURRENTLY`).

## 9. Tests

- pct-change math + rounding; direction thresholds (flat when |pct| < 0.5).
- baseline-year selection (≥10yr prior, else earliest; skips years missing the metric).
- single-year degradation (no comparison clause, `pct_change` null).
- percent normalization (Scorecard 0–1 → 0–100) and `{N:,}` formatting.
- golden outputs for MIT (166683) and Stanford across all four sections.
- `build_stats_yearly` idempotency (re-run = same rows).

## 10. Out of scope (v1)

LLM phrasing, faculty/research/demographics sections, ranking trends, hub-page narratives.
`student_faculty_ratio` column exists but is populated best-effort only.

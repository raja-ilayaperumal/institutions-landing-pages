# Scaling Runbook — From 180 → 6,072 Institutions

**Purpose:** the single doc a *cold session* reads to resume the full-corpus
scrape without re-deriving context. It answers: what's done, what's left, the
exact commands for one institution end-to-end, the batch loop for the rest,
and the cost guardrails that must never be tripped again.

**Last verified against live DB:** 2026-05-28 (db `clema_landing`).
**Pairs with:** `docs/scraping-process.md` (how the pipeline works),
`docs/data-blueprint.md` (what data goes on the page), `CLAUDE.md` (rules).

> If the numbers below feel stale, **re-run the snapshot query in §1** before
> trusting them. This doc is a frozen reading; the DB is the truth.

---

## 1. Progress snapshot (live query)

Run this first thing in any session to refresh the picture:

```bash
psql -U rajailayaperumal -d clema_landing -t -A -F'|' <<'SQL'
SELECT 'total_institutions', count(*) FROM landing.institutions;
SELECT 'exportable (slug set)', count(*) FROM landing.institutions WHERE slug IS NOT NULL;  -- ~all; "shipped" = json files on disk (see last line)
SELECT 'ir_pages', count(*) FROM landing.ir_pages;
SELECT 'ir_contacts (rows / insts)', count(*) || ' / ' || count(DISTINCT unitid) FROM landing.ir_contacts;
SELECT 'peers carnegie / dfr (insts)',
  (SELECT count(DISTINCT unitid) FROM landing.peer_groups WHERE algorithm='carnegie_state_control_v1') || ' / ' ||
  (SELECT count(DISTINCT unitid) FROM landing.peer_groups WHERE algorithm='dfr_v1');
SELECT 'research_awards', count(*) FROM landing.research_awards;
SELECT 'raw_payloads', count(*) FROM landing.raw_payloads;
SQL
ls -1 data/json/ | grep -v institutions.json | wc -l   # institutions exported to disk
```

**Snapshot as of 2026-05-28:**

| Metric | Value | Note |
|---|---:|---|
| Institutions seeded (`landing.institutions`) | **6,072** | the full target; all have slugs → all exportable |
| **Shipped** (institution JSONs in `data/json/`, in KV) | **180** | + `institutions.json` listing index = 181 keys |
| **Remaining to scrape+ship** | **~5,892** | the work this runbook covers |
| `ir_pages` | 177 | conf: 1.0→58, 0.9→105, 0.8→8, null→6 |
| `ir_contacts` | 570 rows / 124 institutions | head + team members |
| peers — `carnegie_state_control_v1` | 3,757 institutions | pure DB join, no scraping (cheap to extend) |
| peers — `dfr_v1` | 209 institutions | scraped from NCES DFR report |
| `research_awards` | 357 | NIH/NSF/USAspending/HERD APIs (UEI join) |
| `raw_payloads` | 2,814 | the re-parse time machine |

**Cohorts already shipped:** pilot (16, incl. the 5 named pilots), CA top-100,
CA rank 101–200 (79 of 100 — 21 for-profits/online held, no own-domain IR
office), CSU top-20 (subset of CA). Net unique ≈ 180.

---

## 1b. Concurrency & this-machine performance limits (READ before scaling)

Hard-won on an 8-core / 16 GB MacBook (2026-06). The pipeline is
**network-bound** (most time is spent waiting on headless-browser fetches),
and for-profit/trade-school sites serve content only to a real browser (httpx
gets 403, Playwright gets the page), so nearly every page needs a browser.

- **`CRAWL4AI_MAX_BROWSERS` (env, default 6)** — global cap on concurrent
  headless-Chromium instances, enforced by a semaphore in `core/crawler.py`.
  WITHOUT this, `--institution-concurrency` × per-target browser escalations
  spawn 100+ Chromium processes → load avg >190 → swap-thrash → the run
  effectively stalls (observed: 1 payload in 10 min). Each browser ≈ 250 MB.
- **Sustainable settings for 8-core/16 GB**: `--institution-concurrency 4
  --target-concurrency 2` with `CRAWL4AI_MAX_BROWSERS=4`. Keeps load ~10-12,
  RAM comfortable, no swap. conc 8 / browsers 6 pushed load to ~30 and is the
  upper edge; do NOT exceed it on this hardware.
- **Swap is the failure mode, not CPU.** Once `sysctl vm.swapusage` shows swap
  near-full (~20 GB), throughput collapses and a **reboot** is the only fast
  fix — macOS will not reclaim it mid-run. Always start a big run from a quiet
  baseline (`load < 6`, swap mostly free).
- Two hard caps protect every run (both in `core/crawler.py`): an
  `asyncio.wait_for` on each browser fetch (`timeout+10s`) so a hung site can't
  freeze a slot for 6 min, and a per-host browser **circuit breaker** that stops
  escalating to the browser after 3 proven-blocked (403/empty) escalations.
- **Resume marker = `landing.canonical_urls`**, NOT `raw_payloads`. It's
  written when the pipeline reaches the URL-decision stage even for empty-yield
  schools, but is absent for killed-mid-run ones — so empty for-profits aren't
  re-selected forever and partials ARE re-run. Pending =
  `cohort unitids − (SELECT DISTINCT unitid FROM landing.canonical_urls)`.

## 2. The end-to-end loop for ONE batch

This is the canonical sequence. Everything is keyed on `unitid`
(comma-separated, **never** space-separated — the runner does `int()` per
token and space-separated raises `ValueError`).

```bash
# 0. pick the next cohort: a text file, ONE unitid per line, `#` comment
#    lines allowed (see scripts/cohorts/ca_rank_101_200.txt). Build via §3.
COHORT=scripts/cohorts/<name>.txt
IDS=$(grep -vE '^\s*#|^\s*$' "$COHORT" | paste -sd, -)  # strip comments/blanks
                                                        # NOTE the trailing `-`:
                                                        # BSD/macOS paste needs it
                                                        # to read stdin, else it
                                                        # errors "usage: paste".

# 1. SCRAPE (Discover→Validate→Extract→Store for all web/doc source types)
#    Settings tuned for 8-core/16GB — see §1b. Start from a quiet baseline.
CRAWL4AI_MAX_BROWSERS=4 python scripts/run_many_parallel.py \
  --unitids "$IDS" --institution-concurrency 4 --target-concurrency 2

# 2. COMPUTE peers (Carnegie+state+control nearest-neighbors; pure DB)
python scripts/compute_peers.py --unitids "$IDS" --top-n 5

# 3. DFR peers (scraped IPEDS institution-selected comparison group)
python scripts/run_dfr.py --unitids "$IDS" --concurrency 4

# 3b. REFRESH the materialized views — *** DO NOT SKIP ***
#     export (step 4) reads landing.mv_institution_page, NOT the live tables.
#     run_many_parallel.py does NOT refresh, so without this the new scrape
#     data is invisible and pages ship with empty team/about/office while the
#     rows sit unused in the DB. The refresh is GLOBAL (all institutions), so
#     run it ONCE after the last batch's scrape, before exporting — not per id.
psql -d clema_landing -c "REFRESH MATERIALIZED VIEW landing.mv_research_funding_summary;"
psql -d clema_landing -c "REFRESH MATERIALIZED VIEW landing.mv_institution_page;"

# 4. EXPORT per-institution JSON (reads the MV → data/json/<slug>.json)
#    --cohort parses the file itself (bare unitids OR legacy pipe + # comments).
python scripts/export_landing_json.py --cohort "$COHORT"

# 5. VALIDATE before shipping (web cross-check QA; same --cohort/--top flags)
python scripts/validate_institution.py --cohort "$COHORT" --concurrency 4

# 6. REBUILD the listing index (stats, filters, categories across ALL shipped)
python scripts/build_institutions_index.py   # → data/json/institutions.json

# 7. UPLOAD to Cloudflare KV (dry-run first, then verify)
python scripts/upload_to_cloudflare_kv.py --dry-run
python scripts/upload_to_cloudflare_kv.py --verify
```

**Cohort-file format:** one bare `unitid` per line; lines starting with `#`
are comments and are ignored (see `scripts/cohorts/ca_rank_101_200.txt`).
`export_landing_json.py` and `validate_institution.py` parse this file
directly via `--cohort` (+ optional `--top N`). `run_many_parallel.py` takes
comma-separated ids, so strip comments and join — the `$IDS` line above does
this (`grep -vE '^\s*#|^\s*$' | paste -sd,`).

Single institution (debugging): `--unitid 166683` on `run_one_full.py`,
`compute_peers.py`, `export_landing_json.py`; `--unitids 166683` (plural) on
`run_many_parallel.py` / `validate_institution.py`.

---

## 3. Scaling plan: ordering the remaining ~5,892

The master list is `assets/all-institutions.csv` (6,050 active postsec, 2024).
We have **no national cohort files yet** — only CA + CSU. To go national,
generate cohorts and process in priority order.

**Recommended ordering (highest SEO/credibility value first):**

1. **Public 4-year / R1–R2 universities** — richest IR footprint, best data,
   highest search value. Order by enrollment desc within state.
2. **Public 2-year (community colleges)** — large count; IR often under
   `/prie/`, `/research/`, `/institutional-effectiveness/`. Expect thinner data.
3. **Private non-profit 4-year** — liberal arts, often IR inside Provost.
4. **For-profit / online / specialized** — process LAST and expect many to
   have **no own-domain IR office**; ship with `ir_page` null (do not fabricate).
   (This is why 21 of CA 101–200 were held — verified, not lazy.)

**Build a cohort file** (example — public 4-year by state, batches of 20):

```sql
-- VERIFIED columns on landing.institutions: control_label
-- ('Public' | 'Private not-for-profit' | 'Private for-profit'),
-- sector_label (encodes 2-/4-year + control), carnegie_basic_label,
-- stabbr, state_name, is_hbcu/is_hsi/is_tribal, registrable_domain.
-- NOTE: enrollment is NOT in this table — the CA cohorts were ranked by
-- ipeds.fall_enrollment_2024; reuse that query's enrollment join (filter
-- efalevel for the grand-total grain) if you want size ordering.
COPY (
  SELECT unitid FROM landing.institutions
  WHERE control_label = 'Public'
    AND sector_label ILIKE '%4-year%'
    AND unitid NOT IN (SELECT unitid FROM landing.ir_pages)   -- skip done
  ORDER BY stabbr
) TO STDOUT;
```

Slice into 20–25 id batches. **Batch size 20, institution-concurrency 3** is
the validated rhythm (keeps Serper/Tavily spend predictable and lets a human
audit each batch). ~5,892 / 20 ≈ **~295 batches**.

> **Decision needed from the user before a mass run:** confirm the ordering
> above and the per-batch size, and whether to auto-skip institutions already
> in `ir_pages` or re-run them with the latest prompt. Don't launch all 295
> batches unattended — the cost incident (§4) happened from an unattended loop.

---

## 4. Cost & search guardrails (NON-NEGOTIABLE)

The ₹4,000 / one-day Google CSE incident (2026-05-27) is why these exist.

- **Provider chain** (`core/search.py`): Serper → Tavily (multi-key
  round-robin) → Google CSE *(GATED OFF)* → DuckDuckGo. Falls through on
  empty results AND on exceptions.
- **Google CSE is disabled** behind `settings.enable_google_cse` (default
  `False`). Do **not** flip it on without first setting a **GCP daily quota
  cap** on `customsearch.googleapis.com`. CSE billing has no built-in ceiling.
- **Serper** is primary (~$0.001/query) but **runs out of credits** — it has
  twice. If you see `{"message":"Not enough credits"}`, stop and ask the user
  for a new `SERPER_API_KEY`; do not silently fall through and burn Tavily.
- **Tavily** is multi-key (`TAVILY_API_KEY`, `_1`, `_2`, `_3`); a key that
  hits quota is marked blocked for the process. It is the *binding capacity
  limit* for the full 6k run — raise the plan before mass-scraping.
- **Agent web-search budget**: `find_url(..., web_search_budget=3)` caps paid
  searches per institution; after that the agent must use the free
  `probe_url` / `fetch_snippet` tools. `max_steps=12` is fine — extra steps
  cost only LLM tokens, not search credits.
- **Never** bulk-scrape while Serper is empty *and* Tavily is exhausted *and*
  CSE is off — that leaves only DDG (low recall) and produces garbage.

**Pending cost work:** task #91 (Serper budget analysis / per-target query
reduction), task #61 (IR-page link-harvester to cut searches).

---

## 5. Open design decisions (resolve before they bite at scale)

### 5a. Dual office: Institutional Research **+** Institutional Effectiveness
**Status: NOT supported by current code.** `landing.ir_pages` has
`PRIMARY KEY (unitid)` and `write_ir_page` does `ON CONFLICT (unitid) DO
UPDATE` — so an institution can hold **only one** office page; scraping a
second office overwrites the first. `ir_contacts` has no column linking a
contact to *which* office. To support both (real at larger universities that
split IR from IE/Assessment) requires:
- Alembic migration: add `office_kind` (`ir`|`ie`|`combined`) to `ir_pages`,
  change PK to `(unitid, office_kind)`; add `office_kind` to `ir_contacts`.
- `writer.py`: key writes on `(unitid, office_kind)`.
- Pipeline: stop forcing one winner — allow up to one IR + one IE page; tag
  each contact with its office.
- Export + render: emit an `offices[]` array instead of a single `office`.

**This is the open question the user raised on 2026-05-28.** Most institutions
(esp. community colleges) have ONE combined office, so for ~80% this is just an
`office_kind='combined'` label. **Get user sign-off on the schema + whether a
combined office stays one `combined` row before implementing** — it changes the
export contract the frontend reads.

### 5b. Frontend peer cap
Some institutions have huge DFR comparison groups (Glendale = 94 — legitimate,
not a bug). The renderer may want to cap displayed DFR peers. Product decision,
not pipeline.

---

## 6. Data-quality traps (hard-won; re-check on every batch)

Standing directive: **data is shown to the institutions' own IR teams. Null/
blank beats wrong. Verify with web search when uncertain.** Specific traps:

- **Wrong-institution attribution** (alumni, contacts): bidirectional
  name-match in `connectors/notable_alumni.py` (`_url_matches_institution`)
  caught "Cañada College" ← "Upper Canada College" and "U.Tenn. Martin" ←
  "Martin University". Spot-check new institutions whose name is a substring
  of a more famous one.
- **PDF false-positives as IR page**: agent once returned a 65-page report PDF.
  Prompt now bans `.pdf/.docx/.xlsx/.pptx` as an office page → returns null.
- **Leadership-block trap**: a real IR page links President/Provost org charts;
  do NOT reject an IR URL just because its snippet leads with leadership names.
  (Caused the NPS miss.) Slug signal (`/institutional-research`, `/oie/`,
  `/prie/`, …) + any related term = HIGH confidence.
- **404 vs anti-bot**: 404 = genuinely absent (skip). 401/403/503/451 =
  Cloudflare anti-bot, page likely EXISTS → `fetch_snippet` (browser) to
  confirm. Wasting steps fetch_snippet-ing 404s starves the search budget.
- **Quoted-phrase searches miss real offices**: use UNQUOTED keywords —
  `site:<domain> institutional research planning effectiveness` — because
  office names reorder ("Planning, Research, and Resource Development").
- **Exec leadership ≠ IR contacts**: verified all 7 VP/Provost-titled contacts
  in `ir_contacts` are genuine IR-office heads, not mis-stored execs. If new
  contacts look like pure execs (CEO/President with no IR title), re-verify.

---

## 7. Cold-start resume checklist

1. Read this doc + `docs/scraping-process.md`.
2. Run §1 snapshot query — confirm shipped count and remaining.
3. Confirm search providers: Serper has credits? Tavily keys present? CSE still
   OFF? (`grep -i serper .env`; check `settings.has_serper`.)
4. Pick / build the next cohort (§3). Skip institutions already in `ir_pages`
   unless re-running with an improved prompt.
5. Run the §2 loop on a **single 20-id batch first**, audit it, then continue.
6. Never launch an unattended multi-hundred-batch loop — watch spend (§4).
7. Commit after each clean batch (the repo's rhythm: one commit per cohort/
   batch with a count in the message, e.g. "Add 47 institutions (CA 101–200)").

---

## 8. Where everything lives (quick map)

| Concern | Path |
|---|---|
| Orchestrated multi-institution scrape | `scripts/run_many_parallel.py` |
| Single institution full run | `scripts/run_one_full.py` |
| Computed peers / DFR peers | `scripts/compute_peers.py`, `scripts/run_dfr.py` |
| Export JSON | `scripts/export_landing_json.py` |
| Listing index | `scripts/build_institutions_index.py` |
| KV upload | `scripts/upload_to_cloudflare_kv.py` |
| Post-scrape QA | `scripts/validate_institution.py` |
| Cohort files | `scripts/cohorts/*.txt` |
| Connectors (one per source) | `src/landing_scraper/connectors/<source>.py` |
| Pipeline 4-stage | `src/landing_scraper/scraper/pipeline.py` |
| URL-finder agent + prompt | `src/landing_scraper/agents/url_finder.py`, `agents/prompts/url_finder.py` |
| Search providers | `src/landing_scraper/core/search.py` |
| Sole write path | `src/landing_scraper/db/writer.py:ProvenanceWriter` |
| Config / flags | `src/landing_scraper/config.py` |

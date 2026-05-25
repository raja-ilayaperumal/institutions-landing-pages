-- =====================================================================
-- Clema Landing — canonical, idempotent schema
-- =====================================================================
-- Single source of truth for every landing.* object. Re-runnable safely
-- (uses CREATE … IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / CREATE OR
-- REPLACE VIEW). Apply via `bash db/install.sh` (full bootstrap) or
-- `psql -d clema_landing -f db/ensure_schema.sql` (repair).
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS landing;

-- =====================================================================
-- MASTER + LOOKUPS
-- =====================================================================

CREATE TABLE IF NOT EXISTS landing.institutions (
  unitid                 INTEGER PRIMARY KEY,
  slug                   TEXT NOT NULL UNIQUE,
  name                   TEXT NOT NULL,
  city                   TEXT,
  stabbr                 CHAR(2) NOT NULL,
  state_name             TEXT,
  state_slug             TEXT NOT NULL,
  zip                    TEXT,
  region                 TEXT,
  fips_code              INTEGER,
  control_code           INTEGER,
  control_label          TEXT,
  control_slug           TEXT,
  sector_code            INTEGER,
  sector_label           TEXT,
  carnegie_basic_code    INTEGER,
  carnegie_basic_label   TEXT,
  carnegie_basic_slug    TEXT,
  is_hbcu                BOOLEAN NOT NULL DEFAULT FALSE,
  is_tribal              BOOLEAN NOT NULL DEFAULT FALSE,
  is_hospital            BOOLEAN NOT NULL DEFAULT FALSE,
  is_medical             BOOLEAN NOT NULL DEFAULT FALSE,
  is_landgrant           BOOLEAN NOT NULL DEFAULT FALSE,
  is_hsi                 BOOLEAN,
  opeid                  TEXT,
  ein                    TEXT,
  ueis                   TEXT,
  webaddr_raw            TEXT,
  webaddr_normalized     TEXT,
  canonical_root_url     TEXT,
  domain                 TEXT,
  registrable_domain     TEXT,
  robots_txt_url         TEXT,
  robots_txt_fetched_at  TIMESTAMPTZ,
  enabled                BOOLEAN NOT NULL DEFAULT TRUE,
  pilot_cohort           BOOLEAN NOT NULL DEFAULT FALSE,
  last_full_crawl_at     TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_landing_inst_slug          ON landing.institutions(slug);
CREATE INDEX IF NOT EXISTS ix_landing_inst_state         ON landing.institutions(stabbr);
CREATE INDEX IF NOT EXISTS ix_landing_inst_state_slug    ON landing.institutions(state_slug);
CREATE INDEX IF NOT EXISTS ix_landing_inst_carnegie      ON landing.institutions(carnegie_basic_code);
CREATE INDEX IF NOT EXISTS ix_landing_inst_carnegie_slug ON landing.institutions(carnegie_basic_slug);
CREATE INDEX IF NOT EXISTS ix_landing_inst_control       ON landing.institutions(control_code);
CREATE INDEX IF NOT EXISTS ix_landing_inst_region        ON landing.institutions(region);
CREATE INDEX IF NOT EXISTS ix_landing_inst_hbcu          ON landing.institutions(is_hbcu) WHERE is_hbcu;
CREATE INDEX IF NOT EXISTS ix_landing_inst_hsi           ON landing.institutions(is_hsi)  WHERE is_hsi;
CREATE INDEX IF NOT EXISTS ix_landing_inst_tribal        ON landing.institutions(is_tribal) WHERE is_tribal;
CREATE INDEX IF NOT EXISTS ix_landing_inst_pilot         ON landing.institutions(pilot_cohort) WHERE pilot_cohort;
CREATE INDEX IF NOT EXISTS ix_landing_inst_domain        ON landing.institutions(registrable_domain);
CREATE INDEX IF NOT EXISTS ix_landing_inst_uei           ON landing.institutions(ueis);

CREATE TABLE IF NOT EXISTS landing.source_types (
  source_type           TEXT PRIMARY KEY,
  description           TEXT NOT NULL,
  default_queue         TEXT NOT NULL,
  refresh_cadence_days  INTEGER,
  is_active             BOOLEAN NOT NULL DEFAULT TRUE
);

-- =====================================================================
-- PIPELINE OPERATIONAL
-- =====================================================================

CREATE TABLE IF NOT EXISTS landing.crawl_log (
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
CREATE INDEX IF NOT EXISTS ix_crawl_log_inst_src ON landing.crawl_log(unitid, source_type);

CREATE TABLE IF NOT EXISTS landing.canonical_urls (
  unitid          INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_type     TEXT    NOT NULL REFERENCES landing.source_types(source_type),
  url             TEXT    NOT NULL,
  confidence      NUMERIC NOT NULL,
  llm_reasoning   TEXT,
  chosen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  superseded_at   TIMESTAMPTZ,
  PRIMARY KEY (unitid, source_type, chosen_at)
);
CREATE INDEX IF NOT EXISTS ix_canon_current ON landing.canonical_urls(unitid, source_type) WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS landing.raw_payloads (
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
CREATE INDEX IF NOT EXISTS ix_raw_dedupe ON landing.raw_payloads(unitid, source_type, body_sha256);
CREATE INDEX IF NOT EXISTS ix_raw_latest ON landing.raw_payloads(unitid, source_type, fetched_at DESC);

CREATE TABLE IF NOT EXISTS landing.scraper_runs (
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
CREATE INDEX IF NOT EXISTS ix_runs_inst   ON landing.scraper_runs(unitid, source_type, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_runs_status ON landing.scraper_runs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS landing.sitemaps (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  sitemap_url      TEXT NOT NULL,
  sitemap_type     TEXT,
  fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  entry_count      INTEGER,
  http_status      INTEGER,
  UNIQUE (unitid, sitemap_url)
);
CREATE INDEX IF NOT EXISTS ix_sitemaps_inst ON landing.sitemaps(unitid);

CREATE TABLE IF NOT EXISTS landing.sitemap_entries (
  id               BIGSERIAL PRIMARY KEY,
  sitemap_id       BIGINT NOT NULL REFERENCES landing.sitemaps(id) ON DELETE CASCADE,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  url              TEXT NOT NULL,
  lastmod          TIMESTAMPTZ,
  changefreq       TEXT,
  priority         NUMERIC,
  classified_as    TEXT REFERENCES landing.source_types(source_type),
  classification_confidence NUMERIC,
  UNIQUE (sitemap_id, url)
);
CREATE INDEX IF NOT EXISTS ix_smentry_inst  ON landing.sitemap_entries(unitid);
CREATE INDEX IF NOT EXISTS ix_smentry_class ON landing.sitemap_entries(classified_as) WHERE classified_as IS NOT NULL;

CREATE TABLE IF NOT EXISTS landing.learned_patterns (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_type      TEXT NOT NULL REFERENCES landing.source_types(source_type),
  pattern_kind     TEXT NOT NULL,
  pattern_value    TEXT NOT NULL,
  confidence       NUMERIC,
  samples_seen     INTEGER DEFAULT 1,
  last_succeeded_at TIMESTAMPTZ,
  last_failed_at   TIMESTAMPTZ,
  UNIQUE (unitid, source_type, pattern_kind, pattern_value)
);
CREATE INDEX IF NOT EXISTS ix_lp_inst ON landing.learned_patterns(unitid, source_type);

-- =====================================================================
-- CONTENT TABLES — one per page block / scraper target
-- =====================================================================

CREATE TABLE IF NOT EXISTS landing.brand_assets (
  unitid           INTEGER PRIMARY KEY REFERENCES landing.institutions(unitid),
  logo_url         TEXT, logo_url_dark TEXT, logo_icon_url TEXT,
  colors_primary   TEXT, colors_secondary TEXT,
  color_palette    JSONB, fonts JSONB,
  source_url       TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS landing.wiki_summaries (
  unitid            INTEGER PRIMARY KEY REFERENCES landing.institutions(unitid),
  wikipedia_url     TEXT, wikidata_qid TEXT,
  short_description TEXT, extract_summary TEXT,
  founded_year INTEGER, motto TEXT, endowment_usd BIGINT,
  president_name TEXT, thumbnail_url TEXT,
  source_url TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS landing.peer_groups (
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
CREATE INDEX IF NOT EXISTS ix_peers_lookup ON landing.peer_groups(unitid, algorithm, algorithm_version, rank);

CREATE TABLE IF NOT EXISTS landing.ir_pages (
  unitid           INTEGER PRIMARY KEY REFERENCES landing.institutions(unitid),
  office_name      TEXT, page_url TEXT, page_summary TEXT, parent_division TEXT,
  source_url TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL, confidence NUMERIC,
  office_phone   TEXT, office_fax TEXT, office_email TEXT, office_address TEXT,
  quality_score  NUMERIC(3,2), needs_review BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS landing.ir_contacts (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  name             TEXT NOT NULL,
  title            TEXT, email TEXT, phone TEXT, linkedin_url TEXT,
  ordering         SMALLINT,
  source_url       TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL, confidence NUMERIC,
  quality_score    NUMERIC(3,2), needs_review BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE (unitid, name, title)
);
CREATE INDEX IF NOT EXISTS ix_ircontacts_inst  ON landing.ir_contacts(unitid);
CREATE INDEX IF NOT EXISTS ix_ircontacts_email ON landing.ir_contacts(email) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS landing.ir_documents (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  doc_type         TEXT NOT NULL,
  title            TEXT, year SMALLINT, doc_url TEXT NOT NULL,
  mime_type        TEXT, page_count INTEGER, storage_path TEXT,
  extracted_text   TEXT, summary TEXT,
  source_url       TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL, confidence NUMERIC,
  quality_score    NUMERIC(3,2), needs_review BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE (unitid, doc_type, year, doc_url)
);
CREATE INDEX IF NOT EXISTS ix_irdocs_inst_type ON landing.ir_documents(unitid, doc_type, year DESC);

CREATE TABLE IF NOT EXISTS landing.cds_links (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  year             SMALLINT NOT NULL,
  cds_url          TEXT NOT NULL, is_pdf BOOLEAN,
  source_url       TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL,
  UNIQUE (unitid, year)
);
CREATE INDEX IF NOT EXISTS ix_cds_inst ON landing.cds_links(unitid, year DESC);

CREATE TABLE IF NOT EXISTS landing.job_postings (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  title            TEXT NOT NULL, department TEXT,
  category         TEXT,        -- 'ir' | 'other'
  posted_at        DATE, expires_at DATE,
  apply_url        TEXT NOT NULL,
  source_board     TEXT,
  raw_description  TEXT,
  source_url       TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL,
  UNIQUE (unitid, apply_url)
);
CREATE INDEX IF NOT EXISTS ix_jobs_inst_cat ON landing.job_postings(unitid, category, posted_at DESC);

CREATE TABLE IF NOT EXISTS landing.notable_alumni (
  id               BIGSERIAL PRIMARY KEY,
  unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  name             TEXT NOT NULL,
  field            TEXT, short_bio TEXT, wikipedia_url TEXT,
  source_url       TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
  parser_version   TEXT NOT NULL,
  UNIQUE (unitid, name)
);
CREATE INDEX IF NOT EXISTS ix_alumni_inst ON landing.notable_alumni(unitid);

CREATE TABLE IF NOT EXISTS landing.research_expenditures (
  unitid                  INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  fiscal_year             SMALLINT NOT NULL,
  total_rd_expend_usd     BIGINT,
  federal_rd_usd          BIGINT, state_local_rd_usd BIGINT,
  institutional_rd_usd    BIGINT, business_rd_usd BIGINT,
  nonprofit_rd_usd        BIGINT, other_rd_usd BIGINT,
  by_field                JSONB, rank_national INTEGER,
  source_url              TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id          BIGINT REFERENCES landing.raw_payloads(id),
  parser_version          TEXT NOT NULL,
  PRIMARY KEY (unitid, fiscal_year)
);

CREATE TABLE IF NOT EXISTS landing.research_awards (
  id                      BIGSERIAL PRIMARY KEY,
  unitid                  INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_system           TEXT NOT NULL,
  external_id             TEXT NOT NULL,
  agency                  TEXT, program_or_mechanism TEXT, title TEXT,
  pi_name                 TEXT, pi_orcid TEXT,
  amount_total_usd        BIGINT, amount_obligated_usd BIGINT,
  fiscal_year             SMALLINT, start_date DATE, end_date DATE,
  abstract                TEXT,
  source_url              TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  raw_payload_id          BIGINT REFERENCES landing.raw_payloads(id),
  parser_version          TEXT NOT NULL,
  UNIQUE (source_system, external_id)
);
CREATE INDEX IF NOT EXISTS ix_research_awards_inst    ON landing.research_awards(unitid, fiscal_year DESC);
CREATE INDEX IF NOT EXISTS ix_research_awards_pi      ON landing.research_awards(pi_name);
CREATE INDEX IF NOT EXISTS ix_research_awards_agency  ON landing.research_awards(unitid, agency, fiscal_year DESC);

CREATE TABLE IF NOT EXISTS landing.grants (
  id BIGSERIAL PRIMARY KEY,
  unitid INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_system TEXT, external_id TEXT, agency TEXT, title TEXT, amount_usd BIGINT,
  award_date DATE, end_date DATE, status TEXT, source_url TEXT,
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL,
  quality_score NUMERIC(3,2), needs_review BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE (source_system, external_id)
);
CREATE INDEX IF NOT EXISTS ix_grants_inst ON landing.grants(unitid);

CREATE TABLE IF NOT EXISTS landing.rfps (
  id BIGSERIAL PRIMARY KEY,
  unitid INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  source_system TEXT, external_id TEXT, title TEXT, due_date DATE, status TEXT,
  apply_url TEXT, source_url TEXT, fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL,
  UNIQUE (source_system, external_id)
);

CREATE TABLE IF NOT EXISTS landing.accreditation_dapip (
  unitid INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  accreditor_name TEXT NOT NULL, program_name TEXT, status TEXT,
  date_first DATE, date_last_action DATE, source_url TEXT,
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL,
  PRIMARY KEY (unitid, accreditor_name, program_name)
);

CREATE TABLE IF NOT EXISTS landing.athletics_eada (
  unitid INTEGER PRIMARY KEY REFERENCES landing.institutions(unitid),
  total_athletes INTEGER, men_athletes INTEGER, women_athletes INTEGER,
  total_revenue_usd BIGINT, total_expenses_usd BIGINT, year SMALLINT,
  source_url TEXT, fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS landing.cdr (
  unitid INTEGER NOT NULL REFERENCES landing.institutions(unitid),
  year SMALLINT NOT NULL, cdr_rate NUMERIC(5,2),
  num_borrowers INTEGER, num_defaulted INTEGER,
  source_url TEXT, fetched_at TIMESTAMPTZ DEFAULT NOW(),
  raw_payload_id BIGINT REFERENCES landing.raw_payloads(id),
  parser_version TEXT NOT NULL,
  PRIMARY KEY (unitid, year)
);

-- =====================================================================
-- CATEGORIZATION VIEWS (institutions per state / Carnegie)
-- =====================================================================

CREATE OR REPLACE VIEW landing.v_state_counts AS
SELECT state_slug, stabbr, state_name, region,
       COUNT(*) AS institution_count,
       SUM(CASE WHEN is_hbcu THEN 1 ELSE 0 END) AS hbcu_count,
       SUM(CASE WHEN is_tribal THEN 1 ELSE 0 END) AS tribal_count
FROM landing.institutions WHERE enabled
GROUP BY state_slug, stabbr, state_name, region;

CREATE OR REPLACE VIEW landing.v_carnegie_counts AS
SELECT carnegie_basic_slug, carnegie_basic_code, carnegie_basic_label,
       COUNT(*) AS institution_count
FROM landing.institutions WHERE enabled AND carnegie_basic_slug IS NOT NULL
GROUP BY carnegie_basic_slug, carnegie_basic_code, carnegie_basic_label;

"""landing pipeline ops + content tables (aligned w/ team's CloudFlare D1 vocab)

Revision ID: 0002_pipeline_content
Revises: 0001_landing_master
Create Date: 2026-05-24
"""
from alembic import op

revision = "0002_pipeline_content"
down_revision = "0001_landing_master"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =====================================================================
    # PIPELINE OPS
    # =====================================================================

    # crawl_log — every candidate URL we considered during discovery (Stage 1)
    op.execute("""
        CREATE TABLE landing.crawl_log (
            id               BIGSERIAL PRIMARY KEY,
            unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            source_type      TEXT    NOT NULL REFERENCES landing.source_types(source_type),
            url              TEXT    NOT NULL,
            discovery_method TEXT    NOT NULL,         -- 'known_api','site_search','deep_crawl','sitemap','manual'
            relevance_score  NUMERIC,
            depth            INTEGER,
            discovered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            run_id           UUID NOT NULL,
            UNIQUE (unitid, source_type, url, run_id)
        )
    """)
    op.execute("CREATE INDEX ix_crawl_log_inst_src ON landing.crawl_log(unitid, source_type)")

    # canonical_urls — the ONE URL chosen per (institution, source_type) (Stage 2)
    op.execute("""
        CREATE TABLE landing.canonical_urls (
            unitid          INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            source_type     TEXT    NOT NULL REFERENCES landing.source_types(source_type),
            url             TEXT    NOT NULL,
            confidence      NUMERIC NOT NULL,
            llm_reasoning   TEXT,
            chosen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            superseded_at   TIMESTAMPTZ,
            PRIMARY KEY (unitid, source_type, chosen_at)
        )
    """)
    op.execute("""
        CREATE INDEX ix_canon_current ON landing.canonical_urls(unitid, source_type)
        WHERE superseded_at IS NULL
    """)

    # raw_payloads — fetched bytes (Stage 3, time-machine for re-parsing)
    op.execute("""
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
            fetcher          TEXT NOT NULL,             -- 'crawl4ai','httpx','playwright'
            run_id           UUID NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_raw_dedupe ON landing.raw_payloads(unitid, source_type, body_sha256)")
    op.execute("CREATE INDEX ix_raw_latest ON landing.raw_payloads(unitid, source_type, fetched_at DESC)")

    # scraper_runs — per-task execution log
    op.execute("""
        CREATE TABLE landing.scraper_runs (
            run_id           UUID PRIMARY KEY,
            unitid           INTEGER REFERENCES landing.institutions(unitid),
            source_type      TEXT REFERENCES landing.source_types(source_type),
            stage            TEXT NOT NULL,             -- 'discover','validate','extract','store'
            status           TEXT NOT NULL,             -- 'success','failed','skipped','retrying'
            started_at       TIMESTAMPTZ NOT NULL,
            finished_at      TIMESTAMPTZ,
            duration_ms      INTEGER,
            error_class      TEXT,
            error_message    TEXT,
            rows_written     INTEGER,
            llm_tokens_in    INTEGER,
            llm_tokens_out   INTEGER,
            cost_usd         NUMERIC(10,5)
        )
    """)
    op.execute("CREATE INDEX ix_runs_inst ON landing.scraper_runs(unitid, source_type, started_at DESC)")
    op.execute("CREATE INDEX ix_runs_status ON landing.scraper_runs(status, started_at DESC)")

    # sitemaps + sitemap_entries — sitemap-based discovery (learned from D1 schema)
    op.execute("""
        CREATE TABLE landing.sitemaps (
            id               BIGSERIAL PRIMARY KEY,
            unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            sitemap_url      TEXT NOT NULL,
            sitemap_type     TEXT,                       -- 'root','image','news','index'
            fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            entry_count      INTEGER,
            http_status      INTEGER,
            UNIQUE (unitid, sitemap_url)
        )
    """)
    op.execute("CREATE INDEX ix_sitemaps_inst ON landing.sitemaps(unitid)")

    op.execute("""
        CREATE TABLE landing.sitemap_entries (
            id               BIGSERIAL PRIMARY KEY,
            sitemap_id       BIGINT NOT NULL REFERENCES landing.sitemaps(id) ON DELETE CASCADE,
            unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            url              TEXT NOT NULL,
            lastmod          TIMESTAMPTZ,
            changefreq       TEXT,
            priority         NUMERIC,
            -- classification: which source_type does this URL look like it belongs to?
            classified_as    TEXT REFERENCES landing.source_types(source_type),
            classification_confidence NUMERIC,
            UNIQUE (sitemap_id, url)
        )
    """)
    op.execute("CREATE INDEX ix_smentry_inst ON landing.sitemap_entries(unitid)")
    op.execute("CREATE INDEX ix_smentry_class ON landing.sitemap_entries(classified_as) WHERE classified_as IS NOT NULL")

    # learned_patterns — per-institution heuristics learned over crawls
    op.execute("""
        CREATE TABLE landing.learned_patterns (
            id               BIGSERIAL PRIMARY KEY,
            unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            source_type      TEXT NOT NULL REFERENCES landing.source_types(source_type),
            pattern_kind     TEXT NOT NULL,              -- 'url_template','css_selector','sub_path'
            pattern_value    TEXT NOT NULL,              -- e.g. '/ir/staff' or 'div.staff-card .name'
            confidence       NUMERIC,
            samples_seen     INTEGER DEFAULT 1,
            last_succeeded_at TIMESTAMPTZ,
            last_failed_at   TIMESTAMPTZ,
            UNIQUE (unitid, source_type, pattern_kind, pattern_value)
        )
    """)
    op.execute("CREATE INDEX ix_lp_inst ON landing.learned_patterns(unitid, source_type)")

    # =====================================================================
    # CONTENT TABLES (one per page block / source_type)
    # =====================================================================

    # Block 00 — brand
    op.execute("""
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
        )
    """)

    # Block 00 / 08 — Wikipedia about
    op.execute("""
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
            thumbnail_url     TEXT,
            source_url        TEXT,
            fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            raw_payload_id    BIGINT REFERENCES landing.raw_payloads(id),
            parser_version    TEXT NOT NULL
        )
    """)

    # Block 07 — peers (algorithm-versioned)
    op.execute("""
        CREATE TABLE landing.peer_groups (
            unitid            INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            peer_unitid       INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            rank              SMALLINT NOT NULL,
            similarity_score  NUMERIC,
            algorithm         TEXT NOT NULL,             -- 'carnegie_state_control_v1','web_embed_v1','dfr_v1'
            algorithm_version TEXT NOT NULL,
            computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (unitid, algorithm, algorithm_version, peer_unitid),
            CHECK (unitid <> peer_unitid)
        )
    """)
    op.execute("""
        CREATE INDEX ix_peers_lookup ON landing.peer_groups(unitid, algorithm, algorithm_version, rank)
    """)

    # Block 09 — IR landing page (one per institution)
    op.execute("""
        CREATE TABLE landing.ir_pages (
            unitid           INTEGER PRIMARY KEY REFERENCES landing.institutions(unitid),
            office_name      TEXT,                       -- 'Office of Institutional Research'
            page_url         TEXT,
            page_summary     TEXT,                       -- 1-paragraph LLM summary
            parent_division  TEXT,                       -- 'Provost', 'Strategic Planning', etc.
            source_url       TEXT,
            fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
            parser_version   TEXT NOT NULL,
            confidence       NUMERIC
        )
    """)

    # Block 09 — IR team members (N per institution)
    op.execute("""
        CREATE TABLE landing.ir_contacts (
            id               BIGSERIAL PRIMARY KEY,
            unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            name             TEXT NOT NULL,
            title            TEXT,
            email            TEXT,
            phone            TEXT,
            linkedin_url     TEXT,                       -- D1 had this field; we kept it
            ordering         SMALLINT,
            source_url       TEXT,
            fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
            parser_version   TEXT NOT NULL,
            confidence       NUMERIC,
            UNIQUE (unitid, name, title)
        )
    """)
    op.execute("CREATE INDEX ix_ircontacts_inst ON landing.ir_contacts(unitid)")
    op.execute("CREATE INDEX ix_ircontacts_email ON landing.ir_contacts(email) WHERE email IS NOT NULL")

    # Multi-block — institutional documents (factbook, strategic plan, glossary, data dict, etc.)
    op.execute("""
        CREATE TABLE landing.ir_documents (
            id               BIGSERIAL PRIMARY KEY,
            unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            doc_type         TEXT NOT NULL,              -- 'factbook','strategic_plan','glossary','data_dictionary','cds_pdf','dashboard_link','annual_report','other'
            title            TEXT,
            year             SMALLINT,
            doc_url          TEXT NOT NULL,
            mime_type        TEXT,
            page_count       INTEGER,
            storage_path     TEXT,                       -- cached blob location
            extracted_text   TEXT,                       -- searchable text (factbook fulltext)
            summary          TEXT,                       -- LLM-generated when applicable
            source_url       TEXT,
            fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
            parser_version   TEXT NOT NULL,
            confidence       NUMERIC,
            UNIQUE (unitid, doc_type, year, doc_url)
        )
    """)
    op.execute("CREATE INDEX ix_irdocs_inst_type ON landing.ir_documents(unitid, doc_type, year DESC)")

    # Block 10 — Common Data Set links
    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX ix_cds_inst ON landing.cds_links(unitid, year DESC)")

    # Block 11 — job postings
    op.execute("""
        CREATE TABLE landing.job_postings (
            id               BIGSERIAL PRIMARY KEY,
            unitid           INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            title            TEXT NOT NULL,
            department       TEXT,
            category         TEXT,                       -- 'ir','other'
            posted_at        DATE,
            expires_at       DATE,
            apply_url        TEXT NOT NULL,
            source_board     TEXT,                       -- 'higheredjobs','institution_site','linkedin'
            raw_description  TEXT,
            source_url       TEXT,
            fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            raw_payload_id   BIGINT REFERENCES landing.raw_payloads(id),
            parser_version   TEXT NOT NULL,
            UNIQUE (unitid, apply_url)
        )
    """)
    op.execute("CREATE INDEX ix_jobs_inst_cat ON landing.job_postings(unitid, category, posted_at DESC)")

    # Block 08 — notable alumni (Wikipedia + optional manual)
    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX ix_alumni_inst ON landing.notable_alumni(unitid)")

    # =====================================================================
    # RESEARCH FUNDING (v1 additions)
    # =====================================================================

    op.execute("""
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
            by_field                JSONB,
            rank_national           INTEGER,
            source_url              TEXT,
            fetched_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            raw_payload_id          BIGINT REFERENCES landing.raw_payloads(id),
            parser_version          TEXT NOT NULL,
            PRIMARY KEY (unitid, fiscal_year)
        )
    """)

    op.execute("""
        CREATE TABLE landing.research_awards (
            id                      BIGSERIAL PRIMARY KEY,
            unitid                  INTEGER NOT NULL REFERENCES landing.institutions(unitid),
            source_system           TEXT NOT NULL,        -- 'nih_reporter','nsf_awards','usaspending'
            external_id             TEXT NOT NULL,
            agency                  TEXT,
            program_or_mechanism    TEXT,
            title                   TEXT,
            pi_name                 TEXT,
            pi_orcid                TEXT,
            amount_total_usd        BIGINT,
            amount_obligated_usd    BIGINT,
            fiscal_year             SMALLINT,
            start_date              DATE,
            end_date                DATE,
            abstract                TEXT,
            source_url              TEXT,
            fetched_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            raw_payload_id          BIGINT REFERENCES landing.raw_payloads(id),
            parser_version          TEXT NOT NULL,
            UNIQUE (source_system, external_id)
        )
    """)
    op.execute("CREATE INDEX ix_research_awards_inst    ON landing.research_awards(unitid, fiscal_year DESC)")
    op.execute("CREATE INDEX ix_research_awards_pi      ON landing.research_awards(pi_name)")
    op.execute("CREATE INDEX ix_research_awards_agency  ON landing.research_awards(unitid, agency, fiscal_year DESC)")


def downgrade() -> None:
    for tbl in [
        "research_awards", "research_expenditures",
        "notable_alumni", "job_postings", "cds_links", "ir_documents",
        "ir_contacts", "ir_pages", "peer_groups",
        "wiki_summaries", "brand_assets",
        "learned_patterns", "sitemap_entries", "sitemaps",
        "scraper_runs", "raw_payloads", "canonical_urls", "crawl_log",
    ]:
        op.execute(f"DROP TABLE IF EXISTS landing.{tbl} CASCADE")

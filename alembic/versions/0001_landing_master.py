"""landing schema: master + source_types

Revision ID: 0001_landing_master
Revises:
Create Date: 2026-05-24
"""
from alembic import op

revision = "0001_landing_master"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS landing")

    op.execute("""
        CREATE TABLE landing.institutions (
            unitid                 INTEGER PRIMARY KEY,
            slug                   TEXT NOT NULL UNIQUE,

            -- denormalized core (kept in sync with ipeds.institutions_2024)
            name                   TEXT NOT NULL,
            city                   TEXT,
            stabbr                 CHAR(2) NOT NULL,
            state_name             TEXT,
            state_slug             TEXT NOT NULL,
            zip                    TEXT,
            region                 TEXT,                  -- 'Far West','Southeast', etc. from ipeds.state_codes
            fips_code              INTEGER,

            -- classification (resolved labels for filtering / categorization)
            control_code           INTEGER,
            control_label          TEXT,                  -- 'Public', 'Private not-for-profit', 'Private for-profit'
            control_slug           TEXT,                  -- 'public','private-np','private-fp'
            sector_code            INTEGER,
            sector_label           TEXT,
            carnegie_basic_code    INTEGER,
            carnegie_basic_label   TEXT,
            carnegie_basic_slug    TEXT,                  -- 'research-1', 'research-2', 'doctoral-professional', etc.

            -- minority-serving and special designations
            is_hbcu                BOOLEAN NOT NULL DEFAULT FALSE,
            is_tribal              BOOLEAN NOT NULL DEFAULT FALSE,
            is_hospital            BOOLEAN NOT NULL DEFAULT FALSE,
            is_medical             BOOLEAN NOT NULL DEFAULT FALSE,
            is_landgrant           BOOLEAN NOT NULL DEFAULT FALSE,
            is_hsi                 BOOLEAN,               -- from scorecard; null when not in scorecard yet

            -- federal identifiers
            opeid                  TEXT,
            ein                    TEXT,
            ueis                   TEXT,                  -- federal awards systems join key

            -- web / scraping
            webaddr_raw            TEXT,
            webaddr_normalized     TEXT,
            canonical_root_url     TEXT,
            domain                 TEXT,                  -- e.g. 'web.mit.edu'
            registrable_domain     TEXT,                  -- e.g. 'mit.edu' (for site: queries that span subdomains)
            robots_txt_url         TEXT,
            robots_txt_fetched_at  TIMESTAMPTZ,

            -- operational flags
            enabled                BOOLEAN NOT NULL DEFAULT TRUE,
            pilot_cohort           BOOLEAN NOT NULL DEFAULT FALSE,
            last_full_crawl_at     TIMESTAMPTZ,

            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_landing_inst_slug          ON landing.institutions(slug)")
    op.execute("CREATE INDEX ix_landing_inst_state         ON landing.institutions(stabbr)")
    op.execute("CREATE INDEX ix_landing_inst_state_slug    ON landing.institutions(state_slug)")
    op.execute("CREATE INDEX ix_landing_inst_carnegie      ON landing.institutions(carnegie_basic_code)")
    op.execute("CREATE INDEX ix_landing_inst_carnegie_slug ON landing.institutions(carnegie_basic_slug)")
    op.execute("CREATE INDEX ix_landing_inst_control       ON landing.institutions(control_code)")
    op.execute("CREATE INDEX ix_landing_inst_region        ON landing.institutions(region)")
    op.execute("CREATE INDEX ix_landing_inst_hbcu          ON landing.institutions(is_hbcu) WHERE is_hbcu")
    op.execute("CREATE INDEX ix_landing_inst_hsi           ON landing.institutions(is_hsi)  WHERE is_hsi")
    op.execute("CREATE INDEX ix_landing_inst_tribal        ON landing.institutions(is_tribal) WHERE is_tribal")
    op.execute("CREATE INDEX ix_landing_inst_pilot         ON landing.institutions(pilot_cohort) WHERE pilot_cohort")
    op.execute("CREATE INDEX ix_landing_inst_domain        ON landing.institutions(registrable_domain)")
    op.execute("CREATE INDEX ix_landing_inst_uei           ON landing.institutions(ueis)")

    op.execute("""
        CREATE TABLE landing.source_types (
            source_type           TEXT PRIMARY KEY,
            description           TEXT NOT NULL,
            default_queue         TEXT NOT NULL,
            refresh_cadence_days  INTEGER,
            is_active             BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

    # Convenience views for categorization
    op.execute("""
        CREATE VIEW landing.v_state_counts AS
        SELECT state_slug, stabbr, state_name, region,
               COUNT(*) AS institution_count,
               SUM(CASE WHEN is_hbcu THEN 1 ELSE 0 END) AS hbcu_count,
               SUM(CASE WHEN is_tribal THEN 1 ELSE 0 END) AS tribal_count
        FROM landing.institutions WHERE enabled
        GROUP BY state_slug, stabbr, state_name, region
    """)
    op.execute("""
        CREATE VIEW landing.v_carnegie_counts AS
        SELECT carnegie_basic_slug, carnegie_basic_code, carnegie_basic_label,
               COUNT(*) AS institution_count
        FROM landing.institutions WHERE enabled AND carnegie_basic_slug IS NOT NULL
        GROUP BY carnegie_basic_slug, carnegie_basic_code, carnegie_basic_label
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS landing.v_carnegie_counts")
    op.execute("DROP VIEW IF EXISTS landing.v_state_counts")
    op.execute("DROP TABLE IF EXISTS landing.source_types")
    op.execute("DROP TABLE IF EXISTS landing.institutions")
    # Don't drop the schema — it'll hold the alembic version table

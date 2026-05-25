"""landing materialized views: research funding summary + institution_page

Revision ID: 0003_landing_views
Revises: 0002_pipeline_content
Create Date: 2026-05-24
"""
from alembic import op

revision = "0003_landing_views"
down_revision = "0002_pipeline_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Per-institution research funding rollup
    op.execute("""
        CREATE MATERIALIZED VIEW landing.mv_research_funding_summary AS
        SELECT
            unitid,
            COUNT(*)                                                                              AS award_count_total,
            COUNT(*) FILTER (WHERE fiscal_year >= EXTRACT(YEAR FROM CURRENT_DATE)::INT - 3)       AS award_count_last_3y,
            SUM(amount_total_usd)                                                                 AS amount_total_all_time_usd,
            SUM(amount_total_usd) FILTER (WHERE fiscal_year >= EXTRACT(YEAR FROM CURRENT_DATE)::INT - 3) AS amount_total_last_3y_usd,
            MAX(end_date)                                                                         AS latest_award_end,
            COUNT(*) FILTER (WHERE source_system = 'nih_reporter')   AS award_count_nih,
            COUNT(*) FILTER (WHERE source_system = 'nsf_awards')     AS award_count_nsf,
            COUNT(*) FILTER (WHERE source_system = 'usaspending')    AS award_count_usaspending,
            SUM(amount_total_usd) FILTER (WHERE source_system = 'nih_reporter')   AS amount_nih_usd,
            SUM(amount_total_usd) FILTER (WHERE source_system = 'nsf_awards')     AS amount_nsf_usd,
            SUM(amount_total_usd) FILTER (WHERE source_system = 'usaspending')    AS amount_usaspending_usd
        FROM landing.research_awards
        GROUP BY unitid
    """)
    op.execute("CREATE UNIQUE INDEX ix_mv_research_summary ON landing.mv_research_funding_summary(unitid)")

    # Per-institution all-data view — scalar subqueries (one-to-one) + jsonb_agg subqueries (one-to-many).
    # This shape has no GROUP BY and is independently optimizable per institution.
    op.execute("""
        CREATE MATERIALIZED VIEW landing.mv_institution_page AS
        SELECT
            li.unitid,
            li.slug,
            li.name                                  AS institution_name,
            li.city, li.stabbr, li.state_name, li.state_slug, li.region, li.zip,
            li.webaddr_normalized                    AS webaddr,
            li.canonical_root_url, li.domain, li.registrable_domain,
            li.opeid, li.ein, li.ueis,
            li.control_label, li.control_slug,
            li.sector_label,
            li.carnegie_basic_label, li.carnegie_basic_slug,
            li.is_hbcu, li.is_tribal, li.is_hospital, li.is_medical, li.is_landgrant, li.is_hsi,

            -- IPEDS+Scorecard scalars (latest year)
            sc_latest.scorecard,

            -- landing one-to-one (JSONB blobs, NULL if no row)
            (SELECT to_jsonb(b.*)   FROM landing.brand_assets    b   WHERE b.unitid   = li.unitid) AS brand,
            (SELECT to_jsonb(w.*)   FROM landing.wiki_summaries  w   WHERE w.unitid   = li.unitid) AS wiki,
            (SELECT to_jsonb(irp.*) FROM landing.ir_pages        irp WHERE irp.unitid = li.unitid) AS ir_page,
            (SELECT to_jsonb(rfs.*) FROM landing.mv_research_funding_summary rfs
                  WHERE rfs.unitid = li.unitid) AS research_summary,

            -- landing one-to-many (JSONB arrays, '[]' if empty)
            (SELECT COALESCE(jsonb_agg(to_jsonb(ic.*) ORDER BY ic.ordering NULLS LAST, ic.id), '[]'::jsonb)
                  FROM landing.ir_contacts ic WHERE ic.unitid = li.unitid) AS ir_contacts,

            (SELECT COALESCE(jsonb_agg(to_jsonb(d.*) ORDER BY d.year DESC NULLS LAST, d.id), '[]'::jsonb)
                  FROM landing.ir_documents d WHERE d.unitid = li.unitid) AS documents,

            (SELECT COALESCE(jsonb_agg(to_jsonb(cds.*) ORDER BY cds.year DESC, cds.id), '[]'::jsonb)
                  FROM landing.cds_links cds WHERE cds.unitid = li.unitid) AS cds_links,

            (SELECT COALESCE(jsonb_agg(to_jsonb(j.*) ORDER BY j.posted_at DESC NULLS LAST, j.id), '[]'::jsonb)
                  FROM landing.job_postings j WHERE j.unitid = li.unitid) AS jobs,

            (SELECT COALESCE(jsonb_agg(to_jsonb(na.*) ORDER BY na.id), '[]'::jsonb)
                  FROM landing.notable_alumni na WHERE na.unitid = li.unitid) AS notable_alumni,

            (SELECT COALESCE(jsonb_agg(to_jsonb(pg.*) ORDER BY pg.rank), '[]'::jsonb)
                  FROM landing.peer_groups pg
                  WHERE pg.unitid = li.unitid AND pg.algorithm = 'carnegie_state_control_v1') AS peers,

            (SELECT COALESCE(jsonb_agg(to_jsonb(rx.*) ORDER BY rx.fiscal_year DESC), '[]'::jsonb)
                  FROM landing.research_expenditures rx WHERE rx.unitid = li.unitid) AS research_expenditures,

            -- freshness signal
            GREATEST(
                (SELECT fetched_at FROM landing.brand_assets   WHERE unitid = li.unitid),
                (SELECT fetched_at FROM landing.wiki_summaries WHERE unitid = li.unitid),
                (SELECT fetched_at FROM landing.ir_pages       WHERE unitid = li.unitid)
            ) AS landing_last_updated_at
        FROM landing.institutions li
        LEFT JOIN LATERAL (
            SELECT to_jsonb(sc.*) AS scorecard
            FROM score_card.fact_institution_yearly sc
            WHERE sc.institution_id = li.unitid
            ORDER BY sc.academic_year_id DESC
            LIMIT 1
        ) sc_latest ON TRUE
        WHERE li.enabled
    """)
    op.execute("CREATE UNIQUE INDEX ix_mv_inst_page  ON landing.mv_institution_page(unitid)")
    op.execute("CREATE INDEX        ix_mv_inst_slug  ON landing.mv_institution_page(slug)")
    op.execute("CREATE INDEX        ix_mv_inst_state ON landing.mv_institution_page(state_slug)")
    op.execute("CREATE INDEX        ix_mv_inst_carn  ON landing.mv_institution_page(carnegie_basic_slug)")


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS landing.mv_institution_page")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS landing.mv_research_funding_summary")

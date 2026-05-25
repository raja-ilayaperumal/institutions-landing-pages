"""Seed landing.source_types with the 13 v1 source types + Phase 2 stubs."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.db.engine import get_conn  # noqa: E402

ROWS = [
    # source_type,                description,                                   default_queue,   refresh_cadence_days, is_active
    ("brandfetch",              "Logo, colors, fonts from brandfetch.io",        "q_api",         90,                   True),
    ("wikipedia",               "Institution about + Wikidata + notable alumni",  "q_api",         180,                  True),
    ("ir_page",                 "IR office landing page discovery + summary",     "q_crawl",       90,                   True),
    ("ir_team",                 "IR team members (name/title/email/phone)",       "q_extract_llm", 90,                   True),
    ("factbook",                "Annual factbook PDF discovery + text",           "q_pdf",         365,                  True),
    ("cds",                     "Common Data Set link discovery",                 "q_search",      365,                  True),
    ("strategic_plan",          "Strategic plan PDF + LLM summary",               "q_pdf",         365,                  True),
    ("jobs",                    "Job postings (IR vs other) from institution",    "q_crawl",       7,                    True),
    ("peers",                   "DB-derived nearest neighbors (Carnegie+state)",  "q_api",         30,                   True),
    ("research_herd",           "NSF HERD R&D expenditures (CSV lookup)",         "q_api",         365,                  True),
    ("research_nih",            "NIH RePORTER awards",                            "q_api",         30,                   True),
    ("research_nsf",            "NSF Awards Search",                              "q_api",         30,                   True),
    ("research_usaspending",    "USAspending federal grants/contracts (UEI)",     "q_api",         30,                   True),
    # Phase 2 stubs (registered so child tables can FK against them)
    ("grants",                  "Grants.gov opportunity feed (PHASE 2)",          "q_api",         7,                    False),
    ("rfps",                    "SAM.gov RFP feed (PHASE 2)",                     "q_api",         7,                    False),
    ("accreditation_dapip",     "DAPIP accreditor scrape (PHASE 2)",              "q_crawl",       180,                  False),
    ("athletics_eada",          "EADA athletics (PHASE 2)",                       "q_api",         365,                  False),
    ("cdr",                     "Cohort Default Rate CSV (PHASE 2)",              "q_api",         365,                  False),
    ("notable_alumni_manual",   "Manually curated alumni (PHASE 2)",              "q_api",         None,                 False),
    ("glossary",                "Per-institution glossary scrape (PHASE 2)",      "q_crawl",       180,                  False),
    ("data_dictionary",         "Per-institution data dictionary (PHASE 2)",      "q_crawl",       180,                  False),
]

UPSERT = """
    INSERT INTO landing.source_types (source_type, description, default_queue, refresh_cadence_days, is_active)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (source_type) DO UPDATE SET
        description = EXCLUDED.description,
        default_queue = EXCLUDED.default_queue,
        refresh_cadence_days = EXCLUDED.refresh_cadence_days,
        is_active = EXCLUDED.is_active
"""


def main() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(UPSERT, ROWS)
    print(f"Upserted {len(ROWS)} rows into landing.source_types")


if __name__ == "__main__":
    main()

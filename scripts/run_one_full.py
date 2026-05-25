"""Full end-to-end scrape for ONE institution with quality summary at the end.

Runs all scrapers sequentially (where dependency matters) or in parallel
(when independent), then refreshes the materialized view and renders the
page. Prints a clean quality summary.

  python scripts/run_one_full.py --unitid 100654
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import click

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from landing_scraper.db.engine import get_conn  # noqa: E402


def _sh(cmd: list[str], label: str) -> tuple[int, str]:
    t0 = time.monotonic()
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    dt = time.monotonic() - t0
    return p.returncode, f"  {('✓' if p.returncode == 0 else '✗')} {label:35s}  {dt:5.1f}s"


def summary(unitid: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT name FROM landing.institutions WHERE unitid=%s) AS name,
              (SELECT slug FROM landing.institutions WHERE unitid=%s) AS slug,
              (SELECT page_url FROM landing.ir_pages WHERE unitid=%s) AS ir_url,
              (SELECT office_name FROM landing.ir_pages WHERE unitid=%s) AS office,
              (SELECT office_phone FROM landing.ir_pages WHERE unitid=%s) AS phone,
              (SELECT COUNT(*) FROM landing.ir_contacts WHERE unitid=%s) AS team,
              (SELECT COUNT(*) FROM landing.ir_documents WHERE unitid=%s) AS docs,
              (SELECT COUNT(*) FROM landing.cds_links WHERE unitid=%s) AS cds,
              (SELECT COUNT(*) FROM landing.notable_alumni WHERE unitid=%s) AS alumni,
              (SELECT COUNT(*) FROM landing.grants WHERE unitid=%s) AS grants,
              (SELECT COUNT(*) FROM landing.job_postings WHERE unitid=%s AND category='ir') AS ir_jobs,
              (SELECT COUNT(*) FROM landing.research_awards WHERE unitid=%s) AS research,
              (SELECT COUNT(*) FROM landing.peer_groups WHERE unitid=%s AND algorithm='dfr_v1') AS dfr_peers,
              (SELECT length(extract_summary) FROM landing.wiki_summaries WHERE unitid=%s) AS wiki_chars
            """,
            (unitid,) * 14,
        )
        r = cur.fetchone()
    click.echo(f"\n=== {r['name']} ({unitid}) — final coverage ===")
    click.echo(f"  IR office:    {r['office'] or '(missing)'}")
    click.echo(f"  IR URL:       {r['ir_url'] or '(missing)'}")
    click.echo(f"  Phone:        {r['phone'] or '(missing)'}")
    click.echo(f"  Wiki About:   {r['wiki_chars'] or 0} chars")
    click.echo(f"  IR contacts:  {r['team']}")
    click.echo(f"  IR documents: {r['docs']}  (factbook, DFR, strategic plan, etc.)")
    click.echo(f"  CDS links:    {r['cds']}")
    click.echo(f"  Notable alumni: {r['alumni']}")
    click.echo(f"  Grants:       {r['grants']}")
    click.echo(f"  IR jobs:      {r['ir_jobs']}")
    click.echo(f"  Research awards: {r['research']}")
    click.echo(f"  DFR peers:    {r['dfr_peers']}")
    click.echo(f"\nRendered page: docs/preview/{r['slug']}.html")


@click.command()
@click.option("--unitid", required=True, type=int)
@click.option("--skip-web", is_flag=True, help="Skip the 10 web pipeline targets")
@click.option("--skip-api", is_flag=True, help="Skip jobs/grants/alumni/dfr/ir-team")
def main(unitid: int, skip_web: bool, skip_api: bool) -> None:
    overall_t0 = time.monotonic()
    uid = str(unitid)

    click.echo(f"=== Running end-to-end for unitid {uid} ===\n")

    if not skip_web:
        click.echo("PHASE 1 — web pipeline targets (ir_page, factbook, cds, strategic_plan,")
        click.echo("           ir_jobs, grants, data_dictionary, data_definition, glossary, dashboards)")
        rc, line = _sh(
            ["python", "scripts/full_pilot_scrape.py", "--unitids", uid, "--web-only"],
            "full_pilot_scrape --web-only",
        )
        click.echo(line)

    if not skip_api:
        click.echo("\nPHASE 2 — IR team deep-dive (depends on ir_page URL)")
        rc, line = _sh(
            ["python", "scripts/run_powerful_ir_team.py", "--unitids", uid, "--concurrency", "1"],
            "run_powerful_ir_team",
        )
        click.echo(line)

        click.echo("\nPHASE 3 — independent sources (HigherEdJobs, SAM.gov, Alumni, DFR)")
        for cmd, label in [
            (["python", "scripts/run_higheredjobs.py",    "--unitids", uid], "HigherEdJobs"),
            (["python", "scripts/run_samgov.py",          "--unitids", uid], "SAM.gov grants"),
            (["python", "scripts/run_notable_alumni.py",  "--unitids", uid], "Notable alumni"),
            (["python", "scripts/run_dfr.py",             "--unitids", uid], "DFR peers"),
        ]:
            rc, line = _sh(cmd, label)
            click.echo(line)

    click.echo("\nPHASE 4 — refresh + render")
    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW landing.mv_institution_page")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT slug FROM landing.institutions WHERE unitid=%s", (unitid,))
        slug = cur.fetchone()["slug"]
    rc, line = _sh(
        ["python", "scripts/render_page.py", "--unitid", uid,
         "--output", f"docs/preview/{slug}.html"],
        "render_page",
    )
    click.echo(line)

    click.echo(f"\n=== Total time: {time.monotonic() - overall_t0:.1f}s ===")
    summary(unitid)


if __name__ == "__main__":
    main()

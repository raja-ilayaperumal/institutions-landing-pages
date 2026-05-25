"""Run the scraper for every institution flagged pilot_cohort, with persist.

Runs sequentially (one institution at a time) to keep rate-limited domains
politely throttled. For full-crawl scale switch to Celery + per-source queues
(see design spec); this script is purely for the 5-institution pilot.

Usage:
    python scripts/run_pilot.py
    python scripts/run_pilot.py --only ir_page,wikipedia,research_nih
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.cli import ALL_CONNECTORS, _run_all  # noqa: E402
from landing_scraper.db.dispatch import persist  # noqa: E402
from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.db.writer import ProvenanceWriter  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402
from landing_scraper.utils.logging import configure_logging, log  # noqa: E402


@click.command()
@click.option("--only", default=None, help=f"Comma-separated subset of: {','.join(ALL_CONNECTORS)}")
@click.option("--log-level", default="WARNING")
def main(only: str | None, log_level: str) -> None:
    configure_logging(level=log_level)
    selected = [s.strip() for s in only.split(",")] if only else ALL_CONNECTORS

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT unitid, slug, name FROM landing.institutions "
            "WHERE pilot_cohort ORDER BY unitid"
        )
        pilots = cur.fetchall()
    click.echo(f"Pilot cohort ({len(pilots)} institutions):")
    for p in pilots:
        click.echo(f"  {p['unitid']} {p['slug']}")
    click.echo()

    overall_t0 = time.monotonic()
    grand_totals = {"succeeded": 0, "failed": 0, "rows_persisted": 0}

    for p in pilots:
        unitid = p["unitid"]
        click.echo(f"--- [{unitid}] {p['name']} ---")
        t0 = time.monotonic()
        try:
            ctx = load_institution(unitid)
            results = asyncio.run(_run_all(ctx, selected))
        except Exception as e:  # noqa: BLE001
            click.echo(f"  CRASH: {e}")
            continue
        rows_written = 0
        with ProvenanceWriter() as writer:
            for r in results:
                rows_written += persist(writer, unitid=unitid, result=r)
        ok = sum(1 for r in results if r.success)
        bad = sum(1 for r in results if not r.success)
        dt = time.monotonic() - t0
        grand_totals["succeeded"] += ok
        grand_totals["failed"] += bad
        grand_totals["rows_persisted"] += rows_written
        click.echo(f"  succeeded={ok}/{len(results)}  rows_persisted={rows_written}  duration={dt:.1f}s")

    click.echo()
    click.echo(f"=== Pilot complete in {time.monotonic() - overall_t0:.1f}s ===")
    click.echo(f"  succeeded:  {grand_totals['succeeded']}")
    click.echo(f"  failed:     {grand_totals['failed']}")
    click.echo(f"  rows total: {grand_totals['rows_persisted']}")


if __name__ == "__main__":
    main()

"""Backfill / enrich the About summary for institutions with a thin or missing
summary, using AboutSummaryConnector (Wikipedia-gated, then the institution's
own /about page LLM-summarized into 2-3 sentences).

The connector persists its own row, so this driver just dispatches with bounded
concurrency and reports. Re-runnable: it overwrites whatever is there with a
richer summary (or leaves a thin Wikipedia stub if /about can't be fetched).

Usage:
    python scripts/backfill_about.py --unitids 126012,117585
    python scripts/backfill_about.py --thin-and-missing   # auto-pick targets
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.connectors.about_summary import (  # noqa: E402
    MIN_RICH_SUMMARY, AboutSummaryConnector,
)
from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402


def _auto_targets(cohort_ids: list[int] | None) -> list[int]:
    """unitids whose summary is missing or below the rich threshold."""
    where = ""
    params: tuple = (MIN_RICH_SUMMARY,)
    if cohort_ids:
        where = "AND i.unitid = ANY(%s)"
        params = (MIN_RICH_SUMMARY, cohort_ids)
    sql = f"""
        SELECT i.unitid
        FROM landing.institutions i
        LEFT JOIN landing.wiki_summaries w USING (unitid)
        WHERE i.enabled
          AND (w.extract_summary IS NULL
               OR length(w.extract_summary) < %s)
          {where}
        ORDER BY i.unitid
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [r["unitid"] for r in cur.fetchall()]


async def _one(unitid: int, sem: asyncio.Semaphore) -> tuple[int, str, str]:
    async with sem:
        ctx = load_institution(unitid)
        try:
            r = await AboutSummaryConnector().run(ctx)
        except Exception as e:  # noqa: BLE001
            return unitid, ctx.name, f"ERROR {type(e).__name__}: {str(e)[:60]}"
        src = (r.data or {}).get("source", "")
        summ = (r.data or {}).get("summary", "") or ""
        verdict = f"{src} ({len(summ)} chars)" if r.success else f"NULL ({r.error_class})"
        return unitid, ctx.name, verdict


@click.command()
@click.option("--unitids", default=None, help="Comma-separated unitids")
@click.option("--thin-and-missing", is_flag=True,
              help="Auto-pick institutions with thin/missing summaries")
@click.option("--cohort", type=click.Path(exists=True),
              help="Limit auto-pick to this cohort file's unitids")
@click.option("--concurrency", default=5, type=int)
def main(unitids, thin_and_missing, cohort, concurrency):
    cohort_ids = None
    if cohort:
        cohort_ids = [int(l.split()[0]) for l in Path(cohort).read_text().splitlines()
                      if l.strip() and not l.startswith("#") and l[0].isdigit()]
    if unitids:
        ids = [int(x) for x in unitids.split(",")]
    elif thin_and_missing:
        ids = _auto_targets(cohort_ids)
    else:
        raise click.UsageError("Provide --unitids or --thin-and-missing")

    click.echo(f"Backfilling About for {len(ids)} institution(s), concurrency={concurrency}")
    sem = asyncio.Semaphore(concurrency)

    async def _all():
        done = 0
        for fut in asyncio.as_completed([_one(u, sem) for u in ids]):
            uid, name, verdict = await fut
            done += 1
            mark = "·" if "NULL" in verdict or "ERROR" in verdict else "✓"
            click.echo(f"  [{done}/{len(ids)}] {mark} {uid:6d} {name[:40]:40} {verdict}", err=False)

    asyncio.run(_all())


if __name__ == "__main__":
    main()

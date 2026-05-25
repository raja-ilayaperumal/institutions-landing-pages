"""Run HigherEdJobs IR-jobs connector for specified institutions."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.connectors.higheredjobs import (  # noqa: E402
    HigherEdJobsIRConnector, persist_postings,
)
from landing_scraper.db.writer import ProvenanceWriter  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402


async def _one(unitid: int) -> tuple[int, str, int, float, str | None]:
    ctx = load_institution(unitid)
    with ProvenanceWriter() as writer:
        conn = HigherEdJobsIRConnector()
        r = await conn.run(ctx)
        rows = await persist_postings(unitid, r, writer)
    return unitid, ctx.name, rows, r.cost_usd, r.error_class


@click.command()
@click.option("--unitids", required=True, help="comma-separated unitids")
@click.option("--concurrency", default=3, type=int)
def main(unitids: str, concurrency: int) -> None:
    ids = [int(x) for x in unitids.split(",")]
    click.echo(f"HigherEdJobs IR-jobs run for {len(ids)} institutions (concurrency={concurrency})")

    sem = asyncio.Semaphore(concurrency)
    async def _bounded(u):
        async with sem:
            return await _one(u)

    async def _all():
        results = await asyncio.gather(*[_bounded(u) for u in ids], return_exceptions=True)
        total_rows = 0
        total_cost = 0.0
        for r in results:
            if isinstance(r, Exception):
                click.echo(f"  CRASH: {r}")
                continue
            uid, name, rows, cost, err = r
            mark = "✓" if rows > 0 else ("✗" if err else "—")
            extra = f" err={err}" if err else ""
            click.echo(f"  {mark} {uid:6d} {name:48s} postings={rows} cost=${cost:.4f}{extra}")
            total_rows += rows
            total_cost += cost
        click.echo(f"\nTotal: {total_rows} IR postings persisted, ${total_cost:.4f}")

    asyncio.run(_all())


if __name__ == "__main__":
    main()

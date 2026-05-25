"""Run SAM.gov grants connector for specified institutions."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.connectors.sam_gov import SAMGovGrantsConnector  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402


async def _one(unitid: int) -> tuple[int, str, int, float, str | None]:
    ctx = load_institution(unitid)
    conn = SAMGovGrantsConnector()
    r = await conn.run(ctx)
    n = len((r.data or {}).get("grants", [])) if r.success else 0
    return unitid, ctx.name, n, r.cost_usd, r.error_class


@click.command()
@click.option("--unitids", required=True)
@click.option("--concurrency", default=3, type=int)
def main(unitids: str, concurrency: int) -> None:
    ids = [int(x) for x in unitids.split(",")]
    sem = asyncio.Semaphore(concurrency)
    async def _b(u):
        async with sem:
            return await _one(u)
    async def _all():
        results = await asyncio.gather(*[_b(u) for u in ids], return_exceptions=True)
        total = 0
        cost = 0.0
        for r in results:
            if isinstance(r, Exception):
                click.echo(f"  CRASH: {r}")
                continue
            uid, name, n, c, err = r
            mark = "✓" if n > 0 else "✗"
            click.echo(f"  {mark} {uid:6d} {name:48s} grants={n} cost=${c:.4f} err={err or ''}")
            total += n
            cost += c
        click.echo(f"\nTotal: {total} grants persisted, ${cost:.4f}")
    asyncio.run(_all())


if __name__ == "__main__":
    main()

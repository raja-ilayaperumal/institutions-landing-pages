"""Run multi-source Notable Alumni connector for specified institutions."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.connectors.notable_alumni import NotableAlumniConnector  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402


async def _one(unitid: int) -> tuple[int, str, int, float, str | None, list]:
    ctx = load_institution(unitid)
    conn = NotableAlumniConnector()
    r = await conn.run(ctx)
    n = (r.data or {}).get("alumni_count", 0) if r.success else 0
    srcs = (r.data or {}).get("sources", []) if r.success else []
    return unitid, ctx.name, n, r.cost_usd, r.error_class, srcs


@click.command()
@click.option("--unitids", required=True)
@click.option("--concurrency", default=2, type=int)
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
            uid, name, n, c, err, srcs = r
            mark = "✓" if n > 0 else "✗"
            src_summary = ", ".join(f"{s['tier']}={s['count']}" for s in srcs) if srcs else ""
            click.echo(f"  {mark} {uid:6d} {name:48s} alumni={n:3d} cost=${c:.4f} [{src_summary}] err={err or ''}")
            total += n
            cost += c
        click.echo(f"\nTotal: {total} alumni persisted, ${cost:.4f}")
    asyncio.run(_all())


if __name__ == "__main__":
    main()

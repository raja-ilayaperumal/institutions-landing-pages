"""Run DFR peers connector for every pilot institution (or specified IDs)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.connectors.dfr_peers import DFRPeersConnector  # noqa: E402
from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402


async def _one(unitid: int) -> tuple[int, str, int, str | None]:
    ctx = load_institution(unitid)
    conn = DFRPeersConnector(year=2024)
    r = await conn.run(ctx)
    matched = (r.data or {}).get("matched_count", 0)
    return unitid, ctx.name, matched, r.error_class


@click.command()
@click.option("--unitids", default=None, help="Comma-separated unitids (default: all pilot)")
@click.option("--concurrency", default=4, type=int)
def main(unitids: str | None, concurrency: int) -> None:
    if unitids:
        ids = [int(x.strip()) for x in unitids.split(",")]
    else:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT unitid FROM landing.institutions WHERE pilot_cohort ORDER BY unitid")
            ids = [r["unitid"] for r in cur.fetchall()]

    sem = asyncio.Semaphore(concurrency)
    async def _bounded(uid):
        async with sem:
            return await _one(uid)

    async def _all():
        results = await asyncio.gather(*[_bounded(u) for u in ids])
        for uid, name, matched, err in results:
            status = "✓" if matched else ("✗" if err else "—")
            click.echo(f"  {status} {uid:6d} {name:50s}  peers={matched}  err={err or ''}")

    asyncio.run(_all())


if __name__ == "__main__":
    main()

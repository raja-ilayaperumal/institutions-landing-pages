"""Re-run ONLY the Wikipedia connector for specific institutions and persist.

Used to repair wrong-entity matches (e.g. "CBD College" → Auckland CBD) after
fixing the connector's disambiguation. Persist skips writing on a NULL result,
so institutions with no real article are left correctly empty (delete the old
row first if replacing).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.connectors.wikipedia import WikipediaConnector  # noqa: E402
from landing_scraper.db.dispatch import persist  # noqa: E402
from landing_scraper.db.writer import ProvenanceWriter  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402


async def _one(unitid: int) -> tuple[int, str, str, int]:
    ctx = load_institution(unitid)
    r = await WikipediaConnector().run(ctx)
    url = (r.data or {}).get("page_url", "") if r.success else ""
    with ProvenanceWriter() as w:
        rows = persist(w, unitid=unitid, result=r)
    verdict = url if r.success else f"NULL ({r.error_class})"
    return unitid, ctx.name, verdict, rows


@click.command()
@click.option("--unitids", required=True)
def main(unitids: str) -> None:
    ids = [int(x) for x in unitids.split(",")]
    async def _all():
        for u in ids:
            uid, name, verdict, rows = await _one(u)
            mark = "✓" if rows else "·"
            click.echo(f"  {mark} {uid:6d} {name:42s} rows={rows}  {verdict}")
    asyncio.run(_all())


if __name__ == "__main__":
    main()

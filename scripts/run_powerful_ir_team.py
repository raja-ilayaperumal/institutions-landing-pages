"""Run the powerful multi-page IR team extractor for institutions that
already have an ir_page URL in landing.ir_pages.

This is the upgrade-after-discovery step: once `ir_page` knows where the
IR office lives, run the deeper extraction to populate landing.ir_contacts
with the actual people.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.db.writer import ProvenanceWriter  # noqa: E402
from landing_scraper.scraper.ir_team_extractor import extract_ir_team  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402


async def _one(unitid: int, ir_url: str) -> tuple[int, str, dict]:
    ctx = load_institution(unitid)
    with ProvenanceWriter() as writer:
        r = await extract_ir_team(
            ir_url,
            institution_name=ctx.name,
            unitid=unitid,
            writer=writer,
            max_subpages=10,
        )
    return unitid, ctx.name, {
        "subpages_discovered": r.subpages_discovered,
        "subpages_succeeded": r.subpages_succeeded,
        "members": len(r.members),
        "office_name": r.office_name,
        "cost_usd": r.cost_usd,
        "error": r.error_class,
    }


@click.command()
@click.option("--unitids", default=None,
              help="Comma-separated unitids (default: all pilot with ir_pages row)")
@click.option("--concurrency", default=2, type=int,
              help="Concurrency cap — keep low to be polite to institution sites")
def main(unitids: str | None, concurrency: int) -> None:
    if unitids:
        unitid_list = [int(x.strip()) for x in unitids.split(",")]
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT unitid, page_url FROM landing.ir_pages WHERE unitid = ANY(%s)",
                (unitid_list,),
            )
            jobs = [(r["unitid"], r["page_url"]) for r in cur.fetchall()]
    else:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT ip.unitid, ip.page_url
                FROM landing.ir_pages ip
                JOIN landing.institutions li ON li.unitid = ip.unitid
                WHERE li.pilot_cohort AND ip.page_url IS NOT NULL
                ORDER BY ip.unitid
                """
            )
            jobs = [(r["unitid"], r["page_url"]) for r in cur.fetchall()]

    if not jobs:
        click.echo("No institutions with ir_page URL found. Run scraper first.")
        return

    click.echo(f"Running powerful IR team extraction for {len(jobs)} institutions:")
    for uid, url in jobs:
        click.echo(f"  {uid}: {url}")
    click.echo()

    sem = asyncio.Semaphore(concurrency)
    async def _bounded(uid, url):
        async with sem:
            return await _one(uid, url)

    async def _all():
        results = await asyncio.gather(
            *[_bounded(uid, url) for uid, url in jobs],
            return_exceptions=True,
        )
        total_members = 0
        total_cost = 0.0
        for r in results:
            if isinstance(r, Exception):
                click.echo(f"  CRASH: {r}")
                continue
            uid, name, data = r
            mark = "✓" if data["members"] > 0 else "✗"
            click.echo(
                f"  {mark} {uid:6d} {name:48s} "
                f"members={data['members']:3d}  "
                f"subpages={data['subpages_succeeded']}/{data['subpages_discovered']}  "
                f"cost=${data['cost_usd']:.4f}"
            )
            total_members += data["members"]
            total_cost += data["cost_usd"]
        click.echo()
        click.echo(f"Total: {total_members} members, ${total_cost:.4f}")

    asyncio.run(_all())


if __name__ == "__main__":
    main()

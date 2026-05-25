"""Look up LinkedIn URLs for IR contacts that don't have one yet."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.connectors.linkedin_lookup import (  # noqa: E402
    find_linkedin_url, update_contact_linkedin,
)
from landing_scraper.db.engine import get_conn  # noqa: E402


async def _one(contact_id: int, name: str, institution: str) -> dict:
    r = await find_linkedin_url(name, institution)
    if r.linkedin_url and r.confidence >= 0.7:
        update_contact_linkedin(contact_id, r.linkedin_url, r.confidence)
        return {"id": contact_id, "name": name, "ok": True,
                "url": r.linkedin_url, "conf": r.confidence,
                "cost": r.cost_usd}
    return {"id": contact_id, "name": name, "ok": False,
            "url": None, "conf": r.confidence, "candidates": r.candidates,
            "cost": r.cost_usd, "reason": r.reason}


@click.command()
@click.option("--unitids", default=None, help="Comma-separated unitids (default: all pilot)")
@click.option("--concurrency", default=3, type=int)
@click.option("--only-missing", is_flag=True, default=True,
              help="Skip rows that already have linkedin_url set")
def main(unitids: str | None, concurrency: int, only_missing: bool) -> None:
    where = "ic.name NOT IN ('', '(unparsed)') AND ic.name IS NOT NULL"
    if only_missing:
        where += " AND ic.linkedin_url IS NULL"
    if unitids:
        ids = [int(x) for x in unitids.split(",")]
        where += f" AND ic.unitid = ANY(ARRAY{ids})"
    else:
        where += " AND ic.unitid IN (SELECT unitid FROM landing.institutions WHERE pilot_cohort)"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT ic.id, ic.name, ic.title, ic.unitid, li.name AS institution
            FROM landing.ir_contacts ic
            JOIN landing.institutions li ON li.unitid = ic.unitid
            WHERE {where}
            ORDER BY ic.unitid, ic.id
        """)
        contacts = cur.fetchall()

    click.echo(f"Looking up LinkedIn for {len(contacts)} contacts (concurrency={concurrency})\n")

    sem = asyncio.Semaphore(concurrency)
    async def _bounded(c):
        async with sem:
            return await _one(c["id"], c["name"], c["institution"])

    async def _all():
        results = await asyncio.gather(*[_bounded(c) for c in contacts])
        ok = sum(1 for r in results if r["ok"])
        cost = sum(r["cost"] or 0 for r in results)
        for r in results:
            if r["ok"]:
                click.echo(f"  ✓ {r['name']:35s}  → {r['url']:60s}  conf={r['conf']:.2f}")
            else:
                click.echo(f"  ✗ {r['name']:35s}  reason: {r.get('reason','')[:80]}")
        click.echo(f"\nFound {ok}/{len(contacts)} LinkedIn URLs.  Cost ${cost:.4f}")

    asyncio.run(_all())


if __name__ == "__main__":
    main()

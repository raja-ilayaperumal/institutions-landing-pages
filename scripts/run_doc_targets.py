"""Run the document-discovery targets (data_dictionary, data_definition,
glossary) across all pilot institutions in parallel.

These three targets all use the new scraper pipeline (search + sitemap +
wellknown + LLM validate + LLM extract). Runs are bounded by per-domain
concurrency to stay polite.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.db.writer import ProvenanceWriter  # noqa: E402
from landing_scraper.scraper import scrape as pipeline_scrape  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402

DEFAULT_TARGETS = ["data_dictionary", "data_definition", "glossary"]


async def _one_target(unitid: int, target: str) -> tuple[int, str, dict]:
    ctx = load_institution(unitid)
    with ProvenanceWriter() as writer:
        try:
            r = await pipeline_scrape(ctx, target, writer=writer)
        except Exception as e:  # noqa: BLE001
            return unitid, target, {"error": str(e), "success": False}
    return unitid, target, {
        "success": r.success,
        "chosen_url": r.chosen_url,
        "candidates": r.candidates_found,
        "cost": r.cost_usd,
        "error": r.error_class,
    }


@click.command()
@click.option("--unitids", default=None, help="comma-separated unitids (default: all pilot)")
@click.option("--targets", default=",".join(DEFAULT_TARGETS), help="comma-separated targets")
@click.option("--concurrency", default=4, type=int)
def main(unitids: str | None, targets: str, concurrency: int) -> None:
    if unitids:
        ids = [int(x.strip()) for x in unitids.split(",")]
    else:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT unitid FROM landing.institutions WHERE pilot_cohort ORDER BY unitid")
            ids = [r["unitid"] for r in cur.fetchall()]

    target_list = [t.strip() for t in targets.split(",")]
    pairs = [(uid, t) for uid in ids for t in target_list]

    click.echo(f"Running {len(target_list)} targets × {len(ids)} institutions = {len(pairs)} pipeline runs")

    sem = asyncio.Semaphore(concurrency)
    async def _bounded(uid, t):
        async with sem:
            return await _one_target(uid, t)

    async def _all():
        results = await asyncio.gather(
            *[_bounded(uid, t) for uid, t in pairs], return_exceptions=True,
        )
        total_cost = 0.0
        ok = 0
        for r in results:
            if isinstance(r, Exception):
                click.echo(f"  CRASH: {r}")
                continue
            uid, target, data = r
            mark = "✓" if data["success"] else "✗"
            url = data.get("chosen_url") or ""
            err = data.get("error") or ""
            cost = data.get("cost", 0.0) or 0.0
            total_cost += cost
            if data["success"]:
                ok += 1
            click.echo(f"  {mark} [{uid}] {target:18s} → {(url or err)[:70]}  cost=${cost:.4f}")
        click.echo(f"\nTotal: {ok}/{len(pairs)} succeeded, ${total_cost:.4f} cost")

    asyncio.run(_all())


if __name__ == "__main__":
    main()

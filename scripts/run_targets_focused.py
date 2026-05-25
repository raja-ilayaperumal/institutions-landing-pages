"""Run specific scraper targets for specific institutions in parallel.
Smaller surface than full_pilot_scrape.py — for quick top-up runs.

  python scripts/run_targets_focused.py --unitids 100654,166683 --targets ir_jobs,grants
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.db.writer import ProvenanceWriter  # noqa: E402
from landing_scraper.scraper import scrape as pipeline_scrape  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402


async def _one(unitid: int, target: str) -> dict:
    ctx = load_institution(unitid)
    t0 = time.monotonic()
    with ProvenanceWriter() as writer:
        try:
            r = await pipeline_scrape(ctx, target, writer=writer)
        except Exception as e:  # noqa: BLE001
            return {"unitid": unitid, "target": target, "ok": False,
                    "err": "CRASH", "msg": str(e), "dt": time.monotonic() - t0}
    return {"unitid": unitid, "target": target, "ok": r.success,
            "chosen": r.chosen_url, "cands": r.candidates_found,
            "cost": r.cost_usd, "err": r.error_class,
            "dt": time.monotonic() - t0}


@click.command()
@click.option("--unitids", required=True, help="comma-separated unitids")
@click.option("--targets", required=True, help="comma-separated target names")
@click.option("--concurrency", default=3, type=int)
def main(unitids: str, targets: str, concurrency: int) -> None:
    ids = [int(x) for x in unitids.split(",")]
    tgts = [t.strip() for t in targets.split(",")]
    pairs = [(u, t) for u in ids for t in tgts]
    click.echo(f"Running {len(tgts)} targets × {len(ids)} institutions = {len(pairs)} runs (concurrency={concurrency})")

    sem = asyncio.Semaphore(concurrency)
    async def _bounded(u, t):
        async with sem:
            return await _one(u, t)

    async def _all():
        results = await asyncio.gather(*[_bounded(u, t) for u, t in pairs], return_exceptions=True)
        ok = 0
        total_cost = 0.0
        for r in results:
            if isinstance(r, Exception):
                click.echo(f"  CRASH: {r}")
                continue
            mark = "✓" if r["ok"] else "✗"
            url = r.get("chosen") or r.get("err", "")
            cost = r.get("cost", 0.0) or 0.0
            total_cost += cost
            if r["ok"]:
                ok += 1
            click.echo(f"  {mark} [{r['unitid']}] {r['target']:15s} → {(url or '')[:70]}  cost=${cost:.4f}  dt={r['dt']:.0f}s")
        click.echo(f"\nTotal: {ok}/{len(pairs)} ok, ${total_cost:.4f}")

    asyncio.run(_all())


if __name__ == "__main__":
    main()

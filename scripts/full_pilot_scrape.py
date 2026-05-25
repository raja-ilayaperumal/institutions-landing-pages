"""Full end-to-end scrape for the 5 pilot institutions.

For each pilot institution, run:
  • Web targets (new pipeline): ir_page, factbook, cds, strategic_plan
  • API connectors (existing):  wikipedia, brandfetch, research_nih,
                                research_nsf, research_usaspending
  • Dependent connector:        ir_team (uses ir_page's URL)

Runs sequentially per institution (politely throttled per-domain).
Persists everything to landing.* via ProvenanceWriter.
Refreshes the materialized views at the end.

Usage:
    python scripts/full_pilot_scrape.py
    python scripts/full_pilot_scrape.py --unitids 166683,100654
    python scripts/full_pilot_scrape.py --web-only
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.connectors.brandfetch import BrandfetchConnector  # noqa: E402
from landing_scraper.connectors.ir_team import IRTeamConnector  # noqa: E402
from landing_scraper.connectors.research_nih import NIHReporterConnector  # noqa: E402
from landing_scraper.connectors.research_nsf import NSFAwardsConnector  # noqa: E402
from landing_scraper.connectors.research_usaspending import USASpendingConnector  # noqa: E402
from landing_scraper.connectors.wikipedia import WikipediaConnector  # noqa: E402
from landing_scraper.db.dispatch import persist  # noqa: E402
from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.db.writer import ProvenanceWriter  # noqa: E402
from landing_scraper.scraper import scrape as pipeline_scrape  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402
from landing_scraper.utils.logging import configure_logging, log  # noqa: E402

WEB_TARGETS = ["ir_page", "factbook", "cds", "strategic_plan",
               "ir_jobs", "grants",
               "data_dictionary", "data_definition", "glossary"]
API_CONNECTORS = [
    ("wikipedia",             WikipediaConnector),
    ("brandfetch",            BrandfetchConnector),
    ("research_nih",          NIHReporterConnector),
    ("research_nsf",          NSFAwardsConnector),
    ("research_usaspending",  USASpendingConnector),
]


async def _run_one_institution(unitid: int, *, web_only: bool, api_only: bool) -> dict:
    ctx = load_institution(unitid)
    summary: dict = {
        "unitid": unitid, "slug": ctx.slug if hasattr(ctx, "slug") else None,
        "name": ctx.name, "targets": {}, "rows_written": 0,
        "total_cost_usd": 0.0,
    }

    with ProvenanceWriter() as writer:
        # 1) WEB TARGETS via new pipeline — parallelized (independent tables,
        #    autocommit-safe). Bounded by semaphore to be polite to institution
        #    sites + Tavily quota.
        if not api_only:
            sem = asyncio.Semaphore(4)

            async def _one_target(target: str) -> tuple[str, dict]:
                async with sem:
                    t0 = time.monotonic()
                    try:
                        res = await pipeline_scrape(ctx, target, writer=writer)
                    except Exception as e:  # noqa: BLE001
                        return target, {
                            "success": False, "error": "CRASHED", "msg": str(e),
                            "duration_s": time.monotonic() - t0,
                        }
                    return target, {
                        "success": res.success,
                        "chosen_url": res.chosen_url,
                        "chosen_via": res.chosen_via,
                        "confidence": res.confidence,
                        "candidates_found": res.candidates_found,
                        "cost_usd": res.cost_usd,
                        "duration_s": round(res.duration_seconds, 1),
                        "error": res.error_class,
                    }

            web_results = await asyncio.gather(*[_one_target(t) for t in WEB_TARGETS])
            for name, data in web_results:
                summary["targets"][name] = data
                summary["total_cost_usd"] += data.get("cost_usd", 0) or 0

        # 2) DEPENDENT: ir_team uses ir_page URL
        ir_url = (summary["targets"].get("ir_page") or {}).get("chosen_url") if not api_only else None
        if ir_url and not api_only:
            try:
                conn = IRTeamConnector(ir_landing_url=ir_url)
                res = await conn.run(ctx)
                rows = persist(writer, unitid=unitid, result=res)
                summary["targets"]["ir_team"] = {
                    "success": res.success, "rows": rows,
                    "members": (res.data or {}).get("members", []) and len((res.data or {}).get("members", [])),
                    "cost_usd": res.cost_usd, "error": res.error_class,
                }
                summary["total_cost_usd"] += res.cost_usd
            except Exception as e:  # noqa: BLE001
                summary["targets"]["ir_team"] = {"success": False, "error": "CRASHED", "msg": str(e)}

        # 3) API CONNECTORS
        if not web_only:
            for name, Conn in API_CONNECTORS:
                try:
                    res = await Conn().run(ctx)
                    rows = persist(writer, unitid=unitid, result=res)
                    summary["targets"][name] = {
                        "success": res.success, "rows": rows,
                        "cost_usd": res.cost_usd, "error": res.error_class,
                    }
                    summary["total_cost_usd"] += res.cost_usd
                except Exception as e:  # noqa: BLE001
                    summary["targets"][name] = {"success": False, "error": "CRASHED", "msg": str(e)}

    return summary


def _print_summary(s: dict) -> None:
    click.echo(f"\n=== [{s['unitid']}] {s['name']} — total cost ${s['total_cost_usd']:.4f} ===")
    for tgt, r in s["targets"].items():
        if r.get("success"):
            extra = ""
            if "chosen_url" in r and r["chosen_url"]:
                extra = f" → {r['chosen_url'][:60]}"
            elif "rows" in r:
                extra = f" → {r['rows']} rows"
            click.echo(f"  ✓ {tgt:25}{extra}")
        else:
            click.echo(f"  ✗ {tgt:25} err={r.get('error', 'unknown')}")


@click.command()
@click.option("--unitids", default=None,
              help="Comma-separated unitids (default: all pilot_cohort)")
@click.option("--web-only", is_flag=True, help="Skip API connectors")
@click.option("--api-only", is_flag=True, help="Skip web pipeline")
@click.option("--log-level", default="WARNING")
def main(unitids: str | None, web_only: bool, api_only: bool, log_level: str) -> None:
    configure_logging(level=log_level)

    if unitids:
        ids = [int(x.strip()) for x in unitids.split(",")]
    else:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT unitid FROM landing.institutions WHERE pilot_cohort ORDER BY unitid"
            )
            ids = [r["unitid"] for r in cur.fetchall()]

    click.echo(f"Scraping {len(ids)} institutions ({'web-only' if web_only else 'api-only' if api_only else 'full'})")
    click.echo(f"Targets: {WEB_TARGETS + ['ir_team'] + [n for n, _ in API_CONNECTORS]}")
    click.echo()

    overall_t0 = time.monotonic()
    grand_cost = 0.0
    for uid in ids:
        try:
            summary = asyncio.run(_run_one_institution(uid, web_only=web_only, api_only=api_only))
            _print_summary(summary)
            grand_cost += summary["total_cost_usd"]
        except Exception as e:  # noqa: BLE001
            click.echo(f"\n!! [{uid}] crashed: {e}\n")

    # Refresh materialized views
    click.echo("\nRefreshing materialized views…")
    try:
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW landing.mv_research_funding_summary")
            cur.execute("REFRESH MATERIALIZED VIEW landing.mv_institution_page")
    except Exception as e:  # noqa: BLE001
        click.echo(f"  MV refresh failed: {e}")

    click.echo(f"\n=== Pilot complete in {time.monotonic() - overall_t0:.0f}s — total LLM cost ${grand_cost:.4f} ===")


if __name__ == "__main__":
    main()

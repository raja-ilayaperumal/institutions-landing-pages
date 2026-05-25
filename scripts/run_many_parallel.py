"""Parallel end-to-end scrape for N institutions at once.

Runs ALL connectors (web pipeline targets + IR team + jobs + grants + alumni
+ DFR) for multiple institutions concurrently. Bounded by per-institution
concurrency cap to be polite to Tavily/sites.

  python scripts/run_many_parallel.py --unitids 199193,168421,245865,151810,491710
  python scripts/run_many_parallel.py --unitids ... --concurrency 3
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.connectors.about_summary import AboutSummaryConnector  # noqa: E402
from landing_scraper.connectors.higheredjobs import (  # noqa: E402
    HigherEdJobsIRConnector, persist_postings,
)
from landing_scraper.connectors.linkedin_lookup import (  # noqa: E402
    find_linkedin_url, update_contact_linkedin,
)
from landing_scraper.connectors.notable_alumni import NotableAlumniConnector  # noqa: E402
from landing_scraper.connectors.sam_gov import SAMGovGrantsConnector  # noqa: E402
from landing_scraper.connectors.dfr_peers import DFRPeersConnector  # noqa: E402
from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.db.writer import ProvenanceWriter  # noqa: E402
from landing_scraper.scraper import scrape as pipeline_scrape  # noqa: E402
from landing_scraper.scraper.ir_team_extractor import extract_ir_team  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402

WEB_TARGETS = [
    "ir_page", "factbook", "cds", "strategic_plan",
    "ir_jobs", "grants", "data_dictionary", "data_definition",
    "glossary", "dashboards",
]


async def _run_one_institution(unitid: int, *, target_concurrency: int) -> dict:
    """Full scrape for one institution. Targets run in parallel internally."""
    t0 = time.monotonic()
    summary = {"unitid": unitid, "phases": {}}
    try:
        ctx = load_institution(unitid)
        summary["name"] = ctx.name
    except Exception as e:  # noqa: BLE001
        summary["error"] = f"load failed: {e}"
        return summary

    # PHASE 1 — web targets in parallel
    sem = asyncio.Semaphore(target_concurrency)
    async def _target(name: str):
        async with sem:
            with ProvenanceWriter() as writer:
                try:
                    r = await pipeline_scrape(ctx, name, writer=writer)
                    return name, {"ok": r.success, "url": r.chosen_url,
                                  "cost": r.cost_usd, "err": r.error_class}
                except Exception as e:  # noqa: BLE001
                    return name, {"ok": False, "err": str(e)[:120]}
    t1 = time.monotonic()
    web_results = await asyncio.gather(*[_target(t) for t in WEB_TARGETS])
    summary["phases"]["web"] = {"duration_s": round(time.monotonic() - t1, 1),
                                 "results": dict(web_results)}

    # PHASE 2 — IR team (depends on ir_page result)
    ir_url = (dict(web_results).get("ir_page") or {}).get("url")
    t2 = time.monotonic()
    if ir_url:
        with ProvenanceWriter() as writer:
            try:
                ir_team_r = await extract_ir_team(
                    ir_url, institution_name=ctx.name,
                    unitid=unitid, writer=writer, max_subpages=10,
                )
                summary["phases"]["ir_team"] = {
                    "duration_s": round(time.monotonic() - t2, 1),
                    "members": len(ir_team_r.members),
                }
            except Exception as e:  # noqa: BLE001
                summary["phases"]["ir_team"] = {"err": str(e)[:120]}

    # PHASE 3 — independent sources (parallel)
    t3 = time.monotonic()
    async def _hej():
        with ProvenanceWriter() as writer:
            r = await HigherEdJobsIRConnector().run(ctx)
            n = await persist_postings(unitid, r, writer)
            return {"ok": r.success, "rows": n}
    async def _sam():
        r = await SAMGovGrantsConnector().run(ctx)
        return {"ok": r.success,
                "rows": len((r.data or {}).get("grants", []) or [])}
    async def _al():
        r = await NotableAlumniConnector().run(ctx)
        return {"ok": r.success,
                "count": (r.data or {}).get("alumni_count", 0)}
    async def _dfr():
        r = await DFRPeersConnector(year=2024).run(ctx)
        return {"ok": r.success,
                "matched": (r.data or {}).get("matched_count", 0)}
    async def _wiki():
        r = await AboutSummaryConnector().run(ctx)
        return {"ok": r.success,
                "source": (r.data or {}).get("source"),
                "err": r.error_class}

    hej, sam, al, dfr, wiki = await asyncio.gather(
        _hej(), _sam(), _al(), _dfr(), _wiki(),
        return_exceptions=True,
    )
    summary["phases"]["independent"] = {
        "duration_s": round(time.monotonic() - t3, 1),
        "higheredjobs": hej if isinstance(hej, dict) else {"err": str(hej)},
        "sam_gov":      sam if isinstance(sam, dict) else {"err": str(sam)},
        "alumni":       al  if isinstance(al, dict)  else {"err": str(al)},
        "dfr":          dfr if isinstance(dfr, dict) else {"err": str(dfr)},
        "wiki":         wiki if isinstance(wiki, dict) else {"err": str(wiki)},
    }

    # PHASE 4 — LinkedIn URL lookup for IR contacts found in Phase 2.
    # Depends on Phase 2's ir_team having persisted rows, so it runs last.
    # Skipped automatically when there are no contacts (no extra cost).
    t4 = time.monotonic()
    summary["phases"]["linkedin"] = await _phase_linkedin(unitid, ctx.name)
    summary["phases"]["linkedin"]["duration_s"] = round(
        time.monotonic() - t4, 1
    )

    summary["total_duration_s"] = round(time.monotonic() - t0, 1)
    return summary


async def _phase_linkedin(unitid: int, institution_name: str) -> dict:
    """Look up LinkedIn URLs for every IR contact at this institution.

    Strategy:
      - Pull contacts that don't have a LinkedIn URL yet.
      - Look up each in parallel (bounded concurrency to be polite).
      - Persist only when confidence >= 0.7 (the existing strict threshold).

    Cost is dominated by Google CSE calls now that the lean Tavily plan
    routes LinkedIn search through CSE first — typically 1-3 free CSE
    queries per contact, falling through to Tavily only when CSE is empty.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, name FROM landing.ir_contacts
                WHERE unitid = %s AND linkedin_url IS NULL
                  AND name IS NOT NULL AND name NOT IN ('', '(unparsed)')
                ORDER BY ordering, id""",
            (unitid,),
        )
        contacts = cur.fetchall()

    if not contacts:
        return {"found": 0, "checked": 0, "cost_usd": 0.0}

    sem = asyncio.Semaphore(3)

    async def _lookup_one(c: dict) -> tuple[int, str, object]:
        async with sem:
            return c["id"], c["name"], await find_linkedin_url(
                c["name"], institution_name,
            )

    results = await asyncio.gather(
        *[_lookup_one(c) for c in contacts], return_exceptions=True,
    )
    found = 0
    cost = 0.0
    for r in results:
        if isinstance(r, Exception):
            continue
        contact_id, _name, lr = r
        cost += getattr(lr, "cost_usd", 0.0)
        if lr.linkedin_url and lr.confidence >= 0.7:
            update_contact_linkedin(
                contact_id, lr.linkedin_url, lr.confidence,
            )
            found += 1
    return {
        "found": found, "checked": len(contacts),
        "cost_usd": round(cost, 4),
    }


@click.command()
@click.option("--unitids", required=True, help="comma-separated unitids")
@click.option("--institution-concurrency", default=3, type=int,
              help="how many institutions run at once")
@click.option("--target-concurrency", default=4, type=int,
              help="how many web targets per institution run at once")
def main(unitids: str, institution_concurrency: int,
         target_concurrency: int) -> None:
    ids = [int(x) for x in unitids.split(",")]
    click.echo(f"Running {len(ids)} institutions  (institution_concurrency={institution_concurrency}, target_concurrency={target_concurrency})")

    sem = asyncio.Semaphore(institution_concurrency)
    async def _b(u):
        async with sem:
            return await _run_one_institution(u, target_concurrency=target_concurrency)

    async def _all():
        t0 = time.monotonic()
        results = await asyncio.gather(*[_b(u) for u in ids],
                                       return_exceptions=True)
        click.echo()
        for r in results:
            if isinstance(r, Exception):
                click.echo(f"  CRASH: {r}")
                continue
            ph = r.get("phases") or {}
            web_ok = sum(1 for v in (ph.get("web") or {}).get("results", {}).values() if v.get("ok"))
            web_total = len((ph.get("web") or {}).get("results") or {})
            team = (ph.get("ir_team") or {}).get("members", 0)
            ind = ph.get("independent") or {}
            li = (ph.get("linkedin") or {})
            click.echo(
                f"  [{r['unitid']}] {r.get('name','?'):45s}  "
                f"web={web_ok}/{web_total}  team={team}  "
                f"hej={(ind.get('higheredjobs') or {}).get('rows',0)}  "
                f"sam={(ind.get('sam_gov') or {}).get('rows',0)}  "
                f"alumni={(ind.get('alumni') or {}).get('count',0)}  "
                f"dfr={(ind.get('dfr') or {}).get('matched',0)}  "
                f"wiki={'✓' if (ind.get('wiki') or {}).get('ok') else '✗'}  "
                f"li={li.get('found',0)}/{li.get('checked',0)}  "
                f"dt={r.get('total_duration_s',0):.0f}s"
            )
        click.echo(f"\nOverall: {time.monotonic() - t0:.0f}s wall for {len(ids)} institutions")

    asyncio.run(_all())


if __name__ == "__main__":
    main()

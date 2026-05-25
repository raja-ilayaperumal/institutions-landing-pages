"""End-to-end CLI runner.

Usage:
  landing-scraper run --unitid 166683
  landing-scraper run --unitid 166683 --only ir_page,ir_team,research_nih

Outputs a single JSON blob to stdout with one entry per source_type.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import click
import structlog

from .connectors.base import ConnectorResult, InstitutionContext
from .connectors.brandfetch import BrandfetchConnector
from .db.dispatch import persist
from .db.writer import ProvenanceWriter
from .connectors.cds import CDSConnector
from .connectors.factbook import FactbookConnector
from .connectors.ir_page import IRPageConnector
from .connectors.ir_team import IRTeamConnector
from .connectors.jobs import JobsConnector
from .connectors.research_herd import HERDConnector
from .connectors.research_nih import NIHReporterConnector
from .connectors.research_nsf import NSFAwardsConnector
from .connectors.research_usaspending import USASpendingConnector
from .connectors.strategic_plan import StrategicPlanConnector
from .connectors.wikipedia import WikipediaConnector
from .utils.institutions import load_institution
from .utils.logging import configure_logging, log

ALL_CONNECTORS: list[str] = [
    "brandfetch",
    "wikipedia",
    "ir_page",
    "ir_team",
    "factbook",
    "cds",
    "strategic_plan",
    "jobs",
    "research_herd",
    "research_nih",
    "research_nsf",
    "research_usaspending",
]


@click.group()
def cli() -> None:
    pass


@cli.command()
@click.option("--unitid", type=int, required=True, help="Institution unitid")
@click.option("--target", required=True, help="One target name (e.g. ir_page, factbook, cds, strategic_plan)")
@click.option("--persist/--no-persist", default=True, help="Write results to landing.* (default on)")
@click.option("--output", type=click.Path(dir_okay=False, writable=True, path_type=Path), default=None,
              help="Write full pipeline report to JSON file")
@click.option("--log-level", default="INFO")
def scrape(unitid: int, target: str, persist: bool, output: Path | None, log_level: str) -> None:
    """Powerful single-target scrape: discover → validate → fetch → extract → persist.

    Examples:
        landing-scraper scrape --unitid 166683 --target ir_page
        landing-scraper scrape --unitid 228778 --target factbook --output ut_fb.json
    """
    import asyncio
    from dataclasses import asdict

    from .scraper import all_target_names, scrape as pipeline_scrape

    configure_logging(level=log_level)
    if target not in all_target_names():
        raise click.BadParameter(
            f"unknown target '{target}'. Known: {', '.join(all_target_names())}"
        )

    ctx = load_institution(unitid)
    log.info(
        "scrape.cli.start",
        unitid=ctx.unitid, name=ctx.name, target=target,
        domain=ctx.domain, registrable=ctx.registrable_domain,
    )

    async def _run():
        if persist:
            with ProvenanceWriter() as writer:
                return await pipeline_scrape(ctx, target, writer=writer)
        return await pipeline_scrape(ctx, target, writer=None)

    result = asyncio.run(_run())
    payload = asdict(result)
    serialized = json.dumps(payload, indent=2, default=str)
    if output:
        output.write_text(serialized)
        click.echo(f"wrote {output}")
    else:
        click.echo(serialized)


@cli.command()
@click.option("--unitid", type=int, required=True)
@click.option(
    "--only",
    default=None,
    help=f"Comma-separated subset of: {','.join(ALL_CONNECTORS)}",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write JSON to file (otherwise stdout)",
)
@click.option("--persist", is_flag=True, help="Write results to landing.* tables via ProvenanceWriter")
@click.option("--log-level", default="INFO")
def run(unitid: int, only: str | None, output: Path | None, persist: bool, log_level: str) -> None:
    """Run all (or a subset of) connectors for one institution."""
    configure_logging(level=log_level)
    selected = [s.strip() for s in only.split(",")] if only else ALL_CONNECTORS
    unknown = [s for s in selected if s not in ALL_CONNECTORS]
    if unknown:
        raise click.BadParameter(f"unknown sources: {unknown}")

    ctx = load_institution(unitid)
    log.info(
        "ctx.loaded",
        unitid=ctx.unitid, name=ctx.name, state=ctx.state,
        domain=ctx.domain, registrable_domain=ctx.registrable_domain,
        ueis=ctx.ueis,
    )

    out = asyncio.run(_run_all(ctx, selected))

    rows_written = 0
    if persist:
        from .db.dispatch import persist as _persist
        with ProvenanceWriter() as writer:
            for r in out:
                rows_written += _persist(writer, unitid=ctx.unitid, result=r)
        log.info("persist.done", rows_written=rows_written, unitid=ctx.unitid)

    payload = {
        "institution": {
            "unitid": ctx.unitid,
            "name": ctx.name,
            "state": ctx.state,
            "domain": ctx.domain,
            "canonical_root_url": ctx.canonical_root_url,
            "ueis": ctx.ueis,
            "opeid": ctx.opeid,
        },
        "results": {r.source_type: _serialize(r) for r in out},
        "summary": {**_summarize(out), "rows_persisted": rows_written if persist else None},
    }
    serialized = json.dumps(payload, indent=2, default=str)
    if output:
        output.write_text(serialized)
        click.echo(f"wrote {output}")
    else:
        click.echo(serialized)


async def _run_all(ctx: InstitutionContext, selected: list[str]) -> list[ConnectorResult]:
    """Run connectors. Some have dependencies (ir_team needs ir_page url)."""
    # Phase 1 — independent connectors run in parallel
    phase1_factory: dict[str, callable] = {
        "brandfetch": BrandfetchConnector,
        "wikipedia": WikipediaConnector,
        "ir_page": IRPageConnector,
        "factbook": FactbookConnector,
        "cds": CDSConnector,
        "strategic_plan": StrategicPlanConnector,
        "jobs": JobsConnector,
        "research_herd": HERDConnector,
        "research_nih": NIHReporterConnector,
        "research_nsf": NSFAwardsConnector,
        "research_usaspending": USASpendingConnector,
    }

    phase1_names = [s for s in selected if s in phase1_factory]
    log.info("phase1.start", connectors=phase1_names)
    phase1_results = await asyncio.gather(
        *[_safe_run(phase1_factory[name](), ctx, name) for name in phase1_names],
        return_exceptions=False,
    )

    by_type: dict[str, ConnectorResult] = {r.source_type: r for r in phase1_results}

    # Phase 2 — ir_team (depends on ir_page)
    if "ir_team" in selected:
        ir_url = None
        if "ir_page" in by_type and by_type["ir_page"].success:
            ir_url = by_type["ir_page"].canonical_url
        log.info("phase2.ir_team", ir_url=ir_url)
        ir_team_res = await _safe_run(IRTeamConnector(ir_landing_url=ir_url), ctx, "ir_team")
        phase1_results.append(ir_team_res)

    return phase1_results


async def _safe_run(connector, ctx: InstitutionContext, name: str) -> ConnectorResult:
    t0 = time.monotonic()
    try:
        res = await connector.run(ctx)
    except Exception as e:  # noqa: BLE001
        log.exception("connector.crashed", source=name, error=str(e))
        return ConnectorResult(
            source_type=name, success=False,
            error_class="UNHANDLED_EXCEPTION", error_message=str(e),
        )
    dt = time.monotonic() - t0
    log.info(
        "connector.done",
        source=name, success=res.success,
        duration_s=round(dt, 2),
        canonical_url=res.canonical_url,
        error=res.error_class,
        cost_usd=res.cost_usd,
    )
    return res


def _serialize(r: ConnectorResult) -> dict:
    d = asdict(r)
    return d


def _summarize(results: list[ConnectorResult]) -> dict:
    succ = [r for r in results if r.success]
    fail = [r for r in results if not r.success]
    total_cost = sum(r.cost_usd for r in results)
    return {
        "total_connectors": len(results),
        "succeeded": len(succ),
        "failed": len(fail),
        "success_rate": round(len(succ) / max(len(results), 1), 2),
        "failures_by_class": {
            r.source_type: r.error_class for r in fail
        },
        "total_cost_usd": round(total_cost, 4),
    }


if __name__ == "__main__":
    cli()

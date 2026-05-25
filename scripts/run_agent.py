"""LangGraph URL-finder agent — escalation for hard-to-discover pages.

When the standard pipeline returns LLM_REJECTED or finds nothing credible,
run this agent for the (institution, target) pair. The agent reasons
iteratively about which tool to use next (web_search, fetch_snippet,
probe_url) and only commits when it has high confidence.

  python scripts/run_agent.py --unitid 166683 --target ir_page
  python scripts/run_agent.py --unitid 142522 --target factbook
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.agents import find_url  # noqa: E402
from landing_scraper.scraper.targets import TARGETS  # noqa: E402
from landing_scraper.utils.institutions import load_institution  # noqa: E402
from landing_scraper.utils.logging import configure_logging  # noqa: E402


@click.command()
@click.option("--unitid", required=True, type=int)
@click.option("--target", required=True,
              help=f"One of: {', '.join(sorted(TARGETS.keys()))}")
@click.option("--max-steps", default=12, type=int)
@click.option("--log-level", default="WARNING")
def main(unitid: int, target: str, max_steps: int, log_level: str) -> None:
    configure_logging(level=log_level)
    if target not in TARGETS:
        raise click.BadParameter(f"unknown target. Known: {sorted(TARGETS.keys())}")
    ctx = load_institution(unitid)
    spec = TARGETS[target]

    result = asyncio.run(find_url(
        institution_name=ctx.name,
        registrable_domain=ctx.registrable_domain or ctx.domain or "",
        target_description=spec.description,
        max_steps=max_steps,
    ))

    click.echo(json.dumps({
        "unitid": unitid, "institution": ctx.name, "target": target,
        "found_url": result.found_url,
        "confidence": result.confidence,
        "reason": result.reason,
        "tool_calls": result.tool_calls,
    }, indent=2))


if __name__ == "__main__":
    main()

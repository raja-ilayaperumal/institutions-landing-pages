"""Compute peer groups for institutions and persist to landing.peer_groups.

Usage:
    python scripts/compute_peers.py                      # all enabled institutions
    python scripts/compute_peers.py --pilot              # only pilot cohort
    python scripts/compute_peers.py --unitids 166683     # specific IDs
"""
from __future__ import annotations

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.peers.carnegie_state_control import compute  # noqa: E402
from landing_scraper.utils.logging import configure_logging, log  # noqa: E402


@click.command()
@click.option("--pilot", is_flag=True, help="Compute only for the pilot cohort")
@click.option("--unitids", default=None, help="Comma-separated unitids")
@click.option("--top-n", default=5, type=int)
@click.option("--log-level", default="INFO")
def main(pilot: bool, unitids: str | None, top_n: int, log_level: str) -> None:
    configure_logging(level=log_level)
    ids = [int(x.strip()) for x in unitids.split(",")] if unitids else None
    n = compute(only_pilot=pilot, only_unitids=ids, top_n=top_n)
    log.info("done", rows_written=n)


if __name__ == "__main__":
    main()

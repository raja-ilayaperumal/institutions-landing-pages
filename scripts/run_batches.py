"""Drive a cohort through run_many_parallel in fixed-size batches.

Each batch runs its N institutions IN PARALLEL, in its OWN subprocess. A fresh
process means a fresh shared browser (see core/crawler._get_crawler) that is torn
down when the batch ends — so memory/swap can't accumulate across the whole
corpus (the failure mode of one giant long-lived run).

Resumable: pending is recomputed from landing.scraper_runs before every batch
(every attempted institution lands there regardless of yield), so a kill/restart
picks up exactly where it left off and never re-runs a completed institution.

Usage:
    python scripts/run_batches.py --cohort scripts/cohorts/ca_rank_201_plus.txt
    python scripts/run_batches.py --cohort <file> --limit-batches 2   # smoke test
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import click
import psycopg

REPO = Path(__file__).resolve().parent.parent


def _cohort_ids(path: Path) -> list[int]:
    ids: list[int] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line[0].isdigit():
            ids.append(int(line.split()[0]))
    return ids


@click.command()
@click.option("--cohort", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--batch-size", default=5, type=int, help="institutions per batch (run in parallel)")
@click.option("--target-concurrency", default=2, type=int)
@click.option("--max-browsers", default=4, type=int, help="CRAWL4AI_MAX_BROWSERS per batch")
@click.option("--limit-batches", default=0, type=int, help="0 = run until cohort is exhausted")
def main(cohort: Path, batch_size: int, target_concurrency: int,
         max_browsers: int, limit_batches: int) -> None:
    cohort_ids = _cohort_ids(cohort)
    conn = psycopg.connect("dbname=clema_landing")

    def pending() -> list[int]:
        rows = conn.execute(
            "SELECT DISTINCT unitid FROM landing.scraper_runs WHERE unitid = ANY(%s)",
            (cohort_ids,),
        ).fetchall()
        done = {r[0] for r in rows}
        return [u for u in cohort_ids if u not in done]

    total = len(cohort_ids)
    b = 0
    while True:
        pend = pending()
        done_n = total - len(pend)
        if not pend:
            print(f"\nALL DONE — {total}/{total} attempted, 0 pending.", flush=True)
            break
        batch = pend[:batch_size]
        b += 1
        print(f"\n=== batch {b}: {len(batch)} institutions in parallel "
              f"| progress {done_n}/{total} done, {len(pend)} pending ===",
              flush=True)
        print(f"    ids: {','.join(map(str, batch))}", flush=True)
        env = {**os.environ, "CRAWL4AI_MAX_BROWSERS": str(max_browsers)}
        t0 = time.time()
        rc = subprocess.run(
            [sys.executable, "scripts/run_many_parallel.py",
             "--unitids", ",".join(map(str, batch)),
             "--institution-concurrency", str(len(batch)),
             "--target-concurrency", str(target_concurrency)],
            cwd=str(REPO), env=env,
        ).returncode
        print(f"    batch {b} finished in {time.time() - t0:.0f}s (rc={rc})", flush=True)
        if limit_batches and b >= limit_batches:
            print(f"\nstopping after {limit_batches} batch(es) as requested.", flush=True)
            break


if __name__ == "__main__":
    main()

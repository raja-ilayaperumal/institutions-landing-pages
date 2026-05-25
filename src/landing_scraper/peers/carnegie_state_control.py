"""Peer-group algorithm: carnegie_state_control_v1.

For each institution, find the closest N peers by:
  1. Same Carnegie basic classification (must match)
  2. Same control (must match)
  3. Same state (preferred; otherwise same region)
  4. Closest enrollment size (12-month unduplicated headcount from IPEDS)

Returns top N peers ranked by composite similarity score:
  score = 1.0 * carnegie_match
        + 0.5 * state_match (or 0.3 for same-region-different-state)
        + 0.3 * exp(-abs(log(my_enroll) - log(peer_enroll)))

All-DB; no scraping. Writes to landing.peer_groups with
algorithm='carnegie_state_control_v1', algorithm_version='1.0.0'.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import structlog

from ..db.engine import get_conn

log = structlog.get_logger(__name__)

ALGORITHM = "carnegie_state_control_v1"
ALGORITHM_VERSION = "1.0.0"
DEFAULT_TOP_N = 5


@dataclass
class _InstFeature:
    unitid: int
    carnegie_slug: str | None
    control_slug: str | None
    state_slug: str | None
    region: str | None
    enrollment: int | None


def _fetch_features(only_pilot: bool, only_unitids: list[int] | None) -> list[_InstFeature]:
    sql = """
        SELECT
            li.unitid,
            li.carnegie_basic_slug,
            li.control_slug,
            li.state_slug,
            li.region,
            -- 12-month unduplicated headcount as the enrollment proxy
            (SELECT efytotlt FROM ipeds.enrollment_12month_2024
              WHERE unitid = li.unitid LIMIT 1) AS enrollment
        FROM landing.institutions li
        WHERE li.enabled
          AND li.carnegie_basic_slug IS NOT NULL
          AND li.control_slug IS NOT NULL
    """
    args: list = []
    if only_pilot:
        sql += " AND li.pilot_cohort"
    if only_unitids:
        sql += " AND li.unitid = ANY(%s)"
        args.append(only_unitids)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return [_InstFeature(
        unitid=r["unitid"],
        carnegie_slug=r["carnegie_basic_slug"],
        control_slug=r["control_slug"],
        state_slug=r["state_slug"],
        region=r["region"],
        enrollment=r["enrollment"],
    ) for r in rows]


def _fetch_all_institution_features() -> dict[int, _InstFeature]:
    """All enabled institutions — used as the candidate pool for any seed."""
    out: dict[int, _InstFeature] = {}
    for f in _fetch_features(only_pilot=False, only_unitids=None):
        out[f.unitid] = f
    return out


def _score(seed: _InstFeature, peer: _InstFeature) -> float:
    """Composite similarity score in [0..2]ish."""
    if peer.unitid == seed.unitid:
        return -1.0
    # Hard filter: must match Carnegie + control
    if peer.carnegie_slug != seed.carnegie_slug:
        return -1.0
    if peer.control_slug != seed.control_slug:
        return -1.0
    score = 1.0  # carnegie + control match
    # State proximity
    if peer.state_slug and peer.state_slug == seed.state_slug:
        score += 0.5
    elif peer.region and peer.region == seed.region:
        score += 0.3
    # Enrollment proximity (log-space exponential decay)
    if seed.enrollment and peer.enrollment and seed.enrollment > 0 and peer.enrollment > 0:
        diff = abs(math.log(seed.enrollment) - math.log(peer.enrollment))
        score += 0.3 * math.exp(-diff)
    return score


def compute(*, only_pilot: bool = False, only_unitids: list[int] | None = None,
            top_n: int = DEFAULT_TOP_N) -> int:
    """Compute peer groups for the requested institutions. Returns row-count written."""
    seeds = _fetch_features(only_pilot=only_pilot, only_unitids=only_unitids)
    pool = _fetch_all_institution_features()
    log.info("peers.compute.start", seeds=len(seeds), pool=len(pool), top_n=top_n)

    write_rows: list[tuple] = []
    for seed in seeds:
        candidates: list[tuple[float, int]] = []
        for peer in pool.values():
            s = _score(seed, peer)
            if s > 0:
                candidates.append((s, peer.unitid))
        candidates.sort(key=lambda x: x[0], reverse=True)
        for rank, (score, peer_unitid) in enumerate(candidates[:top_n], start=1):
            write_rows.append((seed.unitid, peer_unitid, rank, round(score, 4),
                               ALGORITHM, ALGORITHM_VERSION))
        if not candidates:
            log.warning("peers.no_candidates", unitid=seed.unitid,
                        carnegie=seed.carnegie_slug, control=seed.control_slug)

    if not write_rows:
        return 0

    upsert_sql = """
        INSERT INTO landing.peer_groups
          (unitid, peer_unitid, rank, similarity_score, algorithm, algorithm_version)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (unitid, algorithm, algorithm_version, peer_unitid) DO UPDATE
        SET rank = EXCLUDED.rank,
            similarity_score = EXCLUDED.similarity_score,
            computed_at = NOW()
    """
    with get_conn() as conn, conn.cursor() as cur:
        # Drop old top-N for these seeds before re-inserting (so stale rank=6+ rows clear)
        seed_ids = list({r[0] for r in write_rows})
        cur.execute(
            "DELETE FROM landing.peer_groups "
            "WHERE algorithm = %s AND algorithm_version = %s AND unitid = ANY(%s)",
            (ALGORITHM, ALGORITHM_VERSION, seed_ids),
        )
        cur.executemany(upsert_sql, write_rows)
    log.info("peers.compute.done", rows_written=len(write_rows))
    return len(write_rows)

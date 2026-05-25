"""IPEDS Data Feedback Report (DFR) — official peer comparison group.

Source: https://nces.ed.gov/ipeds/dfr/{year}/ReportHTML.aspx?unitId={unitid}

Every institution that reports to IPEDS gets a Data Feedback Report each year.
The report includes a "Comparison Group" of 5-20 peer institutions — either
custom-selected by the institution itself OR auto-selected by NCES. These are
the authoritative peer set; using them is far more credible than algorithmic
guesses.

Output: rows in landing.peer_groups with algorithm='dfr_v1'.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
import structlog
from bs4 import BeautifulSoup

from ..db.engine import get_conn
from ..db import document_store
from .base import BaseConnector, ConnectorResult, InstitutionContext

log = structlog.get_logger(__name__)

ALGORITHM = "dfr_v1"
ALGORITHM_VERSION = "2024"  # bump when NCES publishes a new DFR year
DEFAULT_YEAR = 2024
UA = "Mozilla/5.0 ClemaLandingPages/0.1 (Educational research; contact raja@blocksurvey.org)"

_CITY_STATE = re.compile(r"\(([^,]+),\s*([A-Z]{2})\)$")


@dataclass
class _PeerCandidate:
    name: str
    city: str | None
    state: str | None
    unitid: int | None = None


class DFRPeersConnector(BaseConnector):
    """Scrapes the NCES DFR HTML page and extracts the comparison group."""

    source_type = "dfr_peers"

    def __init__(self, year: int = DEFAULT_YEAR):
        self.year = year

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        url = f"https://nces.ed.gov/ipeds/dfr/{self.year}/ReportHTML.aspx?unitId={ctx.unitid}"
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                         headers={"User-Agent": UA}) as client:
                resp = await client.get(url)
        except Exception as e:  # noqa: BLE001
            return self._err("HTTP_ERROR", str(e))

        if not resp.is_success:
            return self._err("HTTP_ERROR", f"HTTP {resp.status_code} from {url}")

        # Persist the DFR HTML to disk
        stored = document_store.save(
            unitid=ctx.unitid,
            doc_type="dfr_report",
            body_bytes=resp.content,
            content_type="text/html",
            url=url, year=self.year,
            title_hint=f"dfr-{self.year}",
        )
        _write_dfr_document(ctx.unitid, url, self.year, stored)

        soup = BeautifulSoup(resp.text, "lxml")
        tbl = soup.find("table", id="tblComparisonGroup")
        if not tbl:
            return self._err("NO_COMPARISON_GROUP", "tblComparisonGroup not found in DFR")

        candidates = _parse_comparison_group(tbl)
        if not candidates:
            return self._err("EMPTY_COMPARISON_GROUP", "DFR table found but no institutions parsed")

        # Resolve names → unitids via IPEDS lookup
        resolved = _resolve_unitids(candidates)
        matched = [c for c in resolved if c.unitid is not None]
        unmatched = [c for c in resolved if c.unitid is None]

        log.info("dfr.parsed",
                 unitid=ctx.unitid, total_in_group=len(candidates),
                 matched=len(matched), unmatched=len(unmatched))

        # Persist to landing.peer_groups
        if matched:
            _write_peers(ctx.unitid, matched)

        return self._ok(
            canonical_url=url,
            data={
                "year": self.year,
                "comparison_group_size": len(candidates),
                "matched_count": len(matched),
                "unmatched_count": len(unmatched),
                "peers": [
                    {"rank": i + 1, "unitid": c.unitid,
                     "name": c.name, "city": c.city, "state": c.state}
                    for i, c in enumerate(matched)
                ],
                "unmatched_peers": [
                    {"name": c.name, "city": c.city, "state": c.state}
                    for c in unmatched
                ],
            },
            confidence=1.0 if matched else 0.0,
        )


def _parse_comparison_group(tbl) -> list[_PeerCandidate]:
    """Parse the comparison-group table.

    Two formats coexist:
      (a) one institution per <td> with text 'Name (City, ST)'
      (b) one big <td> with all institutions concatenated
    We prefer (a). Fall back to (b) via regex.
    """
    seen: set[str] = set()
    out: list[_PeerCandidate] = []
    # Format (a): each table row containing exactly one institution
    for row in tbl.find_all("tr"):
        text = row.get_text(" ", strip=True)
        if not text:
            continue
        m = _CITY_STATE.search(text)
        if m and len(text) < 200 and "COMPARISON GROUP" not in text:
            name = text[:m.start()].strip()
            if name and name not in seen:
                seen.add(name)
                out.append(_PeerCandidate(name=name, city=m.group(1).strip(), state=m.group(2)))
    if out:
        return out

    # Format (b): one big text blob — split on "(City, ST) " boundaries
    full = tbl.get_text(" ", strip=True)
    # Pattern: <institution name> (<city>, <st>)
    for match in re.finditer(r"([A-Z][^()]+?)\s*\(([^,]+),\s*([A-Z]{2})\)", full):
        name = match.group(1).strip().rstrip(",")
        # filter noise
        if "Comparison Group" in name or len(name) < 5:
            continue
        if name not in seen:
            seen.add(name)
            out.append(_PeerCandidate(name=name, city=match.group(2).strip(), state=match.group(3)))
    return out


def _resolve_unitids(candidates: list[_PeerCandidate]) -> list[_PeerCandidate]:
    """Match each candidate to a landing.institutions row by name+city+state."""
    with get_conn() as conn, conn.cursor() as cur:
        for c in candidates:
            # Try exact name+stabbr+city match first
            cur.execute(
                """
                SELECT unitid FROM landing.institutions
                WHERE LOWER(name) = LOWER(%s) AND stabbr = %s
                LIMIT 1
                """,
                (c.name, c.state),
            )
            row = cur.fetchone()
            if row:
                c.unitid = row["unitid"]
                continue
            # Fuzzy: name LIKE + state. ILIKE handles 'University' variants.
            cur.execute(
                """
                SELECT unitid, name FROM landing.institutions
                WHERE stabbr = %s
                  AND (name ILIKE %s OR %s ILIKE name || '%%' OR name ILIKE %s || '%%')
                ORDER BY LENGTH(name) ASC
                LIMIT 1
                """,
                (c.state, c.name, c.name, c.name),
            )
            row = cur.fetchone()
            if row:
                c.unitid = row["unitid"]
    return candidates


def _write_dfr_document(unitid: int, url: str, year: int, stored) -> None:
    """Register the DFR HTML in landing.ir_documents so the page renderer can link to it."""
    sql = """
        INSERT INTO landing.ir_documents
          (unitid, doc_type, title, year, doc_url, mime_type, storage_path,
           source_url, raw_payload_id, parser_version, confidence)
        VALUES (%s, 'dfr_report', %s, %s, %s, 'text/html', %s, %s, NULL, '0.1.0', 1.0)
        ON CONFLICT (unitid, doc_type, year, doc_url) DO UPDATE SET
          storage_path = EXCLUDED.storage_path,
          fetched_at = NOW()
    """
    title = f"IPEDS Data Feedback Report {year}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (unitid, title, year, url, stored.relative_path, url))


def _write_peers(unitid: int, peers: list[_PeerCandidate]) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        # Clear any existing dfr_v1 rows for this institution before re-inserting
        cur.execute(
            "DELETE FROM landing.peer_groups "
            "WHERE unitid=%s AND algorithm=%s AND algorithm_version=%s",
            (unitid, ALGORITHM, ALGORITHM_VERSION),
        )
        for rank, p in enumerate(peers, start=1):
            cur.execute(
                """
                INSERT INTO landing.peer_groups
                  (unitid, peer_unitid, rank, similarity_score, algorithm, algorithm_version)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (unitid, algorithm, algorithm_version, peer_unitid) DO UPDATE
                SET rank = EXCLUDED.rank, similarity_score = EXCLUDED.similarity_score,
                    computed_at = NOW()
                """,
                (unitid, p.unitid, rank, 1.0, ALGORITHM, ALGORITHM_VERSION),
            )

"""NSF HERD survey connector.

HERD (Higher Education Research and Development Survey) publishes one large
CSV/Excel per year. There is no per-institution API; we cache the latest
HERD dataset locally and look up by IPEDS unitid (HERD's 'IPEDS Unitid' field).

For the pilot we look for the file at:
  assets/herd/herd_latest.csv

If the file is missing, the connector returns NO_HERD_FILE so the rest of the
pipeline keeps moving. A separate scripts/download_herd.py task will handle the
one-time download (HERD URLs change yearly).
"""
from __future__ import annotations

import csv
from pathlib import Path

from .base import BaseConnector, ConnectorResult, InstitutionContext

HERD_CSV_PATH = Path("assets/herd/herd_latest.csv")


class HERDConnector(BaseConnector):
    source_type = "research_herd"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        if not HERD_CSV_PATH.exists():
            return self._err(
                "NO_HERD_FILE",
                f"expected {HERD_CSV_PATH} — run scripts/download_herd.py first",
            )
        try:
            row = self._lookup(ctx.unitid)
        except Exception as e:  # noqa: BLE001
            return self._err("PARSE_ERROR", str(e))
        if not row:
            return self._err("NOT_FOUND", f"no HERD row for unitid={ctx.unitid}")
        return self._ok(
            canonical_url="https://ncses.nsf.gov/surveys/higher-education-research-development",
            data=row,
            confidence=1.0,
        )

    def _lookup(self, unitid: int) -> dict | None:
        with HERD_CSV_PATH.open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Different HERD vintages use different column names
                rid = row.get("IPEDS_UNITID") or row.get("ipeds_unitid") or row.get("unitid")
                if rid and str(rid).strip() == str(unitid):
                    return {k: v for k, v in row.items()}
        return None

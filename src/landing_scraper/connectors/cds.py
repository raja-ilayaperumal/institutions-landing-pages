"""Common Data Set link discovery."""
from __future__ import annotations

import re

from ..core import search
from .base import BaseConnector, ConnectorResult, InstitutionContext

YEAR_RE = re.compile(r"(20\d{2})")


class CDSConnector(BaseConnector):
    source_type = "cds"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        if not ctx.domain:
            return self._err("NO_DOMAIN", "institution has no domain")

        try:
            results = await search.search(
                '"common data set"', site=ctx.registrable_domain or ctx.domain, limit=10,
            )
        except Exception as e:  # noqa: BLE001
            return self._err("SEARCH_FAILED", str(e))

        cds_results: list[dict] = []
        for r in results:
            year_match = YEAR_RE.search(r.url) or YEAR_RE.search(r.title)
            cds_results.append({
                "url": r.url,
                "title": r.title,
                "year": int(year_match.group(1)) if year_match else None,
                "is_pdf": r.url.lower().endswith(".pdf"),
            })

        if not cds_results:
            return self._err("NOT_FOUND", "no CDS hits in site search")

        return self._ok(
            canonical_url=cds_results[0]["url"],
            data={"links": cds_results, "count": len(cds_results)},
            confidence=0.8,
        )

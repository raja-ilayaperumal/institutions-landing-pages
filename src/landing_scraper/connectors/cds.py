"""Common Data Set link discovery."""
from __future__ import annotations

import re

from ..core import search
from .base import BaseConnector, ConnectorResult, InstitutionContext

YEAR_RE = re.compile(r"(20\d{2})")

# A real Common Data Set link names itself: the page title says "common data
# set", or the URL carries a CDS marker (a `common-data-set` path, a `/cds`
# segment, or a `CDS_2024` style filename), or it's a CDS-referencing PDF. A
# bare IR / factbook / institutional-effectiveness index page is NOT a CDS even
# though a `"common data set" site:` search surfaces it for schools that publish
# none — accepting those mislabels the IR homepage as a Common Data Set. Gate on
# the self-naming signal; prefer no CDS over a wrong one.
CDS_URL_RE = re.compile(r"(common[-_ ]?data[-_ ]?set|/cds(?:[-_/.]|$)|cds[-_]\d|_cds[-_])", re.I)


def _looks_like_cds(url: str, title: str) -> bool:
    u, t = url.lower(), (title or "").lower()
    if "common data set" in t or "common-data-set" in u or "common_data_set" in u:
        return True
    if CDS_URL_RE.search(u):
        return True
    if u.endswith(".pdf") and ("cds" in u or "common data" in t):
        return True
    return False


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
            # Reject IR/factbook index pages that merely surface for the query;
            # only keep links that actually identify themselves as a CDS.
            if not _looks_like_cds(r.url, r.title):
                continue
            year_match = YEAR_RE.search(r.url) or YEAR_RE.search(r.title)
            cds_results.append({
                "url": r.url,
                "title": r.title,
                "year": int(year_match.group(1)) if year_match else None,
                "is_pdf": r.url.lower().endswith(".pdf"),
            })

        if not cds_results:
            return self._err("NOT_FOUND", "no real CDS link in site search (index pages filtered)")

        return self._ok(
            canonical_url=cds_results[0]["url"],
            data={"links": cds_results, "count": len(cds_results)},
            confidence=0.8,
        )

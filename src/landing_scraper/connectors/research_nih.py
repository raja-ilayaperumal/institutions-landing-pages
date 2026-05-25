"""NIH RePORTER awards connector.

API: POST https://api.reporter.nih.gov/v2/projects/search
Free, no key required.

Filters by org_names (case-insensitive contains). We collect the most recent
fiscal year for which any awards exist (or last 3 years if you change YEAR_WINDOW).
"""
from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from .base import BaseConnector, ConnectorResult, InstitutionContext

YEAR_WINDOW = 3  # last N fiscal years


class NIHReporterConnector(BaseConnector):
    source_type = "research_nih"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        current_year = date.today().year
        years = list(range(current_year - YEAR_WINDOW, current_year + 1))

        body = {
            "criteria": {
                "fiscal_years": years,
                "org_names": [ctx.name],
            },
            "include_fields": [
                "ProjectNum", "ProjectTitle", "Organization", "FullStudySection",
                "PrincipalInvestigators", "AgencyIcAdmin", "ProjectStartDate",
                "ProjectEndDate", "FiscalYear", "AwardAmount", "AbstractText",
            ],
            "limit": 100,
            "offset": 0,
            "sort_field": "fiscal_year",
            "sort_order": "desc",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.reporter.nih.gov/v2/projects/search", json=body,
                    headers={"Accept": "application/json"},
                )
            if resp.status_code != 200:
                return self._err("HTTP_ERROR", f"HTTP {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            return self._err("HTTP_ERROR", str(e))

        results = resp.json().get("results", [])
        total = resp.json().get("meta", {}).get("total", 0)

        awards: list[dict[str, Any]] = []
        for p in results:
            pis = p.get("principal_investigators", []) or []
            pi = pis[0] if pis else {}
            awards.append({
                "external_id": p.get("project_num"),
                "title": p.get("project_title"),
                "pi_name": pi.get("full_name"),
                "agency": p.get("agency_ic_admin", {}).get("name") if isinstance(p.get("agency_ic_admin"), dict) else None,
                "amount_total_usd": p.get("award_amount"),
                "fiscal_year": p.get("fiscal_year"),
                "start_date": p.get("project_start_date"),
                "end_date": p.get("project_end_date"),
                "abstract": (p.get("abstract_text") or "")[:600],
                "org_name_reported": (p.get("organization") or {}).get("org_name"),
            })

        total_amount = sum(a.get("amount_total_usd") or 0 for a in awards)
        return self._ok(
            canonical_url="https://reporter.nih.gov/",
            data={
                "query_org_name": ctx.name,
                "year_window": years,
                "total_awards_found": total,
                "awards_returned": len(awards),
                "total_amount_usd": total_amount,
                "awards": awards[:20],  # cap response size
            },
            confidence=0.9 if awards else 0.4,
        )

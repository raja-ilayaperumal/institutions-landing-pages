"""USAspending.gov connector — all federal awards (grants + contracts) for an institution.

API: POST https://api.usaspending.gov/api/v2/search/spending_by_award/
Free, no key. We prefer UEI (exact) over recipient_name (fuzzy) when available.

Returns up to N awards from the last 3 fiscal years.
"""
from __future__ import annotations

from datetime import date

import httpx

from .base import BaseConnector, ConnectorResult, InstitutionContext

API = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
# USAspending requires a SINGLE award-type group per request. We use grants
# (universities mostly receive grants/cooperative agreements). Contracts can
# be a separate connector if needed.
AWARD_TYPE_CODES = ["02", "03", "04", "05"]  # grants + cooperative agreements


class USASpendingConnector(BaseConnector):
    source_type = "research_usaspending"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        today = date.today()
        start = f"{today.year - 3}-01-01"
        end = today.isoformat()

        filters: dict = {
            "time_period": [{"start_date": start, "end_date": end}],
            "award_type_codes": AWARD_TYPE_CODES,
        }
        if ctx.ueis:
            filters["recipient_search_text"] = [ctx.ueis]
        else:
            filters["recipient_search_text"] = [ctx.name]

        body = {
            "filters": filters,
            "fields": [
                "Award ID", "Recipient Name", "Recipient UEI",
                "Start Date", "End Date", "Award Amount",
                "Awarding Agency", "Awarding Sub Agency", "Award Type",
                "Description",
            ],
            "page": 1,
            "limit": 50,
            "sort": "Award Amount",
            "order": "desc",
        }
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(API, json=body)
            if not resp.is_success:
                return self._err("HTTP_ERROR", f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:  # noqa: BLE001
            return self._err("HTTP_ERROR", str(e))

        results = resp.json().get("results", [])
        awards: list[dict] = []
        for r in results:
            awards.append({
                "external_id": r.get("Award ID"),
                "title": r.get("Description"),
                "amount_total_usd": _to_int(r.get("Award Amount")),
                "start_date": r.get("Start Date"),
                "end_date": r.get("End Date"),
                "agency": r.get("Awarding Agency"),
                "sub_agency": r.get("Awarding Sub Agency"),
                "recipient_name_reported": r.get("Recipient Name"),
                "recipient_uei": r.get("Recipient UEI"),
                "award_type": r.get("Award Type"),
            })

        total = sum(a.get("amount_total_usd") or 0 for a in awards)
        page_meta = resp.json().get("page_metadata", {})
        return self._ok(
            canonical_url="https://www.usaspending.gov/",
            data={
                "match_key": "uei" if ctx.ueis else "name",
                "match_value": ctx.ueis or ctx.name,
                "awards_returned": len(awards),
                "has_next_page": page_meta.get("hasNext", False),
                "total_amount_usd": total,
                "awards": awards[:20],
            },
            confidence=0.95 if ctx.ueis and awards else (0.7 if awards else 0.3),
        )


def _to_int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None

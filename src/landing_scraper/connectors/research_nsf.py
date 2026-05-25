"""NSF Awards Search connector.

API: https://www.research.gov/common/webapi/awardapisearch-v1.htm
GET https://api.nsf.gov/services/v1/awards.json?awardeeName=...

Free, no key. Returns awards sorted by date.
"""
from __future__ import annotations

from datetime import date

import httpx

from .base import BaseConnector, ConnectorResult, InstitutionContext


class NSFAwardsConnector(BaseConnector):
    source_type = "research_nsf"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        # NSF's awardeeName param is fuzzy/broken — narrow by state and
        # post-filter against the institution name.
        params = {
            "awardeeName": ctx.name,
            "printFields": (
                "id,title,piFirstName,piLastName,awardeeName,fundsObligatedAmt,"
                "date,startDate,expDate,abstractText,fundAgencyCode,awardAgencyCode"
            ),
            "offset": "1",
            "rpp": "100",
            "dateStart": f"01/01/{date.today().year - 3}",
        }
        if ctx.state:
            params["awardeeStateCode"] = ctx.state
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    "https://api.nsf.gov/services/v1/awards.json", params=params
                )
            if not resp.is_success:
                return self._err("HTTP_ERROR", f"HTTP {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            return self._err("HTTP_ERROR", str(e))

        body = resp.json()
        awards_raw = body.get("response", {}).get("award", [])
        awards: list[dict] = []

        # NSF's awardeeName is fuzzy. Build a strict matcher: normalize both sides
        # by lowercasing, stripping punctuation/whitespace, and dropping a "the "
        # prefix; require either exact match OR one being a substring of the other
        # AND the shorter has > 60% of the longer's length (rejects "MIT" matching
        # "Trustees of Boston University").
        import re as _re

        def _norm(s: str) -> str:
            s = (s or "").lower().strip()
            if s.startswith("the "):
                s = s[4:]
            return _re.sub(r"[^a-z0-9]+", " ", s).strip()

        target = _norm(ctx.name)
        for a in awards_raw:
            reported = _norm(a.get("awardeeName") or "")
            if not reported:
                continue
            if reported == target:
                pass  # accept
            elif target in reported or reported in target:
                shorter, longer = sorted([reported, target], key=len)
                if not longer or len(shorter) / len(longer) < 0.6:
                    continue
            else:
                continue
            pi = f"{(a.get('piFirstName') or '').strip()} {(a.get('piLastName') or '').strip()}".strip()
            awards.append({
                "external_id": a.get("id"),
                "title": a.get("title"),
                "pi_name": pi or None,
                "amount_total_usd": _to_int(a.get("fundsObligatedAmt")),
                "start_date": a.get("startDate"),
                "end_date": a.get("expDate"),
                "abstract": (a.get("abstractText") or "")[:600],
                "agency": "NSF",
                "awardee_name_reported": a.get("awardeeName"),
            })

        total = sum(x.get("amount_total_usd") or 0 for x in awards)
        raw_total = len(awards_raw)
        return self._ok(
            canonical_url="https://www.nsf.gov/awardsearch/",
            data={
                "query_awardee_name": ctx.name,
                "raw_results_before_filter": raw_total,
                "awards_returned": len(awards),
                "total_amount_usd": total,
                "awards": awards[:20],
            },
            confidence=0.9 if awards else 0.4,
        )


def _to_int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None

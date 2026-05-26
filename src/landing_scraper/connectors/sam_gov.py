"""SAM.gov connector — Federal grant opportunities relevant to this institution.

Approach:
  - Search SAM.gov (via Tavily site:sam.gov) for open federal grant
    opportunities that higher-ed institutions are eligible for and that
    name the institution OR list higher-ed-relevant NAICS (611310).
  - Also pull AWARDS to this institution (already have via USASpending,
    but SAM.gov has notice posts).
  - LLM filter the search hits to keep only legit opportunities.
  - Persist to landing.grants with source_system='sam_gov'.

No SAM.gov API key required — Tavily handles the search; sam.gov opportunity
pages load fine in crawl4ai when we need details.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog

from ..core import llm, search
from ..db.engine import get_conn
from .base import BaseConnector, ConnectorResult, InstitutionContext

log = structlog.get_logger(__name__)


NOTICE_ID_RE = re.compile(r"/opp/([0-9a-fA-F]{32})/")
SAM_API_TMPL = "https://sam.gov/api/prod/opps/v2/opportunities/{notice_id}"
SAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (clema-landing scraper)",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://sam.gov",
    "Referer": "https://sam.gov/",
}


def _extract_notice_id(url: str) -> str | None:
    m = NOTICE_ID_RE.search(url or "")
    return m.group(1) if m else None


def _parse_iso_date(value):
    """Tolerant ISO-8601 → date. Returns None for missing/malformed values."""
    if not value:
        return None
    s = str(value)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _fetch_sam_dates(client: httpx.Client, notice_id: str) -> dict:
    """Hit the unauthenticated SAM.gov opportunity API for posted/inactive
    /response dates. Always returns a dict (with Nones on failure) so the
    caller can spread it into the row without conditional logic."""
    try:
        r = client.get(SAM_API_TMPL.format(notice_id=notice_id), timeout=15.0)
        if r.status_code != 200:
            return {"posted_at": None, "inactive_at": None, "response_deadline_at": None}
        d = r.json()
    except (httpx.HTTPError, ValueError):
        return {"posted_at": None, "inactive_at": None, "response_deadline_at": None}
    data2 = d.get("data2") or {}
    archive = data2.get("archive") or {}
    deadlines = (data2.get("solicitation") or {}).get("deadlines") or {}
    return {
        "posted_at":            _parse_iso_date(d.get("postedDate")),
        "inactive_at":          _parse_iso_date(archive.get("date")),
        "response_deadline_at": _parse_iso_date(deadlines.get("response")),
    }

QUERIES = (
    'site:sam.gov "{name}" grant opportunity',
    'site:sam.gov "{name}" funding',
    'site:sam.gov higher education grant institutional research',
    'site:sam.gov assistance listing higher education research',
)

FILTER_SYSTEM = """\
You filter SAM.gov search results to identify legitimate Federal grant
opportunities or grant awards that are RELEVANT to higher-education
institutions (especially the named one).

Reply with JSON: {"items": [{
  "index": int (1..N),
  "title": str,
  "url": str,
  "kind": "opportunity" | "award",
  "agency": str | null,
  "amount_usd": int | null,
  "deadline": str | null,
  "is_relevant_to_higher_ed": bool,
  "is_named_institution": bool,
  "reason": str
}]}

Keep ONLY items where is_relevant_to_higher_ed=true. Prefer items where
is_named_institution=true. Skip generic search-result landing pages,
SAM.gov FAQ, login, search forms."""


def _looks_like_opportunity_url(url: str) -> bool:
    """SAM.gov opportunity pages have /opp/ID or /api/.../opportunities/... patterns."""
    u = url.lower()
    if "sam.gov" not in u:
        return False
    bad_paths = ("/help", "/api/help", "/login", "/search?", "/about")
    if any(b in u for b in bad_paths):
        return False
    return any(p in u for p in ("/opp/", "/awards/", "/awd/", "/wage-determinations"))


class SAMGovGrantsConnector(BaseConnector):
    source_type = "grants"  # writes to landing.grants

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        all_hits: dict[str, dict] = {}
        cost = 0.0

        for q in QUERIES:
            rendered = q.format(name=ctx.name)
            try:
                results = await search.search(rendered, limit=10)
            except Exception as e:  # noqa: BLE001
                log.warning("samgov.search_failed", q=q, error=str(e))
                continue
            for r in results:
                if not _looks_like_opportunity_url(r.url):
                    continue
                if r.url in all_hits:
                    continue
                all_hits[r.url] = {"title": r.title, "snippet": r.snippet}

        if not all_hits:
            return self._err("NO_SAMGOV_RESULTS",
                             f"No SAM.gov opportunity URLs for {ctx.name}")

        top = list(all_hits.items())[:15]
        enumerated = "\n".join(
            f"{i+1}. URL: {url}\n   Title: {meta['title']}\n   Snippet: {meta['snippet'][:200]}"
            for i, (url, meta) in enumerate(top)
        )
        filtered = await llm.call(
            system=FILTER_SYSTEM,
            user=f"Institution: {ctx.name}\n\nSearch results:\n{enumerated}",
            tier="judge", max_tokens=1500, expect_json=True,
        )
        cost += filtered.cost_usd

        accepted: list[dict] = []
        if isinstance(filtered.data, dict):
            for it in filtered.data.get("items") or []:
                if not it.get("is_relevant_to_higher_ed"):
                    continue
                idx = int(it.get("index", 0)) - 1
                if idx < 0 or idx >= len(top):
                    continue
                url, meta = top[idx]
                accepted.append({
                    "title": it.get("title") or meta["title"],
                    "url": url, "kind": it.get("kind", "opportunity"),
                    "agency": it.get("agency"),
                    "amount_usd": it.get("amount_usd"),
                    "deadline": it.get("deadline"),
                    "is_named_institution": it.get("is_named_institution", False),
                })

        # Persist to landing.grants
        if accepted:
            _persist(ctx.unitid, accepted)

        return self._ok(
            canonical_url="https://sam.gov/",
            data={
                "institution": ctx.name,
                "total_search_hits": len(all_hits),
                "accepted": len(accepted),
                "grants": accepted,
            },
            confidence=0.8 if accepted else 0.3, cost_usd=cost,
        )


def _persist(unitid: int, items: list[dict]) -> None:
    # Fetch dates from the SAM.gov v2 API for every URL up-front so we never
    # write a row without the recency signal. One HTTP client, reused.
    sql = """
        INSERT INTO landing.grants
          (unitid, source_system, external_id, agency, title, amount_usd,
           award_date, end_date, status, source_url,
           posted_at, inactive_at, response_deadline_at,
           parser_version, quality_score, needs_review)
        VALUES (%s, 'sam_gov', %s, %s, %s, %s, NULL, NULL, %s, %s,
                %s, %s, %s,
                '0.1.0', %s, FALSE)
        ON CONFLICT (source_system, external_id) DO UPDATE SET
          title = EXCLUDED.title,
          amount_usd = EXCLUDED.amount_usd,
          status = EXCLUDED.status,
          posted_at = COALESCE(EXCLUDED.posted_at, landing.grants.posted_at),
          inactive_at = COALESCE(EXCLUDED.inactive_at, landing.grants.inactive_at),
          response_deadline_at = COALESCE(EXCLUDED.response_deadline_at, landing.grants.response_deadline_at),
          fetched_at = NOW()
    """
    with httpx.Client(headers=SAM_HEADERS) as client, \
            get_conn(autocommit=True) as conn, conn.cursor() as cur:
        for it in items:
            ext_id = it["url"]  # SAM.gov URL is unique per opportunity
            notice_id = _extract_notice_id(it["url"])
            dates = _fetch_sam_dates(client, notice_id) if notice_id else {
                "posted_at": None, "inactive_at": None, "response_deadline_at": None,
            }
            cur.execute(sql, (
                unitid, ext_id, it.get("agency"),
                (it.get("title") or "")[:500],
                it.get("amount_usd"),
                it.get("kind"),
                it["url"],
                dates["posted_at"], dates["inactive_at"], dates["response_deadline_at"],
                0.85 if it.get("is_named_institution") else 0.55,
            ))

"""HigherEdJobs connector — IR job postings at this institution.

Strategy (the right one):
  • Run multi-query search across Tavily / Google / DDG, restricted to
    `site:higheredjobs.com`, with the institution name + IR keywords.
  • EACH search hit IS a posting — the URL is the JobCode page on
    higheredjobs.com, the title is the role.
  • LLM-filter to ensure the result is really an IR role at THIS institution
    (HigherEdJobs sometimes returns adjacent matches).
  • Optionally fetch a small snippet of each top result to capture
    posted_at / location.

This avoids the dead-end of JS-loaded vendor HR portals (Workday/PeopleAdmin)
that institutions use for their own careers pages.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from ..core import llm, search
from ..db.writer import ProvenanceWriter
from .base import BaseConnector, ConnectorResult, InstitutionContext

log = structlog.get_logger(__name__)

# Per-institution multi-query angles — using both full and short name variants
QUERIES = (
    'site:higheredjobs.com "{name}" institutional research',
    'site:higheredjobs.com "{name}" institutional effectiveness',
    'site:higheredjobs.com "{name}" assessment',
    'site:higheredjobs.com "{name}" "data analyst"',
    'site:higheredjobs.com "{short}" institutional research',
    'site:higheredjobs.com "{short}" institutional effectiveness',
    'site:higheredjobs.com {name} institutional research job',
)


def _short_name(full: str) -> str:
    """Pick a short institutional alias: 'Brigham Young University-Idaho' → 'BYU-Idaho'.

    Drops 'The', 'University of', 'College of'; tries common abbreviation
    using initials.
    """
    if not full:
        return full
    s = full.strip()
    # Common patterns: "University of X" → X
    for prefix in ("The ", "University of ", "College of "):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # If it has a dash suffix like '-Idaho', preserve as suffix
    if "-" in s and any(s.endswith("-" + loc) for loc in
                       ("Idaho", "Hawaii", "Provo", "Austin", "Boulder")):
        # Take initials of main words + suffix
        base, suffix = s.rsplit("-", 1)
        initials = "".join(w[0] for w in base.split() if w and w[0].isupper())
        if len(initials) >= 2:
            return f"{initials}-{suffix}"
    # Fallback initials when 3+ words
    parts = [w for w in s.split() if w and w[0].isupper()]
    if len(parts) >= 3:
        initials = "".join(w[0] for w in parts)
        return initials
    return s

# Filter prompt — given the top 10 search results, return which ones are
# real IR roles AT the named institution.
FILTER_SYSTEM = """\
You filter HigherEdJobs search results to identify ONLY job postings that are:
  (a) currently open positions (not generic info pages, not closed listings)
  (b) at the named institution (not at peer institutions)
  (c) in Institutional Research, Effectiveness, Analytics, Assessment,
      Decision Support, or Institutional Planning

Reply with JSON: {"postings": [{"index": int (1..N), "title": str,
                                "url": str, "location": str | null,
                                "is_at_institution": bool,
                                "is_ir_role": bool,
                                "reason": str}]}

Include ONLY entries where BOTH is_at_institution AND is_ir_role are true.
If unsure whether the posting is really at the named institution, set
is_at_institution=false and exclude it. Empty list is acceptable."""


@dataclass
class _Posting:
    title: str
    url: str
    location: str | None = None
    posted_at: str | None = None
    snippet: str = ""


def _job_url_looks_real(url: str) -> bool:
    """HigherEdJobs job pages are /details.cfm?JobCode=NNN; sometimes /admin/."""
    if "higheredjobs.com" not in url:
        return False
    if "details.cfm" in url:
        return True
    # Other pages like /faq, /about, /search are not postings
    return False


class HigherEdJobsIRConnector(BaseConnector):
    """Pulls IR job postings for an institution from HigherEdJobs."""

    source_type = "ir_jobs"  # writes to same landing.job_postings (category='ir')

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        # 1. Multi-query search
        all_hits: dict[str, dict] = {}  # url → {title, snippet}
        cost = 0.0
        short = _short_name(ctx.name)
        for q in QUERIES:
            rendered = q.format(name=ctx.name, short=short)
            try:
                results = await search.search(rendered, limit=10)
            except Exception as e:  # noqa: BLE001
                log.warning("higheredjobs.search_failed", q=q, error=str(e))
                continue
            for r in results:
                if not _job_url_looks_real(r.url):
                    continue
                if r.url in all_hits:
                    continue
                all_hits[r.url] = {"title": r.title, "snippet": r.snippet}

        if not all_hits:
            return self._err("NO_HEJ_RESULTS",
                             f"No HigherEdJobs results for {ctx.name}")

        # 2. LLM filter — ensure each hit is really IR at this institution
        top = list(all_hits.items())[:15]
        enumerated = "\n".join(
            f"{i+1}. URL: {url}\n   Title: {meta['title']}\n   Snippet: {meta['snippet'][:200]}"
            for i, (url, meta) in enumerate(top)
        )
        filter_result = await llm.call(
            system=FILTER_SYSTEM,
            user=f"Institution: {ctx.name}\n\nResults:\n{enumerated}",
            tier="judge",
            max_tokens=1500, expect_json=True,
        )
        cost += filter_result.cost_usd
        accepted: list[_Posting] = []
        if isinstance(filter_result.data, dict):
            for p in filter_result.data.get("postings") or []:
                if not p.get("is_at_institution") or not p.get("is_ir_role"):
                    continue
                idx = int(p.get("index", 0)) - 1
                if idx < 0 or idx >= len(top):
                    continue
                url, meta = top[idx]
                accepted.append(_Posting(
                    title=p.get("title") or meta["title"],
                    url=url,
                    location=p.get("location"),
                    snippet=meta["snippet"][:300],
                ))

        if not accepted:
            return self._ok(
                canonical_url="https://www.higheredjobs.com/",
                data={"institution": ctx.name, "total_search_hits": len(all_hits),
                      "accepted_postings": 0, "postings": []},
                confidence=0.5, cost_usd=cost,
            )

        return self._ok(
            canonical_url="https://www.higheredjobs.com/",
            data={
                "institution": ctx.name,
                "total_search_hits": len(all_hits),
                "accepted_postings": len(accepted),
                "postings": [
                    {"title": p.title, "url": p.url, "location": p.location,
                     "snippet": p.snippet}
                    for p in accepted
                ],
            },
            confidence=0.85, cost_usd=cost,
        )


async def persist_postings(unitid: int, result: ConnectorResult,
                            writer: ProvenanceWriter) -> int:
    """Write the accepted postings to landing.job_postings as IR jobs."""
    if not result.success or not result.data:
        return 0
    postings = result.data.get("postings") or []
    if not postings:
        return 0
    return writer.write_job_postings(
        unitid=unitid,
        jobs=[{
            "title": p["title"],
            "category": "ir",
            "url": p["url"],
        } for p in postings if p.get("title") and p.get("url")],
        source_board="higheredjobs",
        source_url="https://www.higheredjobs.com/",
        raw_payload_id=None,
    )

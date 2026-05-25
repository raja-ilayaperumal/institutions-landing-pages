"""IR landing-page discovery.

Strategy:
  1. Use site:search for "institutional research" within institution domain
  2. If no hits, do a crawl4ai deep crawl from root scored on IR keywords
  3. Optionally validate top candidate with LLM judge
"""
from __future__ import annotations

from ..core import crawler, llm, search
from .base import BaseConnector, ConnectorResult, InstitutionContext

IR_KEYWORDS = [
    "institutional research",
    "office of institutional research",
    "ir office",
    "ir analytics",
    "institutional effectiveness",
    "institutional analytics",
    "institutional planning",
]

JUDGE_SYSTEM = (
    "You judge whether a given URL+title+snippet is the homepage of an "
    "Office of Institutional Research (or equivalent institutional research / "
    "analytics / effectiveness office) for a US college or university. "
    "Reply ONLY with JSON: "
    '{"is_ir_page": true|false, "confidence": 0.0-1.0, "reason": "..."}'
)


class IRPageConnector(BaseConnector):
    source_type = "ir_page"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        if not ctx.domain:
            return self._err("NO_DOMAIN", "institution has no domain")

        # Try site search
        candidates: list[dict] = []
        try:
            results = await search.search(
                "institutional research office",
                site=ctx.registrable_domain or ctx.domain,
                limit=5,
            )
            candidates.extend(
                {"url": r.url, "title": r.title, "snippet": r.snippet, "method": "site_search"}
                for r in results
            )
        except Exception as e:  # noqa: BLE001
            return self._err("SEARCH_FAILED", str(e))

        # If thin, deep crawl
        if len(candidates) < 2 and ctx.canonical_root_url:
            try:
                cc = await crawler.deep_crawl(
                    ctx.canonical_root_url,
                    keywords=IR_KEYWORDS,
                    max_pages=15,
                    max_depth=2,
                    allowed_domain=ctx.domain,
                )
                candidates.extend(
                    {"url": c.url, "title": c.title, "snippet": "", "method": "deep_crawl", "score": c.score}
                    for c in cc[:5]
                )
            except Exception as e:  # noqa: BLE001
                # not fatal — keep what site search gave us
                pass

        if not candidates:
            return self._err("NO_CANDIDATES", "no candidate URLs found")

        # If LLM available, judge top candidate
        top = candidates[0]
        confidence = 0.5
        cost = 0.0
        if llm.settings.has_anthropic:
            user_msg = (
                f"Institution: {ctx.name}\n"
                f"URL: {top['url']}\n"
                f"Title: {top.get('title','')}\n"
                f"Snippet: {top.get('snippet','')}\n"
            )
            r = await llm.call(
                system=JUDGE_SYSTEM, user=user_msg,
                model=llm.MODEL_JUDGE, max_tokens=200, expect_json=True,
            )
            cost = r.cost_usd
            if isinstance(r.data, dict):
                if not r.data.get("is_ir_page"):
                    return self._err("LLM_REJECTED", str(r.data.get("reason", "")))
                confidence = float(r.data.get("confidence", 0.5))

        return self._ok(
            canonical_url=top["url"],
            data={"candidates": candidates, "chosen": top["url"]},
            confidence=confidence,
            cost_usd=cost,
        )

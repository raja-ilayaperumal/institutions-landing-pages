"""Strategic plan discovery: site:search + PDF download + (optional) LLM summary."""
from __future__ import annotations

import re

from ..core import crawler, html as html_utils, llm, pdf, search
from .base import (
    BaseConnector, ConnectorResult, InstitutionContext, same_registrable_domain,
)

YEAR_RE = re.compile(r"(20\d{2})")

SUMMARY_SYSTEM = (
    "You summarize an institution's strategic plan in 3 sentences. Highlight "
    "concrete strategic priorities (e.g. 'enrollment growth to X', 'launch Y "
    "research center', 'achieve Z carbon target'). Avoid generic mission-statement "
    "language. Reply with JSON: {\"summary\": str, \"priorities\": [str]}"
)


class StrategicPlanConnector(BaseConnector):
    source_type = "strategic_plan"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        if not ctx.domain:
            return self._err("NO_DOMAIN", "institution has no domain")

        try:
            results = await search.search('"strategic plan"', site=ctx.registrable_domain or ctx.domain, limit=8)
        except Exception as e:  # noqa: BLE001
            return self._err("SEARCH_FAILED", str(e))

        if not results:
            return self._err("NOT_FOUND", "no strategic plan hits")

        # Prefer PDFs; otherwise scrape one landing page for PDF links
        pdf_candidates = [r.url for r in results if r.url.lower().endswith(".pdf")]
        landing_candidates = [r.url for r in results if not r.url.lower().endswith(".pdf")]
        for lp in landing_candidates[:2]:
            f = await crawler.fetch(lp)
            if f.success:
                pdf_candidates.extend(
                    l for l in html_utils.find_pdf_links(f.html, lp)
                    if "strategic" in l.lower() or "plan" in l.lower()
                )
        # Keep only on-domain PDFs — `site:` is a soft hint, so a comparison
        # page can surface another school's strategic plan or a consultant PDF.
        pdf_candidates = [u for u in dict.fromkeys(pdf_candidates)
                          if same_registrable_domain(u, ctx)][:3]

        # Same gate on the bare-search fallback: never summarize an off-domain
        # page as this institution's strategic plan.
        on_domain_results = [r.url for r in results
                             if same_registrable_domain(r.url, ctx)]
        chosen_url = (pdf_candidates[0] if pdf_candidates
                      else (on_domain_results[0] if on_domain_results else None))
        if not chosen_url:
            return self._err("NOT_FOUND", "no on-domain strategic plan found")
        m = YEAR_RE.search(chosen_url)
        year = int(m.group(1)) if m else None

        text = ""
        page_count = None
        sha = None
        storage_path = None
        if chosen_url.lower().endswith(".pdf"):
            pr = await pdf.download_and_extract(chosen_url)
            if pr.success:
                text = pr.text
                page_count = pr.page_count
                sha = pr.sha256
                storage_path = str(pr.storage_path)
        else:
            f = await crawler.fetch(chosen_url)
            if f.success:
                text = f.markdown or html_utils.visible_text(f.html)

        cost = 0.0
        summary: str | None = None
        priorities: list[str] = []
        if text and llm.settings.has_anthropic:
            r = await llm.call(
                system=SUMMARY_SYSTEM,
                user=f"Institution: {ctx.name}\n\nPLAN TEXT (first 40k chars):\n{text[:40000]}",
                model=llm.MODEL_JUDGE,
                max_tokens=600,
                expect_json=True,
            )
            cost = r.cost_usd
            if isinstance(r.data, dict):
                summary = r.data.get("summary")
                priorities = r.data.get("priorities", []) or []

        return self._ok(
            canonical_url=chosen_url,
            data={
                "url": chosen_url,
                "year": year,
                "page_count": page_count,
                "sha256": sha,
                "storage_path": storage_path,
                "summary": summary,
                "priorities": priorities,
                "text_preview": text[:300],
            },
            confidence=0.7,
            cost_usd=cost,
        )

"""Factbook discovery via site: search + PDF link detection."""
from __future__ import annotations

import re

from ..core import crawler, html as html_utils, pdf, search
from .base import BaseConnector, ConnectorResult, InstitutionContext

YEAR_RE = re.compile(r"(20\d{2})")


class FactbookConnector(BaseConnector):
    source_type = "factbook"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        if not ctx.domain:
            return self._err("NO_DOMAIN", "institution has no domain")

        # Site search
        try:
            results = await search.search("factbook OR fact book", site=ctx.registrable_domain or ctx.domain, limit=10)
        except Exception as e:  # noqa: BLE001
            return self._err("SEARCH_FAILED", str(e))

        # Collect direct PDF hits + landing pages with PDF links
        pdf_urls: list[str] = []
        landing_pages: list[str] = []
        for r in results:
            if r.url.lower().endswith(".pdf"):
                pdf_urls.append(r.url)
            else:
                landing_pages.append(r.url)

        # Fetch top 2 landing pages to scrape PDF links
        for lp in landing_pages[:2]:
            f = await crawler.fetch(lp)
            if f.success:
                for link in html_utils.find_pdf_links(f.html, lp):
                    if "factbook" in link.lower() or "fact" in link.lower():
                        pdf_urls.append(link)

        pdf_urls = list(dict.fromkeys(pdf_urls))[:5]
        if not pdf_urls:
            return self._err(
                "NO_PDF_FOUND",
                f"no factbook PDF found; landing pages: {landing_pages[:3]}",
            )

        # Download + extract text from the first PDF
        first = pdf_urls[0]
        result = await pdf.download_and_extract(first)
        if not result.success:
            return self._err("PDF_FAILED", result.error or "")

        # Year heuristic from URL
        m = YEAR_RE.search(first)
        year = int(m.group(1)) if m else None

        return self._ok(
            canonical_url=first,
            data={
                "pdf_url": first,
                "year": year,
                "page_count": result.page_count,
                "sha256": result.sha256,
                "storage_path": str(result.storage_path),
                "all_candidate_pdfs": pdf_urls,
                "text_preview": (result.text or "")[:500],
                "text_length_chars": len(result.text or ""),
            },
            confidence=0.7,
        )

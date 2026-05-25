"""Content extraction — fetch the canonical URL with full fidelity, persist raw,
then optionally run LLM structured extraction.

Returns a typed ExtractionResult ready for ProvenanceWriter.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog

from ..core import crawler, llm, pdf
from ..core.html import visible_text
from .targets import TargetSpec

log = structlog.get_logger(__name__)


@dataclass
class ExtractionResult:
    success: bool
    url: str
    final_url: str
    content_type: str | None = None
    http_status: int | None = None
    body_html: str | None = None
    body_text: str | None = None
    body_blob_path: str | None = None
    body_sha256: str | None = None
    fetcher: str = ""
    structured: dict[str, Any] | None = None     # LLM-extracted fields (when applicable)
    extractor_cost_usd: float = 0.0
    error_class: str | None = None
    error_message: str | None = None


async def extract(
    url: str,
    target: TargetSpec,
    institution_name: str,
    *,
    run_id: UUID | str,
) -> ExtractionResult:
    """Fetch + persist raw + optional LLM extraction."""
    # 1. Determine if URL is a PDF
    is_pdf = url.lower().split("?")[0].endswith(".pdf")

    # 2. Fetch (crawl4ai for HTML, httpx for PDF)
    if is_pdf:
        pr = await pdf.download_and_extract(url)
        if not pr.success:
            return ExtractionResult(
                success=False, url=url, final_url=url,
                error_class="PDF_FETCH_FAILED", error_message=pr.error or "",
            )
        body_text = pr.text
        sha = pr.sha256
        storage_path = str(pr.storage_path) if pr.storage_path else None
        result = ExtractionResult(
            success=True, url=url, final_url=url,
            content_type="application/pdf",
            http_status=200,
            body_html=None, body_text=body_text,
            body_blob_path=storage_path,
            body_sha256=sha,
            fetcher="httpx",
        )
    else:
        fetched = await crawler.fetch(url, timeout=45.0)
        if not fetched.success:
            return ExtractionResult(
                success=False, url=url, final_url=fetched.url or url,
                http_status=fetched.status_code,
                error_class="HTTP_FETCH_FAILED", error_message=fetched.error or "",
                fetcher=fetched.fetcher,
            )
        body_html = fetched.html or ""
        body_text = fetched.markdown or visible_text(body_html)
        sha = hashlib.sha256((body_html or body_text).encode("utf-8")).hexdigest()
        result = ExtractionResult(
            success=True, url=url, final_url=fetched.url or url,
            content_type="text/html",
            http_status=fetched.status_code,
            body_html=body_html, body_text=body_text,
            body_sha256=sha, fetcher=fetched.fetcher,
        )

    # 3. LLM structured extraction (when configured for this target + key present)
    if target.extractor_system and llm.settings.has_llm:
        text = result.body_text or ""
        budget = target.extractor_text_budget
        if len(text) > budget:
            text = text[:budget]
        user = (
            f"Institution: {institution_name}\n"
            f"Page URL: {result.final_url}\n\n"
            f"PAGE TEXT:\n{text}"
        )
        r = await llm.call(
            system=target.extractor_system,
            user=user,
            tier="extract", max_tokens=2000, expect_json=True,
        )
        result.extractor_cost_usd = r.cost_usd
        if isinstance(r.data, dict):
            result.structured = r.data
        else:
            log.warning("extractor.no_json", target=target.name, text=r.text[:200])

    return result

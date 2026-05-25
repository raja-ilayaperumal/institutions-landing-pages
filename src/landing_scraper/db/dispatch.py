"""Dispatch table: map a ConnectorResult → ProvenanceWriter calls.

Keeping this separate from the connectors keeps connectors pure (they return
typed data) and centralizes the "how does this go into the DB" decisions.
"""
from __future__ import annotations

import structlog

from ..connectors.base import ConnectorResult
from .writer import ProvenanceWriter

log = structlog.get_logger(__name__)


def persist(writer: ProvenanceWriter, *, unitid: int, result: ConnectorResult) -> int:
    """Persist one ConnectorResult to its target landing table(s).
    Returns total rows written across all tables touched by this result."""
    if not result.success or result.data is None:
        return 0

    st = result.source_type
    data = result.data
    src_url = result.canonical_url
    conf = result.confidence
    raw_id = None  # raw_payload not wired through connectors yet (Phase 2 of writer integration)

    # Always log the canonical URL choice (when we have one) so we can audit later
    if src_url and conf is not None:
        try:
            writer.record_canonical_url(
                unitid=unitid, source_type=st, url=src_url,
                confidence=conf, llm_reasoning=None,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("dispatch.canonical_failed", source=st, error=str(e))

    try:
        if st == "brandfetch":
            return writer.write_brand_assets(unitid=unitid, data=data, raw_payload_id=raw_id, source_url=src_url)

        if st == "wikipedia":
            return writer.write_wiki(unitid=unitid, data=data, raw_payload_id=raw_id, source_url=src_url)

        if st == "ir_page":
            return writer.write_ir_page(
                unitid=unitid, data=data,
                canonical_url=src_url, confidence=conf, raw_payload_id=raw_id,
            )

        if st == "ir_team":
            return writer.write_ir_contacts(
                unitid=unitid, members=data.get("members", []),
                source_url=src_url, raw_payload_id=raw_id, confidence=conf,
            )

        if st == "factbook":
            return writer.write_ir_document(
                unitid=unitid, doc_type="factbook",
                doc_url=data.get("pdf_url"),
                year=data.get("year"),
                mime_type="application/pdf",
                page_count=data.get("page_count"),
                storage_path=data.get("storage_path"),
                extracted_text=(data.get("text_preview") or "")[:50000],  # cap; full text optional
                source_url=src_url, raw_payload_id=raw_id, confidence=conf,
            )

        if st == "strategic_plan":
            return writer.write_ir_document(
                unitid=unitid, doc_type="strategic_plan",
                doc_url=data.get("url"),
                year=data.get("year"),
                mime_type=("application/pdf" if (data.get("url") or "").lower().endswith(".pdf") else None),
                page_count=data.get("page_count"),
                storage_path=data.get("storage_path"),
                extracted_text=(data.get("text_preview") or "")[:50000],
                summary=data.get("summary"),
                source_url=src_url, raw_payload_id=raw_id, confidence=conf,
            )

        if st == "cds":
            return writer.write_cds_links(
                unitid=unitid, links=data.get("links", []),
                source_url=src_url, raw_payload_id=raw_id,
            )

        if st == "jobs":
            return writer.write_job_postings(
                unitid=unitid,
                jobs=data.get("all_jobs", []),
                source_board="institution_site",
                source_url=src_url, raw_payload_id=raw_id,
            )

        if st in ("research_nih", "research_nsf", "research_usaspending"):
            system = {
                "research_nih": "nih_reporter",
                "research_nsf": "nsf_awards",
                "research_usaspending": "usaspending",
            }[st]
            return writer.write_research_awards(
                unitid=unitid, source_system=system,
                awards=data.get("awards", []), raw_payload_id=raw_id,
            )

        if st == "research_herd":
            # Will be wired when HERD CSV is loaded
            return 0

        log.warning("dispatch.no_handler", source=st)
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("dispatch.write_failed", source=st, error=str(e))
        return 0

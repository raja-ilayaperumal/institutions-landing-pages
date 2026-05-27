"""End-to-end scrape pipeline for a single (institution, target) pair.

Flow:
    discover() → validator.pick() → extractor.extract() → persist()

Returns a PipelineResult with full audit trail (candidates, chosen URL,
extraction outcome, cost). Persistence is optional via the `writer` arg.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID, uuid4

import structlog

from ..connectors.base import InstitutionContext
from ..db.writer import ProvenanceWriter
from . import discovery, extractor, validator
from .targets import TARGETS, TargetSpec

log = structlog.get_logger(__name__)


@dataclass
class PipelineResult:
    target: str
    unitid: int
    run_id: str
    success: bool

    candidates_found: int = 0
    candidates: list[dict] = field(default_factory=list)
    chosen_url: str | None = None
    chosen_via: list[str] = field(default_factory=list)
    confidence: float = 0.0
    judge_reason: str | None = None

    extraction: dict | None = None
    structured: dict[str, Any] | None = None
    raw_payload_id: int | None = None

    duration_seconds: float = 0.0
    cost_usd: float = 0.0

    error_class: str | None = None
    error_message: str | None = None


async def scrape(
    ctx: InstitutionContext,
    target_name: str,
    *,
    writer: ProvenanceWriter | None = None,
) -> PipelineResult:
    """Run the full pipeline for one (institution, target). Writes to DB if writer given."""
    if target_name not in TARGETS:
        raise KeyError(f"unknown target '{target_name}'. Known: {sorted(TARGETS.keys())}")
    target = TARGETS[target_name]
    run_id = uuid4()
    t0 = time.monotonic()
    result = PipelineResult(target=target_name, unitid=ctx.unitid, run_id=str(run_id), success=False)
    total_cost = 0.0

    log.info("scrape.start", unitid=ctx.unitid, target=target_name, domain=ctx.registrable_domain)

    # 1. DISCOVER
    try:
        candidates = await discovery.discover(ctx, target)
    except Exception as e:  # noqa: BLE001
        result.error_class = "DISCOVER_CRASHED"
        result.error_message = str(e)
        result.duration_seconds = time.monotonic() - t0
        return result

    result.candidates_found = len(candidates)
    result.candidates = [
        {
            "url": c.url, "score": round(c.composite_score, 2),
            "sources": sorted(c.sources), "pattern_bonus": c.pattern_bonus,
            "search_rank": c.search_rank, "crawl_score": c.crawl_score,
            "title": c.title[:120], "snippet": c.snippet[:200],
        }
        for c in candidates[:10]
    ]
    if writer:
        for c in candidates:
            method = "_".join(sorted(c.sources))
            try:
                writer.record_crawl_log(
                    unitid=ctx.unitid, source_type=target_name,
                    url=c.url, discovery_method=method,
                    relevance_score=c.composite_score, depth=None, run_id=run_id,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("pipeline.crawl_log_failed", error=str(e))

    if not candidates:
        result.error_class = "NO_CANDIDATES"
        result.error_message = "all discovery channels returned empty"
        result.duration_seconds = time.monotonic() - t0
        _record_run(writer, ctx, target, result, run_id, "discover", "failed")
        return result

    # 2. VALIDATE — pick + verify canonical URL.
    # `pick_and_verify` loops: it picks the LLM judge's top choice, fetches
    # the chosen URL, and asks a verifier "is this really the target page?"
    # If verifier rejects, it blacklists that URL and re-picks from the
    # remaining candidates. Up to 3 attempts before falling through to the
    # agent. This catches cases like CSU Fullerton's `/planning/` being
    # picked over `/data/` — the verifier reads `/planning/`'s content and
    # rejects it because it's not actually the IR office.
    chosen, confidence, reason, judge_cost, rejected_urls = await validator.pick_and_verify(
        candidates, target, ctx.name,
    )
    total_cost += judge_cost
    result.cost_usd = total_cost
    result.confidence = confidence
    result.judge_reason = reason
    if chosen is None:
        # ESCALATION: validator+verifier rejected everything. Try the
        # LangChain url_finder agent with the rejected URLs as anti-examples
        # so it doesn't waste tool calls re-discovering them. Slower
        # (~5-15s, ~$0.001-0.005) but only fires when standard path failed.
        agent_url, agent_conf, agent_reason, agent_calls = await _try_agent_escalation(
            ctx, target, reason,
        )
        if agent_url and agent_conf >= 0.7:
            log.info("pipeline.agent_recovered",
                     unitid=ctx.unitid, target=target.name,
                     url=agent_url, confidence=agent_conf,
                     agent_tool_calls=agent_calls)
            # Synthesize a Candidate from the agent's answer and continue
            from .discovery import Candidate as _Cand
            chosen = _Cand(url=agent_url, sources={"agent_url_finder"})
            confidence = agent_conf
            reason = f"[agent escalation] {agent_reason}"
            result.confidence = confidence
            result.judge_reason = reason
        else:
            result.error_class = "LLM_REJECTED"
            result.error_message = (
                f"validator rejected ({reason}); "
                f"agent escalation also failed: {agent_reason}"
            )
            result.duration_seconds = time.monotonic() - t0
            _record_run(writer, ctx, target, result, run_id, "validate", "failed")
            return result

    result.chosen_url = chosen.url
    result.chosen_via = sorted(chosen.sources)
    if writer:
        try:
            writer.record_canonical_url(
                unitid=ctx.unitid, source_type=target_name,
                url=chosen.url, confidence=confidence, llm_reasoning=reason,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("pipeline.canonical_failed", error=str(e))

    # 3. EXTRACT (when target wants the body fetched)
    if not target.fetch_payload:
        # CDS is URL-only, but we still need rows in landing.cds_links.
        # If the canonical is a hub page, parse it for per-year links.
        if target.name == "cds" and writer is not None:
            try:
                await _persist_cds(writer, ctx, chosen.url)
            except Exception as e:  # noqa: BLE001
                log.warning("pipeline.cds_persist_failed",
                            error=str(e), unitid=ctx.unitid, url=chosen.url)
        result.success = True
        result.extraction = {"skipped": True, "reason": "target.fetch_payload=False"}
        result.duration_seconds = time.monotonic() - t0
        _record_run(writer, ctx, target, result, run_id, "extract", "skipped")
        return result

    extraction = await extractor.extract(chosen.url, target, ctx.name, run_id=run_id)
    total_cost += extraction.extractor_cost_usd
    result.cost_usd = total_cost
    if not extraction.success:
        result.error_class = extraction.error_class
        result.error_message = extraction.error_message
        result.extraction = asdict(extraction)
        result.duration_seconds = time.monotonic() - t0
        _record_run(writer, ctx, target, result, run_id, "extract", "failed")
        return result

    result.extraction = {
        "success": True, "final_url": extraction.final_url,
        "content_type": extraction.content_type, "http_status": extraction.http_status,
        "body_sha256": extraction.body_sha256, "fetcher": extraction.fetcher,
        "body_text_chars": len(extraction.body_text or ""),
        "body_html_chars": len(extraction.body_html or ""),
    }
    result.structured = extraction.structured

    # 4. PERSIST — raw payload + content-table row
    if writer:
        try:
            result.raw_payload_id = writer.record_raw_payload(
                unitid=ctx.unitid, source_type=target_name,
                url=extraction.final_url,
                body_html=extraction.body_html,
                body_text=extraction.body_text,
                body_blob_path=extraction.body_blob_path,
                body_sha256=extraction.body_sha256 or "",
                content_type=extraction.content_type,
                http_status=extraction.http_status,
                fetcher=extraction.fetcher,
                run_id=run_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("pipeline.raw_payload_failed", error=str(e))

        try:
            _write_content(writer, ctx, target, extraction, result)
        except Exception as e:  # noqa: BLE001
            log.warning("pipeline.content_write_failed", target=target.name, error=str(e))

    result.success = True
    result.duration_seconds = time.monotonic() - t0
    _record_run(writer, ctx, target, result, run_id, "store", "success")
    log.info(
        "scrape.done", unitid=ctx.unitid, target=target_name, success=True,
        chosen_url=chosen.url, duration_s=round(result.duration_seconds, 2),
        cost_usd=round(total_cost, 5),
    )
    return result


async def _persist_cds(writer, ctx, canon: str) -> None:
    """Fetch the chosen CDS URL and persist year-tagged links.

    Single-year PDF → one row. Hub page → multiple rows (one per year).
    """
    import re as _re
    from datetime import date as _date

    year_match = _re.search(r"20\d{2}", canon)
    if canon.lower().endswith(".pdf") and year_match:
        writer.write_cds_links(
            unitid=ctx.unitid,
            links=[{"url": canon, "year": int(year_match.group(0)),
                    "is_pdf": True}],
            source_url=canon, raw_payload_id=None,
        )
        return

    from ..connectors.cds_hub import extract_year_links
    year_links = await extract_year_links(canon, ctx.name)
    if year_links:
        writer.write_cds_links(
            unitid=ctx.unitid, links=year_links,
            source_url=canon, raw_payload_id=None,
        )
    else:
        # Hub didn't surface year-tagged children — keep the hub URL itself
        # under the current year so the page has SOMETHING.
        writer.write_cds_links(
            unitid=ctx.unitid,
            links=[{"url": canon, "year": _date.today().year,
                    "is_pdf": False}],
            source_url=canon, raw_payload_id=None,
        )


async def _try_agent_escalation(ctx, target, validator_reason: str) -> tuple[str | None, float, str, int]:
    """Call the url_finder agent as a fallback when validator rejects all
    candidates. Returns (url_or_None, confidence, reason, tool_calls)."""
    try:
        from ..agents import find_url as _agent_find
        result = await _agent_find(
            institution_name=ctx.name,
            registrable_domain=ctx.registrable_domain or ctx.domain or "",
            target_description=target.description,
        )
        return result.found_url, result.confidence, result.reason, result.tool_calls
    except Exception as e:  # noqa: BLE001
        log.warning("pipeline.agent_failed", error=str(e))
        return None, 0.0, f"agent crashed: {e}", 0


def _to_date(s):
    if not s:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(str(s)[:30], fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    return None


def _record_run(
    writer: ProvenanceWriter | None, ctx: InstitutionContext, target: TargetSpec,
    result: PipelineResult, run_id: UUID, stage: str, status: str,
) -> None:
    if writer is None:
        return
    from datetime import datetime, timezone
    try:
        writer.record_scraper_run(
            run_id=run_id, unitid=ctx.unitid, source_type=target.name,
            stage=stage, status=status,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            duration_ms=int(result.duration_seconds * 1000),
            error_class=result.error_class, error_message=result.error_message,
            rows_written=1 if status == "success" else 0,
            llm_tokens_in=None, llm_tokens_out=None,
            cost_usd=result.cost_usd,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("pipeline.run_log_failed", error=str(e))


def _write_content(
    writer: ProvenanceWriter, ctx: InstitutionContext, target: TargetSpec,
    extraction, result: PipelineResult,
) -> None:
    """Dispatch into the right landing.* content table."""
    structured = result.structured or {}
    raw_id = result.raw_payload_id
    canon = extraction.final_url

    if target.name == "ir_page":
        writer.write_ir_page(
            unitid=ctx.unitid,
            data={
                "office_name": structured.get("office_name"),
                "summary": structured.get("office_mission") or structured.get("summary"),
                "parent_division": structured.get("parent_division"),
                "chosen": canon,
            },
            canonical_url=canon, confidence=result.confidence, raw_payload_id=raw_id,
        )
        # Quality scoring: confidence from validator + low-conf flag
        try:
            from ..db.engine import get_conn as _gc2
            with _gc2() as _c, _c.cursor() as _cur:
                _cur.execute(
                    "UPDATE landing.ir_pages SET quality_score=%s, needs_review=%s WHERE unitid=%s",
                    (result.confidence, (result.confidence or 0) < 0.7, ctx.unitid),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("pipeline.quality_update_failed", error=str(e))
        # If we got a head contact, write it as one ir_contact row
        head_name = structured.get("head_name")
        if head_name:
            writer.write_ir_contacts(
                unitid=ctx.unitid,
                members=[{
                    "name": head_name,
                    "title": structured.get("head_title"),
                    "email": structured.get("head_email"),
                    "phone": structured.get("contact_phone"),
                }],
                source_url=canon, raw_payload_id=raw_id, confidence=result.confidence,
            )

    elif target.name == "factbook":
        writer.write_ir_document(
            unitid=ctx.unitid, doc_type="factbook",
            doc_url=canon,
            title=structured.get("title"),
            year=structured.get("year"),
            mime_type=extraction.content_type,
            storage_path=extraction.body_blob_path,
            extracted_text=(extraction.body_text or "")[:100_000],
            summary=structured.get("summary"),
            source_url=canon, raw_payload_id=raw_id, confidence=result.confidence,
        )

    elif target.name == "strategic_plan":
        writer.write_ir_document(
            unitid=ctx.unitid, doc_type="strategic_plan",
            doc_url=canon,
            title=structured.get("title"),
            year=structured.get("publication_year"),
            mime_type=extraction.content_type,
            storage_path=extraction.body_blob_path,
            extracted_text=(extraction.body_text or "")[:100_000],
            summary=structured.get("summary"),
            source_url=canon, raw_payload_id=raw_id, confidence=result.confidence,
        )

    elif target.name in ("data_dictionary", "data_definition", "glossary", "dashboards"):
        # Persist body to disk (only when we have content) + register in ir_documents.
        storage_path = extraction.body_blob_path
        if not storage_path and extraction.body_html:
            try:
                from ..db.document_store import save as _save_doc
                stored = _save_doc(
                    unitid=ctx.unitid, doc_type=target.name,
                    body_bytes=extraction.body_html.encode("utf-8"),
                    content_type=extraction.content_type or "text/html",
                    url=canon, year=(structured.get("publication_year") if isinstance(structured, dict) else None),
                    title_hint=target.name,
                )
                storage_path = stored.relative_path
            except Exception as e:  # noqa: BLE001
                log.warning("pipeline.docstore_failed", target=target.name, error=str(e))

        writer.write_ir_document(
            unitid=ctx.unitid, doc_type=target.name,
            doc_url=canon,
            title=structured.get("title"),
            year=structured.get("publication_year") if isinstance(structured, dict) else None,
            mime_type=extraction.content_type,
            storage_path=storage_path,
            extracted_text=(extraction.body_text or "")[:50_000],
            summary=structured.get("summary"),
            source_url=canon, raw_payload_id=raw_id, confidence=result.confidence,
        )

    elif target.name == "ir_jobs":
        postings = (structured.get("postings") or []) if isinstance(structured, dict) else []
        # Write each posting; ProvenanceWriter dedupes via UNIQUE(unitid, apply_url)
        # Also persist HTML file
        storage_path = extraction.body_blob_path
        if not storage_path and extraction.body_html:
            try:
                from ..db.document_store import save as _save_doc
                stored = _save_doc(
                    unitid=ctx.unitid, doc_type="ir_jobs_listing",
                    body_bytes=extraction.body_html.encode("utf-8"),
                    content_type=extraction.content_type or "text/html",
                    url=canon, title_hint="careers-page",
                )
                storage_path = stored.relative_path
            except Exception as e:  # noqa: BLE001
                log.warning("pipeline.docstore_failed", target=target.name, error=str(e))
        if postings:
            writer.write_job_postings(
                unitid=ctx.unitid,
                jobs=[{
                    "title": p.get("title"), "category": "ir",
                    "url": p.get("apply_url"),
                } for p in postings if p.get("title") and p.get("apply_url")],
                source_board="institution_site",
                source_url=canon, raw_payload_id=raw_id,
            )

    elif target.name == "grants":
        items = (structured.get("grants") or []) if isinstance(structured, dict) else []
        if items:
            try:
                from ..db.engine import get_conn as _gc
                with _gc() as _c, _c.cursor() as _cur:
                    for g in items:
                        title = (g.get("title") or "").strip()
                        if not title:
                            continue
                        # external_id: prefer URL, fall back to deterministic hash of title
                        ext_id = g.get("url") or f"site::{ctx.unitid}::{title[:200]}"
                        _cur.execute(
                            """
                            INSERT INTO landing.grants
                              (unitid, source_system, external_id, agency, title,
                               amount_usd, award_date, end_date, status, source_url,
                               raw_payload_id, parser_version)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (source_system, external_id) DO UPDATE SET
                              title=EXCLUDED.title, amount_usd=EXCLUDED.amount_usd,
                              award_date=EXCLUDED.award_date, end_date=EXCLUDED.end_date,
                              status=EXCLUDED.status, fetched_at=NOW()
                            """,
                            (ctx.unitid, "institution_site", ext_id,
                             g.get("agency"), title[:500],
                             g.get("amount_usd"),
                             _to_date(g.get("award_date")),
                             _to_date(g.get("end_date")),
                             g.get("kind"), canon, raw_id, "0.1.0"),
                        )
            except Exception as e:  # noqa: BLE001
                log.warning("pipeline.grants_write_failed", error=str(e))

    # CDS persistence happens before fetch_payload returns (see scrape()).

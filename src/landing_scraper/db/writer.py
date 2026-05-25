"""ProvenanceWriter — the ONLY sanctioned write path to landing.* content tables.

Every write must carry:
  - source_url
  - fetched_at (defaults to now)
  - parser_version
  - raw_payload_id (when a raw fetch backed the extraction; nullable for API-only)

Public methods per source_type plus generic record_* helpers. All upserts are
idempotent via the UNIQUE constraints declared in the schema.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .engine import get_conn

PARSER_VERSION = "0.1.0"  # bump per-connector when parser changes


@dataclass
class WriteStats:
    rows_written: int = 0
    table: str = ""


class ProvenanceWriter:
    """Wraps a connection. Use as: with ProvenanceWriter() as w: w.write_*()."""

    def __init__(self, conn: psycopg.Connection | None = None):
        self._conn = conn
        self._owned = conn is None

    def __enter__(self) -> "ProvenanceWriter":
        if self._owned:
            # autocommit so a single failure (e.g. UNIQUE conflict on one row)
            # doesn't poison the entire writer's connection for subsequent rows
            self._ctx = get_conn(autocommit=True)
            self._conn = self._ctx.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owned:
            self._ctx.__exit__(exc_type, exc, tb)

    # =====================================================================
    # OPERATIONAL TABLES
    # =====================================================================

    def record_raw_payload(
        self,
        *,
        unitid: int,
        source_type: str,
        url: str,
        body_html: str | None,
        body_text: str | None,
        body_blob_path: str | None,
        body_sha256: str,
        content_type: str | None,
        http_status: int | None,
        fetcher: str,
        run_id: UUID | str,
    ) -> int:
        sql = """
            INSERT INTO landing.raw_payloads
              (unitid, source_type, url, content_type, http_status,
               body_text, body_html, body_blob_path, body_sha256, fetcher, run_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                unitid, source_type, url, content_type, http_status,
                body_text, body_html, body_blob_path, body_sha256, fetcher, str(run_id),
            ))
            return cur.fetchone()["id"]

    def record_scraper_run(
        self,
        *,
        run_id: UUID | str,
        unitid: int | None,
        source_type: str | None,
        stage: str,
        status: str,
        started_at: datetime,
        finished_at: datetime | None,
        duration_ms: int | None,
        error_class: str | None,
        error_message: str | None,
        rows_written: int | None,
        llm_tokens_in: int | None = None,
        llm_tokens_out: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        sql = """
            INSERT INTO landing.scraper_runs
              (run_id, unitid, source_type, stage, status, started_at, finished_at,
               duration_ms, error_class, error_message, rows_written,
               llm_tokens_in, llm_tokens_out, cost_usd)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status,
                finished_at = EXCLUDED.finished_at,
                duration_ms = EXCLUDED.duration_ms,
                error_class = EXCLUDED.error_class,
                error_message = EXCLUDED.error_message,
                rows_written = EXCLUDED.rows_written,
                cost_usd = EXCLUDED.cost_usd
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                str(run_id), unitid, source_type, stage, status,
                started_at, finished_at, duration_ms,
                error_class, error_message, rows_written,
                llm_tokens_in, llm_tokens_out, cost_usd,
            ))

    def record_canonical_url(
        self,
        *,
        unitid: int, source_type: str, url: str,
        confidence: float, llm_reasoning: str | None = None,
    ) -> None:
        # Supersede any current canonical, then insert new
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE landing.canonical_urls SET superseded_at = NOW() "
                "WHERE unitid = %s AND source_type = %s AND superseded_at IS NULL",
                (unitid, source_type),
            )
            cur.execute(
                "INSERT INTO landing.canonical_urls "
                "(unitid, source_type, url, confidence, llm_reasoning) "
                "VALUES (%s,%s,%s,%s,%s)",
                (unitid, source_type, url, confidence, llm_reasoning),
            )

    def record_crawl_log(
        self,
        *,
        unitid: int, source_type: str, url: str,
        discovery_method: str, relevance_score: float | None,
        depth: int | None, run_id: UUID | str,
    ) -> None:
        sql = """
            INSERT INTO landing.crawl_log
              (unitid, source_type, url, discovery_method, relevance_score, depth, run_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (unitid, source_type, url, run_id) DO NOTHING
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (unitid, source_type, url, discovery_method, relevance_score, depth, str(run_id)))

    # =====================================================================
    # CONTENT-TABLE UPSERTS
    # =====================================================================

    def write_brand_assets(self, *, unitid: int, data: dict, raw_payload_id: int | None, source_url: str | None) -> int:
        logos = data.get("logos") or []
        # Flatten first logo's first format URL
        logo_url = None
        if logos and isinstance(logos[0], dict):
            formats = logos[0].get("formats") or []
            if formats:
                logo_url = formats[0] if isinstance(formats[0], str) else None
        colors = data.get("colors") or []
        primary = next((c.get("hex") for c in colors if isinstance(c, dict) and c.get("type") == "primary"), None)
        secondary = next((c.get("hex") for c in colors if isinstance(c, dict) and c.get("type") == "secondary"), None)
        sql = """
            INSERT INTO landing.brand_assets
              (unitid, logo_url, colors_primary, colors_secondary, color_palette, fonts,
               source_url, raw_payload_id, parser_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (unitid) DO UPDATE SET
                logo_url = EXCLUDED.logo_url,
                colors_primary = EXCLUDED.colors_primary,
                colors_secondary = EXCLUDED.colors_secondary,
                color_palette = EXCLUDED.color_palette,
                fonts = EXCLUDED.fonts,
                source_url = EXCLUDED.source_url,
                raw_payload_id = EXCLUDED.raw_payload_id,
                parser_version = EXCLUDED.parser_version,
                fetched_at = NOW()
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                unitid, logo_url, primary, secondary,
                Jsonb(colors), Jsonb(data.get("fonts") or []),
                source_url, raw_payload_id, PARSER_VERSION,
            ))
        return 1

    def write_wiki(self, *, unitid: int, data: dict, raw_payload_id: int | None, source_url: str | None) -> int:
        sql = """
            INSERT INTO landing.wiki_summaries
              (unitid, wikipedia_url, wikidata_qid, short_description, extract_summary,
               thumbnail_url, source_url, raw_payload_id, parser_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (unitid) DO UPDATE SET
                wikipedia_url = EXCLUDED.wikipedia_url,
                wikidata_qid = EXCLUDED.wikidata_qid,
                short_description = EXCLUDED.short_description,
                extract_summary = EXCLUDED.extract_summary,
                thumbnail_url = EXCLUDED.thumbnail_url,
                source_url = EXCLUDED.source_url,
                raw_payload_id = EXCLUDED.raw_payload_id,
                parser_version = EXCLUDED.parser_version,
                fetched_at = NOW()
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                unitid, data.get("page_url"), data.get("wikidata_qid"),
                data.get("short_description"), data.get("extract"),
                data.get("thumbnail"),
                source_url or data.get("page_url"),
                raw_payload_id, PARSER_VERSION,
            ))

        # Notable alumni rows
        alumni = data.get("notable_alumni") or []
        count = 1
        if alumni:
            with self._conn.cursor() as cur:
                for a in alumni:
                    cur.execute("""
                        INSERT INTO landing.notable_alumni
                          (unitid, name, field, short_bio, wikipedia_url, source_url, raw_payload_id, parser_version)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (unitid, name) DO UPDATE SET
                            short_bio = EXCLUDED.short_bio,
                            wikipedia_url = EXCLUDED.wikipedia_url,
                            fetched_at = NOW()
                    """, (
                        unitid, a.get("name"), a.get("field"), a.get("short_bio"),
                        a.get("wikipedia_url"), source_url or data.get("page_url"),
                        raw_payload_id, PARSER_VERSION,
                    ))
                    count += 1
        return count

    def write_ir_page(self, *, unitid: int, data: dict, canonical_url: str | None, confidence: float | None,
                      raw_payload_id: int | None) -> int:
        sql = """
            INSERT INTO landing.ir_pages
              (unitid, office_name, page_url, page_summary, parent_division,
               source_url, raw_payload_id, parser_version, confidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (unitid) DO UPDATE SET
                office_name = EXCLUDED.office_name,
                page_url = EXCLUDED.page_url,
                page_summary = EXCLUDED.page_summary,
                parent_division = EXCLUDED.parent_division,
                source_url = EXCLUDED.source_url,
                raw_payload_id = EXCLUDED.raw_payload_id,
                parser_version = EXCLUDED.parser_version,
                confidence = EXCLUDED.confidence,
                fetched_at = NOW()
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                unitid,
                data.get("office_name"),
                data.get("chosen") or canonical_url,
                data.get("summary"),
                data.get("parent_division"),
                canonical_url, raw_payload_id, PARSER_VERSION, confidence,
            ))
        return 1

    def write_ir_contacts(self, *, unitid: int, members: list[dict], source_url: str | None,
                          raw_payload_id: int | None, confidence: float | None) -> int:
        if not members:
            return 0
        sql = """
            INSERT INTO landing.ir_contacts
              (unitid, name, title, email, phone, linkedin_url, ordering,
               source_url, raw_payload_id, parser_version, confidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (unitid, name, title) DO UPDATE SET
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                linkedin_url = EXCLUDED.linkedin_url,
                source_url = EXCLUDED.source_url,
                raw_payload_id = EXCLUDED.raw_payload_id,
                fetched_at = NOW()
        """
        written = 0
        with self._conn.cursor() as cur:
            for i, m in enumerate(members):
                name = (m.get("name") or "").strip()
                # Quality gate: skip placeholder/empty/junk names so the
                # rendered Team table never shows "(unparsed)" rows.
                if not name or name.lower() in ("(unparsed)", "unparsed", "n/a", "none"):
                    continue
                cur.execute(sql, (
                    unitid, name[:200], m.get("title"),
                    m.get("email"), m.get("phone"), m.get("linkedin_url"),
                    i, source_url, raw_payload_id, PARSER_VERSION, confidence,
                ))
                written += 1
        return written

    def write_ir_document(self, *, unitid: int, doc_type: str, doc_url: str,
                          title: str | None = None, year: int | None = None,
                          mime_type: str | None = None, page_count: int | None = None,
                          storage_path: str | None = None, extracted_text: str | None = None,
                          summary: str | None = None, source_url: str | None = None,
                          raw_payload_id: int | None = None, confidence: float | None = None) -> int:
        sql = """
            INSERT INTO landing.ir_documents
              (unitid, doc_type, title, year, doc_url, mime_type, page_count, storage_path,
               extracted_text, summary, source_url, raw_payload_id, parser_version, confidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (unitid, doc_type, doc_url) DO UPDATE SET
                title = COALESCE(EXCLUDED.title, landing.ir_documents.title),
                year  = COALESCE(EXCLUDED.year,  landing.ir_documents.year),
                mime_type = COALESCE(EXCLUDED.mime_type, landing.ir_documents.mime_type),
                page_count = COALESCE(EXCLUDED.page_count, landing.ir_documents.page_count),
                storage_path = COALESCE(EXCLUDED.storage_path, landing.ir_documents.storage_path),
                extracted_text = COALESCE(EXCLUDED.extracted_text, landing.ir_documents.extracted_text),
                summary = COALESCE(EXCLUDED.summary, landing.ir_documents.summary),
                source_url = EXCLUDED.source_url,
                raw_payload_id = COALESCE(EXCLUDED.raw_payload_id, landing.ir_documents.raw_payload_id),
                confidence = EXCLUDED.confidence,
                fetched_at = NOW()
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                unitid, doc_type, title, year, doc_url, mime_type, page_count,
                storage_path, extracted_text, summary, source_url, raw_payload_id,
                PARSER_VERSION, confidence,
            ))
        return 1

    def write_cds_links(self, *, unitid: int, links: list[dict], source_url: str | None,
                        raw_payload_id: int | None) -> int:
        if not links:
            return 0
        sql = """
            INSERT INTO landing.cds_links
              (unitid, year, cds_url, is_pdf, source_url, raw_payload_id, parser_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (unitid, year) DO UPDATE SET
                cds_url = EXCLUDED.cds_url,
                is_pdf = EXCLUDED.is_pdf,
                source_url = EXCLUDED.source_url,
                fetched_at = NOW()
        """
        written = 0
        seen_years: set[int] = set()
        with self._conn.cursor() as cur:
            for link in links:
                year = link.get("year")
                if year is None or year in seen_years:
                    continue
                seen_years.add(year)
                cur.execute(sql, (
                    unitid, year, link.get("url"),
                    link.get("is_pdf"), source_url, raw_payload_id, PARSER_VERSION,
                ))
                written += 1
        return written

    def write_job_postings(self, *, unitid: int, jobs: list[dict], source_board: str,
                            source_url: str | None, raw_payload_id: int | None) -> int:
        if not jobs:
            return 0
        sql = """
            INSERT INTO landing.job_postings
              (unitid, title, category, apply_url, source_board,
               source_url, raw_payload_id, parser_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (unitid, apply_url) DO UPDATE SET
                title = EXCLUDED.title,
                category = EXCLUDED.category,
                fetched_at = NOW()
        """
        written = 0
        with self._conn.cursor() as cur:
            for j in jobs:
                if not j.get("url") or not j.get("title"):
                    continue
                cur.execute(sql, (
                    unitid, (j["title"] or "")[:500], j.get("category", "other"),
                    j["url"], source_board, source_url, raw_payload_id, PARSER_VERSION,
                ))
                written += 1
        return written

    def write_research_awards(self, *, unitid: int, source_system: str, awards: list[dict],
                              raw_payload_id: int | None = None) -> int:
        if not awards:
            return 0
        sql = """
            INSERT INTO landing.research_awards
              (unitid, source_system, external_id, agency, program_or_mechanism, title,
               pi_name, pi_orcid, amount_total_usd, amount_obligated_usd, fiscal_year,
               start_date, end_date, abstract,
               source_url, raw_payload_id, parser_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source_system, external_id) DO UPDATE SET
                title = EXCLUDED.title,
                amount_total_usd = EXCLUDED.amount_total_usd,
                amount_obligated_usd = EXCLUDED.amount_obligated_usd,
                end_date = EXCLUDED.end_date,
                abstract = EXCLUDED.abstract,
                fetched_at = NOW()
        """
        written = 0
        with self._conn.cursor() as cur:
            for a in awards:
                ext = a.get("external_id")
                if not ext:
                    continue
                cur.execute(sql, (
                    unitid, source_system, str(ext),
                    a.get("agency"), a.get("program_or_mechanism"),
                    a.get("title"), a.get("pi_name"), a.get("pi_orcid"),
                    a.get("amount_total_usd"), a.get("amount_obligated_usd"),
                    a.get("fiscal_year"), _parse_date(a.get("start_date")), _parse_date(a.get("end_date")),
                    (a.get("abstract") or "")[:5000],
                    a.get("source_url"), raw_payload_id, PARSER_VERSION,
                ))
                written += 1
        return written


def _parse_date(v: Any) -> str | None:
    """Best-effort: accept ISO 'YYYY-MM-DD', US 'MM/DD/YYYY', or None."""
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    # Try ISO first
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).date().isoformat()
        except ValueError:
            continue
    return None

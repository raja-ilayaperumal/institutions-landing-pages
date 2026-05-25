"""PDF download + text extraction."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from ..config import settings

log = structlog.get_logger(__name__)


@dataclass
class PDFResult:
    url: str
    success: bool
    storage_path: Path | None
    page_count: int | None
    text: str
    sha256: str | None
    error: str | None = None


async def download_and_extract(url: str, *, timeout: float = 60.0) -> PDFResult:
    settings.raw_payload_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
        if not resp.is_success:
            return PDFResult(
                url=url, success=False, storage_path=None, page_count=None,
                text="", sha256=None, error=f"HTTP {resp.status_code}",
            )
        data = resp.content
        digest = hashlib.sha256(data).hexdigest()
        path = settings.raw_payload_dir / f"{digest}.pdf"
        if not path.exists():
            path.write_bytes(data)
        text, pages = _extract_text(path)
        return PDFResult(
            url=url, success=True, storage_path=path,
            page_count=pages, text=text, sha256=digest,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("pdf.failed", url=url, error=str(e))
        return PDFResult(
            url=url, success=False, storage_path=None, page_count=None,
            text="", sha256=None, error=str(e),
        )


def _extract_text(path: Path) -> tuple[str, int]:
    import pdfplumber

    parts: list[str] = []
    pages = 0
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages += 1
            txt = page.extract_text() or ""
            if txt:
                parts.append(txt)
    return "\n\n".join(parts), pages

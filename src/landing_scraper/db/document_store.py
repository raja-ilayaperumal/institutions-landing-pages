"""Centralized on-disk document storage.

Replaces the flat `data/raw_payloads/<sha256>.<ext>` layout with an organized
hierarchy keyed by document type:

  data/docs/
  ├── dfr_reports/<unitid>_<year>.html
  ├── factbook/<unitid>_<year>_<sha8>.pdf
  ├── strategic_plan/<unitid>_<year>_<sha8>.pdf
  ├── cds/<unitid>_<year>_<sha8>.<ext>
  ├── data_dictionary/<unitid>_<sha8>.<ext>
  ├── data_definition/<unitid>_<sha8>.<ext>
  ├── glossary/<unitid>_<sha8>.<ext>
  └── ir_page/<unitid>/<slug>.html

Every save returns a Path the caller can use as
`landing.ir_documents.storage_path`.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

DOCS_ROOT = Path("data/docs")

# doc_type → subdirectory name (kept stable; pages must rely on these paths)
_DIRS = {
    "dfr_report":      "dfr_reports",
    "factbook":        "factbook",
    "strategic_plan":  "strategic_plan",
    "cds":             "cds",
    "common_data_set": "cds",
    "data_dictionary": "data_dictionary",
    "data_definition": "data_definition",
    "glossary":        "glossary",
    "ir_page":         "ir_page",
    "ir_team":         "ir_team",
    "annual_report":   "annual_report",
    "other":           "other",
}


@dataclass
class StoredDoc:
    path: Path
    sha256: str
    size_bytes: int
    content_type: str
    relative_path: str  # for storage_path column


def _slug(text: str | None, max_len: int = 40) -> str:
    if not text:
        return ""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len]


def _ext_for(content_type: str | None, url: str | None) -> str:
    ct = (content_type or "").lower()
    if "pdf" in ct or (url and url.lower().split("?")[0].endswith(".pdf")):
        return "pdf"
    if "html" in ct:
        return "html"
    if "xml" in ct:
        return "xml"
    if "csv" in ct:
        return "csv"
    if "excel" in ct or "spreadsheet" in ct or (url and url.lower().endswith((".xlsx", ".xls"))):
        return "xlsx"
    if "json" in ct:
        return "json"
    return "bin"


def save(
    *,
    unitid: int,
    doc_type: str,
    body_bytes: bytes,
    content_type: str | None = None,
    url: str | None = None,
    year: int | None = None,
    title_hint: str | None = None,
) -> StoredDoc:
    """Persist a document with a stable, human-readable filename. Idempotent
    via SHA-256: if the same content was saved before, returns the existing path.
    """
    subdir = _DIRS.get(doc_type, "other")
    out_dir = DOCS_ROOT / subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    sha = hashlib.sha256(body_bytes).hexdigest()
    sha8 = sha[:8]
    ext = _ext_for(content_type, url)

    parts: list[str] = [str(unitid)]
    if year:
        parts.append(str(year))
    if title_hint:
        s = _slug(title_hint)
        if s:
            parts.append(s)
    parts.append(sha8)
    filename = "_".join(parts) + f".{ext}"
    full_path = out_dir / filename

    if not full_path.exists():
        full_path.write_bytes(body_bytes)

    return StoredDoc(
        path=full_path,
        sha256=sha,
        size_bytes=len(body_bytes),
        content_type=content_type or f"application/{ext}",
        relative_path=str(full_path),
    )

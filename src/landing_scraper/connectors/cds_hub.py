"""CDS hub-page extractor.

When the CDS canonical URL is a hub page (e.g.
`https://uda.ncsu.edu/institutional-analytics/common-data-set/`), parse the
HTML for anchors that link to per-year CDS files. We don't want a single
year hard-coded — the institution publishes a new CDS every year and the
hub page is the index.

Heuristics for picking a CDS link from anchor + label:
  - URL contains "cds" or "common-data-set" (case-insensitive)
  - Anchor text mentions a year (2018..2030) AND something CDS-like
  - File extension is .pdf (preferred) or .html/.xlsx (accepted)

Returns a list of {"url": str, "year": int, "is_pdf": bool} ready for
writer.write_cds_links.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import structlog
from bs4 import BeautifulSoup

from ..core import crawler

log = structlog.get_logger(__name__)

# A CDS year typically appears as "2023-24" / "2023-2024" / "2023" / "CDS 2023"
_YEAR_RE = re.compile(r"(?:20)(\d{2})(?:[-/](?:20)?\d{2,4})?")
_CDS_HINT_RE = re.compile(r"(?:common[\s\-_]*data[\s\-_]*set|\bcds\b)", re.IGNORECASE)


def _looks_like_cds_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return ("cds" in u or "common-data-set" in u or "common_data_set" in u
            or "commondataset" in u)


def _extract_year(text: str) -> int | None:
    """Return the most-recent academic-year start from the text.

    "2023-24" → 2023, "2024-2025" → 2024, "CDS 2022" → 2022.
    """
    years: list[int] = []
    for m in _YEAR_RE.finditer(text):
        try:
            yy = int(m.group(1))
            years.append(2000 + yy)
        except ValueError:
            continue
    if not years:
        return None
    return max(years)


async def extract_year_links(hub_url: str, institution_name: str) -> list[dict]:
    """Fetch the hub page and return per-year CDS links."""
    f = await crawler.fetch(hub_url, timeout=20.0)
    if not f.success or not f.html:
        log.warning("cds_hub.fetch_failed", url=hub_url, err=f.error)
        return []

    soup = BeautifulSoup(f.html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    base_host = urlparse(hub_url).netloc.lower()

    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        # Resolve relative URLs against the hub
        full = urljoin(hub_url, href)
        label = a.get_text(" ", strip=True) or ""

        link_text = f"{full} {label}"
        if not (_looks_like_cds_url(full) or _CDS_HINT_RE.search(label)):
            continue

        year = _extract_year(label) or _extract_year(full)
        if not year:
            continue

        # Skip the hub itself or pages without a CDS-y URL/label
        if full.rstrip("/") == hub_url.rstrip("/"):
            continue

        # Only keep links on the same institution or trusted CDN host
        full_host = urlparse(full).netloc.lower()
        if full_host and full_host != base_host:
            # Allow same registrable domain (subdomain shift) — coarse check
            # ncsu.edu === report.isa.ncsu.edu acceptable for NC State.
            parts_a = base_host.split(".")[-2:]
            parts_b = full_host.split(".")[-2:]
            if parts_a != parts_b:
                continue

        is_pdf = full.lower().endswith(".pdf")

        key = (full.lower(), year)
        if key in seen:
            continue
        seen.add(key)

        out.append({"url": full, "year": year, "is_pdf": is_pdf})

    # Sort newest first
    out.sort(key=lambda d: d["year"], reverse=True)
    log.info("cds_hub.extracted", url=hub_url, found=len(out))
    return out

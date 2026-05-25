"""Powerful multi-page IR team extraction.

Given an IR landing URL (the office homepage), this:

  1. Discovers subpages via THREE channels in parallel:
     - Wellknown paths: /about, /staff, /people, /team, /contact, /leadership, ...
     - On-page link extraction: same-domain hrefs whose text/path matches
       team/staff/people/about/contact/director keywords
     - Sitemap of the IR subdomain (root or institution-level), filtered to
       team-ish URLs
  2. Fetches every viable subpage via crawl4ai (handles Cloudflare/anti-bot).
  3. Saves each page's HTML/markdown to landing.raw_payloads (time-machine).
  4. Aggregates page text with explicit `--- PAGE: <url> ---` boundaries.
  5. LLM-extracts members with a rich schema (name, title, email, phone,
     photo_url, bio_short, linkedin_url, role_category, page_source).
  6. Returns an IRTeamResult ready to persist.

Designed to replace the basic connectors/ir_team.py for institutions where the
team is split across multiple pages.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import UUID, uuid4

import httpx
import structlog
from bs4 import BeautifulSoup

from ..core import crawler, llm
from ..core.html import absolute_links, visible_text
from ..db.writer import ProvenanceWriter

log = structlog.get_logger(__name__)

TEAM_KEYWORDS = (
    "about", "staff", "team", "people", "directory", "personnel",
    "contact", "leadership", "our team", "our staff", "who we are",
    "meet", "members", "faculty", "associates",
)

# Wellknown subpaths to try directly off the IR landing URL.
WELLKNOWN_SUBPATHS = (
    "/about/", "/about", "/about-us/", "/about-us",
    "/staff/", "/staff", "/people/", "/people",
    "/team/", "/team", "/our-team/", "/our-team",
    "/directory/", "/directory", "/personnel/",
    "/contact/", "/contact", "/contact-us/", "/contact-us",
    "/leadership/", "/leadership", "/who-we-are/", "/who-we-are",
    "/members/",
)

EXTRACTOR_SYSTEM = """\
You extract Institutional Research office contact info AND staff from web pages.

The user will provide a concatenation of multiple pages from one institution's
IR office website (sections separated by `--- PAGE: <url> ---`).

CRITICAL — PAGE PRIORITY:
The FIRST page in the input is explicitly labeled `(IR landing)` — this is
the canonical Office of Institutional Research homepage. ALL contact info
(phone/fax/email/address) MUST come from THAT page. The subsequent pages are
for finding team members only, not for contact info.

CRITICAL — CONTACT INFO RULES:
- `office_phone` must be the phone listed in the IR landing page's OWN
  contact block — typically immediately after "Contact Us", "Phone:", or right
  after the office address. It is NEVER the institution's main switchboard
  (numbers ending in -0000 or -5000 are usually switchboards).
- Reject phone numbers ABOVE the page's main heading or in the page footer —
  those are typically site-wide university numbers, not office-specific.
- If the IR landing page shows "Phone: NNN-NNN-NNNN" right next to
  "Institutional Research", that IS the office phone.
- If you see multiple phone numbers and cannot tell which one belongs to IR,
  return null. Better to omit than to pick the wrong one.
- Same for `office_email`: must be IR-office-specific (e.g. `ir@school.edu`,
  `oir@school.edu`), NOT `info@`, `webmaster@`, or a person's email.
- `office_fax` follows the same rules.

Return ONE JSON object:
{
  "office_name":     str | null,   // e.g. "Office of Institutional Research"
  "parent_division": str | null,   // e.g. "Office of the Provost"
  "office_phone":    str | null,   // IR-OFFICE-SPECIFIC phone (see rules above)
  "office_fax":      str | null,   // IR-OFFICE-SPECIFIC fax
  "office_email":    str | null,   // IR-OFFICE-SPECIFIC email (not info@/webmaster@)
  "office_address":  str | null,   // street address / building / room
  "members": [
    {
      "name":           str,        // REQUIRED, full name
      "title":          str | null, // job title
      "email":          str | null,
      "phone":          str | null,
      "photo_url":      str | null, // absolute URL to headshot if present
      "bio_short":      str | null, // 1-sentence summary if visible
      "linkedin_url":   str | null,
      "role_category":  "director" | "associate_director" | "analyst" | "coordinator" | "research_scientist" | "data_scientist" | "support_staff" | "student" | "other",
      "source_page":    str         // which page URL the member was found on
    },
    ...
  ]
}

Rules:
- Include ONLY people explicitly listed as members of an institutional research,
  analytics, effectiveness, planning, decision-support, or assessment office.
- Do NOT include external collaborators, advisory board members, faculty bios
  unrelated to the office, or alumni.
- De-duplicate: if the same person appears on multiple pages, list them once
  and pick the most informative entry; set `source_page` to that page.
- If you are unsure whether someone belongs to the office, OMIT them.
- Return STRICT JSON (no commentary). Empty `members` list is acceptable.
"""


@dataclass
class _SubPage:
    url: str
    discovered_via: str          # 'wellknown' | 'onpage' | 'sitemap'
    fetched: bool = False
    success: bool = False
    final_url: str | None = None
    body_html: str | None = None
    body_text: str | None = None
    body_sha256: str | None = None
    raw_payload_id: int | None = None


@dataclass
class IRTeamResult:
    success: bool
    ir_landing_url: str
    subpages_discovered: int
    subpages_fetched: int
    subpages_succeeded: int
    pages_visited: list[dict] = field(default_factory=list)
    office_name: str | None = None
    parent_division: str | None = None
    office_phone: str | None = None
    office_fax: str | None = None
    office_email: str | None = None
    office_address: str | None = None
    members: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    error_class: str | None = None
    error_message: str | None = None


async def extract_ir_team(
    ir_landing_url: str,
    *,
    institution_name: str,
    unitid: int,
    writer: ProvenanceWriter | None = None,
    max_subpages: int = 10,
) -> IRTeamResult:
    """Run the full multi-page IR team extraction."""
    run_id = uuid4()
    log.info("ir_team.start", unitid=unitid, ir_url=ir_landing_url)

    # 1. DISCOVER subpages from 3 channels in parallel
    landing_fetch, subpages = await _discover_subpages(ir_landing_url, max_subpages)
    if landing_fetch is None or not landing_fetch.success:
        return IRTeamResult(
            success=False, ir_landing_url=ir_landing_url,
            subpages_discovered=0, subpages_fetched=0, subpages_succeeded=0,
            error_class="LANDING_FETCH_FAILED",
            error_message=(landing_fetch.error if landing_fetch else "no fetch attempt"),
        )

    # 2. FETCH each subpage via crawl4ai (handles Cloudflare)
    if subpages:
        await _fetch_subpages(subpages, max_concurrency=3)

    # 3. PERSIST raw payloads
    if writer:
        try:
            landing_payload_id = writer.record_raw_payload(
                unitid=unitid, source_type="ir_team",
                url=landing_fetch.url, body_html=landing_fetch.html,
                body_text=landing_fetch.markdown,
                body_blob_path=None,
                body_sha256=hashlib.sha256(
                    (landing_fetch.html or landing_fetch.markdown or "").encode()
                ).hexdigest(),
                content_type="text/html",
                http_status=landing_fetch.status_code,
                fetcher=landing_fetch.fetcher, run_id=run_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ir_team.raw_landing_failed", error=str(e))
            landing_payload_id = None

        for sp in subpages:
            if sp.success and sp.body_html:
                try:
                    sp.raw_payload_id = writer.record_raw_payload(
                        unitid=unitid, source_type="ir_team",
                        url=sp.final_url or sp.url,
                        body_html=sp.body_html, body_text=sp.body_text,
                        body_blob_path=None,
                        body_sha256=sp.body_sha256 or "",
                        content_type="text/html",
                        http_status=200, fetcher="crawl4ai", run_id=run_id,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("ir_team.raw_subpage_failed", url=sp.url, error=str(e))

    # 4. AGGREGATE text for LLM
    parts: list[str] = []
    parts.append(f"--- PAGE: {landing_fetch.url} (IR landing) ---")
    parts.append(landing_fetch.markdown or visible_text(landing_fetch.html or ""))
    for sp in subpages:
        if sp.success and (sp.body_text or sp.body_html):
            parts.append(f"\n\n--- PAGE: {sp.final_url or sp.url} ({sp.discovered_via}) ---")
            parts.append(sp.body_text or visible_text(sp.body_html or ""))

    aggregated = "\n".join(parts)
    if len(aggregated) > 80_000:
        aggregated = aggregated[:80_000]

    # 5. LLM EXTRACT
    cost = 0.0
    members: list[dict] = []
    office_name: str | None = None
    parent_division: str | None = None
    office_phone = office_fax = office_email = office_address = None
    if llm.settings.has_llm:
        try:
            r = await llm.call(
                system=EXTRACTOR_SYSTEM,
                user=f"Institution: {institution_name}\n\nAGGREGATED PAGES:\n{aggregated}",
                tier="extract", max_tokens=3000, expect_json=True,
            )
            cost = r.cost_usd
            if isinstance(r.data, dict):
                office_name = r.data.get("office_name")
                parent_division = r.data.get("parent_division")
                office_phone = r.data.get("office_phone")
                office_fax = r.data.get("office_fax")
                office_email = r.data.get("office_email")
                office_address = r.data.get("office_address")
                members = r.data.get("members") or []
        except Exception as e:  # noqa: BLE001
            log.warning("ir_team.llm_failed", error=str(e))
            office_phone = office_fax = office_email = office_address = None

    # 6. PERSIST members
    if writer and members:
        try:
            writer.write_ir_contacts(
                unitid=unitid,
                members=[
                    {
                        "name": m.get("name"),
                        "title": m.get("title"),
                        "email": m.get("email"),
                        "phone": m.get("phone"),
                        "linkedin_url": m.get("linkedin_url"),
                    }
                    for m in members if m.get("name")
                ],
                source_url=ir_landing_url,
                raw_payload_id=None,  # could attribute to specific subpage
                confidence=0.8,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ir_team.write_failed", error=str(e))

    log.info(
        "ir_team.done", unitid=unitid,
        subpages_discovered=len(subpages),
        subpages_fetched=sum(1 for sp in subpages if sp.fetched),
        subpages_succeeded=sum(1 for sp in subpages if sp.success),
        members_extracted=len(members), cost_usd=round(cost, 5),
    )

    # Verify office_phone against page text near IR keywords (and web search if needed).
    from .verification import verify_office_phone as _verify_phone
    try:
        phone_verification = await _verify_phone(
            institution_name=institution_name,
            extracted_phone=office_phone,
            landing_html=landing_fetch.html or "",
        )
        if phone_verification.verified_value != office_phone:
            log.info("ir_team.phone_overridden",
                     unitid=unitid,
                     original=office_phone,
                     verified=phone_verification.verified_value,
                     reason=phone_verification.reason)
            office_phone = phone_verification.verified_value
    except Exception as e:  # noqa: BLE001
        log.warning("ir_team.phone_verify_failed", error=str(e))

    # Persist office-level contact info to ir_pages (when fields are populated)
    if writer and (office_name or office_phone or office_address or office_email):
        try:
            from ..db.engine import get_conn as _gc
            with _gc() as _conn, _conn.cursor() as _cur:
                _cur.execute(
                    """
                    UPDATE landing.ir_pages
                       SET office_name     = COALESCE(%s, office_name),
                           parent_division = COALESCE(%s, parent_division),
                           office_phone    = COALESCE(%s, office_phone),
                           office_fax      = COALESCE(%s, office_fax),
                           office_email    = COALESCE(%s, office_email),
                           office_address  = COALESCE(%s, office_address),
                           fetched_at      = NOW()
                     WHERE unitid = %s
                    """,
                    (office_name, parent_division, office_phone, office_fax,
                     office_email, office_address, unitid),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("ir_team.contact_update_failed", error=str(e))

    return IRTeamResult(
        success=True, ir_landing_url=ir_landing_url,
        subpages_discovered=len(subpages),
        subpages_fetched=sum(1 for sp in subpages if sp.fetched),
        subpages_succeeded=sum(1 for sp in subpages if sp.success),
        pages_visited=[
            {"url": sp.url, "via": sp.discovered_via, "success": sp.success}
            for sp in subpages
        ],
        office_name=office_name, parent_division=parent_division,
        office_phone=office_phone, office_fax=office_fax,
        office_email=office_email, office_address=office_address,
        members=members, cost_usd=cost,
    )


# ============================================================================
# Discovery
# ============================================================================

async def _discover_subpages(ir_landing_url: str, max_subpages: int) -> tuple[Any, list[_SubPage]]:
    """Fetch IR landing + run 3 discovery channels in parallel."""
    # Fetch landing first (we need its links)
    landing_fetch = await crawler.fetch(ir_landing_url, timeout=30.0)
    if not landing_fetch.success:
        return landing_fetch, []

    # Run 3 channels in parallel
    coros = [
        asyncio.create_task(_channel_wellknown(ir_landing_url)),
        asyncio.create_task(_channel_onpage(ir_landing_url, landing_fetch.html or "")),
        asyncio.create_task(_channel_sitemap(ir_landing_url)),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    # Merge + dedupe
    seen: set[str] = set()
    merged: list[_SubPage] = []
    seen.add(_canon(ir_landing_url))  # exclude landing itself
    for r in results:
        if isinstance(r, list):
            for sp in r:
                key = _canon(sp.url)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(sp)
    return landing_fetch, merged[:max_subpages]


def _canon(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.rstrip('/').lower() or '/'}"


async def _channel_wellknown(ir_landing_url: str) -> list[_SubPage]:
    """Try wellknown team subpaths.

    Two URL shapes to handle:
      • Subdomain root: `https://ir.mit.edu/` → probe `https://ir.mit.edu/about/`
      • Path file:      `https://a.edu/x/y/institutional-research.html`
                         → probe `https://a.edu/x/y/staff.html`, `/team.html`, etc.
                         AND `https://a.edu/x/y/staff/`, `/team/`, etc.
    """
    parsed = urlparse(ir_landing_url)
    path = parsed.path or "/"
    urls: list[str] = []

    if path.endswith("/") or "." not in path.rsplit("/", 1)[-1]:
        # Directory-like base: simple suffix append
        base = ir_landing_url.rstrip("/")
        urls.extend(base + p for p in WELLKNOWN_SUBPATHS)
    else:
        # File-like base (.html, .aspx, etc.): use parent directory
        parent_path = path.rsplit("/", 1)[0] + "/"
        parent_url = f"{parsed.scheme}://{parsed.netloc}{parent_path}"
        # Sibling .html files (e.g. .../staff.html)
        for sub in ("staff", "team", "people", "directory", "personnel",
                    "about", "about-us", "contact", "contact-us",
                    "leadership", "our-team", "members"):
            urls.append(f"{parent_url}{sub}.html")
            urls.append(f"{parent_url}{sub}/")
        # And the parent directory itself (often a department landing with team)
        urls.append(parent_url)

    # Dedupe
    out: list[_SubPage] = []
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 ClemaScraper/0.1"}) as client:
        async def _probe(u: str) -> _SubPage | None:
            try:
                resp = await client.head(u)
                if resp.status_code in (405, 501) or resp.status_code >= 400:
                    # Some servers don't support HEAD; treat 403 as worth fetching anyway
                    if resp.status_code in (401, 403, 503):
                        return _SubPage(url=u, discovered_via="wellknown")
                    if resp.status_code == 405 or resp.status_code == 501:
                        resp = await client.get(u)
                if 200 <= resp.status_code < 400:
                    return _SubPage(url=u, discovered_via="wellknown")
            except Exception:
                return None
            return None

        probes = await asyncio.gather(*[_probe(u) for u in urls], return_exceptions=True)
        for p in probes:
            if isinstance(p, _SubPage):
                out.append(p)
    return out


async def _channel_onpage(ir_landing_url: str, html: str) -> list[_SubPage]:
    """Same-domain links from the IR landing whose href/text matches team keywords."""
    if not html:
        return []
    landing_host = urlparse(ir_landing_url).netloc.lower()
    soup = BeautifulSoup(html, "lxml")
    out: list[_SubPage] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url = urljoin(ir_landing_url, href)
        host = urlparse(abs_url).netloc.lower()
        # same registrable domain (handles ir.mit.edu → ir.mit.edu/about/)
        if host != landing_host:
            continue
        text = (a.get_text(" ", strip=True) or "").lower()
        path = urlparse(abs_url).path.lower()
        if not any(k in text or k in path for k in TEAM_KEYWORDS):
            continue
        key = _canon(abs_url)
        if key in seen:
            continue
        seen.add(key)
        out.append(_SubPage(url=abs_url, discovered_via="onpage"))
    return out


async def _channel_sitemap(ir_landing_url: str) -> list[_SubPage]:
    """Fetch sitemap.xml at the IR subdomain root; filter URLs matching team kw."""
    parsed = urlparse(ir_landing_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    sitemap_urls = [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml", f"{root}/wp-sitemap.xml"]
    out: list[_SubPage] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for sm in sitemap_urls:
            try:
                resp = await client.get(sm)
            except Exception:
                continue
            if not resp.is_success:
                continue
            try:
                soup = BeautifulSoup(resp.text, "xml")
            except Exception:
                soup = BeautifulSoup(resp.text, "lxml")
            for loc in soup.find_all("loc"):
                url = (loc.get_text(strip=True) or "").lower()
                if not url.startswith("http"):
                    continue
                if not any(k in url for k in TEAM_KEYWORDS):
                    continue
                if _canon(url) in seen:
                    continue
                seen.add(_canon(url))
                out.append(_SubPage(url=url, discovered_via="sitemap"))
            if out:
                break  # found a working sitemap
    return out


async def _fetch_subpages(subpages: list[_SubPage], *, max_concurrency: int) -> None:
    """Fetch each subpage via crawl4ai (handles JS + Cloudflare)."""
    sem = asyncio.Semaphore(max_concurrency)

    async def _fetch_one(sp: _SubPage) -> None:
        async with sem:
            sp.fetched = True
            try:
                f = await crawler.fetch(sp.url, timeout=30.0)
                if f.success:
                    sp.success = True
                    sp.final_url = f.url
                    sp.body_html = f.html
                    sp.body_text = f.markdown or visible_text(f.html or "")
                    sp.body_sha256 = hashlib.sha256(
                        (f.html or f.markdown or "").encode()
                    ).hexdigest()
            except Exception as e:  # noqa: BLE001
                log.warning("ir_team.subpage_fetch_failed", url=sp.url, error=str(e))

    await asyncio.gather(*[_fetch_one(sp) for sp in subpages])

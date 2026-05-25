"""URL discovery — three channels run in parallel: search, sitemap, deep crawl.

Returns a ranked candidate list. Each candidate carries provenance (which
channel found it, raw score per channel, URL-pattern bonus) so the validator
can break ties intelligently.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

import httpx
import structlog
from bs4 import BeautifulSoup

from ..connectors.base import InstitutionContext
from ..core import crawler, search
from .targets import TargetSpec, matches_negative, matches_positive

log = structlog.get_logger(__name__)


@dataclass
class Candidate:
    url: str
    sources: set[str] = field(default_factory=set)   # 'search'|'open_search'|'sitemap'|'crawl'|'wellknown'
    search_rank: int | None = None                   # lowest rank across search providers
    crawl_score: float | None = None                 # crawl4ai relevance score
    pattern_bonus: int = 0                           # URL-pattern positive matches
    wellknown_http_status: int | None = None         # 200 on wellknown probe = strong positive
    title: str = ""
    snippet: str = ""

    @property
    def composite_score(self) -> float:
        s = 0.0
        if "search" in self.sources:
            s += 2.0 + (10 - min(self.search_rank or 9, 9)) * 0.2
        if "open_search" in self.sources:
            s += 1.5 + (10 - min(self.search_rank or 9, 9)) * 0.15
        if "sitemap" in self.sources:
            s += 1.0
        if "crawl" in self.sources and self.crawl_score is not None:
            s += min(self.crawl_score, 2.0)
        # Well-known probe: 200 OK is very strong signal (precision >> recall here)
        if "wellknown" in self.sources and self.wellknown_http_status == 200:
            s += 3.0
        s += 1.0 * self.pattern_bonus
        # Cross-channel agreement bonus: multiple channels = more trust
        s += 0.5 * (len(self.sources) - 1)
        return s


def _canonicalize(url: str) -> str:
    """Normalize for dedupe: drop fragment, lowercase netloc, strip trailing slash from path."""
    p = urlparse(url)
    netloc = p.netloc.lower()
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme or "https", netloc, path, "", p.query, ""))


async def discover(
    ctx: InstitutionContext,
    target: TargetSpec,
    *,
    max_candidates: int = 15,
    per_channel_cap: int = 8,
) -> list[Candidate]:
    """Run all enabled discovery channels in parallel; return merged ranked candidates."""
    domain = ctx.registrable_domain or ctx.domain
    if not domain:
        return []

    # All channels run in parallel for full coverage. Deep crawl is kept fast
    # via small max_pages/max_depth (set in _channel_deep_crawl below) so it
    # doesn't dominate runtime — but it always runs to ensure we find the URL.
    coros: list[asyncio.Task] = []
    if target.wellknown_subdomains or target.wellknown_paths:
        coros.append(asyncio.create_task(_channel_wellknown(ctx, target)))
    if target.use_sitemap and ctx.canonical_root_url:
        coros.append(asyncio.create_task(_channel_sitemap(ctx.canonical_root_url, target, per_channel_cap)))
    if target.search_queries:
        coros.append(asyncio.create_task(_channel_search(domain, target, ctx, per_channel_cap)))
    if target.search_queries_open:
        coros.append(asyncio.create_task(_channel_search_open(target, ctx, per_channel_cap)))
    if target.use_deep_crawl and ctx.canonical_root_url:
        coros.append(asyncio.create_task(_channel_deep_crawl(ctx.canonical_root_url, target, domain, per_channel_cap)))

    results = await asyncio.gather(*coros, return_exceptions=True)

    # Merge — keyed on canonicalized URL
    by_url: dict[str, Candidate] = {}
    for r in results:
        if isinstance(r, Exception):
            log.warning("discovery.channel_failed", error=str(r))
            continue
        for cand in r:
            canon = _canonicalize(cand.url)
            if canon in by_url:
                existing = by_url[canon]
                existing.sources |= cand.sources
                if cand.search_rank is not None:
                    existing.search_rank = (
                        cand.search_rank if existing.search_rank is None
                        else min(existing.search_rank, cand.search_rank)
                    )
                if cand.crawl_score is not None:
                    existing.crawl_score = max(existing.crawl_score or 0, cand.crawl_score)
                existing.pattern_bonus = max(existing.pattern_bonus, cand.pattern_bonus)
                if not existing.title and cand.title:
                    existing.title = cand.title
                if not existing.snippet and cand.snippet:
                    existing.snippet = cand.snippet
            else:
                by_url[canon] = cand

    # Drop negative-pattern hits
    filtered = [c for c in by_url.values() if not matches_negative(c.url, target)]
    # Add pattern bonus to anything that didn't have it set yet
    for c in filtered:
        if c.pattern_bonus == 0:
            c.pattern_bonus = matches_positive(c.url, target)

    filtered.sort(key=lambda c: c.composite_score, reverse=True)
    return filtered[:max_candidates]


# ============================================================================
# Channels
# ============================================================================

async def _channel_search(
    domain: str, target: TargetSpec, ctx: InstitutionContext, per_channel_cap: int,
) -> list[Candidate]:
    """site:<domain> <query> across all configured search query templates."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for q in target.search_queries:
        try:
            rendered = q.format(name=ctx.name, city=ctx.state, state=ctx.state)
            results = await search.search(rendered, site=domain, limit=per_channel_cap)
        except Exception as e:  # noqa: BLE001
            log.warning("discovery.search.failed", query=q, error=str(e))
            continue
        for rank, r in enumerate(results):
            if r.url in seen:
                continue
            seen.add(r.url)
            out.append(Candidate(
                url=r.url, sources={"search"},
                search_rank=rank, title=r.title, snippet=r.snippet,
                pattern_bonus=matches_positive(r.url, target),
            ))
    return out[:per_channel_cap]


async def _channel_search_open(
    target: TargetSpec, ctx: InstitutionContext, per_channel_cap: int,
) -> list[Candidate]:
    """Non-site-restricted searches — useful when the IR page lives on a subdomain
    not surfaced by site: queries."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for q in target.search_queries_open:
        try:
            rendered = q.format(name=ctx.name, city=ctx.state, state=ctx.state)
            results = await search.search(rendered, limit=per_channel_cap)
        except Exception as e:  # noqa: BLE001
            log.warning("discovery.open_search.failed", query=q, error=str(e))
            continue
        for rank, r in enumerate(results):
            if r.url in seen:
                continue
            # Restrict to same registrable_domain OR an allowlisted external host
            host = urlparse(r.url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            in_domain = ctx.registrable_domain and ctx.registrable_domain in host
            in_allow  = any(allowed in host for allowed in target.allow_external_hosts)
            if not (in_domain or in_allow):
                continue
            seen.add(r.url)
            out.append(Candidate(
                url=r.url, sources={"open_search"},
                search_rank=rank, title=r.title, snippet=r.snippet,
                pattern_bonus=matches_positive(r.url, target),
            ))
    return out[:per_channel_cap]


async def _channel_sitemap(
    root_url: str, target: TargetSpec, per_channel_cap: int,
) -> list[Candidate]:
    """Fetch /sitemap.xml and /sitemap_index.xml; pick URLs matching positive patterns."""
    out: list[Candidate] = []
    seen: set[str] = set()
    sitemap_candidates = [
        f"{root_url.rstrip('/')}/sitemap.xml",
        f"{root_url.rstrip('/')}/sitemap_index.xml",
        f"{root_url.rstrip('/')}/sitemap-index.xml",
    ]
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for sm in sitemap_candidates:
            try:
                resp = await client.get(sm)
            except Exception:
                continue
            if not resp.is_success:
                continue
            urls = _extract_sitemap_urls(resp.text)
            # If this is a sitemap index, fetch the first 3 child sitemaps too
            children = [u for u in urls if u.endswith(".xml")][:3]
            for child in children:
                try:
                    c_resp = await client.get(child)
                    if c_resp.is_success:
                        urls.extend(_extract_sitemap_urls(c_resp.text))
                except Exception:
                    pass
            for url in urls:
                if not url.startswith("http"):
                    continue
                bonus = matches_positive(url, target)
                if bonus <= 0:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                out.append(Candidate(url=url, sources={"sitemap"}, pattern_bonus=bonus))
            # Found a working sitemap — stop trying alternates
            if out:
                break
    out.sort(key=lambda c: c.pattern_bonus, reverse=True)
    return out[:per_channel_cap]


def _extract_sitemap_urls(xml_text: str) -> list[str]:
    """Parse sitemap XML (sitemap or sitemap index) and return all <loc> URLs."""
    try:
        soup = BeautifulSoup(xml_text, "xml")
    except Exception:
        soup = BeautifulSoup(xml_text, "lxml")
    return [loc.get_text(strip=True) for loc in soup.find_all("loc") if loc.get_text(strip=True)]


async def _channel_wellknown(
    ctx: InstitutionContext, target: TargetSpec,
) -> list[Candidate]:
    """Probe common URL patterns (e.g. https://ir.mit.edu/, mit.edu/institutional-research).

    Uses parallel HEAD/GET. 200 OK = strong signal. Very high precision —
    institutions follow strong naming conventions for IR / factbook / CDS pages.
    """
    domain = ctx.registrable_domain or ctx.domain
    if not domain:
        return []

    # Tag each probe URL with whether it's a subdomain probe (very strong if it exists)
    urls_to_try: list[tuple[str, bool]] = []
    for sub in target.wellknown_subdomains:
        urls_to_try.append((f"https://{sub}.{domain}/", True))
    bases = [f"https://{domain}", f"https://www.{domain}"]
    if ctx.canonical_root_url:
        root_clean = ctx.canonical_root_url.rstrip("/")
        if root_clean not in bases:
            bases.append(root_clean)
    for base in bases:
        for path in target.wellknown_paths:
            urls_to_try.append((base + path, False))

    seen: set[str] = set()
    unique: list[tuple[str, bool]] = []
    for u, is_sub in urls_to_try:
        c = _canonicalize(u)
        if c not in seen:
            seen.add(c)
            unique.append((u, is_sub))

    async def _probe(url: str, is_subdomain: bool) -> Candidate | None:
        try:
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 ClemaScraper/0.1"},
            ) as client:
                resp = await client.get(url)
            final_url = str(resp.url)
            # 2xx/3xx: clear hit. Extract title/snippet.
            if 200 <= resp.status_code < 400:
                title, snippet = "", ""
                ct = resp.headers.get("content-type", "")
                if "html" in ct.lower():
                    body = resp.text[:50_000]
                    soup = BeautifulSoup(body, "lxml")
                    if soup.title and soup.title.string:
                        title = soup.title.string.strip()[:300]
                    meta_desc = soup.find("meta", attrs={"name": "description"})
                    if meta_desc and meta_desc.get("content"):
                        snippet = meta_desc["content"].strip()[:400]
                    elif soup.find("p"):
                        snippet = soup.find("p").get_text(" ", strip=True)[:400]
                return Candidate(
                    url=final_url, sources={"wellknown"},
                    wellknown_http_status=resp.status_code,
                    pattern_bonus=matches_positive(final_url, target),
                    title=title, snippet=snippet,
                )
            # 403/503 from a subdomain probe = strong signal (anti-bot protection
            # over a *named* IR/OIR subdomain rarely happens by accident).
            # Treat as candidate; extractor will use crawl4ai (Playwright) which
            # can solve Cloudflare-style challenges.
            if is_subdomain and resp.status_code in (401, 403, 503):
                return Candidate(
                    url=url, sources={"wellknown"},
                    wellknown_http_status=resp.status_code,
                    pattern_bonus=matches_positive(url, target) + 1,
                    title=f"(anti-bot {resp.status_code} — likely valid)",
                    snippet="Page exists but is behind anti-bot protection; will be fetched via browser.",
                )
        except Exception:
            return None
        return None

    results = await asyncio.gather(*[_probe(u, is_sub) for u, is_sub in unique], return_exceptions=True)
    return [r for r in results if isinstance(r, Candidate)]


async def _channel_deep_crawl(
    root_url: str, target: TargetSpec, domain: str, per_channel_cap: int,
) -> list[Candidate]:
    """Fast bounded BFS — capped at 6 pages, depth 1, 30s wall-clock.

    Designed to find the right URL when sitemap/wellknown/search miss it,
    without dominating runtime. The httpx fallback (in crawler.py) kicks in
    when crawl4ai is slow.
    """
    if not target.crawl_keywords:
        return []
    try:
        cc = await asyncio.wait_for(
            crawler.deep_crawl(
                root_url,
                keywords=list(target.crawl_keywords),
                max_pages=6,
                max_depth=1,
                allowed_domain=domain,
                timeout=25.0,
            ),
            timeout=45.0,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("discovery.deep_crawl.failed", error=str(e))
        return []
    out: list[Candidate] = []
    for c in cc[:per_channel_cap * 2]:
        out.append(Candidate(
            url=c.url, sources={"crawl"},
            crawl_score=c.score, title=c.title,
            pattern_bonus=matches_positive(c.url, target),
        ))
    return out[:per_channel_cap]

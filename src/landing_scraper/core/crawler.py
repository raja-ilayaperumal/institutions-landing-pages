"""Web fetch + crawl wrapper.

Two modes:
- fetch(url): single-page fetch → markdown + html (uses crawl4ai's AsyncWebCrawler)
- deep_crawl(root, keywords, max_pages): BFS scored crawl returning candidate URLs

Falls back to plain httpx + BeautifulSoup if crawl4ai/playwright errors out
(so the system stays useful on sites that block headless browsers).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from bs4 import BeautifulSoup

log = structlog.get_logger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "ClemaInstLandingPages/0.1 (+https://clema.ai; mailto:raja@blocksurvey.org)"
)


@dataclass
class FetchResult:
    url: str
    success: bool
    status_code: int | None
    html: str
    markdown: str
    fetcher: str  # 'crawl4ai' | 'httpx'
    error: str | None = None


@dataclass
class CrawlCandidate:
    url: str
    depth: int
    score: float
    title: str = ""


async def fetch(url: str, *, timeout: float = 30.0,
                force_browser: bool = False) -> FetchResult:
    """Fetch a single page.

    Speed-first: try httpx (~200-500ms, no browser launch). Escalate to
    crawl4ai (Playwright) ONLY when needed:
      • httpx returned 401/403/429/503 (anti-bot challenge likely)
      • Body looks like a Cloudflare/challenge interstitial
      • Body too short to be a real page (<500 chars rendered text)
      • PDF — handled by core/pdf.py, not this function
      • caller forces browser via force_browser=True
    """
    if not force_browser:
        r = await _fetch_httpx(url, timeout=min(timeout, 15.0))
        if _httpx_result_is_good(r):
            return r
        log.info("fetch.escalating_to_browser",
                 url=url, status=r.status_code,
                 body_chars=len(r.markdown or ""),
                 reason=_escalation_reason(r))

    # Escalation path: full Playwright via crawl4ai
    try:
        c4 = await _fetch_crawl4ai(url, timeout=timeout)
        if c4.success and len(c4.markdown or "") >= 300:
            return c4
        # crawl4ai returned a response but it's empty/broken — let it fall
        # through to Wayback unless the failure was a real 4xx (in which case
        # the page is genuinely gone, not blocked).
        if c4.status_code and 400 <= c4.status_code < 500:
            return c4
        log.info("fetch.escalating_to_wayback", url=url,
                 status=c4.status_code, body_chars=len(c4.markdown or ""))
    except Exception as e:  # noqa: BLE001
        log.warning("fetch.crawl4ai_failed", url=url, error=str(e))

    # Last-resort: Wayback Machine. Useful for institution sites that
    # block public crawlers or are reachable only on campus networks
    # (asir.sdsu.edu being the canonical example). The snapshot may be a
    # few months old but for landing-page metadata (team, office mission,
    # CDS links) it's normally fine.
    wb = await _fetch_wayback(url, timeout=timeout)
    if wb.success and len(wb.markdown or "") >= 300:
        return wb

    # Nothing worked — return the most informative failure we have.
    return r if not force_browser else FetchResult(
        url=url, success=False, status_code=None, html="", markdown="",
        fetcher="httpx", error="all fetchers failed (httpx, crawl4ai, wayback)",
    )


_CF_MARKERS = (
    "just a moment", "checking your browser",
    "cf-browser-verification", "challenge-platform",
    "ddos-guard", "incapsula",
)


def _httpx_result_is_good(r: "FetchResult") -> bool:
    if not r.success:
        return False
    if r.status_code in (401, 403, 429, 503):
        return False
    body = (r.markdown or "")[:5000].lower()
    if len(body) < 300:
        return False
    if any(marker in body for marker in _CF_MARKERS):
        return False
    return True


def _escalation_reason(r: "FetchResult") -> str:
    if not r.success:
        return f"httpx failed ({r.error or 'unknown'})"
    if r.status_code in (401, 403, 429, 503):
        return f"status {r.status_code}"
    body = (r.markdown or "")[:5000].lower()
    if any(marker in body for marker in _CF_MARKERS):
        return "anti-bot challenge in body"
    if len(body) < 300:
        return "body too short"
    return "unknown"


async def _fetch_crawl4ai(url: str, *, timeout: float) -> FetchResult:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    browser = BrowserConfig(
        headless=True,
        user_agent=DEFAULT_UA,
        verbose=False,
    )
    run = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=int(timeout * 1000),
        word_count_threshold=1,
    )
    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url, config=run)
        return FetchResult(
            url=result.url or url,
            success=bool(result.success),
            status_code=result.status_code if hasattr(result, "status_code") else None,
            html=result.html or "",
            markdown=(result.markdown.raw_markdown if hasattr(result.markdown, "raw_markdown") else (result.markdown or "")) or "",
            fetcher="crawl4ai",
            error=getattr(result, "error_message", None),
        )


async def _fetch_wayback(url: str, *, timeout: float) -> FetchResult:
    """Fetch the closest Wayback Machine snapshot for `url`.

    Two-step: (1) ask Wayback's availability API for the closest snapshot
    URL, (2) httpx-fetch that snapshot. Wayback's `id_` flag in the
    timestamp path strips Wayback chrome from the returned HTML, so what
    we get back is the original page as the institution served it.
    """
    try:
        async with httpx.AsyncClient(
            timeout=min(timeout, 12.0),
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_UA},
        ) as client:
            avail = await client.get(
                "https://archive.org/wayback/available",
                params={"url": url},
            )
            data = avail.json() if avail.is_success else {}
            snap = (data.get("archived_snapshots") or {}).get("closest") or {}
            if not snap.get("available") or not snap.get("url"):
                return FetchResult(
                    url=url, success=False, status_code=None,
                    html="", markdown="", fetcher="wayback",
                    error="no snapshot in wayback",
                )
            # Use `id_` modifier to get the original page bytes (no Wayback toolbar)
            snap_url = snap["url"].replace(
                f"/web/{snap['timestamp']}/",
                f"/web/{snap['timestamp']}id_/",
            )
            resp = await client.get(snap_url)
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text("\n", strip=True)
        return FetchResult(
            url=url,  # report the original URL, not the wayback URL
            success=resp.is_success,
            status_code=resp.status_code,
            html=resp.text, markdown=text,
            fetcher="wayback",
            error=None if resp.is_success else f"wayback HTTP {resp.status_code}",
        )
    except Exception as e:  # noqa: BLE001
        return FetchResult(
            url=url, success=False, status_code=None, html="", markdown="",
            fetcher="wayback", error=str(e),
        )


async def _fetch_httpx(url: str, *, timeout: float) -> FetchResult:
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_UA},
        ) as client:
            resp = await client.get(url)
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text("\n", strip=True)
        return FetchResult(
            url=str(resp.url),
            success=resp.is_success,
            status_code=resp.status_code,
            html=resp.text,
            markdown=text,
            fetcher="httpx",
            error=None if resp.is_success else f"HTTP {resp.status_code}",
        )
    except Exception as e:  # noqa: BLE001
        return FetchResult(
            url=url, success=False, status_code=None, html="", markdown="",
            fetcher="httpx", error=str(e),
        )


async def deep_crawl(
    root_url: str,
    *,
    keywords: list[str],
    max_pages: int = 20,
    max_depth: int = 2,
    allowed_domain: str | None = None,
    timeout: float = 45.0,
) -> list[CrawlCandidate]:
    """Best-first deep crawl scored by keyword relevance.

    Returns list of candidates sorted by score (highest first).
    On failure, falls back to a single-level link extraction via httpx.
    """
    try:
        return await _deep_crawl_crawl4ai(
            root_url, keywords=keywords, max_pages=max_pages,
            max_depth=max_depth, allowed_domain=allowed_domain, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("deep_crawl.crawl4ai_failed", url=root_url, error=str(e))
        return await _deep_crawl_httpx(
            root_url, keywords=keywords, max_pages=max_pages,
            allowed_domain=allowed_domain, timeout=timeout,
        )


async def _deep_crawl_crawl4ai(
    root_url: str,
    *,
    keywords: list[str],
    max_pages: int,
    max_depth: int,
    allowed_domain: str | None,
    timeout: float,
) -> list[CrawlCandidate]:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
    from crawl4ai.deep_crawling import BestFirstCrawlingStrategy
    from crawl4ai.deep_crawling.filters import (
        ContentTypeFilter,
        DomainFilter,
        FilterChain,
    )
    from crawl4ai.deep_crawling.scorers import KeywordRelevanceScorer

    filters: list[Any] = [ContentTypeFilter(allowed_types=["text/html", "application/xhtml+xml"])]
    if allowed_domain:
        filters.append(DomainFilter(allowed_domains=[allowed_domain]))
    filter_chain = FilterChain(filters)

    scorer = KeywordRelevanceScorer(keywords=keywords, weight=1.0)
    strategy = BestFirstCrawlingStrategy(
        max_depth=max_depth,
        include_external=False,
        filter_chain=filter_chain,
        url_scorer=scorer,
        max_pages=max_pages,
    )

    browser = BrowserConfig(headless=True, user_agent=DEFAULT_UA, verbose=False)
    run = CrawlerRunConfig(
        deep_crawl_strategy=strategy,
        scraping_strategy=LXMLWebScrapingStrategy(),
        cache_mode=CacheMode.BYPASS,
        stream=False,
        page_timeout=int(timeout * 1000),
        word_count_threshold=1,
    )
    candidates: list[CrawlCandidate] = []
    async with AsyncWebCrawler(config=browser) as crawler:
        results = await crawler.arun(root_url, config=run)
        if not isinstance(results, list):
            results = [results]
        for r in results:
            md = r.metadata or {}
            candidates.append(
                CrawlCandidate(
                    url=r.url,
                    depth=int(md.get("depth", 0) or 0),
                    score=float(md.get("score", 0.0) or 0.0),
                    title=(md.get("title") or "")[:200],
                )
            )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


async def _deep_crawl_httpx(
    root_url: str,
    *,
    keywords: list[str],
    max_pages: int,
    allowed_domain: str | None,
    timeout: float,
) -> list[CrawlCandidate]:
    """Fallback: fetch root, score all in-domain links by keyword match in href + anchor text."""
    candidates: list[CrawlCandidate] = []
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": DEFAULT_UA}
        ) as client:
            resp = await client.get(root_url)
        if not resp.is_success:
            return candidates
        soup = BeautifulSoup(resp.text, "lxml")
        kws = [k.lower() for k in keywords]
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
                continue
            abs_url = httpx.URL(href, base=str(resp.url))
            url_str = str(abs_url)
            if url_str in seen:
                continue
            seen.add(url_str)
            if allowed_domain and allowed_domain not in url_str:
                continue
            text = (a.get_text(" ", strip=True) or "").lower()
            url_l = url_str.lower()
            score = 0.0
            for kw in kws:
                if kw in text:
                    score += 1.0
                if kw in url_l:
                    score += 0.5
            if score > 0:
                candidates.append(
                    CrawlCandidate(url=url_str, depth=1, score=score, title=text[:200])
                )
    except Exception as e:  # noqa: BLE001
        log.warning("deep_crawl_httpx.failed", url=root_url, error=str(e))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:max_pages]

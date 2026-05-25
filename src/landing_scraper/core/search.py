"""Unified web-search interface.

Tries Google CSE → Tavily → DuckDuckGo (HTML scrape fallback).
Google CSE first because it's free (100 queries/day on the standard tier)
while Tavily costs credits per call. We fall back to the next provider
when the current one returns ZERO results, not just on exceptions —
quota-blocked Tavily often returns an error AFTER consuming a credit,
so the priority order matters even when later providers are reachable.

Returns SearchResult list. All providers are optional; the first
available one is used unless `provider` is forced.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from dataclasses import dataclass
from typing import Literal

import httpx
import structlog
from bs4 import BeautifulSoup

from ..config import settings

log = structlog.get_logger(__name__)

Provider = Literal["tavily", "google_cse", "ddg"]


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    provider: Provider
    raw_score: float | None = None


async def search(
    query: str,
    *,
    limit: int = 10,
    provider: Provider | None = None,
    site: str | None = None,
) -> list[SearchResult]:
    """Run a search query. If `site` is given, restricts to that domain."""
    q = f"site:{site} {query}" if site else query

    providers: list[Provider] = (
        [provider]
        if provider
        else _available_providers()
    )

    last_error: Exception | None = None
    for p in providers:
        try:
            if p == "tavily":
                results = await _search_tavily(q, limit)
            elif p == "google_cse":
                results = await _search_google_cse(q, limit)
            elif p == "ddg":
                results = await _search_ddg(q, limit)
            else:
                continue
            # Fall through to next provider on empty (not just on exception).
            # Tavily credits are precious — prefer a CSE hit even when Tavily
            # is reachable, and don't burn credits chasing dead queries.
            if results:
                return results
            log.info("search.empty", provider=p, query=q[:80])
        except Exception as e:  # noqa: BLE001 — try next provider
            log.warning("search.provider_failed", provider=p, error=str(e))
            last_error = e

    # All providers exhausted with nothing to return. Don't raise — the
    # caller can decide what to do with "no results" (try sitemap, well-known
    # paths, agent escalation, etc.).
    return []


def _available_providers() -> list[Provider]:
    """Priority order: Google CSE (free) → Tavily (paid credit) → DDG (free, lower quality).

    Reorder rationale: each provider returns roughly comparable results for
    institutional queries, and CSE has a free daily allowance. Putting
    Tavily second means we only spend credits on queries CSE couldn't answer
    (or when CSE quota is exhausted).
    """
    out: list[Provider] = []
    if settings.has_google_cse:
        out.append("google_cse")
    if settings.has_tavily:
        out.append("tavily")
    out.append("ddg")  # always-available last-resort
    return out


async def _search_tavily(query: str, limit: int) -> list[SearchResult]:
    from tavily import TavilyClient

    def _call() -> dict:
        client = TavilyClient(api_key=settings.tavily_api_key)
        return client.search(
            query=query,
            max_results=limit,
            search_depth="basic",
        )

    raw = await asyncio.to_thread(_call)
    return [
        SearchResult(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=r.get("content", ""),
            provider="tavily",
            raw_score=r.get("score"),
        )
        for r in raw.get("results", [])[:limit]
    ]


async def _search_google_cse(query: str, limit: int) -> list[SearchResult]:
    from googleapiclient.discovery import build

    def _call() -> dict:
        svc = build("customsearch", "v1", developerKey=settings.google_api_key)
        return svc.cse().list(q=query, cx=settings.google_cse_id, num=min(limit, 10)).execute()

    raw = await asyncio.to_thread(_call)
    return [
        SearchResult(
            title=item.get("title", ""),
            url=item.get("link", ""),
            snippet=item.get("snippet", ""),
            provider="google_cse",
        )
        for item in raw.get("items", [])[:limit]
    ]


async def _search_ddg(query: str, limit: int) -> list[SearchResult]:
    """DuckDuckGo HTML scrape fallback (no API key required)."""
    url = "https://html.duckduckgo.com/html/"
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.post(
            url,
            data={"q": query, "kl": "us-en"},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    results: list[SearchResult] = []
    for r in soup.select("div.result")[:limit]:
        a = r.select_one("a.result__a")
        snip = r.select_one(".result__snippet")
        if not a or not a.get("href"):
            continue
        raw_href = a["href"]
        # DDG wraps real urls in /l/?uddg=…&kh=…
        parsed = urllib.parse.urlparse(raw_href)
        qs = urllib.parse.parse_qs(parsed.query)
        real = qs.get("uddg", [raw_href])[0]
        results.append(
            SearchResult(
                title=a.get_text(strip=True),
                url=urllib.parse.unquote(real),
                snippet=snip.get_text(" ", strip=True) if snip else "",
                provider="ddg",
            )
        )
    return results

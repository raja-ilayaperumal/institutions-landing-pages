"""Unified web-search interface.

Provider chain (priority order):
  Serper → Tavily (multi-key) → Google CSE (off by default) → DuckDuckGo

Rationale (post-2026-05-27 CSE cost incident, see project memory):
  - **Serper.dev** is the cheapest paid search at ~$0.001/query and currently
    has credits. First in line.
  - **Tavily** costs more per credit but has multi-key round-robin; falls
    through automatically when a key hits monthly quota.
  - **Google CSE** is GATED behind `settings.enable_google_cse=True`. After
    ~9,700 queries burned ₹4,000 in one day, we refuse to call CSE unless
    explicitly opted in — and the user is expected to set a GCP-side daily
    quota cap before flipping the flag.
  - **DuckDuckGo** HTML scrape is last-resort, free, lower recall.

Fall-through rule: empty results AND exceptions both advance to the next
provider. Returns SearchResult list. All paid providers are optional.
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

Provider = Literal["serper", "tavily", "google_cse", "ddg"]


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
            if p == "serper":
                results = await _search_serper(q, limit)
            elif p == "tavily":
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
    """Priority order: Serper → Tavily → Google CSE (gated) → DDG.

    See module docstring for the rationale and the 2026-05-27 cost incident.
    """
    out: list[Provider] = []
    if settings.has_serper:
        out.append("serper")
    if settings.has_tavily:
        out.append("tavily")
    if settings.google_cse_active:  # gated by enable_google_cse flag
        out.append("google_cse")
    out.append("ddg")  # always-available last-resort
    return out


async def _search_serper(query: str, limit: int) -> list[SearchResult]:
    """Serper.dev — Google results, ~$0.001/credit. POST /search."""
    if not settings.serper_api_key:
        return []
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": settings.serper_api_key,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": min(max(limit, 1), 10)},
        )
    if r.status_code == 429:
        log.warning("search.serper_rate_limited", status=r.status_code)
        raise httpx.HTTPStatusError("serper 429", request=r.request, response=r)
    r.raise_for_status()
    data = r.json()
    out: list[SearchResult] = []
    for item in (data.get("organic") or [])[:limit]:
        out.append(SearchResult(
            title=item.get("title", "") or "",
            url=item.get("link", "") or "",
            snippet=item.get("snippet", "") or "",
            provider="serper",
            raw_score=item.get("position"),
        ))
    return out


_tavily_key_idx = 0
_tavily_quota_blocked: set[str] = set()


def _next_tavily_key() -> str | None:
    """Pick the next Tavily key that hasn't been quota-blocked this run.

    Round-robins across all populated keys. Once a key returns a quota
    error, it's skipped until the process restarts (Tavily plan limits
    reset on a daily/monthly schedule, not within a session). Returns
    None when every key is blocked — caller should fall through to the
    next provider.
    """
    global _tavily_key_idx
    keys = settings.tavily_api_keys
    if not keys:
        return None
    for _ in range(len(keys)):
        k = keys[_tavily_key_idx % len(keys)]
        _tavily_key_idx += 1
        if k not in _tavily_quota_blocked:
            return k
    return None


async def _search_tavily(query: str, limit: int) -> list[SearchResult]:
    from tavily import TavilyClient

    key = _next_tavily_key()
    if not key:
        log.warning("search.tavily_all_keys_exhausted", query=query[:80])
        return []

    def _call(api_key: str) -> dict:
        client = TavilyClient(api_key=api_key)
        return client.search(
            query=query, max_results=limit, search_depth="basic",
        )

    try:
        raw = await asyncio.to_thread(_call, key)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        # Tavily SDK raises UsageLimitExceededError / 429 on quota.
        # Mark this key blocked and let the caller move on.
        if "usage" in msg.lower() or "limit" in msg.lower() or "429" in msg:
            _tavily_quota_blocked.add(key)
            log.warning("search.tavily_key_blocked",
                        key_suffix=key[-4:], err=msg[:120])
        raise

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

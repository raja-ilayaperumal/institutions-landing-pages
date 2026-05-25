"""fetch_snippet tool — first 1500 chars of a page; auto-escalates browser."""
from __future__ import annotations

import json

from bs4 import BeautifulSoup
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ...core import crawler


class FetchSnippetArgs(BaseModel):
    url: str = Field(description="Absolute URL to fetch")


async def fetch_snippet(url: str) -> str:
    """Fetch first 1500 chars + title. Uses `crawler.fetch` which is
    httpx-first and auto-escalates to crawl4ai (Playwright) on 403/Cloudflare."""
    try:
        r = await crawler.fetch(url, timeout=20.0)
        if not r.success:
            return json.dumps({"status": r.status_code, "body": None,
                               "error": r.error or "fetch failed"})
        body = (r.markdown or "")[:1500]
        title = ""
        if r.html:
            soup = BeautifulSoup(r.html[:60_000], "lxml")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()[:200]
        return json.dumps({
            "status": r.status_code, "final_url": r.url,
            "fetcher": r.fetcher, "title": title, "body": body,
            "body_chars": len(r.markdown or ""),
        })
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


fetch_snippet_tool = StructuredTool.from_function(
    coroutine=fetch_snippet, name="fetch_snippet",
    description=(
        "Fetch first 1500 chars of a page's visible text + title. Use to "
        "verify a candidate URL is the right page before committing. "
        "Auto-escalates to a browser (~3s) when needed — gets past Cloudflare."
    ),
    args_schema=FetchSnippetArgs,
)

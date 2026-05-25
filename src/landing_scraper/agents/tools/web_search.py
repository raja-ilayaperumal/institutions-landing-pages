"""web_search tool — Tavily/Google/DDG-backed search for the agent."""
from __future__ import annotations

import json

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ...core import search


class WebSearchArgs(BaseModel):
    query: str = Field(description="Search query — use site: filters and quoted phrases when helpful")
    limit: int = Field(default=5, description="Max results (1-10)")


async def web_search(query: str, limit: int = 5) -> str:
    """Web search; returns JSON list of {url, title, snippet}."""
    try:
        results = await search.search(query, limit=min(max(limit, 1), 10))
        return json.dumps([
            {"url": r.url, "title": r.title[:200], "snippet": r.snippet[:300]}
            for r in results
        ])
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


web_search_tool = StructuredTool.from_function(
    coroutine=web_search, name="web_search",
    description=(
        "Search the web. Use site: filters (site:domain.edu, "
        "site:higheredjobs.com) and quoted phrases to narrow. "
        "Returns top results with URL, title, snippet."
    ),
    args_schema=WebSearchArgs,
)

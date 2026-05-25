"""probe_url tool — fast HEAD/GET with anti-bot awareness."""
from __future__ import annotations

import json

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class ProbeUrlArgs(BaseModel):
    url: str = Field(description="Absolute URL to probe (HEAD then GET)")


async def probe_url(url: str) -> str:
    """Quick HEAD/GET probe. 401/403/503 often means the page exists but
    is behind anti-bot — DOES NOT mean "URL is wrong". Use fetch_snippet
    to confirm via browser."""
    try:
        async with httpx.AsyncClient(
            timeout=6.0, follow_redirects=True,
            headers={"User-Agent": "ClemaScraper/0.1"},
        ) as c:
            r = await c.head(url)
            if r.status_code in (405, 501):
                r = await c.get(url)
        likely_antibot = r.status_code in (401, 403, 503)
        return json.dumps({
            "status": r.status_code, "final_url": str(r.url),
            "ok": 200 <= r.status_code < 400,
            "likely_antibot": likely_antibot,
            "hint": "anti-bot page → try fetch_snippet to confirm real content"
                    if likely_antibot else None,
        })
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e), "ok": False})


probe_url_tool = StructuredTool.from_function(
    coroutine=probe_url, name="probe_url",
    description=(
        "Quickly check if a URL exists. Returns status + likely_antibot flag. "
        "Use to test guessed URL patterns like ir.<domain>/ before fetching "
        "content. NOTE: 401/403/503 is OFTEN anti-bot, not 'wrong URL' — "
        "always escalate to fetch_snippet to confirm."
    ),
    args_schema=ProbeUrlArgs,
)

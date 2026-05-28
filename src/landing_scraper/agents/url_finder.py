"""url_finder agent — finds the exact URL of a target page for an institution.

ReAct-style agent that reasons iteratively about which tool to use next.
See agents/README.md and agents/prompts/url_finder.py for design.

Public API:
    result = await find_url(
        institution_name="MIT",
        registrable_domain="mit.edu",
        target_description="Office of Institutional Research homepage",
        max_steps=12,
    )
    # result.found_url, .confidence, .reason, .tool_calls
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import structlog
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from ..config import settings
from .prompts.url_finder import URL_FINDER_SYSTEM
from .tools import fetch_snippet_tool, probe_url_tool, web_search_tool

log = structlog.get_logger(__name__)


@dataclass
class AgentResult:
    found_url: str | None
    confidence: float
    reason: str
    tool_calls: int
    cost_usd: float = 0.0


async def find_url(
    *,
    institution_name: str,
    registrable_domain: str,
    target_description: str,
    target_examples: str = "",
    max_steps: int = 12,
    web_search_budget: int = 3,
    model: str = "gpt-4o-mini",
) -> AgentResult:
    """Find the exact URL of one target page for one institution via agentic
    reasoning. Returns AgentResult with found_url, confidence, and trace info.

    `web_search_budget` caps the number of paid web_search calls per run; once
    exceeded the agent must reason with probe_url + fetch_snippet (both free).
    `max_steps` was reduced to 8 in the 2026-05-27 cost trim but raised back
    to 12 after the NPS audit — sites with Cloudflare anti-bot return 403 to
    probe_url and the agent needs extra steps to fetch_snippet (browser) on
    each candidate to verify the page is real. The cost trim is preserved by
    `web_search_budget`, not by step count: probe_url and fetch_snippet are
    free, so extra steps cost only LLM tokens, not paid search credits."""
    if not settings.has_openai:
        return AgentResult(found_url=None, confidence=0.0,
                           reason="no OPENAI_API_KEY", tool_calls=0)

    tools = [web_search_tool, fetch_snippet_tool, probe_url_tool]
    tools_by_name = {t.name: t for t in tools}
    llm = ChatOpenAI(
        api_key=settings.openai_api_key, model=model, temperature=0,
    ).bind_tools(tools)
    web_search_used = 0

    user_prompt = (
        f"Institution: {institution_name}\n"
        f"Registrable domain: {registrable_domain}\n"
        f"Target page type: {target_description}\n"
    )
    if target_examples:
        user_prompt += f"Examples of what this page looks like:\n{target_examples}\n"

    messages = [SystemMessage(content=URL_FINDER_SYSTEM),
                HumanMessage(content=user_prompt)]
    tool_calls_count = 0

    for step in range(max_steps):
        resp = await llm.ainvoke(messages)
        messages.append(resp)

        if not resp.tool_calls:
            text = resp.content or ""
            data = _parse_final(text)
            return AgentResult(
                found_url=data.get("found_url"),
                confidence=float(data.get("confidence") or 0),
                reason=data.get("reason") or "(no reason)",
                tool_calls=tool_calls_count,
            )

        for tc in resp.tool_calls:
            name = tc["name"]
            args = tc.get("args", {}) or {}
            tool = tools_by_name.get(name)
            if tool is None:
                result = json.dumps({"error": f"unknown tool {name}"})
            elif name == "web_search" and web_search_used >= web_search_budget:
                # Hard cap: agent must finish using probe_url/fetch_snippet.
                # See url_finder cost notes in module docstring.
                result = json.dumps({
                    "error": (f"web_search budget exhausted ({web_search_budget}). "
                              "Use probe_url for URL patterns "
                              "(ir.<domain>/, <domain>/ir/, <domain>/factbook/, "
                              "<domain>/about/institutional-research/) and "
                              "fetch_snippet on candidates you already have.")
                })
            else:
                try:
                    result = await tool.coroutine(**args)
                    if name == "web_search":
                        web_search_used += 1
                except Exception as e:  # noqa: BLE001
                    result = json.dumps({"error": str(e)})
            messages.append(ToolMessage(
                content=result, tool_call_id=tc["id"], name=name,
            ))
            tool_calls_count += 1
            log.info("agent.tool", step=step + 1, name=name,
                     args=str(args)[:120])

    return AgentResult(
        found_url=None, confidence=0.0,
        reason=f"hit max_steps={max_steps} without converging",
        tool_calls=tool_calls_count,
    )


def _parse_final(text: str) -> dict:
    """Parse the final JSON answer from the agent."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}

# Agents

LLM-driven agents for hard-case URL discovery and content verification.

Unlike the rule-based `scraper/` pipeline (which always runs the same channels
in the same order), agents in this folder use LLM function-calling to decide
which tool to use NEXT based on what they've learned so far. They're slower
and more expensive per call, so they're used as **escalation** when the
deterministic pipeline can't find an answer.

## Layout

```
agents/
├── README.md                  ← this file
├── url_finder.py              ← agent that finds the exact URL of a target
│                                 page for a given institution
├── prompts/
│   └── url_finder.py          ← system prompt for url_finder
└── tools/
    ├── web_search.py          ← Tavily/Google/DDG-backed search
    ├── fetch_snippet.py       ← first 1500 chars of a page; auto-escalates
    │                              httpx → crawl4ai/Playwright on 403
    └── probe_url.py           ← HEAD/GET probe with anti-bot awareness
```

## Available agents

| Agent | Purpose | When to use |
|---|---|---|
| `url_finder` | Find the exact URL of a target page (IR landing, factbook, etc.) for one institution via iterative web search + fetch + probe | When standard pipeline returns LLM_REJECTED or fails to find a credible URL |

## How to invoke

```python
from landing_scraper.agents.url_finder import find_url

result = await find_url(
    institution_name="MIT",
    registrable_domain="mit.edu",
    target_description="Office of Institutional Research homepage",
    max_steps=12,
)
# result.found_url, result.confidence, result.reason, result.tool_calls
```

CLI:

```bash
python scripts/run_agent.py --unitid 166683 --target ir_page
```

## Adding a new agent

1. Create `agents/my_agent.py` (orchestrator)
2. Create `agents/prompts/my_agent.py` (system prompt)
3. Reuse tools from `agents/tools/` or add new ones
4. Add a row to the table above

## Adding a new tool

1. Create `agents/tools/my_tool.py` with a `StructuredTool.from_function(...)`
2. Export the tool from `agents/tools/__init__.py`
3. Reference it from any agent's `_build_tools()`

## Cost notes

- Each agent step is one LLM call (gpt-4o-mini for url_finder).
- Typical run: 2-8 steps × ~$0.0002 = ~$0.001-0.002 per URL found.
- Use sparingly; the standard pipeline handles 80%+ of cases at ~10x less cost.

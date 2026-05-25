"""LLM-as-judge validator (snippet-aware).

Given a list of ranked candidates, pick the canonical URL for the target.
For quality, we fetch the FIRST 800 chars of each top-K candidate's page
content (in parallel) and include that in the judge prompt. URL + title is
insufficient discrimination — same-title pages can be wildly different
(faculty bio vs office homepage).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import structlog
from bs4 import BeautifulSoup

from ..core import llm
from .discovery import Candidate
from .targets import TargetSpec

log = structlog.get_logger(__name__)


async def _fetch_snippet(url: str, max_chars: int = 800) -> str:
    """Fetch first chars of page body (text-only) for validator preview."""
    try:
        async with httpx.AsyncClient(
            timeout=8.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 ClemaScraper/0.1"},
        ) as client:
            r = await client.get(url)
        if not r.is_success:
            return ""
        ct = r.headers.get("content-type", "")
        if "html" not in ct.lower():
            return ""
        soup = BeautifulSoup(r.text[:80_000], "lxml")
        for el in soup(["script", "style", "nav", "header", "footer"]):
            el.decompose()
        text = soup.get_text(" ", strip=True)
        return text[:max_chars]
    except Exception:
        return ""


async def pick(
    candidates: list[Candidate],
    target: TargetSpec,
    institution_name: str,
    *,
    judge_top_k: int = 10,
) -> tuple[Candidate | None, float, str | None, float]:
    """Return (chosen_candidate, confidence, reasoning, cost_usd).

    Strategy:
      1) If no candidates → (None, 0, reason, 0)
      2) If exactly one candidate → return it with score-derived confidence
      3) Else, if LLM available → batch-judge top-K, pick highest-confidence match
      4) Else, fall back to highest composite score
    """
    if not candidates:
        return None, 0.0, "no candidates", 0.0

    if len(candidates) == 1:
        c = candidates[0]
        return c, _score_to_confidence(c.composite_score), "single candidate", 0.0

    if not llm.settings.has_llm or not target.validator_system:
        c = candidates[0]
        return c, _score_to_confidence(c.composite_score), "no llm; picked by composite score", 0.0

    top = candidates[:judge_top_k]

    # Snippet-aware: fetch first chars of each candidate's page in parallel
    # to give the validator real content, not just URL + title.
    page_snippets = await asyncio.gather(*[_fetch_snippet(c.url) for c in top])

    enumerated = "\n\n".join(
        f"CANDIDATE {i+1}:\n"
        f"  URL: {c.url}\n"
        f"  Title: {c.title or '(none)'}\n"
        f"  Search snippet: {c.snippet[:300] if c.snippet else '(none)'}\n"
        f"  Page preview (first 800 chars): {page_snippets[i] or '(could not fetch)'}\n"
        f"  Discovered via: {sorted(c.sources)}\n"
        f"  URL pattern matches: {c.pattern_bonus}"
        for i, c in enumerate(top)
    )

    system = (
        target.validator_system
        + "\n\nYou will receive N candidates. Reply with JSON: "
        '{"best_index": int (1..N or 0 if NONE match), '
        '"confidence": 0.0-1.0, "reason": str}.'
    )
    user = (
        f"Institution: {institution_name}\n"
        f"Target page type: {target.description}\n\n"
        f"{enumerated}\n\n"
        "Pick the best candidate (or 0 if none qualify)."
    )
    result = await llm.call(
        system=system, user=user,
        tier="judge", max_tokens=400, expect_json=True,
    )
    cost = result.cost_usd

    if not isinstance(result.data, dict):
        log.warning("validator.no_json", text=result.text[:200])
        c = top[0]
        return c, _score_to_confidence(c.composite_score) * 0.7, "llm returned non-JSON; fallback", cost

    idx = int(result.data.get("best_index", 0) or 0)
    confidence = float(result.data.get("confidence", 0.5) or 0.5)
    reason = result.data.get("reason", "")

    if idx == 0:
        return None, 0.0, f"LLM rejected all candidates: {reason}", cost
    if idx < 1 or idx > len(top):
        return None, 0.0, f"LLM returned out-of-range index {idx}", cost

    return top[idx - 1], confidence, reason, cost


def _score_to_confidence(score: float) -> float:
    """Squash composite_score (~0-10 range) to a 0-1 confidence."""
    # log1p flatters out; cap at 0.85 when no LLM judge ran
    import math
    return min(0.85, math.log1p(max(score, 0)) / math.log1p(10))

"""LLM-as-judge validator (snippet-aware) with verification cascade.

Picking the right URL is a TWO-step process:

  1. `pick()` — LLM judge picks the most likely candidate from top-K, using
     URL + title + search snippet + first 800 chars of page content +
     discovery channels + pattern bonus.

  2. `verify()` — fetch the chosen URL's first ~2000 chars and run a
     lightweight LLM check: "Is this really the {target} page for {inst}?"
     If not, the pick is rejected and the caller can try the next-best
     candidate (or escalate to the agent).

`pick_and_verify()` combines the two with a small retry budget: up to 3
candidates are picked-and-verified before falling back to agent escalation.
This is the "always pick the right URL" loop — the validator can be
overconfident on its first guess, and a quick content-check catches the
mismatch before the wrong URL pollutes downstream extraction.
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


VERIFY_PROMPT = """\
You are checking whether a fetched web page IS the institution's
{target_description}.

You will see:
  - The institution name and what target page type we want
  - The chosen URL
  - The first ~2000 chars of visible text from that page

Reply with JSON only:
  {{"is_correct": bool, "confidence": 0.0-1.0, "reason": str}}

Return is_correct = true ONLY when the page content clearly IS the target
page for THIS institution. Reject when:
  - The page is a different department / office than the target
  - The page is for a different institution (template / shared CSU page)
  - The page is a news article, faq, form, or login page mentioning the
    target only in passing
  - The page is empty / a placeholder / a 404 disguised as 200

Be strict — false-accepts pollute the landing page with wrong data. The
caller has more candidates to try, so a rejection is cheap; a wrong-accept
is expensive."""


async def verify(
    chosen_url: str,
    target: TargetSpec,
    institution_name: str,
    *,
    page_text: str | None = None,
) -> tuple[bool, float, str, float]:
    """Fetch `chosen_url` (or use provided page_text) and LLM-verify it
    matches the target description for the institution.

    Returns (is_correct, confidence, reason, cost_usd).
    Empty page = treated as a failure (can't verify what isn't there).
    """
    if page_text is None:
        page_text = await _fetch_snippet(chosen_url, max_chars=2000)

    if not page_text or len(page_text) < 150:
        return False, 0.0, "could not fetch page content (or page is too thin)", 0.0

    if not llm.settings.has_llm:
        # Without LLM we can only do keyword-presence heuristic against
        # target.crawl_keywords. Better than nothing for a final guard.
        kw = [k.lower() for k in (target.crawl_keywords or ()) if k]
        hits = sum(1 for k in kw if k in page_text.lower())
        if hits >= 2:
            return True, 0.6, f"{hits} target keywords present (no-LLM heuristic)", 0.0
        return False, 0.2, "no LLM and target keywords not present in page text", 0.0

    system = VERIFY_PROMPT.format(target_description=target.description)
    user = (
        f"Institution: {institution_name}\n"
        f"Target page type: {target.description}\n"
        f"Chosen URL: {chosen_url}\n\n"
        f"Page text (first ~2000 chars):\n{page_text}"
    )
    r = await llm.call(
        system=system, user=user,
        tier="judge", max_tokens=200, expect_json=True,
    )
    cost = r.cost_usd
    if not isinstance(r.data, dict):
        return False, 0.0, "verify LLM returned non-JSON", cost
    return (
        bool(r.data.get("is_correct")),
        float(r.data.get("confidence", 0.0) or 0.0),
        str(r.data.get("reason", "")),
        cost,
    )


async def pick_and_verify(
    candidates: list[Candidate],
    target: TargetSpec,
    institution_name: str,
    *,
    judge_top_k: int = 10,
    max_attempts: int = 3,
) -> tuple[Candidate | None, float, str | None, float, list[str]]:
    """Pick → verify → retry loop.

    Loops up to `max_attempts` times:
      - Run `pick()` over candidates not yet rejected.
      - Run `verify()` on the chosen URL.
      - If verify rejects: blacklist that URL, repeat.
      - If verify accepts: return it.

    Returns (chosen_candidate, confidence, reason, total_cost_usd, rejected_urls).
    The rejected_urls list is useful as anti-examples for the agent escalation.
    """
    total_cost = 0.0
    rejected: list[str] = []
    rejected_set: set[str] = set()
    last_reason = "no attempts"

    for attempt in range(max_attempts):
        pool = [c for c in candidates if c.url not in rejected_set]
        if not pool:
            return None, 0.0, f"exhausted candidates after {attempt} attempts ({last_reason})", total_cost, rejected

        chosen, conf, pick_reason, pick_cost = await pick(
            pool, target, institution_name, judge_top_k=judge_top_k,
        )
        total_cost += pick_cost
        if chosen is None:
            return None, 0.0, f"pick returned None: {pick_reason}", total_cost, rejected

        ok, ver_conf, ver_reason, ver_cost = await verify(
            chosen.url, target, institution_name,
        )
        total_cost += ver_cost
        log.info(
            "validator.pick_and_verify",
            attempt=attempt + 1, target=target.name,
            chosen=chosen.url, pick_conf=conf, ver_ok=ok, ver_conf=ver_conf,
            ver_reason=ver_reason[:120],
        )

        if ok:
            # Confidence is the joint min — both must be confident.
            return chosen, min(conf, ver_conf), f"{pick_reason} | verified: {ver_reason}", total_cost, rejected

        rejected.append(chosen.url)
        rejected_set.add(chosen.url)
        last_reason = ver_reason or "verifier rejected"

    return None, 0.0, f"3 strikes — all verified rejected. Last: {last_reason}", total_cost, rejected

"""LinkedIn URL lookup for IR contacts.

We don't scrape LinkedIn (TOS-risky and they aggressively block bots).
We use search engines (Tavily/Google) — they have public LinkedIn profile
URLs indexed. For each (name, institution) we:

  1. Search:  `"{name}" "{institution}" site:linkedin.com/in`
  2. Filter results: must be https://(www.)?linkedin.com/in/<handle>
  3. LLM-verify the top candidate matches BOTH the person's name AND the
     institution (snippet usually contains current job title + employer).
  4. Normalize the URL (strip query params, trailing slash, lowercase handle).
  5. Persist to landing.ir_contacts.linkedin_url.

Strict acceptance — better to leave linkedin_url=null than to point to the
wrong person, since this data is shared with IR teams who'll spot any mismatch.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import structlog

from ..core import llm, search
from ..db.engine import get_conn

log = structlog.get_logger(__name__)

VERIFY_PROMPT = """\
You verify which (if any) of several LinkedIn profile URLs belongs to a
specific person at a specific institution.

You'll receive a list of candidates with index, URL, and search snippet.
Reply with JSON:
{"match_index": int|null, "confidence": 0.0-1.0, "reason": str}

Pick match_index = the index of the ONE candidate that satisfies BOTH:
  - The profile is clearly for the named person (full name match, or
    common short-form like "Jonathan" → "Jon"). Reject if the name is
    different.
  - The profile mentions the institution (or its short form) as current
    or recent employer. Reject if it's a totally different employer.

If NO candidate clearly matches, return match_index = null. We never want
to attribute the wrong LinkedIn profile to someone — this data is shared
with the institution's IR team who will spot any mismatch immediately."""


@dataclass
class LinkedInResult:
    name: str
    institution: str
    linkedin_url: str | None
    confidence: float
    candidates: int
    cost_usd: float
    reason: str


def _short_institution_name(name: str) -> str:
    """Drop verbose suffixes that LinkedIn profiles rarely use verbatim.

    "North Carolina State University at Raleigh" → "North Carolina State University"
    "Georgia Institute of Technology-Main Campus" → "Georgia Institute of Technology"
    "University of California-Berkeley" → "University of California Berkeley"
    "Antioch University-New England" → "Antioch University New England"
    "Brigham Young University-Idaho" → "Brigham Young University-Idaho"  (preserve - meaning subschool)
    """
    s = name
    # Strip " at <City>" / " - Main Campus" / "—Main Campus"
    s = re.sub(r"\s+at\s+[A-Z][\w\s]+$", "", s)
    s = re.sub(r"\s*[-–—]\s*Main Campus$", "", s, flags=re.IGNORECASE)
    # Hyphenated branches: replace "-" with " " so LinkedIn snippets match
    # (LinkedIn shows "UC Berkeley" not "University of California-Berkeley")
    s = s.replace("-", " ") if " " in s and "-" in s else s
    return s.strip()


def _normalize_linkedin_url(raw: str) -> str | None:
    """Canonicalize: https://www.linkedin.com/in/<handle> with no query string."""
    if not raw:
        return None
    try:
        p = urlparse(raw)
    except Exception:
        return None
    host = p.netloc.lower()
    if host not in ("linkedin.com", "www.linkedin.com",
                    "in.linkedin.com", "uk.linkedin.com",
                    "ca.linkedin.com", "au.linkedin.com"):
        return None
    # Path must match /in/<handle> (or country-prefixed /pub/, but most are /in/)
    m = re.match(r"^/in/([^/?#]+)/?$", p.path)
    if not m:
        return None
    handle = m.group(1).lower()
    # Filter junk handles
    if not handle or len(handle) > 100 or handle in ("login", "signup"):
        return None
    return f"https://www.linkedin.com/in/{handle}"


async def find_linkedin_url(
    name: str,
    institution_name: str,
    *,
    require_llm_verify: bool = True,
) -> LinkedInResult:
    """Search engines for the person's LinkedIn profile + verify with LLM."""
    cost = 0.0
    short = _short_institution_name(institution_name)
    queries: list[str] = []
    queries.append(f'"{name}" "{short}" site:linkedin.com/in')
    if short != institution_name:
        queries.append(f'"{name}" "{institution_name}" site:linkedin.com/in')
    # Last-resort fallback uses the IR role rather than the institution
    queries.append(f'"{name}" "institutional research" site:linkedin.com/in')

    candidates: list[tuple[str, str]] = []  # (url, snippet)
    seen: set[str] = set()
    last_err: str | None = None
    for q in queries:
        try:
            results = await search.search(q, limit=5)
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            continue
        for r in results:
            norm = _normalize_linkedin_url(r.url)
            if norm and norm not in seen:
                seen.add(norm)
                candidates.append((norm, f"{r.title} || {r.snippet}"[:600]))
        if candidates:
            break  # got at least one — stop burning quota

    if not candidates:
        return LinkedInResult(
            name=name, institution=institution_name,
            linkedin_url=None, confidence=0.0, candidates=0, cost_usd=0.0,
            reason=(f"search failed: {last_err}" if last_err
                    else "no linkedin.com/in/ URLs in search results"),
        )

    # Pick top 1 (or top 3 to give LLM choices). LLM-verify.
    if not require_llm_verify or not llm.settings.has_llm:
        # No LLM available — return only if it's a high-confidence match
        # (exact-name candidate URL contains a slug derived from the name)
        for url, _ in candidates:
            handle = url.rsplit("/in/", 1)[-1].lower()
            if all(part.lower() in handle for part in name.lower().split()[:2]):
                return LinkedInResult(
                    name=name, institution=institution_name,
                    linkedin_url=url, confidence=0.6, candidates=len(candidates),
                    cost_usd=0.0, reason="slug heuristic match (no LLM)",
                )
        return LinkedInResult(
            name=name, institution=institution_name,
            linkedin_url=None, confidence=0.0, candidates=len(candidates),
            cost_usd=0.0, reason="no LLM available + slug heuristic didn't match",
        )

    # Send top 3 candidates to the LLM judge — the right profile is often
    # not the first result when the search query is broad.
    top = candidates[:3]
    blocks = "\n\n".join(
        f"[{i}] URL: {u}\n    Snippet: {s}" for i, (u, s) in enumerate(top)
    )
    user_msg = (
        f"Person: {name}\n"
        f"Institution: {institution_name}\n\n"
        f"Candidates:\n{blocks}\n"
    )
    r = await llm.call(
        system=VERIFY_PROMPT, user=user_msg,
        tier="judge", max_tokens=250, expect_json=True,
    )
    cost += r.cost_usd
    if not isinstance(r.data, dict):
        return LinkedInResult(
            name=name, institution=institution_name,
            linkedin_url=None, confidence=0.0, candidates=len(candidates),
            cost_usd=cost, reason="LLM returned non-JSON",
        )
    idx = r.data.get("match_index")
    if idx is None or not isinstance(idx, int) or not (0 <= idx < len(top)):
        return LinkedInResult(
            name=name, institution=institution_name,
            linkedin_url=None, confidence=float(r.data.get("confidence", 0)),
            candidates=len(candidates), cost_usd=cost,
            reason=r.data.get("reason", "LLM rejected all candidates"),
        )
    return LinkedInResult(
        name=name, institution=institution_name,
        linkedin_url=top[idx][0],
        confidence=float(r.data.get("confidence", 0.5)),
        candidates=len(candidates), cost_usd=cost,
        reason=r.data.get("reason", "LLM verified"),
    )


def update_contact_linkedin(contact_id: int, url: str | None,
                             confidence: float | None) -> None:
    """Persist LinkedIn URL to a specific ir_contacts row."""
    if not url:
        return
    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE landing.ir_contacts SET linkedin_url = %s
               WHERE id = %s AND linkedin_url IS NULL""",
            (url, contact_id),
        )

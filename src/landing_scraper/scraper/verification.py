"""Post-extraction verification — cross-check key fields against web search.

Today's targets:
  - office_phone: extract phones from the IR landing page's raw HTML via regex,
    cross-reference with a Tavily search for the institution's IR phone number,
    and prefer numbers that appear in BOTH the page raw text AND the search
    results.

The pattern is: trust the page over the LLM's interpretation. If the page
literally contains a phone number near "institutional research" or "contact"
text, prefer that over whatever the LLM picked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import structlog
from bs4 import BeautifulSoup

from ..core import search

log = structlog.get_logger(__name__)

# E.164/US phone formats: 256-372-8173, (256) 372-8173, 256.372.8173, +1 256 372 8173
_PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})\b"
)
_NEAR_KEYWORDS = (
    "institutional research", "office of institutional research",
    "institutional planning", "institutional effectiveness",
    "contact us", "contact ir", "ir office",
)


@dataclass
class VerificationResult:
    field: str                       # e.g. "office_phone"
    original_value: str | None
    verified_value: str | None
    source: str                      # 'page' | 'page+web' | 'original' | 'rejected'
    reason: str


def _normalize_phone(p: str) -> str:
    """Strip to 10 digits for comparison."""
    digits = re.sub(r"\D", "", p)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _format_phone(digits: str) -> str:
    if len(digits) != 10:
        return digits
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def _phones_near_keywords(html: str, *, window: int = 200) -> list[tuple[str, str]]:
    """Find phones within `window` chars of any IR-related keyword. Returns
    list of (raw_phone, surrounding_context)."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    for el in soup(["script", "style", "nav", "header", "footer", "aside"]):
        el.decompose()
    text = soup.get_text(" ", strip=True).lower()
    raw = soup.get_text(" ", strip=True)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kw in _NEAR_KEYWORDS:
        idx = 0
        while True:
            pos = text.find(kw, idx)
            if pos == -1:
                break
            start = max(0, pos - window)
            end = min(len(text), pos + len(kw) + window)
            chunk = raw[start:end]
            for m in _PHONE_RE.finditer(chunk):
                d = "".join(m.groups())
                if d not in seen and not d.startswith(("800", "888", "877", "866")):
                    seen.add(d)
                    out.append((m.group(0), chunk[max(0, m.start() - 40):m.end() + 40]))
            idx = pos + len(kw)
    return out


async def verify_office_phone(
    *,
    institution_name: str,
    extracted_phone: str | None,
    landing_html: str,
) -> VerificationResult:
    """Verify the LLM-extracted office_phone against the page text + a web search.

    Decision logic:
      1. If page text contains phones near IR-related keywords AND the LLM
         picked one of them, return that (formatted) as 'page'.
      2. If page text has IR-context phones but LLM picked a DIFFERENT
         number, override to the page-context phone with source='page'.
      3. If page has no IR-context phones but LLM has one, optionally
         web-search to verify (return 'page+web' if matches) or keep
         original (source='original') if no verification available.
      4. Else return original.
    """
    extracted_digits = _normalize_phone(extracted_phone or "")
    page_hits = _phones_near_keywords(landing_html)
    page_digits = [_normalize_phone(p) for p, _ in page_hits]

    # Case 1: LLM picked one of the page's IR-context phones — accept it
    if extracted_digits and extracted_digits in page_digits:
        return VerificationResult(
            field="office_phone",
            original_value=extracted_phone,
            verified_value=_format_phone(extracted_digits),
            source="page",
            reason="LLM phone matched page text near IR keywords",
        )

    # Case 2: Page has IR-context phone(s), LLM picked something else — override
    if page_digits:
        chosen = page_digits[0]  # closest to first IR keyword
        return VerificationResult(
            field="office_phone",
            original_value=extracted_phone,
            verified_value=_format_phone(chosen),
            source="page",
            reason=f"Overrode LLM '{extracted_phone}' with page-context phone "
                   f"found near IR keywords (context: '{page_hits[0][1][:140]}')",
        )

    # Case 3: No page-context phones — try web search to verify
    if extracted_digits and search.settings.has_tavily:
        try:
            results = await search.search(
                f'"{institution_name}" institutional research phone',
                limit=5,
            )
            search_text = " ".join(f"{r.title} {r.snippet}" for r in results)
            search_digit_set = {
                _normalize_phone(m.group(0))
                for m in _PHONE_RE.finditer(search_text)
            }
            if extracted_digits in search_digit_set:
                return VerificationResult(
                    field="office_phone",
                    original_value=extracted_phone,
                    verified_value=_format_phone(extracted_digits),
                    source="page+web",
                    reason="LLM phone confirmed by Tavily web search",
                )
            # Web disagrees — flag but keep extracted
            return VerificationResult(
                field="office_phone",
                original_value=extracted_phone,
                verified_value=extracted_phone,
                source="original",
                reason=f"Web search did not confirm extracted phone "
                       f"(web found: {sorted(search_digit_set)[:3]})",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("verify.search_failed", error=str(e))

    # Case 4: no verification available
    return VerificationResult(
        field="office_phone",
        original_value=extracted_phone,
        verified_value=extracted_phone,
        source="original",
        reason="No verification source available",
    )

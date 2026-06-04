"""About-summary connector — Wikipedia first, institution /about page as fallback.

Per user: "If wiki doesn't have it, you should get it accurate from another
source, like from their website, google search and other".

Order:
  1. Wikipedia REST search by institution name → extract
  2. If empty: probe institution's /about, /about-us, /about/index.html
  3. If still empty: Tavily search `"{name}" about overview`
  4. LLM-summarize the chosen page into 2-3 sentences

Writes to landing.wiki_summaries (extract_summary + wikipedia_url/source_url).
"""
from __future__ import annotations

import httpx
import structlog
from bs4 import BeautifulSoup

from ..core import crawler, llm, search
from ..core.html import visible_text
from ..db.engine import get_conn
from .base import BaseConnector, ConnectorResult, InstitutionContext
# Reuse the disambiguation gates so this connector (the one actually dispatched
# for source_type "wikipedia") rejects wrong-entity hits — a search for
# "Bridges Beauty College" returns the poet "Robert Seymour Bridges" as the top
# result, "Victory Career College" returns the warship "HMS Victory", etc.
from .wikipedia import (
    _is_disambiguation_or_junk, _looks_like_institution, _name_matches,
    _subject_is_institution,
)

log = structlog.get_logger(__name__)

UA = "ClemaInstLandingPages/0.1 (raja@blocksurvey.org)"

# Wikipedia extracts shorter than this (≈ one sentence) are stubs — too thin for
# a client-facing page, so we enrich them from the institution's own /about page.
MIN_RICH_SUMMARY = 200

SUMMARIZE_PROMPT = """\
Write a 2-3 sentence factual summary of the institution from this About page.
Focus on: type of institution, location, founding year if mentioned, size,
notable focus areas. No marketing fluff, no "we are committed to" phrases.

Return JSON: {"summary": str, "founded_year": int | null, "motto": str | null}"""


class AboutSummaryConnector(BaseConnector):
    source_type = "wikipedia"  # we keep writing to landing.wiki_summaries

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        cost = 0.0

        # 1. Try Wikipedia REST (gated against wrong-entity matches).
        wiki = await self._try_wikipedia(ctx)
        wiki_summary = (wiki or {}).get("summary") or ""

        # A rich Wikipedia extract is the best source — use it as-is. But many
        # smaller institutions have only a one-sentence stub ("The Wright
        # Institute is a private graduate school … in Berkeley."), which is too
        # thin for a client-facing page. Treat a short extract as "not good
        # enough" and enrich from the institution's own /about page below.
        if len(wiki_summary) >= MIN_RICH_SUMMARY:
            _persist(ctx.unitid, summary=wiki_summary,
                     wikipedia_url=wiki.get("page_url"),
                     source_url=wiki.get("page_url"))
            return self._ok(canonical_url=wiki.get("page_url"),
                            data={"source": "wikipedia", **wiki},
                            confidence=0.9, cost_usd=cost)

        # 2. Wikipedia missing or a stub → summarize the institution's /about page.
        about_url, about_text = await self._fetch_about(ctx)
        if about_text:
            r = await llm.call(
                system=SUMMARIZE_PROMPT,
                user=f"Institution: {ctx.name}\nSource: {about_url}\n\nPAGE TEXT:\n{about_text[:25_000]}",
                tier="judge", max_tokens=400, expect_json=True,
            )
            cost += r.cost_usd
            if isinstance(r.data, dict) and r.data.get("summary"):
                # Keep the (verified) Wikipedia URL as the canonical reference
                # when we had one, even though the prose came from /about.
                _persist(ctx.unitid, summary=r.data["summary"],
                         wikipedia_url=(wiki or {}).get("page_url"),
                         source_url=(wiki or {}).get("page_url") or about_url)
                return self._ok(
                    canonical_url=(wiki or {}).get("page_url") or about_url,
                    data={"source": "wikipedia+about" if wiki else "institution_about_page",
                          "summary": r.data["summary"],
                          "founded_year": r.data.get("founded_year"),
                          "url": about_url},
                    confidence=0.8 if wiki else 0.75, cost_usd=cost)

        # 3. /about failed — a thin Wikipedia stub still beats nothing.
        if wiki_summary:
            _persist(ctx.unitid, summary=wiki_summary,
                     wikipedia_url=wiki.get("page_url"),
                     source_url=wiki.get("page_url"))
            return self._ok(canonical_url=wiki.get("page_url"),
                            data={"source": "wikipedia_stub", **wiki},
                            confidence=0.85, cost_usd=cost)
        return self._err("NO_ABOUT_FOUND",
                         "no wikipedia + couldn't fetch /about pages")

    async def _try_wikipedia(self, ctx: InstitutionContext) -> dict | None:
        """Search Wikipedia and return the first candidate that passes BOTH
        gates: it reads as an educational institution AND it's THIS one. The
        top hit is frequently a same-name person/place/ship/film, so never
        trust pages[0] blindly — verify, then fall through to null (→ the
        caller tries the institution's own /about page instead)."""
        try:
            async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": UA}) as client:
                s = await client.get(
                    "https://en.wikipedia.org/w/rest.php/v1/search/page",
                    params={"q": ctx.name, "limit": 5},
                )
                pages = s.json().get("pages") or []
                for cand in pages[:5]:
                    title = cand["key"]
                    summ = await client.get(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
                    )
                    if not summ.is_success:
                        continue
                    j = summ.json()
                    extract = j.get("extract") or ""
                    desc = j.get("description") or ""
                    if not extract:
                        continue
                    # Gate 0: not a disambiguation page or dead-link stub.
                    if _is_disambiguation_or_junk(extract):
                        continue
                    # Gate 1: it's an institution (not a city/person/ship/film).
                    # Gate 2: it's THIS institution (not a same-named other).
                    if not _looks_like_institution(desc, extract):
                        continue
                    if not _name_matches(ctx.name, j.get("title") or title, extract):
                        continue
                    # Gate 3: the article's SUBJECT is the institution — not a
                    # person/film that merely mentions it (the Path-1a hole).
                    if not _subject_is_institution(ctx.name, extract):
                        continue
                    return {"summary": extract,
                            "page_url": f"https://en.wikipedia.org/wiki/{title}",
                            "wikidata_qid": j.get("wikibase_item")}
                return None
        except Exception as e:  # noqa: BLE001
            log.warning("about.wiki_failed", error=str(e))
            return None

    async def _fetch_about(self, ctx: InstitutionContext) -> tuple[str | None, str]:
        if not ctx.canonical_root_url:
            return None, ""
        base = ctx.canonical_root_url.rstrip("/")
        well_known = [
            f"{base}/about",          f"{base}/about/",
            f"{base}/about-us",       f"{base}/about-us/",
            f"{base}/about/index.html",
            f"{base}/about/who-we-are",
        ]

        async def _first_good(urls: list[str]) -> tuple[str | None, str]:
            for url in urls:
                f = await crawler.fetch(url, timeout=15.0)
                if not f.success:
                    continue
                text = f.markdown or visible_text(f.html or "")
                if len(text) < 300:  # nav/footer-only stub
                    continue
                return url, text
            return None, ""

        # Standard /about paths first — these need NO search, so the common case
        # costs zero search credits (important when the Serper budget is tight).
        url, text = await _first_good(well_known)
        if text:
            return url, text

        # Only when well-known paths miss do we spend a search to discover the
        # right /about URL.
        try:
            results = await search.search(f'"{ctx.name}" about overview', limit=4)
            discovered = [
                r.url for r in results
                if ctx.registrable_domain and ctx.registrable_domain in r.url
            ]
        except Exception:
            discovered = []
        return await _first_good(discovered)


def _persist(unitid: int, *, summary: str, wikipedia_url: str | None,
             source_url: str | None) -> None:
    sql = """
        INSERT INTO landing.wiki_summaries
          (unitid, wikipedia_url, extract_summary, source_url, parser_version)
        VALUES (%s, %s, %s, %s, '0.2.0')
        ON CONFLICT (unitid) DO UPDATE SET
          extract_summary = EXCLUDED.extract_summary,
          wikipedia_url = COALESCE(EXCLUDED.wikipedia_url, landing.wiki_summaries.wikipedia_url),
          source_url = EXCLUDED.source_url,
          fetched_at = NOW()
    """
    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql, (unitid, wikipedia_url, summary, source_url))

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

log = structlog.get_logger(__name__)

UA = "ClemaInstLandingPages/0.1 (raja@blocksurvey.org)"

SUMMARIZE_PROMPT = """\
Write a 2-3 sentence factual summary of the institution from this About page.
Focus on: type of institution, location, founding year if mentioned, size,
notable focus areas. No marketing fluff, no "we are committed to" phrases.

Return JSON: {"summary": str, "founded_year": int | null, "motto": str | null}"""


class AboutSummaryConnector(BaseConnector):
    source_type = "wikipedia"  # we keep writing to landing.wiki_summaries

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        cost = 0.0

        # 1. Try Wikipedia REST
        wiki = await self._try_wikipedia(ctx)
        if wiki and wiki.get("summary"):
            _persist(ctx.unitid,
                     summary=wiki["summary"],
                     wikipedia_url=wiki.get("page_url"),
                     source_url=wiki.get("page_url"))
            return self._ok(canonical_url=wiki.get("page_url"),
                            data={"source": "wikipedia", **wiki},
                            confidence=0.9, cost_usd=cost)

        # 2. Probe /about etc.
        about_url, about_text = await self._fetch_about(ctx)
        if not about_text:
            return self._err("NO_ABOUT_FOUND",
                             "no wikipedia + couldn't fetch /about pages")

        # 3. LLM summarize
        r = await llm.call(
            system=SUMMARIZE_PROMPT,
            user=f"Institution: {ctx.name}\nSource: {about_url}\n\nPAGE TEXT:\n{about_text[:25_000]}",
            tier="judge", max_tokens=400, expect_json=True,
        )
        cost += r.cost_usd
        if not isinstance(r.data, dict) or not r.data.get("summary"):
            return self._err("LLM_FAILED", "summarizer returned no summary")
        _persist(ctx.unitid,
                 summary=r.data["summary"],
                 wikipedia_url=None,
                 source_url=about_url)
        return self._ok(
            canonical_url=about_url,
            data={"source": "institution_about_page",
                  "summary": r.data["summary"],
                  "founded_year": r.data.get("founded_year"),
                  "url": about_url},
            confidence=0.75, cost_usd=cost,
        )

    async def _try_wikipedia(self, ctx: InstitutionContext) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=12.0, headers={"User-Agent": UA}) as client:
                s = await client.get(
                    "https://en.wikipedia.org/w/rest.php/v1/search/page",
                    params={"q": ctx.name, "limit": 3},
                )
                pages = s.json().get("pages") or []
                if not pages:
                    return None
                title = pages[0]["key"]
                page_url = f"https://en.wikipedia.org/wiki/{title}"
                summ = await client.get(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
                )
                if not summ.is_success:
                    return None
                j = summ.json()
                extract = j.get("extract") or ""
                if not extract:
                    return None
                return {"summary": extract, "page_url": page_url,
                        "wikidata_qid": j.get("wikibase_item")}
        except Exception as e:  # noqa: BLE001
            log.warning("about.wiki_failed", error=str(e))
            return None

    async def _fetch_about(self, ctx: InstitutionContext) -> tuple[str | None, str]:
        if not ctx.canonical_root_url:
            return None, ""
        base = ctx.canonical_root_url.rstrip("/")
        candidates = [
            f"{base}/about",          f"{base}/about/",
            f"{base}/about-us",       f"{base}/about-us/",
            f"{base}/about/index.html",
            f"{base}/about/who-we-are",
        ]
        # Also Tavily for "{name} about" overview pages
        try:
            results = await search.search(f'"{ctx.name}" about overview', limit=4)
            for r in results:
                if ctx.registrable_domain and ctx.registrable_domain in r.url:
                    candidates.append(r.url)
        except Exception:
            pass
        # Try each candidate
        for url in candidates:
            f = await crawler.fetch(url, timeout=15.0)
            if not f.success:
                continue
            text = f.markdown or visible_text(f.html or "")
            # Strip nav/footer-heavy pages
            if len(text) < 300:
                continue
            return url, text
        return None, ""


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

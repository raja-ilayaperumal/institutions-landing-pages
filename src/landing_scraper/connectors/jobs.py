"""Jobs discovery.

Two paths:
  1. Institution careers page crawl (look for /careers, /jobs, /employment).
  2. (Stub) HigherEdJobs feed — requires an account key; gracefully skipped.

LLM (Haiku) categorizes each title as 'ir' vs 'other'.
"""
from __future__ import annotations

from urllib.parse import urlparse

from ..core import crawler, html as html_utils, llm, search
from .base import BaseConnector, ConnectorResult, InstitutionContext

CATEGORIZE_SYSTEM = (
    "You categorize higher-ed job titles. Reply with JSON: "
    '{"items": [{"title": str, "category": "ir"|"other"}]}. '
    '"ir" = institutional research / assessment / effectiveness / analytics / '
    "data scientist / data analyst / planning / accreditation / decision support. "
    'Everything else is "other".'
)


class JobsConnector(BaseConnector):
    source_type = "jobs"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        if not ctx.domain:
            return self._err("NO_DOMAIN", "institution has no domain")

        try:
            results = await search.search("careers OR jobs OR employment", site=ctx.registrable_domain or ctx.domain, limit=5)
        except Exception as e:  # noqa: BLE001
            return self._err("SEARCH_FAILED", str(e))

        if not results:
            return self._err("NO_CAREERS_PAGE", "no careers landing found")

        # Visit top result; collect job titles from anchor text on that page
        landing = results[0].url
        f = await crawler.fetch(landing)
        if not f.success:
            return self._err("FETCH_FAILED", f.error or "")

        soup = html_utils.to_soup(f.html)
        host = urlparse(landing).netloc
        titles: list[dict] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            href = a["href"]
            if not text or len(text) < 5 or len(text) > 150:
                continue
            if any(skip in text.lower() for skip in ("about us", "contact", "home", "search", "apply now")):
                continue
            abs_url = href if href.startswith("http") else f"https://{host}{href if href.startswith('/') else '/' + href}"
            if abs_url in seen:
                continue
            seen.add(abs_url)
            titles.append({"title": text, "url": abs_url})
            if len(titles) >= 80:
                break

        if not titles:
            return self._err("NO_TITLES_PARSED", f"could not parse job titles from {landing}")

        # Categorize
        cost = 0.0
        categorized: list[dict] = []
        if llm.settings.has_anthropic and titles:
            r = await llm.call(
                system=CATEGORIZE_SYSTEM,
                user="Titles:\n" + "\n".join(f"- {t['title']}" for t in titles[:50]),
                model=llm.MODEL_JUDGE,
                max_tokens=2000,
                expect_json=True,
            )
            cost = r.cost_usd
            cat_map: dict[str, str] = {}
            if isinstance(r.data, dict):
                for item in r.data.get("items", []):
                    if isinstance(item, dict) and item.get("title"):
                        cat_map[item["title"]] = item.get("category", "other")
            categorized = [
                {**t, "category": cat_map.get(t["title"], "other")} for t in titles
            ]
        else:
            # Keyword fallback
            kws = ["institutional research", "ir analyst", "ir director", "assessment",
                   "effectiveness", "analytics", "data scientist", "data analyst",
                   "planning", "accreditation", "decision support"]
            categorized = [
                {**t, "category": "ir" if any(k in t["title"].lower() for k in kws) else "other"}
                for t in titles
            ]

        ir_jobs = [t for t in categorized if t.get("category") == "ir"]
        return self._ok(
            canonical_url=landing,
            data={
                "careers_landing": landing,
                "total_titles_parsed": len(categorized),
                "ir_jobs": ir_jobs,
                "all_jobs": categorized,
            },
            confidence=0.6 if categorized else 0.2,
            cost_usd=cost,
        )

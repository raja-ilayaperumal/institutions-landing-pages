"""Notable Alumni connector — official-first, Wikipedia-as-fallback.

Strategy (per user's source-trust ranking):
  1. OFFICIAL institutional "notable alumni" page (highest trust — the
     university verified their own alumni). Tavily search:
       site:{registrable_domain} "notable alumni"
     or:
       "{institution_name}" "notable alumni" site:.edu
  2. Wikipedia "List of {institution} alumni" or "Category:{institution}
     alumni" page — best for scale, lower trust (must verify).
  3. Main Wikipedia article "Notable alumni" section as last resort.

For each chosen source page:
  - Fetch via crawl4ai
  - LLM extract: [{name, field, graduation_year, role, source_url}]
  - Persist to landing.notable_alumni with source attribution

Quality rules:
  - Skip if no name + no source URL
  - Prefer entries with a wikipedia_url (each alumnus's own page) for linkability
  - Tag source per row (official_site / wikipedia / dbpedia) so frontend can show provenance
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

from ..core import crawler, llm, search
from ..core.html import visible_text
from ..db.engine import get_conn
from .base import BaseConnector, ConnectorResult, InstitutionContext

log = structlog.get_logger(__name__)


_STOP_WORDS = {"of", "the", "and", "at", "in", "for", "a", "an", "&"}


def _url_matches_institution(url: str, inst_name: str) -> bool:
    """Verify a Wikipedia List_of_X_alumni URL really refers to inst_name.

    Bidirectional significant-word match:
      (a) Every significant word from inst_name appears in the URL segment.
      (b) Every significant word in the URL segment appears in inst_name.

    (a) alone caught partial-match traps like:
        Martin University → /List_of_Wittenberg_University_alumni  (wrong)
    (b) catches *prefix-extension* traps where the URL refers to a DIFFERENT,
    longer-named institution that shares all of inst_name's words:
        Canada College → /List_of_Upper_Canada_College_alumni  (wrong — that
            is Upper Canada College, a Toronto prep school, not Cañada
            College in Redwood City CA).

    Both directions use the same _STOP_WORDS list so connecting words like
    "of" / "the" don't influence the decision. Wikipedia disambiguator
    parens (e.g. `_(California)`) and punctuation like "Mt." are stripped
    before tokenizing so they don't cause false rejections.
    """
    import re as _re
    # Anchor the suffix to the end of the path segment so `_alumni|_people|
    # _faculty` doesn't match against `_People_alumni` (UoPeople trap).
    m = _re.search(
        r"/(?:List_of_|Category:)(.+?)(?:_alumni|_people|_faculty)(?:$|[/?#])",
        url, _re.IGNORECASE,
    )
    if not m:
        return False
    url_seg = m.group(1)
    # Drop Wikipedia disambiguator suffix like `_(California)`, `_(New_York)`.
    url_seg = _re.sub(r"_?\([^)]*\)", "", url_seg)

    def _normalize(s: str) -> str:
        # Lowercase, remove punctuation that varies (".,-"), collapse to spaces.
        s = s.lower().replace("_", " ")
        s = _re.sub(r"[.,\-]", " ", s)
        return s

    def _significant_words(s: str) -> list[str]:
        return [w for w in _normalize(s).split()
                if w not in _STOP_WORDS and len(w) > 1]

    inst_words = _significant_words(inst_name)
    url_words = _significant_words(url_seg)
    if not inst_words or not url_words:
        return False
    return (all(w in url_words for w in inst_words)
            and all(w in inst_words for w in url_words))


@dataclass
class _Page:
    url: str
    source_tier: str          # 'official' | 'wikipedia_list' | 'wikipedia_main'
    title: str = ""
    snippet: str = ""


FIND_OFFICIAL_PROMPT = """\
You are picking the BEST official institutional "Notable Alumni" page from
search results. Accept: institution's own /alumni, /notable-alumni,
/distinguished-alumni, /about/alumni page on its .edu domain. Reject:
fundraising appeals, alumni magazines, single-person bio pages, news
articles about one alumnus.

Reply ONLY with JSON: {"best_index": int (1..N) or 0 if none, "confidence": 0-1, "reason": str}"""


EXTRACT_PROMPT = """\
Extract the institution's notable alumni from the given page text.

Return JSON: {"alumni": [{
  "name": str,                  // REQUIRED — full name
  "field": str | null,          // career field (e.g., "Politics", "Computer Science", "Athletics")
  "title_or_role": str | null,  // notable role (e.g., "US Senator", "Nobel laureate", "CEO of X")
  "graduation_year": int | null,
  "degree": str | null,         // e.g., "BS 1980", "PhD"
  "wikipedia_url": str | null,  // absolute Wikipedia link if present on the page
  "short_bio": str | null       // 1-sentence description (max 200 chars)
}]}

Rules:
- Include ONLY people EXPLICITLY listed as alumni of this institution on the page.
- Exclude: faculty (unless also alumni), honorary degree recipients (unless noted as honorary),
  current students, sports recruits.
- Each alumni record must have a name; everything else can be null.
- If the page lists 100+ alumni, return the top 30 (most well-known).
- De-duplicate by name within your response."""


class NotableAlumniConnector(BaseConnector):
    source_type = "notable_alumni"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        cost = 0.0
        all_alumni: list[dict] = []
        sources_used: list[dict] = []

        # ── Tier 1: Official institutional "notable alumni" page ─────────
        official_page = await self._find_official(ctx)
        if official_page:
            n, c = await self._extract_from_page(ctx, official_page)
            cost += c
            log.info("alumni.tier1_official", url=official_page.url,
                     extracted=len(n))
            for a in n:
                a["source_tier"] = "official"
                a["source_url"] = official_page.url
            all_alumni.extend(n)
            sources_used.append({"tier": "official", "url": official_page.url,
                                 "count": len(n)})

        # ── Tier 2: Wikipedia "List of X alumni" or Category page ────────
        if len(all_alumni) < 5:
            wiki_list_page = await self._find_wikipedia_list(ctx)
            if wiki_list_page:
                n, c = await self._extract_from_page(ctx, wiki_list_page)
                cost += c
                log.info("alumni.tier2_wikilist", url=wiki_list_page.url,
                         extracted=len(n))
                for a in n:
                    a["source_tier"] = "wikipedia_list"
                    a["source_url"] = wiki_list_page.url
                # De-dupe by name (lowercase) with already-collected
                existing = {a["name"].lower().strip() for a in all_alumni}
                added = [a for a in n if a["name"].lower().strip() not in existing]
                all_alumni.extend(added)
                sources_used.append({"tier": "wikipedia_list",
                                     "url": wiki_list_page.url,
                                     "count": len(added)})

        if not all_alumni:
            return self._err("NO_ALUMNI_FOUND",
                             f"No notable alumni discovered for {ctx.name}")

        # Persist
        _persist(ctx.unitid, all_alumni)

        return self._ok(
            canonical_url=sources_used[0]["url"] if sources_used else None,
            data={"institution": ctx.name, "sources": sources_used,
                  "alumni_count": len(all_alumni),
                  "alumni_preview": all_alumni[:5]},
            confidence=0.85 if sources_used and sources_used[0]["tier"] == "official" else 0.65,
            cost_usd=cost,
        )

    # ────────────────────────────────────────────────────────────────────

    async def _find_official(self, ctx: InstitutionContext) -> _Page | None:
        domain = ctx.registrable_domain or ctx.domain
        if not domain:
            return None
        queries = [
            f'site:{domain} "notable alumni"',
            f'site:{domain} "distinguished alumni"',
            f'"{ctx.name}" "notable alumni" site:.edu',
        ]
        candidates: list[_Page] = []
        seen: set[str] = set()
        for q in queries:
            try:
                results = await search.search(q, limit=5)
            except Exception as e:  # noqa: BLE001
                log.warning("alumni.search_failed", q=q, error=str(e))
                continue
            for r in results:
                if r.url in seen:
                    continue
                # Must be on the institution's own domain (Tier 1 = official only)
                if domain not in r.url.lower():
                    continue
                seen.add(r.url)
                candidates.append(_Page(url=r.url, source_tier="official",
                                        title=r.title, snippet=r.snippet))
        if not candidates:
            return None

        # LLM picks the best (or rejects all)
        if not llm.settings.has_llm:
            return candidates[0]

        enumerated = "\n".join(
            f"{i+1}. URL: {p.url}\n   Title: {p.title}\n   Snippet: {p.snippet[:200]}"
            for i, p in enumerate(candidates[:6])
        )
        r = await llm.call(
            system=FIND_OFFICIAL_PROMPT,
            user=f"Institution: {ctx.name}\n\nCandidates:\n{enumerated}",
            tier="judge", max_tokens=200, expect_json=True,
        )
        if isinstance(r.data, dict):
            idx = int(r.data.get("best_index", 0) or 0) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]
        return None

    async def _find_wikipedia_list(self, ctx: InstitutionContext) -> _Page | None:
        # Probe well-known Wikipedia patterns (these are direct, won't mis-match)
        name_underscored = ctx.name.replace(" ", "_")
        wiki_candidates = [
            f"https://en.wikipedia.org/wiki/List_of_{name_underscored}_alumni",
            f"https://en.wikipedia.org/wiki/List_of_{name_underscored}_people",
            f"https://en.wikipedia.org/wiki/Category:{name_underscored}_alumni",
        ]
        for url in wiki_candidates:
            try:
                f = await crawler.fetch(url, timeout=15.0)
                if f.success and f.status_code and 200 <= f.status_code < 300:
                    if "does not have an article" in (f.markdown or f.html or "").lower():
                        continue
                    return _Page(url=url, source_tier="wikipedia_list",
                                 title=ctx.name + " alumni")
            except Exception:
                continue

        # Fallback: search Wikipedia — BUT verify the URL really matches THIS
        # institution. Without this guard, Tavily can return "List_of_Sam_Houston_State_alumni"
        # for "Houston Community College", "List_of_Wittenberg_University_alumni"
        # for "Martin University", etc. (partial word matches).
        try:
            results = await search.search(
                f'site:en.wikipedia.org "{ctx.name}" alumni list',
                limit=5,
            )
            for r in results:
                if not ("wikipedia.org/wiki/List_of" in r.url
                        or "wikipedia.org/wiki/Category" in r.url):
                    continue
                if not _url_matches_institution(r.url, ctx.name):
                    log.info("alumni.wiki_url_rejected",
                             url=r.url, institution=ctx.name,
                             reason="URL institution segment doesn't match all significant name words")
                    continue
                return _Page(url=r.url, source_tier="wikipedia_list",
                             title=r.title, snippet=r.snippet)
        except Exception:
            pass
        return None

    async def _extract_from_page(self, ctx: InstitutionContext, page: _Page) -> tuple[list[dict], float]:
        f = await crawler.fetch(page.url, timeout=30.0)
        if not f.success:
            log.warning("alumni.fetch_failed", url=page.url, error=f.error)
            return [], 0.0
        text = f.markdown or visible_text(f.html or "")
        if len(text) > 35_000:
            text = text[:35_000]
        if not llm.settings.has_llm:
            return [], 0.0
        r = await llm.call(
            system=EXTRACT_PROMPT,
            user=f"Institution: {ctx.name}\nSource page: {page.url}\n\nPAGE TEXT:\n{text}",
            tier="extract", max_tokens=3500, expect_json=True,
        )
        if not isinstance(r.data, dict):
            return [], r.cost_usd
        out = []
        for a in r.data.get("alumni") or []:
            name = (a.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "name": name[:200],
                "field": a.get("field"),
                "title_or_role": a.get("title_or_role"),
                "graduation_year": a.get("graduation_year"),
                "degree": a.get("degree"),
                "wikipedia_url": a.get("wikipedia_url"),
                "short_bio": (a.get("short_bio") or "")[:400] or None,
            })
        return out, r.cost_usd


def _persist(unitid: int, alumni: list[dict]) -> None:
    sql = """
        INSERT INTO landing.notable_alumni
          (unitid, name, field, short_bio, wikipedia_url, source_url,
           parser_version)
        VALUES (%s, %s, %s, %s, %s, %s, '0.1.0')
        ON CONFLICT (unitid, name) DO UPDATE SET
          field = COALESCE(EXCLUDED.field, landing.notable_alumni.field),
          short_bio = COALESCE(EXCLUDED.short_bio, landing.notable_alumni.short_bio),
          wikipedia_url = COALESCE(EXCLUDED.wikipedia_url, landing.notable_alumni.wikipedia_url),
          source_url = EXCLUDED.source_url,
          fetched_at = NOW()
    """
    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        for a in alumni:
            # Build composite bio with role + year when available
            bio_parts = []
            if a.get("title_or_role"):
                bio_parts.append(a["title_or_role"])
            if a.get("graduation_year"):
                bio_parts.append(f"({a['graduation_year']})")
            if a.get("short_bio"):
                bio_parts.append(a["short_bio"])
            bio = " — ".join(bio_parts) if bio_parts else None
            cur.execute(sql, (
                unitid, a["name"],
                a.get("field"), bio, a.get("wikipedia_url"),
                a.get("source_url"),
            ))

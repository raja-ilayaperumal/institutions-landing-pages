"""Notable Alumni connector — Wikipedia-authoritative.

Strategy:
  1. Locate Wikipedia's curated "List of {institution} alumni" /
     "List of {institution} people" / "Category:{institution} alumni" page.
     Discovery is QUOTA-FREE — direct URL probes over normalized name variants
     (IPEDS "University of California-Berkeley" → Wikipedia
     "University of California, Berkeley") plus the free Wikipedia REST search
     API. No paid Serper/Tavily call, so the connector is safe to run across
     all ~6k institutions.
  2. Extract with an LLM that is given the institution's STATE + DOMAIN and is
     instructed to return [] when the page is about a DIFFERENT same-named
     institution (e.g. Sofia University CA vs. Sofia University Bulgaria).

Why Wikipedia-only: institutional "notable alumni" pages are frequently a
single club or department roster (e.g. bases.stanford.edu, an entrepreneurship
club) — unrepresentative and below the IR-facing quality bar. Wikipedia lists
are editorially balanced and carry per-person citations. Schools with no
Wikipedia list ship no alumni section (null > unrepresentative).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
import structlog

from ..core import crawler, llm
from ..core.html import visible_text
from ..db.engine import get_conn
from .base import BaseConnector, ConnectorResult, InstitutionContext

log = structlog.get_logger(__name__)

_WIKI_UA = "ClemaLandingBot/1.0 (institutional research; contact: data@clema.ai)"

_STOP_WORDS = {"of", "the", "and", "at", "in", "for", "a", "an", "&"}

# IPEDS records branch campuses as "{System}-{Campus}" and tacks on verbose
# suffixes ("-Main Campus", " Campus Immersion") that Wikipedia never uses.
# To hit Wikipedia's canonical "List of {name} alumni" article directly —
# WITHOUT spending a paid search query — we probe a few normalized variants.
_DROP_SUFFIXES = re.compile(
    r"\s*[-,]?\s*(Main Campus|Campus Immersion|Digital Immersion|"
    r"Online|All Campuses)\s*$",
    re.IGNORECASE,
)


def _name_variants(name: str) -> list[str]:
    """Yield distinct Wikipedia-friendly spellings of an IPEDS institution name.

    Order matters: the most likely canonical form first. Examples:
        "University of California-Berkeley"
            → "University of California, Berkeley"  (Wikipedia uses a comma)
        "Texas A&M University-College Station"
            → "Texas A&M University, College Station"
        "Pennsylvania State University-Main Campus"
            → "Pennsylvania State University"       (suffix dropped)
    """
    base = _DROP_SUFFIXES.sub("", name).strip()
    variants = [base]
    # First hyphen separates system from campus on most IPEDS branch names;
    # Wikipedia writes that as ", ".
    if "-" in base:
        variants.append(base.replace("-", ", ", 1))
        variants.append(base.replace("-", " ", 1))
    # De-dupe preserving order; also keep the raw IPEDS name as a last resort.
    out: list[str] = []
    for v in (*variants, name):
        v = v.strip()
        if v and v not in out:
            out.append(v)
    return out


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
    source_tier: str          # 'wikipedia_list'
    title: str = ""
    snippet: str = ""


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
- De-duplicate by name within your response.

CRITICAL — same-name collision guard:
Many institutions share a name (e.g. "Sofia University" in California vs. "Sofia University"
in Bulgaria; "Columbia College" the California community college vs. Columbia College / Columbia
University in New York City). You are given the target institution's STATE and WEBSITE DOMAIN.
If the page clearly describes a DIFFERENT institution that merely shares the name — judged by
location (a different country or U.S. state), founding era, or the alumni themselves predating
the institution — return {"alumni": []}. It is far better to return nothing than to attribute
another school's alumni. When in doubt that the page matches the target institution, return []."""


class NotableAlumniConnector(BaseConnector):
    source_type = "notable_alumni"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        cost = 0.0
        all_alumni: list[dict] = []
        sources_used: list[dict] = []

        # ── Tier 1 (authoritative): Wikipedia "List of X alumni"/Category ─
        # Wikipedia's curated alumni lists are editorially representative
        # (academics, business, politics, arts, athletics) and carry per-
        # person citations. We deliberately make this the PRIMARY source:
        # institutional "official" pages are frequently a single club or
        # department roster (e.g. bases.stanford.edu — an entrepreneurship
        # club listing only startup founders), which is unrepresentative and
        # fails the IR-facing quality bar. The entire shipped corpus is
        # Wikipedia-sourced for exactly this reason.
        wiki_list_page = await self._find_wikipedia_list(ctx)
        if wiki_list_page:
            n, c = await self._extract_from_page(ctx, wiki_list_page)
            cost += c
            log.info("alumni.wikilist", url=wiki_list_page.url, extracted=len(n))
            for a in n:
                a["source_tier"] = "wikipedia_list"
                a["source_url"] = wiki_list_page.url
            all_alumni.extend(n)
            sources_used.append({"tier": "wikipedia_list",
                                 "url": wiki_list_page.url, "count": len(n)})

        # No official-page fallback: a non-Wikipedia source has repeatedly
        # produced club/department rosters that misrepresent the institution.
        # null > unrepresentative — schools without a Wikipedia alumni list
        # simply ship no alumni section.

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
            confidence=0.8 if sources_used else 0.0,
            cost_usd=cost,
        )

    # ────────────────────────────────────────────────────────────────────

    async def _find_wikipedia_list(self, ctx: InstitutionContext) -> _Page | None:
        """Locate the Wikipedia alumni list for this institution.

        Quota-safe by design — uses ONLY direct Wikipedia URL probes and the
        free Wikipedia REST search API. No paid Serper/Tavily call, so this is
        safe to run across all 6k institutions. Every candidate URL is gated by
        ``_url_matches_institution`` so a different same-named school's list is
        rejected (the extractor's state/domain guard is the second line).
        """
        variants = _name_variants(ctx.name)
        async with httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": _WIKI_UA},
            follow_redirects=True,
        ) as client:
            # 1) Direct URL probes for each name variant (lightweight httpx —
            #    NO browser escalation, critical at 6k scale). Wikipedia writes
            #    branch campuses with a comma ("University of California,
            #    Berkeley"), which _name_variants reconstructs from the IPEDS
            #    hyphenated form. A missing article returns HTTP 404.
            for variant in variants:
                seg = variant.replace(" ", "_")
                for url in (
                    f"https://en.wikipedia.org/wiki/List_of_{seg}_alumni",
                    f"https://en.wikipedia.org/wiki/List_of_{seg}_people",
                    f"https://en.wikipedia.org/wiki/Category:{seg}_alumni",
                ):
                    try:
                        resp = await client.get(url)
                    except Exception:
                        continue
                    if resp.is_success:
                        return _Page(url=str(resp.url),
                                     source_tier="wikipedia_list",
                                     title=ctx.name + " alumni")

            # 2) Free Wikipedia REST search (no paid quota). Resolves redirects
            #    and punctuation we didn't guess; gated by name-word match so a
            #    DIFFERENT-named school's list can't slip in (same-NAME namesakes
            #    are caught later by the extractor's state/domain guard).
            seen: set[str] = set()
            for variant in variants:
                for q in (f"List of {variant} alumni", f"{variant} alumni"):
                    try:
                        resp = await client.get(
                            "https://en.wikipedia.org/w/rest.php/v1/search/page",
                            params={"q": q, "limit": 5},
                        )
                        if not resp.is_success:
                            continue
                    except Exception:
                        continue
                    for pg in resp.json().get("pages", []):
                        key = pg.get("key") or ""
                        if key in seen:
                            continue
                        seen.add(key)
                        if not (key.startswith("List_of_")
                                or key.startswith("Category:")):
                            continue
                        url = f"https://en.wikipedia.org/wiki/{key}"
                        if not _url_matches_institution(url, ctx.name):
                            log.info("alumni.wiki_url_rejected",
                                     url=url, institution=ctx.name)
                            continue
                        return _Page(url=url, source_tier="wikipedia_list",
                                     title=pg.get("title") or ctx.name)
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
            user=(
                f"Institution: {ctx.name}\n"
                f"State: {ctx.state or 'unknown'}\n"
                f"Website domain: {ctx.domain or 'unknown'}\n"
                f"Source page: {page.url}\n\nPAGE TEXT:\n{text}"
            ),
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

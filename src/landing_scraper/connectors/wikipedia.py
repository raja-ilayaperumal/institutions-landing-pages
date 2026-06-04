"""Wikipedia REST connector — about summary + infobox via wikidata link.

API docs: https://www.mediawiki.org/wiki/Wikimedia_REST_API
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import httpx

from ..core.html import to_soup
from .base import BaseConnector, ConnectorResult, InstitutionContext

UA = "ClemaInstLandingPages/0.1 (mailto:raja@blocksurvey.org)"

# Positive signal that a Wikipedia article is about an educational institution
# (not a city, district, river, etc.). Wikipedia search returns *something* for
# any query — e.g. "CBD College" → "CBD" (Auckland city centre), "Escondido
# Adult School" → "Escondido" (the city). Requiring one of these words in the
# article's description/extract rejects those wrong-entity matches; null is
# always better than attributing a city's text to a college.
INSTITUTION_WORDS = (
    "college", "university", "school", "institute", "institution", "seminary",
    "academy", "polytechnic", "conservatory", "campus", "education",
)
# Strong negative signal: article is primarily about a place/geographic feature.
PLACE_WORDS = (
    "is a city", "is a town", "is a village", "is a census-designated",
    "is an unincorporated", "metropolitan area", "city centre", "city center",
    "central business district", "is a neighborhood", "is a suburb",
    "is a county", "is a region",
)
# Distinctive program qualifiers. When a verbose institution name carries one of
# these and the candidate article's title does NOT, the article is a *different*
# (usually larger, namesake) school — e.g. "Santa Ana Beauty College" vs the
# article "Santa Ana College". Used only to veto the loose token-subset path.
QUALIFIER_WORDS = frozenset({
    "beauty", "barber", "barbering", "cosmetology", "hair", "esthetics",
    "aesthetics", "technical", "vocational", "career", "careers", "medical",
    "nursing", "bible", "biblical", "theological", "theology", "massage",
    "healing", "culinary", "aviation", "design", "salon", "broadcasting",
    "court", "paralegal", "dental", "chiropractic", "acupuncture",
})


def _norm(s: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for name comparison."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _name_matches(inst_name: str, title: str, extract: str) -> bool:
    """True only if the article is plausibly about THIS institution.

    Two acceptance paths:
      • the article title is close to the institution name (difflib ratio
        absorbs branch suffixes, e.g. '...College-Visalia' vs '...College'); or
      • the full institution name appears in the article's opening text — this
        catches rebrands whose article is titled differently (e.g. MTI College's
        article is 'Campus (college)' but the extract says 'formerly MTI College').
    Rejects same-name-token-but-different-entity matches (CBD College →
    'colleges in Metro Cebu', Poway Adult School → 'Palomar College')."""
    core = _norm(inst_name)
    if not core:
        return False
    # Try the full name and a de-branded parent form ("UEI College-Riverside"
    # → "UEI College", "...College-Visalia" → "...College"), since branch
    # campuses legitimately map to the parent's article. Only de-brand when the
    # parent still has ≥2 tokens, so single-token results (e.g. "BYU-Idaho" →
    # "BYU") don't collapse onto a different institution.
    forms = {core}
    parent = _norm(re.split(r"[-–,]", inst_name, maxsplit=1)[0])
    if len(parent.split()) >= 2:
        forms.add(parent)
    title_n = _norm(title)
    head = _norm(extract)[:240]
    stop = {"of", "the", "at", "and", "a", "an", "for", "in"}
    inst_list = [t for t in core.split() if t not in stop]
    title_list = [t for t in title_n.split() if t not in stop]
    inst_toks, title_toks = set(inst_list), set(title_list)

    # GLOBAL VETO — distinctive program qualifier the article drops. This tail is
    # full of vocational schools whose namesake community college shares every
    # other token AND scores a high string-similarity ratio: "Santa Ana Beauty
    # College" vs the article "Santa Ana College" (ratio 0.83). The qualifier
    # ("beauty"/"barber"/"technical"/…) is the only discriminator, so veto BEFORE
    # any acceptance path — unless the qualifier also shows up in the extract
    # (a genuine rebrand article would say "formerly … Beauty College").
    missing_q = (QUALIFIER_WORDS & inst_toks) - title_toks
    if missing_q and not any(q in head for q in missing_q):
        return False

    # Path 1a — the full institution name (or de-branded parent) appears verbatim
    # in the article's opening text. Strong signal; catches rebrands titled
    # differently (MTI College → extract "formerly MTI College").
    if any(f in head for f in forms):
        return True
    # Path 1b — string-similar title, BUT only when the title introduces no
    # distinctive token the institution lacks. A high difflib ratio over-credits
    # shared type-words ("… Coast College", "… Polytechnic College", "… Career
    # Institute"), so "Central Coast College" scores 0.8 against "Orange Coast
    # College" and "National Polytechnic College" against "Kerch Polytechnic
    # College". Requiring title ⊆ inst tokens kills those: the article may only
    # be a shorter form of the name, never a same-type *different* entity.
    if not (title_toks - inst_toks) and any(
        SequenceMatcher(None, f, title_n).ratio() >= 0.72 for f in forms
    ):
        return True
    # Path 2 — token-subset: IPEDS names are verbose ("El Camino Community
    # College District") while Wikipedia uses the short form ("El Camino
    # College"). Accept only when every significant title token is in the
    # institution name (≥2 shared) AND the leading word matches — a *reordered*
    # subset is a different entity ("…Maritime Academy" vs "Maritime State
    # University") and is rejected despite the token overlap.
    if not (title_toks and title_toks <= inst_toks and len(title_toks) >= 2):
        return False
    return inst_list[:1] == title_list[:1]


# An article can contain institution words AND the institution's name yet still
# be unusable as a summary: a Wikipedia *disambiguation* page ("Mission College
# may refer to:"), a dead-link stub ("No information available due to a broken
# link."), or a generic-concept article reached via a loose token match
# ("Community education, also known as …" for "Tri-Community Adult Education").
# These read as plausible to the name/subject gates, so veto them explicitly.
_DISAMBIG_RE = re.compile(
    r"\b(?:may|can|could|commonly)\s+refer(?:s)?\s+to\b"
    r"|refers?\s+to\s+the\s+following"
    r"|\bis\s+a\s+disambiguation\b"
    r"|no\s+information\s+available"
    r"|broken\s+link",
    re.I,
)


def _is_disambiguation_or_junk(extract: str) -> bool:
    """True if the extract is a disambiguation page or a dead-link/no-content
    stub — institution-shaped text that is not actually a summary of a school."""
    return bool(_DISAMBIG_RE.search(extract or ""))


def _subject_is_institution(inst_name: str, extract: str) -> bool:
    """True only if the article's grammatical SUBJECT is an institution.

    `_name_matches` Path-1a accepts when the institution name merely *appears* in
    the opening text — which wrongly admits articles whose subject is a person or
    creative work that mentions the school: "Raymond Pun is an academic and
    research librarian at the Alder Graduate School of Education", "Fraternity Row
    is a 1977 American drama film … at a fictional college", "Richard … Van Veen
    is an American entrepreneur". The subject (noun phrase before the first
    is/was/are/were) must itself read as an institution: it carries an
    institution-type word, or it shares a significant token with the institution's
    own name. A person/film subject shares neither → reject (caller falls back to
    the institution's own /about page). Rare false-reject (extract opens with a
    bare acronym) degrades safely to /about, never to wrong attribution."""
    head = (extract or "").strip()
    if not head:
        return False
    m = re.search(r"\b(is|was|are|were)\b", head)
    subject = _norm(head[: m.start()] if m else head[:80])
    if not subject:
        return False
    if any(w in subject for w in INSTITUTION_WORDS):
        return True
    stop = {"of", "the", "at", "and", "a", "an", "for", "in"}
    inst_toks = {t for t in _norm(inst_name).split() if t not in stop and len(t) > 3}
    subj_toks = {t for t in subject.split() if len(t) > 3}
    return bool(inst_toks & subj_toks)


def _looks_like_institution(description: str, extract: str) -> bool:
    """True if the article reads as an educational institution, not a place."""
    blob = f"{description} {extract}".lower()
    if not blob.strip():
        return False
    # A place-defining opening sentence vetoes even if "school"/"college"
    # appears later (cities mention their schools).
    if any(p in blob for p in PLACE_WORDS) and not any(
        w in (description or "").lower() for w in INSTITUTION_WORDS
    ):
        return False
    return any(w in blob for w in INSTITUTION_WORDS)


class WikipediaConnector(BaseConnector):
    source_type = "wikipedia"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": UA}) as client:
            # 1) Search for the page
            try:
                s = await client.get(
                    "https://en.wikipedia.org/w/rest.php/v1/search/page",
                    params={"q": ctx.name, "limit": 5},
                )
                s.raise_for_status()
            except Exception as e:  # noqa: BLE001
                return self._err("SEARCH_FAILED", str(e))

            pages = s.json().get("pages", [])
            if not pages:
                return self._err("NOT_FOUND", "no wikipedia page")

            # 2) Walk the top candidates; accept the FIRST that actually reads
            #    as an educational institution. Wikipedia's top hit is often the
            #    wrong entity (a city/district sharing the name), so we verify
            #    against the summary rather than trusting pages[0] blindly.
            title = None
            summary_json: dict = {}
            for cand in pages[:5]:
                cand_title = cand["key"]
                try:
                    summ = await client.get(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{cand_title}"
                    )
                    cand_summary = summ.json() if summ.is_success else {}
                except Exception:
                    continue
                desc = cand_summary.get("description") or ""
                extract = cand_summary.get("extract") or ""
                # BOTH gates: (1) it's an educational institution (not a city),
                # and (2) it's THIS institution (not another school of the same
                # name). Either failing → keep looking, then fall through to null.
                if (
                    not _is_disambiguation_or_junk(extract)
                    and _looks_like_institution(desc, extract)
                    and _name_matches(
                        ctx.name, cand_summary.get("title") or cand_title, extract
                    )
                    and _subject_is_institution(ctx.name, extract)
                ):
                    title = cand_title
                    summary_json = cand_summary
                    break

            # No candidate read as an institution → null beats wrong-entity text.
            if title is None:
                return self._err(
                    "NO_INSTITUTION_MATCH",
                    f"no wikipedia candidate looked like an institution for {ctx.name!r}",
                )

            page_url = f"https://en.wikipedia.org/wiki/{title}"

            # 3) HTML page for notable alumni extraction
            alumni: list[dict] = []
            try:
                page_html_resp = await client.get(
                    f"https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "parse", "page": title,
                        "prop": "text", "format": "json",
                        "section": None,
                    },
                )
                page_html_json = page_html_resp.json() if page_html_resp.is_success else {}
                page_html = page_html_json.get("parse", {}).get("text", {}).get("*", "")
                alumni = self._parse_notable_alumni(page_html, max_count=10)
            except Exception:
                alumni = []

            data = {
                "page_url": page_url,
                "wikidata_qid": summary_json.get("wikibase_item"),
                "short_description": summary_json.get("description"),
                "extract": summary_json.get("extract"),
                "thumbnail": (summary_json.get("thumbnail") or {}).get("source"),
                "notable_alumni": alumni,
            }
            return self._ok(canonical_url=page_url, data=data, confidence=0.95)

    def _parse_notable_alumni(self, html: str, *, max_count: int) -> list[dict]:
        """Lightweight heuristic: scan for an 'Notable alumni' / 'Alumni' section list."""
        if not html:
            return []
        soup = to_soup(html)
        out: list[dict] = []
        # Look at section headings
        for h in soup.find_all(["h2", "h3", "h4"]):
            text = h.get_text(" ", strip=True).lower()
            if "alumni" in text or "notable people" in text:
                node = h
                while node:
                    node = node.find_next_sibling()
                    if node is None or node.name in ("h2",):
                        break
                    if node.name in ("ul", "ol"):
                        for li in node.find_all("li", recursive=False):
                            a = li.find("a")
                            name = a.get_text(strip=True) if a else li.get_text(" ", strip=True).split(" – ", 1)[0]
                            short = li.get_text(" ", strip=True)
                            if not name:
                                continue
                            out.append({
                                "name": name[:200],
                                "short_bio": short[:400],
                                "wikipedia_url": ("https://en.wikipedia.org" + a["href"])
                                                  if a and a.get("href", "").startswith("/wiki/") else None,
                            })
                            if len(out) >= max_count:
                                return out
        return out

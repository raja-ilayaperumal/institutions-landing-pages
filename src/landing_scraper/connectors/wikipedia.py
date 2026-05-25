"""Wikipedia REST connector — about summary + infobox via wikidata link.

API docs: https://www.mediawiki.org/wiki/Wikimedia_REST_API
"""
from __future__ import annotations

import httpx

from ..core.html import to_soup
from .base import BaseConnector, ConnectorResult, InstitutionContext

UA = "ClemaInstLandingPages/0.1 (mailto:raja@blocksurvey.org)"


class WikipediaConnector(BaseConnector):
    source_type = "wikipedia"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": UA}) as client:
            # 1) Search for the page
            try:
                s = await client.get(
                    "https://en.wikipedia.org/w/rest.php/v1/search/page",
                    params={"q": ctx.name, "limit": 3},
                )
                s.raise_for_status()
            except Exception as e:  # noqa: BLE001
                return self._err("SEARCH_FAILED", str(e))

            pages = s.json().get("pages", [])
            if not pages:
                return self._err("NOT_FOUND", "no wikipedia page")

            title = pages[0]["key"]
            page_url = f"https://en.wikipedia.org/wiki/{title}"

            # 2) Summary endpoint
            try:
                summ = await client.get(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
                )
                summary_json = summ.json() if summ.is_success else {}
            except Exception:
                summary_json = {}

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

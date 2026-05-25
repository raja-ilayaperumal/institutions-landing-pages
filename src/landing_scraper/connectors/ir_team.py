"""IR team-member extraction from the IR landing page (+1 hop).

Workflow:
  1. Take the IR landing URL (from IRPageConnector's canonical_url if available;
     otherwise re-search).
  2. Fetch IR landing page (crawl4ai).
  3. Look for a 'staff' / 'team' / 'people' sub-link; fetch that too.
  4. Use LLM (Sonnet) to extract [{name, title, email, phone}] from the combined text.
  5. Fall back to regex-based email/phone extraction if no LLM key.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from ..core import crawler, html as html_utils, llm
from .base import BaseConnector, ConnectorResult, InstitutionContext

TEAM_KEYWORDS = ["staff", "team", "people", "directory", "personnel", "director", "analyst"]

EXTRACT_SYSTEM = (
    "You extract Institutional Research office staff/team members from web page text. "
    'Return ONLY a JSON object {"members": [{"name": str, "title": str|null, '
    '"email": str|null, "phone": str|null}], "office_name": str|null, "summary": str|null}. '
    "Only include people who are explicitly listed as members of an institutional "
    "research, analytics, effectiveness, or planning office. Exclude faculty bios, "
    "students, or unrelated administrative staff. If unsure, omit. "
    "If office_name is mentioned (e.g. 'Office of Institutional Research'), include it. "
    "summary is a 1-sentence description of the office, if any."
)


class IRTeamConnector(BaseConnector):
    source_type = "ir_team"

    def __init__(self, ir_landing_url: str | None = None):
        self.ir_landing_url = ir_landing_url

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        if not self.ir_landing_url:
            return self._err("NO_IR_URL", "ir_landing_url not provided")

        landing = await crawler.fetch(self.ir_landing_url)
        if not landing.success:
            return self._err("FETCH_FAILED", landing.error or "")

        # Find team-ish sub-links on the landing page (same-domain only)
        domain = urlparse(self.ir_landing_url).netloc
        links = html_utils.absolute_links(landing.html, self.ir_landing_url, same_domain=True)
        team_links: list[str] = []
        for link in links:
            low = link.lower()
            if any(k in low for k in TEAM_KEYWORDS):
                team_links.append(link)
            if len(team_links) >= 3:
                break

        combined_text = landing.markdown or html_utils.visible_text(landing.html)
        for link in team_links:
            sub = await crawler.fetch(link)
            if sub.success:
                combined_text += "\n\n--- PAGE: " + link + " ---\n\n"
                combined_text += sub.markdown or html_utils.visible_text(sub.html)

        # Truncate for LLM cost discipline
        if len(combined_text) > 50000:
            combined_text = combined_text[:50000]

        cost = 0.0
        members: list[dict] = []
        office_name: str | None = None
        summary: str | None = None

        if llm.settings.has_anthropic:
            r = await llm.call(
                system=EXTRACT_SYSTEM,
                user=f"Institution: {ctx.name}\n\nPAGE TEXT:\n{combined_text}",
                model=llm.MODEL_EXTRACT,
                max_tokens=2000,
                expect_json=True,
            )
            cost = r.cost_usd
            if isinstance(r.data, dict):
                members = r.data.get("members", []) or []
                office_name = r.data.get("office_name")
                summary = r.data.get("summary")
        else:
            # No LLM key — emit empty members rather than creating placeholder
            # rows with "(unparsed)" names. We still surface the office
            # phone/fax/address via the ir_pages contact fields.
            phones = html_utils.extract_phones(combined_text)
            emails = html_utils.extract_emails(combined_text)
            if phones or emails:
                summary = (
                    f"Detected {len(emails)} email(s) and {len(phones)} phone "
                    f"number(s) on IR pages but no individual contacts could be "
                    f"matched without an LLM key."
                )

        return self._ok(
            canonical_url=self.ir_landing_url,
            data={
                "office_name": office_name,
                "summary": summary,
                "members": members,
                "team_pages_visited": team_links,
            },
            confidence=0.7 if members else 0.2,
            cost_usd=cost,
        )

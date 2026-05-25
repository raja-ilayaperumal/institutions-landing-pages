"""BrandFetch connector — logo, colors, fonts from brandfetch.com.

API: https://docs.brandfetch.com/reference/brand-api
Public endpoint also exists (no key) but is rate-limited.
"""
from __future__ import annotations

import httpx

from ..config import settings
from .base import BaseConnector, ConnectorResult, InstitutionContext


class BrandfetchConnector(BaseConnector):
    source_type = "brandfetch"

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        if not ctx.domain:
            return self._err("NO_DOMAIN", "institution has no domain")

        # Prefer authenticated API if key present; fall back to public endpoint
        url = f"https://api.brandfetch.io/v2/brands/{ctx.domain}"
        headers: dict[str, str] = {}
        if settings.has_brandfetch:
            headers["Authorization"] = f"Bearer {settings.brandfetch_api_key}"

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers)
        except Exception as e:  # noqa: BLE001
            return self._err("HTTP_ERROR", str(e))

        if resp.status_code == 404:
            return self._err("NOT_FOUND", f"brandfetch has no record for {ctx.domain}")
        if resp.status_code == 401 or resp.status_code == 403:
            return self._err("AUTH_REQUIRED", "brandfetch API key required for this brand")
        if not resp.is_success:
            return self._err("HTTP_ERROR", f"HTTP {resp.status_code}")

        raw = resp.json()
        data = {
            "domain": ctx.domain,
            "name": raw.get("name"),
            "description": raw.get("description"),
            "logos": [
                {"type": l.get("type"), "theme": l.get("theme"), "formats": [f.get("src") for f in l.get("formats", [])]}
                for l in raw.get("logos", [])
            ],
            "colors": raw.get("colors", []),
            "fonts": raw.get("fonts", []),
            "links": raw.get("links", []),
        }
        return self._ok(canonical_url=url, data=data, confidence=1.0)

"""Base connector ABC.

Every source implements: discover() → validate() → extract() → returns typed dict.

In this scraper-only phase, store() is NOT implemented — connectors return their
data, and the CLI assembles a single JSON blob per institution. The DB layer is
added in the next phase.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


def same_registrable_domain(url: str | None, ctx: "InstitutionContext") -> bool:
    """True if `url`'s host shares the institution's registrable domain (eTLD+1).

    Search providers treat `site:` as a soft hint, not a hard filter, so a
    `site:` query can still surface another school's PDF or a third-party page.
    Connectors that store a discovered URL as *this* institution's document must
    gate on this to avoid wrong-entity attribution (College A showing College
    B's factbook). Coarse eTLD+1 compare — lenient across subdomains
    (ir.foo.edu == foo.edu), good enough for `.edu`.
    """
    reg = (ctx.registrable_domain or ctx.domain or "").lower().lstrip(".")
    host = urlparse(url or "").netloc.lower()
    if not reg or not host:
        return False
    if host.startswith("www."):
        host = host[4:]
    return host == reg or host.endswith("." + reg)


@dataclass
class InstitutionContext:
    unitid: int
    name: str
    state: str
    webaddr: str | None  # raw IPEDS webaddr (may be dirty)
    canonical_root_url: str | None  # https-normalized
    ueis: str | None  # UEI for federal awards systems
    opeid: str | None
    domain: str | None  # full host of webaddr, e.g. 'web.mit.edu' or 'aamu.edu'
    registrable_domain: str | None = None  # eTLD+1, e.g. 'mit.edu' (for site: that spans subdomains)


@dataclass
class ConnectorResult:
    source_type: str
    success: bool
    canonical_url: str | None = None
    data: Any = None
    confidence: float | None = None
    error_class: str | None = None
    error_message: str | None = None
    cost_usd: float = 0.0
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class BaseConnector(ABC):
    """All connectors implement run(); helpers may override discover/validate/extract."""

    source_type: str = ""

    async def run(self, ctx: InstitutionContext) -> ConnectorResult:
        raise NotImplementedError

    # ------- shared error builder -------
    def _err(self, error_class: str, message: str) -> ConnectorResult:
        return ConnectorResult(
            source_type=self.source_type,
            success=False,
            error_class=error_class,
            error_message=message,
        )

    def _ok(self, *, canonical_url: str | None, data: Any, confidence: float | None = None, cost_usd: float = 0.0) -> ConnectorResult:
        return ConnectorResult(
            source_type=self.source_type,
            success=True,
            canonical_url=canonical_url,
            data=data,
            confidence=confidence,
            cost_usd=cost_usd,
        )

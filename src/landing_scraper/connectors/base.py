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

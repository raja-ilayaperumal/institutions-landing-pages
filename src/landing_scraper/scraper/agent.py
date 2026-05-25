"""DEPRECATED — moved to landing_scraper.agents.

This shim re-exports the public API for backwards compatibility. New code
should import from landing_scraper.agents directly:

    from landing_scraper.agents import find_url, AgentResult
"""
from ..agents import AgentResult, find_url  # noqa: F401

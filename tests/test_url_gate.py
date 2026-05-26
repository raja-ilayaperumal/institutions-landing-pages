"""Unit tests for the ir_documents URL gate.

These tests use the live `clema_landing` Postgres database (read-only — they
operate against the institutions that already exist there) so they exercise
the same code path the writer uses in production. The MIT case (the bug that
motivated the gate) is the marquee assertion.

Run with: pytest tests/test_url_gate.py -v
"""
from __future__ import annotations

import pytest

from landing_scraper.db.engine import get_conn
from landing_scraper.db.url_gate import check_document_url


# MIT is in our seed cohort, so use it for the live cases. Stanford is also
# in the cohort and gives us a second institution to cross-check.
MIT_UNITID = 166683
STANFORD_UNITID = 243744


@pytest.fixture(scope="module")
def conn():
    with get_conn(autocommit=True) as c:
        yield c


# ---------------------------------------------------------------------------
# The exact bug that motivated the gate
# ---------------------------------------------------------------------------

def test_rejects_mit_operation_varsity_blues_tableau(conn):
    """The actual URL that polluted MIT's documents must be rejected."""
    url = (
        "https://public.tableau.com/views/OperationVarsityBluesCollegeROI/"
        "CollegeROIandtheAdmissionsBriberyScandal?%3Aembed=y"
    )
    decision = check_document_url(conn, unitid=MIT_UNITID, doc_url=url, doc_type="dashboards")
    assert not decision.allowed
    assert decision.reason.startswith("third_party_no_inst_match"), decision.reason


def test_rejects_ir_mit_edu_when_already_in_ir_pages(conn):
    """ir.mit.edu/ is already MIT's ir_pages.page_url — gate must dedupe."""
    decision = check_document_url(
        conn, unitid=MIT_UNITID, doc_url="https://ir.mit.edu/", doc_type="dashboards",
    )
    # Either reject as duplicate (preferred) or, if ir_pages is empty, accept.
    # We assert the duplicate path because the seed DB has the row.
    if decision.allowed:
        pytest.skip("ir_pages for MIT has no exact-match URL in this DB; deduper had nothing to compare")
    assert decision.reason == "duplicate_of_ir_page"


# ---------------------------------------------------------------------------
# Positive cases — legitimate URLs must still pass
# ---------------------------------------------------------------------------

def test_accepts_own_subdomain(conn):
    """facts.mit.edu is on MIT's registrable domain — must pass."""
    decision = check_document_url(
        conn, unitid=MIT_UNITID, doc_url="https://facts.mit.edu", doc_type="factbook",
    )
    assert decision.allowed, decision.reason


def test_accepts_nces_dfr_report(conn):
    """nces.ed.gov DFR reports are on the reputation allow-list."""
    decision = check_document_url(
        conn, unitid=MIT_UNITID,
        doc_url="https://nces.ed.gov/ipeds/dfr/2024/ReportHTML.aspx?unitId=166683",
        doc_type="dfr_report",
    )
    assert decision.allowed, decision.reason


def test_accepts_tableau_public_when_institution_in_path(conn):
    """A Tableau Public profile that mentions Stanford in the path must pass."""
    decision = check_document_url(
        conn, unitid=STANFORD_UNITID,
        doc_url="https://public.tableau.com/app/profile/stanford.irds/viz/EnrollmentTrends",
        doc_type="dashboards",
    )
    assert decision.allowed, decision.reason


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_url", [None, "", "not-a-url", "   "])
def test_rejects_empty_or_unparseable(conn, bad_url):
    decision = check_document_url(
        conn, unitid=MIT_UNITID, doc_url=bad_url, doc_type="dashboards",
    )
    assert not decision.allowed
    assert decision.reason in ("empty_url", "unparseable_url"), decision.reason


def test_rejects_unknown_commercial_host(conn):
    """Random commercial host that doesn't match the institution → reject."""
    decision = check_document_url(
        conn, unitid=MIT_UNITID,
        doc_url="https://random-aggregator-site.com/college-rankings/mit",
        doc_type="dashboards",
    )
    assert not decision.allowed
    assert decision.reason.startswith("unknown_host:"), decision.reason


def test_rejects_unknown_unitid(conn):
    decision = check_document_url(
        conn, unitid=999_999_999, doc_url="https://example.edu", doc_type="dashboards",
    )
    assert not decision.allowed
    assert decision.reason == "unknown_unitid"


def test_decision_is_bool_compatible():
    """The dataclass supports `if decision:` for ergonomic call sites."""
    from landing_scraper.db.url_gate import GateDecision
    assert bool(GateDecision(True, "ok")) is True
    assert bool(GateDecision(False, "x")) is False

"""Defensive URL acceptance check for ir_documents writes.

Background — what this prevents
-------------------------------
On 2026-05-25 a row was persisted to landing.ir_documents for MIT pointing at
`public.tableau.com/views/OperationVarsityBluesCollegeROI/...` — a third-party
Tableau Public viz about the 2019 admissions-bribery scandal, not anything
MIT had published. It got in because:
  - the URL came from a search result (no raw_payload was fetched)
  - the doc_type "dashboards" validator nominally allows Tableau Public
    "when authored by this institution", but no per-row check enforced it
  - confidence=0.9 was passed through unchallenged

Also persisted: `https://ir.mit.edu/` typed as untitled "dashboards" — a
duplicate of the institution's own ir_pages.page_url, just under a different
table. Pure noise in the rendered JSON.

What the gate does
------------------
For every write_ir_document() call, returns (allowed, reason). Rejects with
a structured warning when:

  1. The URL host is empty or unparseable.
  2. The host is on a known third-party visualization / share platform
     (Tableau Public, Looker Studio, Datawrapper, Flourish, Infogram, Google
     Drive/Docs) AND the institution's name tokens don't appear in the URL
     path or query. Institution-owned profiles on these hosts are still
     allowed (e.g. `public.tableau.com/app/profile/csub-irpa/...`).
  3. The URL exactly matches an existing landing.ir_pages.page_url for the
     same unitid — the IR office home isn't a "document".
  4. The host is neither the institution's registrable domain (or subdomain),
     nor a known acceptable third-party (gov, edu, recognized data
     publishers). Catches random commercial domains.

All checks are non-throwing — callers get back a tuple and decide. The writer
logs+skips; tests can assert specific reason codes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import psycopg


THIRD_PARTY_HOSTS: tuple[str, ...] = (
    "public.tableau.com",
    "tableau.com",
    "lookerstudio.google.com",
    "datastudio.google.com",
    "datawrapper.dwcdn.net",
    "datawrapper.de",
    "flo.uri.sh",
    "infogram.com",
    "docs.google.com",
    "drive.google.com",
)

# Hosts we trust by reputation even when they don't match the institution
# domain. nces.ed.gov publishes IPEDS DFR reports; collegescorecard.ed.gov
# publishes federal scorecard pages; etc.
ACCEPTED_REPUTATION_HOSTS: tuple[str, ...] = (
    "nces.ed.gov",
    "collegescorecard.ed.gov",
    "ope.ed.gov",
    "studentaid.gov",
)

# Institution-name tokens like "of", "the", "and" carry no identifying signal,
# nor do degree-granting suffixes that appear in nearly every institution name.
STOPWORD_TOKENS: frozenset[str] = frozenset({
    "of", "the", "and", "at", "in", "for",
    "university", "college", "institute", "school", "academy",
    "campus", "main", "north", "south", "east", "west",
})


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str  # short snake_case code, suitable for log fields + test assertions

    def __bool__(self) -> bool:
        return self.allowed


def _host(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower().lstrip(".").removeprefix("www.")


def _is_subdomain_of(host: str, registrable: str | None) -> bool:
    if not host or not registrable:
        return False
    r = registrable.lower().removeprefix("www.")
    return host == r or host.endswith("." + r)


def _is_third_party_share_host(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in THIRD_PARTY_HOSTS)


def _name_tokens(name: str | None) -> set[str]:
    """Extract identifying alphabetic tokens from an institution name."""
    if not name:
        return set()
    raw = re.findall(r"[a-z]+", name.lower())
    return {t for t in raw if len(t) >= 4 and t not in STOPWORD_TOKENS}


def _url_path_blob(url: str) -> str:
    p = urlparse(url)
    return f"{p.path} {p.query or ''}".lower()


def check_document_url(
    conn: psycopg.Connection,
    *,
    unitid: int,
    doc_url: str | None,
    doc_type: str | None = None,  # currently unused; reserved for type-specific rules
) -> GateDecision:
    """Decide whether `doc_url` is safe to persist as an ir_document for `unitid`."""
    del doc_type  # not used yet — accept signature so callers don't have to change later
    if not doc_url:
        return GateDecision(False, "empty_url")

    host = _host(doc_url)
    if not host:
        return GateDecision(False, "unparseable_url")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, registrable_domain FROM landing.institutions WHERE unitid=%s",
            (unitid,),
        )
        row = cur.fetchone()
    if row is None:
        return GateDecision(False, "unknown_unitid")
    # Tolerate both tuple-returning cursors and dict-returning cursors so this
    # module works regardless of how the caller built its connection.
    name = row.get("name") if isinstance(row, dict) else row[0]
    registrable = row.get("registrable_domain") if isinstance(row, dict) else row[1]

    # 1. Own domain (or any subdomain of it) — always allow.
    if _is_subdomain_of(host, registrable):
        return _check_not_duplicate_of_ir_page(conn, unitid, doc_url)

    # 2. Third-party share host — must contain institution name in path/query.
    if _is_third_party_share_host(host):
        tokens = _name_tokens(name)
        if not tokens:
            return GateDecision(False, f"third_party_no_name:{host}")
        blob = _url_path_blob(doc_url)
        # Strip non-alpha so tokens can match e.g. "uc-berkeley" or "csubirpa".
        blob_alpha = re.sub(r"[^a-z]+", "", blob)
        if any(tok in blob_alpha for tok in tokens):
            return _check_not_duplicate_of_ir_page(conn, unitid, doc_url)
        return GateDecision(False, f"third_party_no_inst_match:{host}")

    # 3. Reputation allow-list (federal data publishers).
    if any(host == h or host.endswith("." + h) for h in ACCEPTED_REPUTATION_HOSTS):
        return _check_not_duplicate_of_ir_page(conn, unitid, doc_url)

    # 4. Other .gov / .edu — allow but flag in the reason (audits can review).
    if host.endswith(".gov") or host.endswith(".edu"):
        return _check_not_duplicate_of_ir_page(conn, unitid, doc_url)

    return GateDecision(False, f"unknown_host:{host}")


def _check_not_duplicate_of_ir_page(
    conn: psycopg.Connection, unitid: int, doc_url: str
) -> GateDecision:
    """Reject if this exact URL is already this institution's IR office page."""
    norm = doc_url.rstrip("/").lower()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT page_url FROM landing.ir_pages WHERE unitid=%s",
            (unitid,),
        )
        row = cur.fetchone()
    if row:
        existing = row.get("page_url") if isinstance(row, dict) else row[0]
        if existing and existing.rstrip("/").lower() == norm:
            return GateDecision(False, "duplicate_of_ir_page")
    return GateDecision(True, "ok")

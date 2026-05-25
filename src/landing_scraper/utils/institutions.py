"""Load institution context from the existing IPEDS Postgres DB.

This is a *read-only* helper. We hit ipeds.institutions_2024 and (for UEI) the
same row. Adds normalized URL + domain extraction.
"""
from __future__ import annotations

from urllib.parse import urlparse

import psycopg

from ..config import settings
from ..connectors.base import InstitutionContext


def _conn_str() -> str:
    pw = f":{settings.postgres_password}" if settings.postgres_password else ""
    return (
        f"host={settings.postgres_host} port={settings.postgres_port} "
        f"user={settings.postgres_user}{pw} dbname={settings.postgres_db}"
    )


def load_institution(unitid: int) -> InstitutionContext:
    sql = """
        SELECT unitid, instnm, stabbr, webaddr, ueis, opeid
        FROM ipeds.institutions_2024
        WHERE unitid = %s
    """
    with psycopg.connect(_conn_str()) as conn, conn.cursor() as cur:
        cur.execute(sql, (unitid,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"unitid {unitid} not found in ipeds.institutions_2024")
    unitid_, name, state, webaddr_raw, ueis, opeid = row
    canonical, domain = _normalize_url(webaddr_raw)
    registrable = _registrable_domain(domain)
    return InstitutionContext(
        unitid=unitid_,
        name=name,
        state=state,
        webaddr=webaddr_raw,
        canonical_root_url=canonical,
        ueis=ueis,
        opeid=opeid,
        domain=domain,
        registrable_domain=registrable,
    )


def _registrable_domain(host: str | None) -> str | None:
    """eTLD+1 heuristic for US higher-ed (.edu = single-label TLD).

    'web.mit.edu' → 'mit.edu'; 'aamu.edu' → 'aamu.edu';
    'cs.berkeley.edu' → 'berkeley.edu'. .edu institutions only.
    """
    if not host:
        return None
    parts = host.lower().split(".")
    if len(parts) < 2:
        return host
    # US .edu / .org / .com — eTLD is the last label
    return ".".join(parts[-2:])


def _normalize_url(webaddr: str | None) -> tuple[str | None, str | None]:
    if not webaddr:
        return None, None
    raw = webaddr.strip()
    if not raw:
        return None, None
    # Strip leading scheme variants
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    # Trim trailing slash variants but keep one
    parsed = urlparse(raw)
    netloc = parsed.netloc.lower()
    if not netloc:
        return None, None
    canonical = f"https://{netloc}/"
    # registrable domain (best-effort): drop www.
    if netloc.startswith("www."):
        domain = netloc[4:]
    else:
        domain = netloc
    return canonical, domain

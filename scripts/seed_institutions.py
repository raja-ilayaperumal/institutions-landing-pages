"""Seed landing.institutions from ipeds.* lookups.

Pulls one row per active postsec institution from ipeds.institutions_2024,
resolves Carnegie / control / sector / state labels via the IPEDS lookup
tables, generates SEO-friendly slugs, normalizes the web address, and flags
the 5 pilot institutions.

Idempotent: re-run safely; uses ON CONFLICT (unitid) DO UPDATE.

Usage:
    python scripts/seed_institutions.py
    python scripts/seed_institutions.py --filter-active        # only currently active
    python scripts/seed_institutions.py --csv-only              # only unitids in assets/all-institutions.csv
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import click

# Make src importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.db.lookups import (  # noqa: E402
    CARNEGIE_LEGACY_LABEL,
    carnegie_slug,
    control_slug,
    slugify,
    state_slug,
)

CSV_PATH = Path(__file__).resolve().parent.parent / "assets" / "all-institutions.csv"

PILOT_UNITIDS = [166683, 228778, 100654, 225423, 168342]


def normalize_url(webaddr: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    """Returns (raw, normalized, full_domain, registrable_domain)."""
    if not webaddr or not webaddr.strip():
        return None, None, None, None
    raw = webaddr.strip()
    candidate = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
    except Exception:
        return raw, None, None, None
    netloc = (parsed.netloc or "").lower()
    if not netloc:
        return raw, None, None, None
    normalized = f"https://{netloc}/"
    full_domain = netloc[4:] if netloc.startswith("www.") else netloc
    parts = full_domain.split(".")
    registrable = ".".join(parts[-2:]) if len(parts) >= 2 else full_domain
    return raw, normalized, full_domain, registrable


def generate_slug(name: str, unitid: int, taken: set[str]) -> str:
    base = slugify(name)
    if not base:
        base = f"institution-{unitid}"
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    taken.add(candidate)
    return candidate


_TRAILING_PAREN = re.compile(r"\s*\([^)]+\)\s*$")


def clean_name(name: str | None) -> str:
    """Strip trailing parentheticals like '(Closed)' or branch indicators."""
    if not name:
        return ""
    return _TRAILING_PAREN.sub("", name).strip()


@click.command()
@click.option("--filter-active", is_flag=True, help="only cyactive=1 institutions")
@click.option("--csv-only", is_flag=True, help="only unitids present in assets/all-institutions.csv")
@click.option("--dry-run", is_flag=True, help="print what would happen, don't write")
def main(filter_active: bool, csv_only: bool, dry_run: bool) -> None:
    csv_unitids: set[int] = set()
    if csv_only:
        with CSV_PATH.open() as f:
            csv_unitids = {int(row["unitid"]) for row in csv.DictReader(f)}
        click.echo(f"CSV filter active: {len(csv_unitids)} unitids")

    # Pull base rows from IPEDS with all lookups joined
    base_sql = """
        SELECT
            i.unitid, i.instnm, i.city, i.stabbr, i.zip, i.fips,
            i.control, c.control_name,
            i.sector, sc.sector_name,
            i.c21basic, cc.classification_name AS carnegie_label,
            i.hbcu, i.tribal, i.hospital, i.medical, i.landgrnt,
            i.webaddr, i.opeid, i.ein, i.ueis, i.cyactive,
            st.state_name, st.region, st.fips_code AS state_fips,
            -- HSI from scorecard (latest year)
            (SELECT is_hispanic_serving FROM score_card.fact_institution_yearly
              WHERE institution_id = i.unitid
              ORDER BY academic_year_id DESC LIMIT 1) AS is_hsi
        FROM ipeds.institutions_2024 i
        LEFT JOIN ipeds.control_codes  c  ON c.control_code = i.control
        LEFT JOIN ipeds.sector_codes   sc ON sc.sector_code = i.sector
        LEFT JOIN ipeds.carnegie_codes cc ON cc.c21basic    = i.c21basic
        LEFT JOIN ipeds.state_codes    st ON st.stabbr      = i.stabbr
    """
    filters = []
    if filter_active:
        filters.append("i.cyactive = 1")
    if filters:
        base_sql += " WHERE " + " AND ".join(filters)
    base_sql += " ORDER BY i.unitid"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(base_sql)
        rows = cur.fetchall()

    if csv_unitids:
        rows = [r for r in rows if r["unitid"] in csv_unitids]

    click.echo(f"Loaded {len(rows)} institutions from IPEDS")

    upsert_sql = """
        INSERT INTO landing.institutions (
            unitid, slug,
            name, city, stabbr, state_name, state_slug, zip, region, fips_code,
            control_code, control_label, control_slug,
            sector_code, sector_label,
            carnegie_basic_code, carnegie_basic_label, carnegie_basic_slug,
            is_hbcu, is_tribal, is_hospital, is_medical, is_landgrant, is_hsi,
            opeid, ein, ueis,
            webaddr_raw, webaddr_normalized, canonical_root_url, domain, registrable_domain,
            enabled, pilot_cohort, updated_at
        ) VALUES (
            %(unitid)s, %(slug)s,
            %(name)s, %(city)s, %(stabbr)s, %(state_name)s, %(state_slug)s, %(zip)s, %(region)s, %(fips_code)s,
            %(control_code)s, %(control_label)s, %(control_slug)s,
            %(sector_code)s, %(sector_label)s,
            %(carnegie_basic_code)s, %(carnegie_basic_label)s, %(carnegie_basic_slug)s,
            %(is_hbcu)s, %(is_tribal)s, %(is_hospital)s, %(is_medical)s, %(is_landgrant)s, %(is_hsi)s,
            %(opeid)s, %(ein)s, %(ueis)s,
            %(webaddr_raw)s, %(webaddr_normalized)s, %(canonical_root_url)s, %(domain)s, %(registrable_domain)s,
            %(enabled)s, %(pilot_cohort)s, NOW()
        )
        ON CONFLICT (unitid) DO UPDATE SET
            slug = EXCLUDED.slug,
            name = EXCLUDED.name,
            city = EXCLUDED.city, stabbr = EXCLUDED.stabbr,
            state_name = EXCLUDED.state_name, state_slug = EXCLUDED.state_slug,
            zip = EXCLUDED.zip, region = EXCLUDED.region, fips_code = EXCLUDED.fips_code,
            control_code = EXCLUDED.control_code,
            control_label = EXCLUDED.control_label,
            control_slug = EXCLUDED.control_slug,
            sector_code = EXCLUDED.sector_code,
            sector_label = EXCLUDED.sector_label,
            carnegie_basic_code  = EXCLUDED.carnegie_basic_code,
            carnegie_basic_label = EXCLUDED.carnegie_basic_label,
            carnegie_basic_slug  = EXCLUDED.carnegie_basic_slug,
            is_hbcu = EXCLUDED.is_hbcu, is_tribal = EXCLUDED.is_tribal,
            is_hospital = EXCLUDED.is_hospital, is_medical = EXCLUDED.is_medical,
            is_landgrant = EXCLUDED.is_landgrant, is_hsi = EXCLUDED.is_hsi,
            opeid = EXCLUDED.opeid, ein = EXCLUDED.ein, ueis = EXCLUDED.ueis,
            webaddr_raw = EXCLUDED.webaddr_raw,
            webaddr_normalized = EXCLUDED.webaddr_normalized,
            canonical_root_url = EXCLUDED.canonical_root_url,
            domain = EXCLUDED.domain, registrable_domain = EXCLUDED.registrable_domain,
            enabled = EXCLUDED.enabled, pilot_cohort = EXCLUDED.pilot_cohort,
            updated_at = NOW()
    """

    taken_slugs: set[str] = set()
    batch: list[dict] = []
    pilot_set = set(PILOT_UNITIDS)
    for r in rows:
        name = clean_name(r["instnm"])
        slug = generate_slug(name, r["unitid"], taken_slugs)
        webaddr_raw, normalized, full_domain, registrable = normalize_url(r["webaddr"])
        cc = r["c21basic"]
        # Resolve carnegie label: prefer ipeds.carnegie_codes (joined as r["carnegie_label"]);
        # fall back to CARNEGIE_LEGACY_LABEL for legacy 2021 codes 1-14 that aren't in that table.
        carnegie_label_resolved = r["carnegie_label"]
        if not carnegie_label_resolved and cc is not None:
            carnegie_label_resolved = CARNEGIE_LEGACY_LABEL.get(cc)
        batch.append({
            "unitid": r["unitid"],
            "slug": slug,
            "name": name,
            "city": r["city"],
            "stabbr": r["stabbr"],
            "state_name": r["state_name"],
            "state_slug": state_slug(r["state_name"]) or "unknown",
            "zip": r["zip"],
            "region": r["region"],
            "fips_code": r["state_fips"] or r["fips"],
            "control_code": r["control"],
            "control_label": r["control_name"],
            "control_slug": control_slug(r["control"]),
            "sector_code": r["sector"],
            "sector_label": r["sector_name"],
            "carnegie_basic_code": cc,
            "carnegie_basic_label": carnegie_label_resolved,
            "carnegie_basic_slug": carnegie_slug(cc, carnegie_label_resolved),
            "is_hbcu": r["hbcu"] == 1,
            "is_tribal": r["tribal"] == 1,
            "is_hospital": r["hospital"] == 1,
            "is_medical": r["medical"] == 1,
            "is_landgrant": r["landgrnt"] == 1,
            "is_hsi": r["is_hsi"] if r["is_hsi"] is not None else None,
            "opeid": r["opeid"],
            "ein": r["ein"],
            "ueis": r["ueis"],
            "webaddr_raw": webaddr_raw,
            "webaddr_normalized": normalized,
            "canonical_root_url": normalized,
            "domain": full_domain,
            "registrable_domain": registrable,
            "enabled": (r["cyactive"] == 1),
            "pilot_cohort": r["unitid"] in pilot_set,
        })

    if dry_run:
        click.echo(f"DRY RUN — would upsert {len(batch)} rows")
        for r in batch[:5]:
            click.echo(f"  {r['unitid']:6d} {r['slug']:40s} | {r['carnegie_basic_slug']} | {r['state_slug']}")
        return

    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(upsert_sql, batch)
    click.echo(f"Upserted {len(batch)} rows into landing.institutions")

    # Sanity report
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM landing.institutions")
        click.echo(f"Total rows: {cur.fetchone()['n']}")
        cur.execute("""
            SELECT carnegie_basic_slug, COUNT(*) AS n
            FROM landing.institutions WHERE enabled
            GROUP BY 1 ORDER BY n DESC LIMIT 10
        """)
        click.echo("Top Carnegie groups (enabled):")
        for row in cur.fetchall():
            click.echo(f"  {row['carnegie_basic_slug'] or '(null)':45s} {row['n']:5d}")
        cur.execute("""
            SELECT state_slug, COUNT(*) AS n FROM landing.institutions
            WHERE enabled GROUP BY 1 ORDER BY n DESC LIMIT 10
        """)
        click.echo("Top states (enabled):")
        for row in cur.fetchall():
            click.echo(f"  {row['state_slug']:25s} {row['n']:5d}")
        cur.execute("""
            SELECT unitid, slug, name, state_slug, carnegie_basic_slug
            FROM landing.institutions WHERE pilot_cohort ORDER BY unitid
        """)
        click.echo("Pilot cohort:")
        for row in cur.fetchall():
            click.echo(f"  {row['unitid']} {row['slug']} | {row['state_slug']} | {row['carnegie_basic_slug']}")


if __name__ == "__main__":
    main()

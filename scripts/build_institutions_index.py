"""Build data/json/institutions.json — the listing-page index.

Mirrors the layout shown in the Clema listing-page reference (hero + 4
KPIs + filter widget + per-institution cards + browse-by-category
sections). One JSON drives the entire listing page; the frontend can
filter / sort / group in-browser without needing to call the DB.

For each institution in the corpus (whichever JSONs exist in data/json/),
this script pulls the four card-level stats (grad rate, admit rate,
enrollment, avg net price) plus the metadata needed for badges + filters.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.db.engine import get_conn  # noqa: E402


SCHEMA_VERSION = "1.0"

# Map full Carnegie label → frontend-friendly tier label + slug.
# Order matters for the "best label wins" lookup (more specific first).
CARNEGIE_TIERS = [
    ("Doctoral Universities: Very High Research Activity", "R1 Research", "r1"),
    ("Doctoral Universities: High Research Activity",      "R2 Research", "r2"),
    ("Doctoral/Professional Universities",                  "Doctoral/Professional", "doctoral-professional"),
    ("Masters Colleges & Universities: Larger Programs",   "Master's",    "masters"),
    ("Masters Colleges & Universities: Medium Programs",   "Master's",    "masters"),
    ("Masters Colleges & Universities: Small Programs",    "Master's",    "masters"),
    ("Baccalaureate Colleges:",                             "Baccalaureate", "baccalaureate"),
    ("Baccalaureate/Associate's Colleges",                  "Baccalaureate/Associate's", "baccalaureate-associates"),
    ("Associate's Colleges",                                "Community College", "community-college"),
    ("Associate's Colleges:",                               "Community College", "community-college"),
]


def carnegie_tier(label: str | None) -> tuple[str | None, str | None]:
    """Return (display_label, slug) for a Carnegie basic label."""
    if not label:
        return None, None
    for prefix, disp, slug in CARNEGIE_TIERS:
        if label.startswith(prefix):
            return disp, slug
    return label, label.lower().replace(" ", "-").replace(":", "")[:40]


def control_short(label: str | None) -> tuple[str | None, str | None]:
    """Map IPEDS control_label to short chip text + filter slug.

    Chip text stays simple ("Public" / "Private") so the frontend's chip
    matcher recognizes it. The for-profit / not-for-profit distinction is
    preserved in the slug ("private-fp" vs "private-np") for filtering.
    """
    if not label:
        return None, None
    L = label.lower()
    if L.startswith("public"):              return "Public",  "public"
    if "private not-for-profit" in L:       return "Private", "private-np"
    if "private for-profit" in L:           return "Private", "private-fp"
    return label, label.lower().replace(" ", "-")


def _f(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def normalize_url(raw: str | None) -> str | None:
    """IPEDS stores webaddr as `www.foo.edu/` or `https://www.foo.edu/` —
    normalize to `https://www.foo.edu` (no scheme missing, no trailing slash).
    """
    if not raw:
        return None
    u = raw.strip()
    if not u:
        return None
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


@click.command()
@click.option("--json-dir", default="data/json",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Only include institutions whose per-institution JSON lives here")
@click.option("--out", default="data/json/institutions.json",
              type=click.Path(dir_okay=False, path_type=Path))
@click.option("--kv-base-url", default=None,
              help="Optional public KV/R2 URL prefix to embed as `json_url` per institution")
def main(json_dir: Path, out: Path, kv_base_url: str | None) -> None:
    # Which institutions have a per-institution JSON? They're the listable set.
    available_slugs = {fp.stem for fp in json_dir.glob("*.json")
                       if fp.stem != "institutions"}
    click.echo(f"Found {len(available_slugs)} per-institution JSONs in {json_dir}")

    # Single big join — institution metadata + the 4 card stats + 6 federal sources.
    # Sources of each metric match the per-institution exporter:
    #   admit_rate     IPEDS admissions (admssn/applcn) — latest data_year
    #   grad_rate      IPEDS drv_graduation_rates.gba6rtt — latest data_year
    #   enrollment     IPEDS fall_enrollment_2024.eftotlt (efalevel=1)
    #   avg_net_price  Scorecard (private→public fallback)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              li.unitid, li.slug, li.name,
              li.city, li.stabbr, li.state_name,
              li.control_label, li.sector_label,
              li.carnegie_basic_label, li.carnegie_basic_slug,
              li.is_hbcu, li.is_hsi, li.is_landgrant,
              li.is_medical, li.is_tribal,
              (SELECT webaddr FROM ipeds.institutions_2024 i
                 WHERE i.unitid=li.unitid LIMIT 1) AS webaddr,
              (SELECT a.admssn::float / NULLIF(a.applcn, 0)
                 FROM ipeds.admissions a
                 WHERE a.unitid=li.unitid
                 ORDER BY data_year DESC LIMIT 1) AS admit_rate,
              (SELECT g.gba6rtt::float / 100
                 FROM ipeds.drv_graduation_rates g
                 WHERE g.unitid=li.unitid
                 ORDER BY data_year DESC LIMIT 1) AS grad_rate,
              (SELECT e.eftotlt FROM ipeds.fall_enrollment_2024 e
                 WHERE e.unitid=li.unitid AND e.efalevel=1 LIMIT 1) AS enrollment,
              (SELECT COALESCE(f.avg_net_price_private,
                               f.avg_net_price_public)::int
                 FROM score_card.fact_institution_yearly f
                 WHERE f.institution_id=li.unitid
                 ORDER BY f.academic_year_id DESC NULLS LAST LIMIT 1) AS avg_net_price
            FROM landing.institutions li
            WHERE li.slug = ANY(%s)
            ORDER BY li.name
            """,
            (list(available_slugs),),
        )
        rows = cur.fetchall()

    institutions: list[dict] = []
    for r in rows:
        # Mirror the detail-page rule: IPEDS codes some 4-year specialized schools
        # (law/med/health/theology) into an "Associates Colleges" bucket. Suppress
        # that misleading label on 4-year schools so the index and the detail page
        # agree (e.g. Charles R Drew, a medical university).
        carn_label = r.get("carnegie_basic_label")
        if (carn_label or "").startswith("Associates Colleges") and \
                "4-year or above" in (r.get("sector_label") or ""):
            r = {**r, "carnegie_basic_label": None, "carnegie_basic_slug": None}
        tier_label, tier_slug = carnegie_tier(r.get("carnegie_basic_label"))
        ctrl_label, ctrl_slug = control_short(r.get("control_label"))

        designations: list[str] = []
        if r.get("is_hbcu"):      designations.append("hbcu")
        if r.get("is_hsi"):       designations.append("hsi")
        if r.get("is_landgrant"): designations.append("land-grant")
        if r.get("is_medical"):   designations.append("medical")
        if r.get("is_tribal"):    designations.append("tribal")

        # Tags = visual badges shown on each card, in order:
        # [carnegie tier, control, designations…]
        tags = []
        if tier_label: tags.append(tier_label)
        if ctrl_label: tags.append(ctrl_label)
        for d in designations:
            tags.append({"hbcu": "HBCU", "hsi": "HSI",
                         "land-grant": "Land-grant",
                         "medical": "Medical",
                         "tribal": "Tribal"}.get(d, d))

        # Scorecard sometimes reports a NEGATIVE avg_net_price for California
        # community colleges (BOG Fee Waiver + Promise + Pell can turn into
        # a net stipend). The raw value is correct, but a negative-dollar
        # chip on a card reads like a bug, so we clamp at $0 for display.
        # The detail JSON keeps the un-clamped value for analysts.
        raw_netprice = r.get("avg_net_price")
        chip_netprice = max(0, raw_netprice) if raw_netprice is not None else None

        inst = {
            "unitid":      r["unitid"],
            "slug":        r["slug"],
            "name":        r["name"],
            "website_url": normalize_url(r.get("webaddr")),
            "city":        r["city"],
            "state":       r["stabbr"],
            "state_name":  r["state_name"],
            "control":     ctrl_label,
            "control_slug": ctrl_slug,
            "carnegie":    r.get("carnegie_basic_label"),
            "carnegie_tier":      tier_label,
            "carnegie_tier_slug": tier_slug,
            "designations": designations,
            "tags": tags,
            "stats": {
                "grad_rate":     _f(r.get("grad_rate")),
                "admit_rate":    _f(r.get("admit_rate")),
                "enrollment":    r.get("enrollment"),
                "avg_net_price": chip_netprice,
            },
        }
        if kv_base_url:
            inst["json_url"] = f"{kv_base_url.rstrip('/')}/{r['slug']}"
        institutions.append(inst)

    # Facet aggregations for filters + browse-by-category sections.
    states  = Counter((i["state"], i["state_name"]) for i in institutions
                      if i["state"] and i["state_name"])
    controls = Counter((i["control"], i["control_slug"]) for i in institutions
                       if i["control"])
    tiers   = Counter((i["carnegie_tier"], i["carnegie_tier_slug"])
                      for i in institutions if i["carnegie_tier"])

    designation_counts = Counter()
    for i in institutions:
        for d in i["designations"]:
            designation_counts[d] += 1

    total_enrollment = sum(i["stats"]["enrollment"] or 0 for i in institutions)
    distinct_states  = len({i["state"] for i in institutions if i["state"]})

    doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "source":         "Clema Landing Page Builder",

        "meta": {
            "title":       "Federal data for every U.S. college, made readable.",
            "description": ("Federal data on every U.S. college and university — "
                            "enrollment, graduation rates, tuition, peers, and "
                            "IPEDS outcomes."),
            "data_sources": ["IPEDS 2024", "College Scorecard 2024",
                              "EADA", "PSEO", "DAPIP"],
            "updated_year": 2026,
            "vintage":      {"ipeds_year": 2024, "scorecard_year": 2024},
            "breadcrumbs":  [{"label": "Home", "url": "/"},
                              {"label": "Institutions", "url": "/institutions"}],
        },

        # Hero KPI tiles
        "stats": {
            "institutions_indexed": len(institutions),
            "states_count":         distinct_states,
            "combined_enrollment":  total_enrollment,
            "federal_data_sources": 6,
        },

        # Filter dropdowns + designation chips (counts on right)
        "filters": {
            "states": sorted(
                [{"code": code, "name": name, "count": n}
                 for (code, name), n in states.items()],
                key=lambda x: x["name"]),
            "controls": sorted(
                [{"label": lbl, "slug": slug, "count": n}
                 for (lbl, slug), n in controls.items() if lbl],
                key=lambda x: -x["count"]),
            "carnegie_tiers": sorted(
                [{"label": lbl, "slug": slug, "count": n}
                 for (lbl, slug), n in tiers.items() if lbl],
                key=lambda x: -x["count"]),
            "designations": [
                {"label": "HBCU",       "slug": "hbcu",
                 "count": designation_counts["hbcu"]},
                {"label": "HSI",        "slug": "hsi",
                 "count": designation_counts["hsi"]},
                {"label": "Land-grant", "slug": "land-grant",
                 "count": designation_counts["land-grant"]},
                {"label": "Medical",    "slug": "medical",
                 "count": designation_counts["medical"]},
                {"label": "Tribal",     "slug": "tribal",
                 "count": designation_counts["tribal"]},
            ],
        },

        # "Browse by category" section at bottom of the listing page —
        # surfaces top 5 of each axis with deep-link slugs.
        "categories": {
            "by_state": sorted(
                [{"name": name, "code": code, "slug": name.lower().replace(" ", "-"),
                  "count": n}
                 for (code, name), n in states.items()],
                key=lambda x: -x["count"])[:5],
            "by_carnegie": sorted(
                [{"label": lbl, "slug": slug, "count": n}
                 for (lbl, slug), n in tiers.items() if lbl],
                key=lambda x: -x["count"])[:5],
            "by_designation": [
                {"label": "HBCUs",       "slug": "hbcu",
                 "count": designation_counts["hbcu"]},
                {"label": "HSIs",        "slug": "hsi",
                 "count": designation_counts["hsi"]},
                {"label": "Land-grant",  "slug": "land-grant",
                 "count": designation_counts["land-grant"]},
                {"label": "Tribal",      "slug": "tribal",
                 "count": designation_counts["tribal"]},
            ],
        },

        # The full sortable / filterable list. Sorted A-Z by name to match
        # the reference page's default sort.
        "institutions": institutions,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    size_kb = out.stat().st_size // 1024

    click.echo(f"\n✅ Wrote {out} ({size_kb} KB)")
    click.echo(f"   institutions:        {doc['stats']['institutions_indexed']}")
    click.echo(f"   states:              {doc['stats']['states_count']}")
    click.echo(f"   combined enrollment: {doc['stats']['combined_enrollment']:,}")
    tiers_summary = ", ".join(
        f"{t['label']} ({t['count']})"
        for t in doc["filters"]["carnegie_tiers"][:5]
    )
    click.echo(f"   Carnegie tiers:      {tiers_summary}")
    click.echo(f"   Designations:        "
               f"HBCU={designation_counts['hbcu']}, "
               f"HSI={designation_counts['hsi']}, "
               f"Land-grant={designation_counts['land-grant']}, "
               f"Tribal={designation_counts['tribal']}")


if __name__ == "__main__":
    main()

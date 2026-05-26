"""Export one structured JSON per institution — the contract the frontend
landing-page renderer reads.

The JSON mirrors the MIT-style preview page (the gold-standard layout)
section-by-section, but uses stable, frontend-friendly key names so the
renderer doesn't depend on internal DB column names. One file per
institution at `data/json/<slug>.json`; will be uploaded to Cloudflare R2
in a separate step.

Usage:
    # One institution
    python scripts/export_landing_json.py --unitid 166683

    # By slug
    python scripts/export_landing_json.py --slug massachusetts-institute-of-technology

    # A cohort
    python scripts/export_landing_json.py --cohort scripts/cohorts/ca_top100.txt --top 20

Schema is versioned (`schema_version`) so the frontend can detect format
changes. Bump `SCHEMA_VERSION` when you make incompatible changes.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from landing_scraper.db.engine import get_conn  # noqa: E402
# Reuse the renderer's IPEDS loaders so JSON and HTML stay in lockstep
from render_page import (  # noqa: E402
    _fetch_admissions, _fetch_cost, _fetch_enrollment,
    _fetch_faculty, _fetch_grants, _fetch_ipeds_grad,
    _fetch_peers, _fetch_programs,
)


SCHEMA_VERSION = "1.0"

# IPEDS CIP 2-digit families → friendly labels. Same list the HTML renderer uses.
CIP_FAMILY = {
    "01": "Agriculture", "03": "Natural Resources", "04": "Architecture",
    "05": "Area/Ethnic Studies", "09": "Communication", "10": "Comm. Technologies",
    "11": "Computer Sciences", "12": "Personal/Culinary", "13": "Education",
    "14": "Engineering", "15": "Engineering Tech", "16": "Foreign Languages",
    "19": "Family/Consumer Sci", "22": "Legal Professions",
    "23": "English Language", "24": "Liberal Arts", "25": "Library Science",
    "26": "Biological Sciences", "27": "Mathematics", "29": "Military Tech",
    "30": "Multi/Interdisciplinary", "31": "Parks/Recreation",
    "38": "Philosophy/Religion", "39": "Theology",
    "40": "Physical Sciences", "41": "Science Technologies",
    "42": "Psychology", "43": "Security/Protective", "44": "Public Admin",
    "45": "Social Sciences", "46": "Construction Trades", "47": "Mechanic",
    "48": "Precision Production", "49": "Transportation",
    "50": "Visual/Performing Arts", "51": "Health Professions",
    "52": "Business", "54": "History",
}


def _scrub(v):
    """Decode psycopg JSON columns into Python objects. Pass-through dicts/lists."""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            return v
    return v


def _to_jsonable(v):
    """Recursively convert non-JSON-native types (Decimal, date/datetime) to JSON-safe primitives."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _to_jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_to_jsonable(x) for x in v]
    return v


def _clean_dict(d: dict, *, drop_nones: bool = True) -> dict:
    """Drop DB-internal noise (id columns, raw_payload pointers) and None values."""
    drop = {"id", "raw_payload_id", "parser_version"}
    out = {}
    for k, v in d.items():
        if k in drop:
            continue
        if drop_nones and v is None:
            continue
        out[k] = _to_jsonable(v)
    return out


def _pct(v) -> float | None:
    """Normalize percent values to 0.0-1.0 range when DB stores them as 0-100."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f / 100.0 if f > 1.0 else f


# ----------------------------------------------------------------------
# Builders — one per top-level JSON section. Each mirrors a section in
# the MIT preview HTML so the frontend renderer maps 1:1.
# ----------------------------------------------------------------------

def _normalize_url(raw: str | None) -> str | None:
    """IPEDS webaddr is sometimes scheme-less ("www.foo.edu/") and often
    has a trailing slash. Normalize to "https://www.foo.edu" (https + no
    trailing slash) so the frontend can use it as-is."""
    if not raw:
        return None
    u = raw.strip()
    if not u:
        return None
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def _section_institution(row: dict) -> dict:
    """Hero / identity block — name, location, control, Carnegie, tags."""
    return {
        "unitid":         row["unitid"],
        "slug":           row["slug"],
        "name":           row["institution_name"],
        "website_url":    _normalize_url(row.get("webaddr")),
        "city":           row.get("city"),
        "state":          row.get("stabbr"),
        "state_name":     row.get("state_name"),
        "zip":            row.get("zip"),
        "region":         row.get("region"),
        "control":        row.get("control_label"),
        "control_slug":   row.get("control_slug"),
        "sector":         row.get("sector_label"),
        "carnegie":       row.get("carnegie_basic_label"),
        "carnegie_slug":  row.get("carnegie_basic_slug"),
        "tags": {
            "is_hbcu":       row.get("is_hbcu", False),
            "is_hsi":        row.get("is_hsi", False),
            "is_landgrant":  row.get("is_landgrant", False),
            "is_medical":    row.get("is_medical", False),
            "is_tribal":     row.get("is_tribal", False),
        },
        "identifiers": {
            "ueis":  row.get("ueis"),
            "opeid": row.get("opeid"),
            "ein":   row.get("ein"),
        },
    }


def _section_about(row: dict) -> dict | None:
    """About / Wikipedia summary."""
    wiki = _scrub(row.get("wiki")) or {}
    if not wiki:
        return None
    return _clean_dict({
        "summary":       wiki.get("extract_summary"),
        "wikipedia_url": wiki.get("wikipedia_url"),
        "source_url":    wiki.get("source_url"),
        "wikidata_qid":  wiki.get("wikidata_qid"),
        "fetched_at":    wiki.get("fetched_at"),
    })


def _section_admissions(admissions: dict, scorecard: dict) -> dict | None:
    """Admissions stat block — acceptance rate, SAT/ACT bands."""
    if not admissions and not scorecard:
        return None
    appl = admissions.get("applcn")
    adm  = admissions.get("admssn")
    enr  = admissions.get("enrlt")
    enrft = admissions.get("enrlft")
    out = {
        "applicants":              appl,
        "admits":                  adm,
        "enrolled":                enr,
        "first_time_freshmen":     enr,
        "first_time_freshmen_ft":  enrft,
        "admit_rate":              (adm / appl) if (appl and adm) else None,
        "yield_rate":              (enr / adm) if (adm and enr) else None,
        "sat_average":             scorecard.get("sat_average"),
        "sat_math_25_75":          [scorecard.get("sat_math_25"),
                                     scorecard.get("sat_math_75")]
                                    if scorecard.get("sat_math_25") else None,
        "sat_reading_25_75":       [scorecard.get("sat_reading_25"),
                                     scorecard.get("sat_reading_75")]
                                    if scorecard.get("sat_reading_25") else None,
        "act_composite_25_75":     [scorecard.get("act_composite_25"),
                                     scorecard.get("act_composite_75")]
                                    if scorecard.get("act_composite_25") else None,
    }
    return _to_jsonable({k: v for k, v in out.items() if v is not None})


def _section_cost(scorecard: dict, cost: dict, is_private: bool) -> dict | None:
    """Tuition, room & board, net price by income, Pell %."""
    if not scorecard and not cost:
        return None
    np_prefix = "net_price_private_" if is_private else "net_price_public_"
    out = {
        "in_state_tuition":      scorecard.get("in_state_tuition"),
        "out_of_state_tuition":  scorecard.get("out_of_state_tuition"),
        "room_and_board":        cost.get("rmbrdamt"),
        "total_cost_in_state":   (
            (scorecard.get("in_state_tuition") or 0) + (cost.get("rmbrdamt") or 0)
            if scorecard.get("in_state_tuition") and cost.get("rmbrdamt") else None
        ),
        "total_cost_out_of_state": (
            (scorecard.get("out_of_state_tuition") or 0) + (cost.get("rmbrdamt") or 0)
            if scorecard.get("out_of_state_tuition") and cost.get("rmbrdamt") else None
        ),
        "avg_net_price": scorecard.get(
            "avg_net_price_private" if is_private else "avg_net_price_public",
        ),
        "is_private_for_net_price": is_private,
        "pell_grant_rate":           _pct(scorecard.get("pell_grant_rate")),
        "federal_loan_rate":         _pct(scorecard.get("federal_loan_rate")),
        "net_price_by_family_income": {
            "0_30k":    scorecard.get(f"{np_prefix}0_30k"),
            "30_48k":   scorecard.get(f"{np_prefix}30_48k"),
            "48_75k":   scorecard.get(f"{np_prefix}48_75k"),
            "75_110k":  scorecard.get(f"{np_prefix}75_110k"),
            "110k_plus": scorecard.get(f"{np_prefix}110k_plus"),
        },
    }
    return _to_jsonable({k: v for k, v in out.items()
                         if v is not None and v != {}})


def _section_graduation(ipeds_grad: dict, scorecard: dict) -> dict | None:
    """Graduation / retention / earnings."""
    if not ipeds_grad and not scorecard:
        return None
    out = {
        "overall_completion_rate":  _pct(scorecard.get("completion_rate")),
        "grad_rate_total":          _pct(ipeds_grad.get("grrttot")),
        "grad_4yr_bachelors":       _pct(ipeds_grad.get("gba4rtt")),
        "grad_5yr_bachelors":       _pct(ipeds_grad.get("gba5rtt")),
        "grad_6yr_bachelors":       _pct(ipeds_grad.get("gba6rtt")),
        "pell_grad_rate":           _pct(ipeds_grad.get("pggrrtt")),
        "pell_6yr_bachelors_rate":  _pct(ipeds_grad.get("pgba6rt")),
        "non_pell_grad_rate":       _pct(ipeds_grad.get("ssgrrtt")),
        "non_pell_6yr_bachelors":   _pct(ipeds_grad.get("ssba6rt")),
        "retention_full_time":      _pct(ipeds_grad.get("ret_pcf")),
        "retention_part_time":      _pct(ipeds_grad.get("ret_pcp")),
        "median_earnings_6yr":      scorecard.get("earnings_6yr_median"),
        "median_earnings_10yr":     scorecard.get("earnings_10yr_median"),
    }
    return _to_jsonable({k: v for k, v in out.items() if v is not None})


def _section_enrollment(enrollment: dict, scorecard: dict) -> dict | None:
    """Headcount, FT/PT, M/F, distance ed."""
    if not enrollment and not scorecard:
        return None
    total = enrollment.get("enrollment_12mo_total")
    out = {
        "enrollment_12mo_total":     total,
        "ug_12mo":                   enrollment.get("ug_12mo"),
        "grad_12mo":                 enrollment.get("grad_12mo"),
        "women_12mo":                enrollment.get("women_12mo"),
        "men_12mo":                  enrollment.get("men_12mo"),
        "fall_total":                enrollment.get("fall_total"),
        "fall_full_time":            enrollment.get("fall_full_time"),
        "fall_part_time":            enrollment.get("fall_part_time"),
        "distance_ed_any":           enrollment.get("distance_ed_any"),
        "distance_ed_exclusive":     enrollment.get("distance_ed_exclusive"),
        # Race/ethnicity from College Scorecard (demo_* fields, already 0-1 ratios).
        # Note: Scorecard uses `demo_*` column names, not `pct_*` — getting these
        # right is what surfaces the bar chart in the rendered page.
        "race_ethnicity": _clean_dict({
            "white":              scorecard.get("demo_white"),
            "black":              scorecard.get("demo_black"),
            "hispanic":           scorecard.get("demo_hispanic"),
            "asian":              scorecard.get("demo_asian"),
            "ai_an":              scorecard.get("demo_aian"),
            "nh_pi":              scorecard.get("demo_nhpi"),
            "two_or_more":        scorecard.get("demo_two_or_more"),
            "non_resident":       scorecard.get("demo_non_resident_alien"),
            "race_unknown":       scorecard.get("demo_unknown"),
        }) or None,
    }
    return _to_jsonable({k: v for k, v in out.items() if v is not None})


def _section_programs(programs: dict) -> dict | None:
    """Top CIP families + program count + degree levels offered."""
    if not programs:
        return None
    top = []
    for c in programs.get("top_cips") or []:
        cip = c.get("cip", "")
        top.append({
            "cip_family_code":  cip,
            "cip_family_label": CIP_FAMILY.get(cip, f"CIP {cip}"),
            "completions":      c.get("completions"),
        })
    return _to_jsonable({
        "top_cip_families":   top,
        "program_count":      programs.get("program_count"),
        "highest_degree":     programs.get("hloffer"),
        "offers_undergraduate": programs.get("ugoffer"),
        "offers_graduate":      programs.get("groffer"),
        "highest_degree_offered_code": programs.get("hdegofr1"),
    })


def _section_faculty(faculty: dict) -> dict | None:
    if not faculty:
        return None
    return _to_jsonable(_clean_dict(faculty))


def _section_peers(peers: list[dict], source_label: str, conn) -> dict | None:
    """Build the peers section, enriching each peer with a `metrics` object
    so the frontend can render a radar/table without loading per-peer JSONs.

    Metrics included (from IPEDS + College Scorecard):
      - admit_rate, yield_rate (latest year IPEDS admissions)
      - grad_rate (latest 6-yr Bachelor's grad rate)
      - retention_rate (latest fall full-time retention)
      - enrollment (fall total headcount)
      - avg_net_price (Scorecard, falls back public→private)
    """
    if not peers:
        return None

    slugs = [p.get("slug") for p in peers if p.get("slug")]
    metrics_by_slug: dict[str, dict] = {}
    if slugs:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  li.slug,
                  li.unitid,
                  (SELECT a.admssn::float / NULLIF(a.applcn, 0)
                     FROM ipeds.admissions a
                     WHERE a.unitid=li.unitid
                     ORDER BY data_year DESC LIMIT 1) AS admit_rate,
                  (SELECT a.enrlt::float / NULLIF(a.admssn, 0)
                     FROM ipeds.admissions a
                     WHERE a.unitid=li.unitid
                     ORDER BY data_year DESC LIMIT 1) AS yield_rate,
                  (SELECT g.gba6rtt::float / 100
                     FROM ipeds.drv_graduation_rates g
                     WHERE g.unitid=li.unitid
                     ORDER BY data_year DESC LIMIT 1) AS grad_rate,
                  (SELECT r.ret_pcf::float / 100
                     FROM ipeds.fall_enrollment_retention r
                     WHERE r.unitid=li.unitid
                     ORDER BY data_year DESC LIMIT 1) AS retention_rate,
                  (SELECT e.eftotlt FROM ipeds.fall_enrollment_2024 e
                     WHERE e.unitid=li.unitid AND e.efalevel=1 LIMIT 1) AS enrollment,
                  (SELECT COALESCE(f.avg_net_price_private, f.avg_net_price_public)::int
                     FROM score_card.fact_institution_yearly f
                     WHERE f.institution_id=li.unitid
                     ORDER BY f.academic_year_id DESC NULLS LAST LIMIT 1) AS avg_net_price
                FROM landing.institutions li
                WHERE li.slug = ANY(%s)
                """,
                (slugs,),
            )
            for r in cur.fetchall():
                # _to_jsonable handles Decimal → float; drop None-valued fields
                # so the frontend can see at-a-glance which metrics are available.
                m = {
                    k: _to_jsonable(r[k])
                    for k in ("admit_rate", "yield_rate", "grad_rate",
                              "retention_rate", "enrollment", "avg_net_price")
                    if r.get(k) is not None
                }
                metrics_by_slug[r["slug"]] = {
                    "unitid":  r["unitid"],
                    "metrics": m,
                }

    return {
        "source":        "dfr_v1" if "DFR" in source_label else "carnegie_state_control_v1",
        "source_label":  source_label,
        "institutions": [
            {
                "rank":      p.get("rank"),
                "unitid":    (metrics_by_slug.get(p.get("slug") or "") or {}).get("unitid"),
                "slug":      p.get("slug"),
                "name":      p.get("name"),
                "state":     p.get("state"),
                "carnegie":  p.get("carnegie"),
                "similarity_score": _to_jsonable(p.get("score")),
                "metrics":   (metrics_by_slug.get(p.get("slug") or "") or {}).get("metrics") or {},
            }
            for p in peers
        ],
    }


def _section_research_funding(rs: dict | None) -> dict | None:
    if not rs:
        return None
    return _to_jsonable({
        "nsf_total_usd":           rs.get("nsf_total_usd"),
        "nih_total_usd":           rs.get("nih_total_usd"),
        "herd_total_usd":          rs.get("herd_total_usd"),
        "usaspending_total_usd":   rs.get("usaspending_total_usd"),
        "all_sources_total_usd":   rs.get("all_sources_total_usd"),
        "years_covered":           rs.get("years_covered"),
    })


def _section_ir_office(ir_page: dict | None) -> dict | None:
    if not ir_page:
        return None
    return _to_jsonable(_clean_dict({
        "url":              ir_page.get("page_url"),
        "office_name":      ir_page.get("office_name"),
        "parent_division":  ir_page.get("parent_division"),
        "summary":          ir_page.get("page_summary"),
        "phone":            ir_page.get("office_phone"),
        "fax":              ir_page.get("office_fax"),
        "email":            ir_page.get("office_email"),
        "address":          ir_page.get("office_address"),
        "confidence":       ir_page.get("confidence"),
        "needs_review":     ir_page.get("needs_review"),
        "fetched_at":       ir_page.get("fetched_at"),
    }))


def _section_ir_team(ir_contacts: list[dict]) -> list[dict]:
    return [
        _clean_dict({
            "ordering":      c.get("ordering"),
            "name":          c.get("name"),
            "title":         c.get("title"),
            "email":         c.get("email"),
            "phone":         c.get("phone"),
            "linkedin_url":  c.get("linkedin_url"),
            "photo_url":     c.get("photo_url"),
            "bio_short":     c.get("bio_short"),
            "role_category": c.get("role_category"),
            "source_page":   c.get("source_page"),
        })
        for c in (ir_contacts or [])
    ]


def _section_cds_files(cds_links: list[dict]) -> list[dict]:
    return [
        {
            "year":   c.get("year"),
            "url":    c.get("cds_url"),
            "is_pdf": bool(c.get("is_pdf")),
        }
        for c in sorted(cds_links or [],
                        key=lambda x: x.get("year") or 0, reverse=True)
    ]


def _section_documents(documents: list[dict]) -> list[dict]:
    return [
        _clean_dict({
            "doc_type":    d.get("doc_type"),
            "year":        d.get("year"),
            "title":       d.get("title"),
            "url":         d.get("doc_url"),
            "summary":     d.get("summary"),
            "stored":      d.get("local_storage_path") is not None,
            "fetched_at":  d.get("fetched_at"),
        })
        for d in sorted(documents or [],
                        key=lambda x: (x.get("doc_type") or "",
                                       -(x.get("year") or 0)))
    ]


def _section_ir_jobs(jobs: list[dict]) -> list[dict]:
    return [
        _clean_dict({
            "title":      j.get("title"),
            "source":     j.get("source"),
            "apply_url":  j.get("apply_url"),
            "posted_at":  j.get("posted_at"),
            "summary":    j.get("summary"),
        })
        for j in (jobs or [])
    ]


def _section_grants(grants: list[dict]) -> list[dict]:
    return [
        _clean_dict({
            "title":      g.get("title"),
            "agency":     g.get("agency"),
            "kind":       g.get("kind"),
            "amount_usd": g.get("amount_usd"),
            "url":        g.get("source_url"),
            "source":     g.get("source_system"),
            "award_date": g.get("award_date"),
        })
        for g in (grants or [])
    ]


def _section_alumni(alumni: list[dict]) -> list[dict]:
    return [
        _clean_dict({
            "name":          a.get("name"),
            "summary":       a.get("summary"),
            "wikipedia_url": a.get("wikipedia_url"),
            "grad_year":     a.get("grad_year"),
            "field":         a.get("field"),
            "notable_for":   a.get("notable_for"),
        })
        for a in (alumni or [])
    ]


def _section_brand(brand: dict | None) -> dict | None:
    if not brand:
        return None
    return _to_jsonable(_clean_dict({
        "logo_url":      brand.get("logo_url"),
        "icon_url":      brand.get("icon_url"),
        "primary_color": brand.get("primary_color"),
        "secondary_color": brand.get("secondary_color"),
        "domain":        brand.get("domain"),
    }))


# ----------------------------------------------------------------------
# Top-level export
# ----------------------------------------------------------------------

def build_landing_json(unitid: int | None = None,
                       slug: str | None = None) -> dict:
    """Assemble the full landing-page JSON for one institution."""
    where = "li.unitid = %s" if unitid else "li.slug = %s"
    arg = unitid if unitid else slug
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM landing.mv_institution_page li WHERE {where} LIMIT 1",
            (arg,),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"institution not found: {arg}")

        # Pull the institution's official root website URL from IPEDS HD.
        # Not in the MV (HD has it under `webaddr`); fetched separately and
        # normalized to https://<host> with no trailing slash.
        cur.execute(
            "SELECT webaddr FROM ipeds.institutions_2024 WHERE unitid=%s LIMIT 1",
            (row["unitid"],),
        )
        webaddr_row = cur.fetchone()
        row["webaddr"] = (webaddr_row or {}).get("webaddr") if webaddr_row else None

        scorecard      = _scrub(row.get("scorecard"))    or {}
        brand          = _scrub(row.get("brand"))        or {}
        ir_page        = _scrub(row.get("ir_page"))      or {}
        ir_contacts    = _scrub(row.get("ir_contacts"))    or []
        documents      = _scrub(row.get("documents"))      or []
        cds_links      = _scrub(row.get("cds_links"))      or []
        jobs           = _scrub(row.get("jobs"))           or []
        notable_alumni = _scrub(row.get("notable_alumni")) or []
        research_summary = _scrub(row.get("research_summary")) or {}

        peers, peers_label = _fetch_peers(conn, row["unitid"])
        ipeds_grad = _fetch_ipeds_grad(conn, row["unitid"])
        enrollment = _fetch_enrollment(conn, row["unitid"])
        admissions = _fetch_admissions(conn, row["unitid"])
        cost       = _fetch_cost(conn, row["unitid"])
        faculty    = _fetch_faculty(conn, row["unitid"])
        programs   = _fetch_programs(conn, row["unitid"])
        grants     = _fetch_grants(conn, row["unitid"])
        # Build peers section inside the connection scope so we can enrich
        # each peer with the radar/table metrics in one shared transaction.
        peers_section = _section_peers(peers, peers_label, conn)

    is_private = (row.get("control_label") or "").lower().startswith("private")

    doc = {
        "schema_version":  SCHEMA_VERSION,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "source":          "Clema Landing Page Builder",
        "data_vintage":    {"ipeds_year": 2024, "scorecard_year": 2024},

        "institution":     _section_institution(row),
        "brand":           _section_brand(brand),
        "about":           _section_about(row),

        "admissions":      _section_admissions(admissions, scorecard),
        "cost":            _section_cost(scorecard, cost, is_private),
        "graduation":      _section_graduation(ipeds_grad, scorecard),
        "enrollment":      _section_enrollment(enrollment, scorecard),
        "programs":        _section_programs(programs),
        "faculty":         _section_faculty(faculty),

        "peers":           peers_section,
        "research_funding": _section_research_funding(research_summary),

        "ir_office":       _section_ir_office(ir_page),
        "ir_team":         _section_ir_team(ir_contacts),
        "cds_files":       _section_cds_files(cds_links),
        "documents":       _section_documents(documents),
        "ir_jobs":         _section_ir_jobs(jobs),
        "grants":          _section_grants(grants),
        "notable_alumni":  _section_alumni(notable_alumni),
    }
    return doc


def _write_json(doc: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = doc["institution"]["slug"]
    path = out_dir / f"{slug}.json"
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def _print_summary(doc: dict, path: Path) -> None:
    inst = doc["institution"]
    counts = {
        "team":      len(doc.get("ir_team") or []),
        "cds":       len(doc.get("cds_files") or []),
        "docs":      len(doc.get("documents") or []),
        "jobs":      len(doc.get("ir_jobs") or []),
        "grants":    len(doc.get("grants") or []),
        "alumni":    len(doc.get("notable_alumni") or []),
        "peers":     len((doc.get("peers") or {}).get("institutions", [])),
    }
    has = lambda k: "✓" if doc.get(k) else "·"  # noqa: E731
    counts_str = "  ".join(f"{k}={v}" for k, v in counts.items())
    click.echo(
        f"  [{inst['unitid']}] {inst['name']:<48} "
        f"about={has('about')} office={has('ir_office')} "
        f"admit={has('admissions')} cost={has('cost')} "
        f"grad={has('graduation')} enroll={has('enrollment')}  "
        f"{counts_str}  "
        f"→ {path} "
        f"({path.stat().st_size // 1024} KB)"
    )


def _cohort_unitids(cohort_file: Path, top: int | None) -> list[int]:
    """Parse a `scripts/cohorts/*.txt` file (unitid | enrollment | name | ...)."""
    ids: list[int] = []
    for line in cohort_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"\s*(\d+)\s*\|", line)
        if m:
            ids.append(int(m.group(1)))
    return ids[:top] if top else ids


@click.command()
@click.option("--unitid", type=int, default=None,
              help="One institution by unitid")
@click.option("--slug", default=None, help="One institution by slug")
@click.option("--cohort", type=click.Path(exists=True, dir_okay=False),
              default=None, help="Cohort file (one unitid per line)")
@click.option("--top", type=int, default=None,
              help="Take only first N unitids from --cohort")
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/json"),
              help="Output directory for the JSON files")
def main(unitid: int | None, slug: str | None,
         cohort: str | None, top: int | None,
         out_dir: Path) -> None:
    if unitid or slug:
        targets: list[tuple[int | None, str | None]] = [(unitid, slug)]
    elif cohort:
        ids = _cohort_unitids(Path(cohort), top)
        targets = [(u, None) for u in ids]
        click.echo(f"Exporting {len(targets)} institution(s) from {cohort}...")
    else:
        raise click.UsageError("Provide --unitid, --slug, or --cohort")

    for u, s in targets:
        try:
            doc = build_landing_json(unitid=u, slug=s)
            path = _write_json(doc, out_dir)
            _print_summary(doc, path)
        except SystemExit as e:
            click.echo(f"  [skip] {e}", err=True)
        except Exception as e:  # noqa: BLE001
            click.echo(f"  [error] unitid={u} slug={s}: {e}", err=True)


if __name__ == "__main__":
    main()

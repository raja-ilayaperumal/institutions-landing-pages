"""Render an institution landing page from landing.mv_institution_page.

Standalone HTML — single file, no JS framework — so you can open it in a
browser to review the scraped data. Mirrors the SEO doc's 12 blocks.

Usage:
    python scripts/render_page.py --unitid 166683 --output docs/preview/mit.html
    python scripts/render_page.py --slug massachusetts-institute-of-technology
"""
from __future__ import annotations

import html as html_mod
import json
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.db.engine import get_conn  # noqa: E402


def _esc(v) -> str:
    if v is None:
        return ""
    return html_mod.escape(str(v))


def _money(v) -> str:
    if v is None:
        return "—"
    try:
        return f"${int(v):,}"
    except (ValueError, TypeError):
        return "—"


def _pct(v, places: int = 1) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.{places}f}%"
    except (ValueError, TypeError):
        return "—"


def _num(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except (ValueError, TypeError):
        return str(v)


def _fmt_year(v) -> str:
    return f" [{v}]" if v else ""


def _scrub(maybe_jsonb):
    """Coerce JSONB returned from psycopg into a Python object."""
    if maybe_jsonb is None:
        return None
    if isinstance(maybe_jsonb, (dict, list)):
        return maybe_jsonb
    if isinstance(maybe_jsonb, str):
        try:
            return json.loads(maybe_jsonb)
        except json.JSONDecodeError:
            return None
    return None


def _fetch_peers(conn, unitid: int) -> tuple[list[dict], str]:
    """Prefer DFR peers (official IPEDS comparison group); fall back to Carnegie+state+control algorithm.

    Returns (peers, source_label) where source_label is shown to the reader.
    """
    for algorithm, label in (
        ("dfr_v1", "IPEDS Data Feedback Report (official comparison group)"),
        ("carnegie_state_control_v1", "Auto-generated (Carnegie + state + control + size)"),
    ):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg.rank, pg.peer_unitid, pg.similarity_score,
                       i.slug, i.name, i.state_name, i.carnegie_basic_label
                FROM landing.peer_groups pg
                JOIN landing.institutions i ON i.unitid = pg.peer_unitid
                WHERE pg.unitid = %s AND pg.algorithm = %s
                ORDER BY pg.rank
                """,
                (unitid, algorithm),
            )
            rows = cur.fetchall()
        if rows:
            return [
                {"rank": r["rank"], "slug": r["slug"], "name": r["name"],
                 "state": r["state_name"], "carnegie": r["carnegie_basic_label"],
                 "score": r["similarity_score"]}
                for r in rows
            ], label
    return [], "—"


def _fetch_enrollment(conn, unitid: int) -> dict:
    """12-month + fall headcount, UG/grad split, FT/PT, M/F, online-only."""
    out: dict = {}
    with conn.cursor() as cur:
        # 12-month total + UG/grad (effyalev: 1=total, 2=ug, 3=grad/post-bac)
        cur.execute(
            """
            SELECT effyalev, efytotlt, efytotlw, efytotlm
            FROM ipeds.enrollment_12month_2024
            WHERE unitid = %s AND effyalev IN (1, 2, 3) AND lstudy IN (1, 999)
            """,
            (unitid,),
        )
        for r in cur.fetchall():
            lvl = r.get("effyalev")
            if lvl == 1:
                out["enrollment_12mo_total"] = r["efytotlt"]
                out["women_12mo"] = r["efytotlw"]
                out["men_12mo"] = r["efytotlm"]
            elif lvl == 2:
                out["ug_12mo"] = r["efytotlt"]
            elif lvl == 3:
                out["grad_12mo"] = r["efytotlt"]
        # Fall headcount + FT/PT (efalevel: 1=total, 2=FT, 3=PT)
        cur.execute(
            """
            SELECT efalevel, eftotlt, eftotlm, eftotlw
            FROM ipeds.fall_enrollment_2024
            WHERE unitid=%s AND efalevel IN (1,2,3) AND line IN (14, 29, 99)
            """,
            (unitid,),
        )
        for r in cur.fetchall():
            lvl = r.get("efalevel")
            if lvl == 1:
                out["fall_total"] = r["eftotlt"]
            elif lvl == 2:
                out["fall_full_time"] = r["eftotlt"]
            elif lvl == 3:
                out["fall_part_time"] = r["eftotlt"]
        # Online-only (distance ed) — exclusively distance
        cur.execute(
            """
            SELECT efdetot, efdeexc
            FROM ipeds.fall_enrollment_distance_2024
            WHERE unitid=%s AND efdelev IN (1, 12) LIMIT 1
            """,
            (unitid,),
        )
        d = cur.fetchone()
        if d:
            out["distance_ed_any"] = d.get("efdetot")
            out["distance_ed_exclusive"] = d.get("efdeexc")
    return out


def _fetch_admissions(conn, unitid: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applcn, admssn, enrlt, enrlft FROM ipeds.admissions "
            "WHERE unitid=%s ORDER BY data_year DESC LIMIT 1",
            (unitid,),
        )
        r = cur.fetchone() or {}
    return r


def _fetch_cost(conn, unitid: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rmbrdamt FROM ipeds.cost1 WHERE unitid=%s ORDER BY data_year DESC LIMIT 1",
            (unitid,),
        )
        r = cur.fetchone() or {}
    return r


def _fetch_faculty(conn, unitid: int) -> dict:
    """Student-to-faculty ratio + instructional staff total + FT/PT split."""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stufacr FROM ipeds.fall_enrollment_retention "
            "WHERE unitid=%s ORDER BY data_year DESC LIMIT 1",
            (unitid,),
        )
        r = cur.fetchone()
        if r:
            out["student_faculty_ratio"] = r.get("stufacr")
        # Aggregate from staff_instructional_summary: sistotl per facstat
        cur.execute(
            """
            SELECT facstat, SUM(sistotl) AS total
            FROM ipeds.staff_instructional_summary
            WHERE unitid=%s AND data_year=(
              SELECT MAX(data_year) FROM ipeds.staff_instructional_summary WHERE unitid=%s
            )
            GROUP BY facstat
            """,
            (unitid, unitid),
        )
        rows = cur.fetchall()
        # facstat codes: 0=total, 1=FT total, 2=PT total
        for r in rows:
            if r["facstat"] == 0:
                out["instr_total_fte"] = r["total"]
            elif r["facstat"] == 1:
                out["instr_ft"] = r["total"]
            elif r["facstat"] == 2:
                out["instr_pt"] = r["total"]
    return out


def _fetch_programs(conn, unitid: int) -> dict:
    """Top CIP areas by completions + total program count + degree levels."""
    out: dict = {"top_cips": [], "degree_levels": []}
    with conn.cursor() as cur:
        # Top CIPs by total completions, excluding aggregate (99) — show 2-digit family
        cur.execute(
            """
            SELECT LEFT(cipcode, 2) AS cip_family, SUM(ctotalt) AS total
            FROM ipeds.completions_by_program_2024
            WHERE unitid=%s AND cipcode <> '99' AND ctotalt IS NOT NULL
              AND awlevel IN (3,5,7,17,18)
            GROUP BY 1
            ORDER BY total DESC NULLS LAST
            LIMIT 10
            """,
            (unitid,),
        )
        out["top_cips"] = [{"cip": r["cip_family"], "completions": r["total"]}
                           for r in cur.fetchall() if r["cip_family"]]
        # Number of distinct programs offered (by 6-digit CIP)
        cur.execute(
            "SELECT COUNT(DISTINCT cipcode) FROM ipeds.completions_by_program_2024 "
            "WHERE unitid=%s AND cipcode <> '99'",
            (unitid,),
        )
        out["program_count"] = (cur.fetchone() or {}).get("count")
        # Degree levels offered from institutions_2024
        cur.execute(
            "SELECT hloffer, ugoffer, groffer, hdegofr1 FROM ipeds.institutions_2024 WHERE unitid=%s",
            (unitid,),
        )
        r = cur.fetchone()
        if r:
            out["hloffer"] = r.get("hloffer")
            out["ugoffer"] = r.get("ugoffer")
            out["groffer"] = r.get("groffer")
            out["hdegofr1"] = r.get("hdegofr1")
    return out


# Friendly labels for IPEDS CIP 2-digit families
_CIP_FAMILY = {
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


def _fetch_grants(conn, unitid: int) -> list[dict]:
    """Recent + still-active grants for the institution.

    Filtering rules (so IR teams never see an opp that closed 18 months ago):
      - Opportunities (status='opportunity'): posted within the last
        12 months AND either still active OR closed within last 3 months.
        Lets us show "Closes in N days" today and "Recently closed" for a
        short tail without dumping a multi-year backlog.
      - Awards (status='award'): posted within last 24 months AND not
        archived more than 6 months ago. Awards have lasting signal but
        a 2-year-old archive entry is just noise.
      - Non-SAM rows (institution_site, LLM-curated funding pages) pass
        through — they're not date-stamped and were already curated.

    Ordering: still-open first (closest deadline next), then most recent.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT title, agency, amount_usd, status AS kind, source_url,
                   source_system, award_date, posted_at, inactive_at,
                   response_deadline_at
            FROM landing.grants
            WHERE unitid = %s
              AND (
                    source_system <> 'sam_gov'
                 OR (
                      status = 'opportunity'
                      AND posted_at >= CURRENT_DATE - INTERVAL '12 months'
                      AND (inactive_at IS NULL
                           OR inactive_at >= CURRENT_DATE - INTERVAL '3 months')
                    )
                 OR (
                      status = 'award'
                      AND posted_at >= CURRENT_DATE - INTERVAL '24 months'
                      AND (inactive_at IS NULL
                           OR inactive_at >= CURRENT_DATE - INTERVAL '6 months')
                    )
              )
            ORDER BY
              (inactive_at IS NOT NULL AND inactive_at >= CURRENT_DATE) DESC,
              response_deadline_at ASC NULLS LAST,
              posted_at DESC NULLS LAST,
              (amount_usd IS NOT NULL) DESC,
              amount_usd DESC NULLS LAST,
              title
            """,
            (unitid,),
        )
        return list(cur.fetchall())


def _fetch_ipeds_grad(conn, unitid: int) -> dict:
    """Pull most recent IPEDS-derived graduation/retention rates."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT grrttot, gba4rtt, gba5rtt, gba6rtt,
                   pggrrtt, pgba6rt, ssgrrtt, ssba6rt
            FROM ipeds.drv_graduation_rates
            WHERE unitid = %s
            ORDER BY data_year DESC LIMIT 1
            """,
            (unitid,),
        )
        gr = cur.fetchone() or {}
        cur.execute(
            """
            SELECT ret_pcf, ret_pcp
            FROM ipeds.fall_enrollment_retention
            WHERE unitid = %s
            ORDER BY data_year DESC LIMIT 1
            """,
            (unitid,),
        )
        ret = cur.fetchone() or {}
    return {**gr, **ret}


def render(unitid: int | None = None, slug: str | None = None) -> str:
    where = "li.unitid = %s" if unitid else "li.slug = %s"
    arg = unitid if unitid else slug

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT *
            FROM landing.mv_institution_page li
            WHERE {where}
            LIMIT 1
        """, (arg,))
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"institution not found: {arg}")

        scorecard = _scrub(row.get("scorecard")) or {}
        brand    = _scrub(row.get("brand"))    or {}
        wiki     = _scrub(row.get("wiki"))     or {}
        ir_page  = _scrub(row.get("ir_page"))  or {}
        ir_contacts    = _scrub(row.get("ir_contacts"))    or []
        documents      = _scrub(row.get("documents"))      or []
        cds_links      = _scrub(row.get("cds_links"))      or []
        jobs           = _scrub(row.get("jobs"))           or []
        notable_alumni = _scrub(row.get("notable_alumni")) or []
        research_summary = _scrub(row.get("research_summary")) or {}
        peers, peers_source = _fetch_peers(conn, row["unitid"])
        ipeds_grad = _fetch_ipeds_grad(conn, row["unitid"])
        grants_rows = _fetch_grants(conn, row["unitid"])
        enrollment = _fetch_enrollment(conn, row["unitid"])
        admissions = _fetch_admissions(conn, row["unitid"])
        cost = _fetch_cost(conn, row["unitid"])
        faculty = _fetch_faculty(conn, row["unitid"])
        programs = _fetch_programs(conn, row["unitid"])

    name  = row["institution_name"]
    year  = 2024

    # ---- assemble HTML ----
    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(name)} — Enrollment, Tuition & Outcomes Data [{year}]</title>
<meta name="description" content="Key federal data for {_esc(name)}: enrollment, graduation rate, tuition cost, and peer benchmarks. Updated {year} from federal data sources.">
<script type="application/ld+json">
{json.dumps({
    "@context": "https://schema.org",
    "@type": "Dataset",
    "name": f"{name} — Federal Education Data {year}",
    "description": f"Federal education data on {name} from IPEDS, College Scorecard, NSF, NIH, USAspending.",
    "url": f"https://clema.ai/institutions/{row['slug']}/",
    "creator": {"@type": "Organization", "name": "Clema"},
    "temporalCoverage": str(year),
    "license": "https://creativecommons.org/licenses/by/4.0/",
    "keywords": [name, "enrollment", "graduation rate", "tuition", "outcomes"],
}, indent=2)}
</script>
<style>
  :root {{
    --primary: {_esc(brand.get("colors_primary") or "#1a365d")};
    --accent: {_esc(brand.get("colors_secondary") or "#2c5282")};
  }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 1200px; margin: 0 auto; padding: 0; color: #1a202c; background: #f7fafc; }}
  header {{ background: var(--primary); color: white; padding: 24px 32px; }}
  header h1 {{ margin: 0; font-size: 30px; line-height: 1.2; }}
  header .sub {{ opacity: 0.85; margin-top: 8px; font-size: 14px; }}
  .badges {{ margin-top: 12px; }}
  .badge {{ display: inline-block; background: rgba(255,255,255,0.18); padding: 4px 10px;
            border-radius: 14px; font-size: 12px; margin-right: 6px; }}
  .data-source {{ margin: 14px 32px 0; color: #4a5568; font-size: 12px; font-style: italic; }}
  section {{ background: white; margin: 16px; padding: 24px 32px; border-radius: 8px;
             box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
  h2 {{ margin: 0 0 16px; font-size: 22px; color: var(--primary); border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }}
  .stat {{ padding: 12px 16px; background: #f7fafc; border-left: 3px solid var(--accent); border-radius: 4px; }}
  .stat .label {{ font-size: 11px; color: #718096; text-transform: uppercase; letter-spacing: 0.5px; }}
  .stat .value {{ font-size: 22px; font-weight: 600; color: #1a202c; margin-top: 2px; }}
  .stat .sub {{ font-size: 11px; color: #4a5568; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  table th, table td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
  table th {{ background: #f7fafc; font-weight: 600; color: #4a5568; }}
  ul.links li {{ margin-bottom: 4px; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .empty {{ color: #a0aec0; font-style: italic; padding: 8px 0; }}
  .pill-good {{ background: #c6f6d5; color: #22543d; padding: 2px 8px; border-radius: 10px; font-size: 11px; }}
  .pill-warn {{ background: #fefcbf; color: #744210; padding: 2px 8px; border-radius: 10px; font-size: 11px; }}
  footer {{ padding: 24px 32px; color: #718096; font-size: 12px; text-align: center; }}
  .logo {{ max-height: 56px; margin-bottom: 12px; }}
</style>
</head>
<body>
""")

    # BLOCK 00 — Hero
    logo = brand.get("logo_url")
    badges = []
    if row.get("is_hbcu"):     badges.append("HBCU")
    if row.get("is_hsi"):      badges.append("HSI")
    if row.get("is_tribal"):   badges.append("Tribal")
    if row.get("is_landgrant"): badges.append("Land-grant")
    if row.get("is_medical"):  badges.append("Medical School")
    parts.append(f"""<header>
  {f'<img class="logo" src="{_esc(logo)}" alt="logo">' if logo else ''}
  <h1>{_esc(name)}: Enrollment, Tuition &amp; Outcomes Data {year}</h1>
  <div class="sub">{_esc(', '.join(p for p in [row.get('city'), row.get('state_name')] if p))} · {_esc(row.get('control_label'))} · {_esc(row.get('carnegie_basic_label') or '')}</div>
  <div class="badges">
    {''.join(f'<span class="badge">{_esc(b)}</span>' for b in badges)}
  </div>
</header>
<div class="data-source">Federal data from IPEDS {year}, College Scorecard, NSF, NIH, USAspending. Last updated {_esc(row.get('landing_last_updated_at') or '')[:10]}.</div>
""")

    # BLOCK 00 (cont) — About from Wikipedia
    parts.append('<section><h2>About</h2>')
    if wiki.get("extract_summary"):
        parts.append(f'<p>{_esc(wiki["extract_summary"])}</p>')
        parts.append(f'<p style="font-size:12px;color:#718096">Source: <a href="{_esc(wiki.get("wikipedia_url"))}" target="_blank">Wikipedia</a></p>')
    else:
        parts.append('<div class="empty">No Wikipedia summary scraped.</div>')
    parts.append('</section>')

    # BLOCK 01 — Admissions (expanded: applicants, admitted, freshmen enrolled)
    applcn = admissions.get('applcn'); admssn = admissions.get('admssn')
    enrlt = admissions.get('enrlt'); enrlft = admissions.get('enrlft')
    yield_pct = (enrlt / admssn) if admssn and enrlt else None
    parts.append(f"""<section><h2>Admissions Data</h2>
<div class="grid">
  <div class="stat"><div class="label">Acceptance Rate</div><div class="value">{_pct(scorecard.get('admission_rate'))}</div></div>
  <div class="stat"><div class="label">Total Applicants</div><div class="value">{_num(applcn)}</div></div>
  <div class="stat"><div class="label">Total Admitted</div><div class="value">{_num(admssn)}</div></div>
  <div class="stat"><div class="label">First-time Freshmen Enrolled</div><div class="value">{_num(enrlt)}<div class="sub">{_num(enrlft) + ' full-time' if enrlft else ''}</div></div></div>
  <div class="stat"><div class="label">Yield (Enrolled / Admitted)</div><div class="value">{_pct(yield_pct)}</div></div>
  <div class="stat"><div class="label">SAT Average</div><div class="value">{_num(scorecard.get('sat_avg'))}</div></div>
  <div class="stat"><div class="label">SAT Math 25–75</div><div class="value">{_num(scorecard.get('sat_math_25th'))}–{_num(scorecard.get('sat_math_75th'))}</div></div>
  <div class="stat"><div class="label">SAT Reading 25–75</div><div class="value">{_num(scorecard.get('sat_reading_25th'))}–{_num(scorecard.get('sat_reading_75th'))}</div></div>
  <div class="stat"><div class="label">ACT Composite 25–75</div><div class="value">{_num(scorecard.get('act_cumulative_25th'))}–{_num(scorecard.get('act_cumulative_75th'))}</div></div>
</div>
</section>
""")

    # BLOCK 02 — Tuition & Cost
    tin = scorecard.get("tuition_in_state")
    tout = scorecard.get("tuition_out_of_state")
    is_private = (row.get("control_label") or "").lower().startswith("private")
    np_prefix = "net_price_private_" if is_private else "net_price_public_"
    rmbrd = cost.get('rmbrdamt')
    total_in  = (tin  + rmbrd) if tin  and rmbrd else None
    total_out = (tout + rmbrd) if tout and rmbrd else None
    parts.append(f"""<section><h2>Tuition &amp; Cost</h2>
<div class="grid">
  <div class="stat"><div class="label">In-State Tuition</div><div class="value">{_money(tin)}</div></div>
  <div class="stat"><div class="label">Out-of-State Tuition</div><div class="value">{_money(tout)}</div></div>
  <div class="stat"><div class="label">Room &amp; Board</div><div class="value">{_money(rmbrd)}</div></div>
  <div class="stat"><div class="label">Total Cost (In-State)</div><div class="value">{_money(total_in)}<div class="sub">tuition + room &amp; board</div></div></div>
  <div class="stat"><div class="label">Total Cost (Out-of-State)</div><div class="value">{_money(total_out)}</div></div>
  <div class="stat"><div class="label">Avg Net Price ({'Private' if is_private else 'Public'})</div><div class="value">{_money(scorecard.get('avg_net_price_private' if is_private else 'avg_net_price_public'))}</div></div>
  <div class="stat"><div class="label">% Pell Recipients</div><div class="value">{_pct(scorecard.get('pell_grant_rate'))}</div></div>
  <div class="stat"><div class="label">% Federal Loan Recipients</div><div class="value">{_pct(scorecard.get('federal_loan_rate'))}</div></div>
</div>
<h3 style="margin-top:24px;font-size:14px;color:#4a5568;text-transform:uppercase;letter-spacing:0.5px">Avg Net Price by Family Income</h3>
<table>
  <tr><th>$0–30k</th><th>$30–48k</th><th>$48–75k</th><th>$75–110k</th><th>$110k+</th></tr>
  <tr>
    <td>{_money(scorecard.get(np_prefix + '0_30k'))}</td>
    <td>{_money(scorecard.get(np_prefix + '30_48k'))}</td>
    <td>{_money(scorecard.get(np_prefix + '48_75k'))}</td>
    <td>{_money(scorecard.get(np_prefix + '75_110k'))}</td>
    <td>{_money(scorecard.get(np_prefix + '110k_plus'))}</td>
  </tr>
</table>
</section>
""")

    # BLOCK 03 — Graduation & Outcomes (Scorecard preferred; IPEDS as fallback)
    def _pct_int_or_frac(scorecard_v, ipeds_v_pct):
        if scorecard_v is not None:
            return _pct(scorecard_v)
        if ipeds_v_pct is not None:
            try:
                return f"{float(ipeds_v_pct):.1f}%"
            except (ValueError, TypeError):
                return "—"
        return "—"

    parts.append(f"""<section><h2>Graduation &amp; Outcomes</h2>
<div class="grid">
  <div class="stat"><div class="label">Overall Completion Rate</div><div class="value">{_pct_int_or_frac(scorecard.get('completion_rate_overall'), ipeds_grad.get('grrttot'))}</div><div class="sub">IPEDS / Scorecard, most recent year</div></div>
  <div class="stat"><div class="label">6-Year Graduation (Bachelors)</div><div class="value">{_pct_int_or_frac(None, ipeds_grad.get('gba6rtt'))}</div></div>
  <div class="stat"><div class="label">4-Year Graduation (Bachelors)</div><div class="value">{_pct_int_or_frac(scorecard.get('completion_rate_4yr_150pct'), ipeds_grad.get('gba4rtt'))}</div></div>
  <div class="stat"><div class="label">Pell Grant Recipients — Grad Rate</div><div class="value">{_pct_int_or_frac(None, ipeds_grad.get('pggrrtt'))}</div></div>
  <div class="stat"><div class="label">Retention (Full-Time)</div><div class="value">{_pct_int_or_frac(None, ipeds_grad.get('ret_pcf'))}</div></div>
  <div class="stat"><div class="label">Retention (Part-Time)</div><div class="value">{_pct_int_or_frac(None, ipeds_grad.get('ret_pcp'))}</div></div>
  <div class="stat"><div class="label">Median Earnings (6yr after entry)</div><div class="value">{_money(scorecard.get('earnings_6yr_median'))}<div class="sub">per year</div></div></div>
  <div class="stat"><div class="label">Median Earnings (10yr)</div><div class="value">{_money(scorecard.get('earnings_10yr_median'))}<div class="sub">per year</div></div></div>
  <div class="stat"><div class="label">Median Debt (Completers)</div><div class="value">{_money(scorecard.get('median_debt_completers'))}</div></div>
  <div class="stat"><div class="label">Repayment Rate (5yr)</div><div class="value">{_pct(scorecard.get('repayment_5yr_rate'))}</div></div>
</div>
</section>
""")

    # BLOCK 04 — Enrollment & Demographics (now full per SEO spec)
    def _demo(k):  # noqa
        return _pct(scorecard.get(k))
    e = enrollment
    parts.append(f"""<section><h2>Enrollment &amp; Demographics</h2>
<div class="grid">
  <div class="stat"><div class="label">Total Enrollment (Fall)</div><div class="value">{_num(e.get('fall_total'))}</div></div>
  <div class="stat"><div class="label">12-month Unduplicated Headcount</div><div class="value">{_num(e.get('enrollment_12mo_total'))}</div></div>
  <div class="stat"><div class="label">Undergraduate Enrollment</div><div class="value">{_num(e.get('ug_12mo'))}</div></div>
  <div class="stat"><div class="label">Graduate Enrollment</div><div class="value">{_num(e.get('grad_12mo'))}</div></div>
  <div class="stat"><div class="label">Full-Time</div><div class="value">{_num(e.get('fall_full_time'))}</div></div>
  <div class="stat"><div class="label">Part-Time</div><div class="value">{_num(e.get('fall_part_time'))}</div></div>
  <div class="stat"><div class="label">Men</div><div class="value">{_num(e.get('men_12mo'))}</div></div>
  <div class="stat"><div class="label">Women</div><div class="value">{_num(e.get('women_12mo'))}</div></div>
  <div class="stat"><div class="label">Online-Only Enrollment</div><div class="value">{_num(e.get('distance_ed_exclusive'))}<div class="sub">exclusively distance ed</div></div></div>
</div>
<h3 style="margin-top:20px;font-size:14px;color:#4a5568;text-transform:uppercase;letter-spacing:0.5px">Race / Ethnicity Breakdown</h3>
<table>
  <tr><th>Race / Ethnicity</th><th>Share</th></tr>
  <tr><td>White</td><td>{_demo('demo_white')}</td></tr>
  <tr><td>Black</td><td>{_demo('demo_black')}</td></tr>
  <tr><td>Hispanic</td><td>{_demo('demo_hispanic')}</td></tr>
  <tr><td>Asian</td><td>{_demo('demo_asian')}</td></tr>
  <tr><td>American Indian / Alaska Native</td><td>{_demo('demo_aian')}</td></tr>
  <tr><td>Native Hawaiian / Pacific Islander</td><td>{_demo('demo_nhpi')}</td></tr>
  <tr><td>Two or More</td><td>{_demo('demo_two_or_more')}</td></tr>
  <tr><td>Non-Resident Alien</td><td>{_demo('demo_non_resident_alien')}</td></tr>
  <tr><td>Unknown</td><td>{_demo('demo_unknown')}</td></tr>
</table>
</section>
""")

    # NEW BLOCK 05 — Academic Programs & Degrees
    DEGREE_OFFER = {1: 'Yes, implied', 2: 'Yes, formal', 3: 'No'}
    DEGREE_TYPE = {0: 'Other', 1: '<2yr Certificate', 2: 'Associates', 3: '2-4yr Certificate',
                   4: "Bachelor's", 5: 'Post-Bach Certificate', 6: "Master's",
                   7: 'Post-Master Cert', 8: 'Doctoral - Research', 9: 'Doctoral - Professional',
                   10: 'Doctoral - Other'}
    top_cips_html = ''.join(
        f'<tr><td>{_esc(_CIP_FAMILY.get(c["cip"], "CIP "+str(c["cip"])))}</td>'
        f'<td>{_num(c["completions"])}</td></tr>'
        for c in (programs.get('top_cips') or [])
    )
    parts.append(f"""<section><h2>Academic Programs &amp; Degrees</h2>
<div class="grid">
  <div class="stat"><div class="label">Highest Degree Offered</div><div class="value" style="font-size:14px">{_esc(DEGREE_TYPE.get(programs.get('hdegofr1'), '—'))}</div></div>
  <div class="stat"><div class="label">Undergrad Programs</div><div class="value" style="font-size:14px">{_esc(DEGREE_OFFER.get(programs.get('ugoffer'), '—'))}</div></div>
  <div class="stat"><div class="label">Graduate Programs</div><div class="value" style="font-size:14px">{_esc(DEGREE_OFFER.get(programs.get('groffer'), '—'))}</div></div>
  <div class="stat"><div class="label">Total Distinct CIP Programs</div><div class="value">{_num(programs.get('program_count'))}</div></div>
</div>
<h3 style="margin-top:20px;font-size:14px;color:#4a5568;text-transform:uppercase;letter-spacing:0.5px">Top Program Areas by Completions</h3>
<table>
  <tr><th>CIP Family</th><th>Completions (latest year)</th></tr>
  {top_cips_html if top_cips_html else '<tr><td colspan=2><i>No program data available</i></td></tr>'}
</table>
</section>
""")

    # NEW BLOCK 06 — Faculty & Staff
    fac = faculty
    parts.append(f"""<section><h2>Faculty &amp; Staff</h2>
<div class="grid">
  <div class="stat"><div class="label">Student-to-Faculty Ratio</div><div class="value">{(str(fac.get('student_faculty_ratio'))+':1') if fac.get('student_faculty_ratio') else '—'}</div></div>
  <div class="stat"><div class="label">Total Instructional Staff</div><div class="value">{_num(fac.get('instr_total_fte'))}</div></div>
  <div class="stat"><div class="label">Full-Time Instructors</div><div class="value">{_num(fac.get('instr_ft'))}</div></div>
  <div class="stat"><div class="label">Part-Time Instructors</div><div class="value">{_num(fac.get('instr_pt'))}</div></div>
</div>
</section>
""")

    # BLOCK 07 — Rankings & Peers (with prominent source attribution)
    parts.append('<section><h2>Peer Institutions</h2>')
    if peers:
        # Prominent source banner
        if "DFR" in peers_source or "Data Feedback" in peers_source:
            src_badge = ('<span class="pill-good">Official — IPEDS DFR</span>',
                         "These are the institutions the school itself chose as its comparison "
                         "group for the IPEDS Data Feedback Report. Authoritative.")
        else:
            src_badge = ('<span class="pill-warn">Algorithmic</span>',
                         "Auto-generated from Carnegie classification + state + control + "
                         "enrollment-size similarity. Used when no official DFR comparison "
                         "group is available.")
        parts.append(
            f'<div style="background:#f7fafc;border-left:3px solid var(--accent);'
            f'padding:10px 14px;margin-bottom:14px;border-radius:4px">'
            f'<strong>Source: {src_badge[0]}</strong>'
            f'<div style="font-size:13px;color:#4a5568;margin-top:4px">{src_badge[1]}</div>'
            f'</div>'
        )
        parts.append('<table><tr><th>Rank</th><th>Institution</th><th>State</th><th>Carnegie</th></tr>')
        for p in peers:
            parts.append(f"<tr><td>{p['rank']}</td><td>{_esc(p['name'])}</td><td>{_esc(p['state'])}</td><td>{_esc(p['carnegie'])}</td></tr>")
        parts.append('</table>')
        parts.append(f'<p style="font-size:12px;color:#a0aec0;margin-top:8px">{_esc(peers_source)}</p>')
    else:
        parts.append('<div class="empty">No peers computed.</div>')
    parts.append('</section>')

    # NEW BLOCK — Research Funding (v1.1 addition)
    rs = research_summary
    parts.append(f"""<section><h2>Research Funding (Last 3 Years)</h2>
<div class="grid">
  <div class="stat"><div class="label">Total Federal Awards</div><div class="value">{_money(rs.get('amount_total_last_3y_usd'))}<div class="sub">across {_num(rs.get('award_count_last_3y'))} awards</div></div></div>
  <div class="stat"><div class="label">NIH</div><div class="value">{_money(rs.get('amount_nih_usd'))}<div class="sub">{_num(rs.get('award_count_nih'))} awards</div></div></div>
  <div class="stat"><div class="label">NSF</div><div class="value">{_money(rs.get('amount_nsf_usd'))}<div class="sub">{_num(rs.get('award_count_nsf'))} awards</div></div></div>
  <div class="stat"><div class="label">USAspending Total</div><div class="value">{_money(rs.get('amount_usaspending_usd'))}<div class="sub">{_num(rs.get('award_count_usaspending'))} awards</div></div></div>
</div>
</section>
""")

    # BLOCK 09 — Institutional Research at <name>
    parts.append(f'<section><h2>Institutional Research at {_esc(name)}</h2>')
    if ir_page.get("page_url"):
        parts.append('<div class="grid">')
        parts.append(f'<div class="stat"><div class="label">Office</div><div class="value" style="font-size:16px">{_esc(ir_page.get("office_name") or "(unnamed)")}</div></div>')
        if ir_page.get("parent_division"):
            parts.append(f'<div class="stat"><div class="label">Reports To</div><div class="value" style="font-size:16px">{_esc(ir_page["parent_division"])}</div></div>')
        if ir_page.get("office_phone"):
            parts.append(f'<div class="stat"><div class="label">Phone</div><div class="value" style="font-size:16px">{_esc(ir_page["office_phone"])}</div></div>')
        if ir_page.get("office_email"):
            parts.append(f'<div class="stat"><div class="label">Office Email</div><div class="value" style="font-size:14px"><a href="mailto:{_esc(ir_page["office_email"])}">{_esc(ir_page["office_email"])}</a></div></div>')
        if ir_page.get("office_fax"):
            parts.append(f'<div class="stat"><div class="label">Fax</div><div class="value" style="font-size:14px">{_esc(ir_page["office_fax"])}</div></div>')
        if ir_page.get("confidence") is not None:
            conf_pill = '<span class="pill-good">strong match</span>' if (ir_page.get("confidence") or 0) >= 0.7 else '<span class="pill-warn">moderate confidence</span>'
            parts.append(f'<div class="stat"><div class="label">Validation</div><div class="value" style="font-size:14px">{conf_pill}</div></div>')
        parts.append('</div>')
        if ir_page.get("office_address"):
            parts.append(f'<p style="margin-top:14px;color:#4a5568"><strong>Address:</strong> {_esc(ir_page["office_address"])}</p>')
        parts.append(f'<p style="margin-top:14px"><a href="{_esc(ir_page["page_url"])}" target="_blank">Visit IR office page →</a></p>')
        if ir_page.get("page_summary"):
            parts.append(f'<p>{_esc(ir_page["page_summary"])}</p>')
        if ir_contacts:
            parts.append('<h3 style="margin-top:18px;font-size:14px;color:#4a5568;text-transform:uppercase;letter-spacing:0.5px">Team</h3>')
            parts.append('<table><tr><th>Name</th><th>Title</th><th>Email</th><th>Phone</th><th>LinkedIn</th></tr>')
            for c in ir_contacts[:20]:
                li_url = c.get("linkedin_url")
                li_cell = (f'<a href="{_esc(li_url)}" target="_blank" rel="noopener" '
                           f'title="LinkedIn profile">in →</a>') if li_url else ""
                parts.append(
                    f"<tr><td>{_esc(c.get('name'))}</td>"
                    f"<td>{_esc(c.get('title'))}</td>"
                    f"<td>{_esc(c.get('email'))}</td>"
                    f"<td>{_esc(c.get('phone'))}</td>"
                    f"<td>{li_cell}</td></tr>"
                )
            parts.append('</table>')
        else:
            parts.append('<div class="empty" style="margin-top:14px">Institution does not publish individual team members on this page.</div>')
    else:
        parts.append('<div class="empty">IR office page not yet discovered.</div>')
    parts.append('</section>')

    # BLOCK 10 — Common Data Set
    parts.append('<section><h2>Common Data Set</h2>')
    if cds_links:
        parts.append('<ul class="links">')
        for c in sorted(cds_links, key=lambda x: x.get("year") or 0, reverse=True):
            parts.append(f'<li><strong>{c.get("year") or "?"}</strong> — <a href="{_esc(c.get("cds_url"))}" target="_blank">{_esc(c.get("cds_url"))[:80]}</a></li>')
        parts.append('</ul>')
    else:
        parts.append('<div class="empty">No CDS link discovered yet.</div>')
    parts.append('</section>')

    # Documents — grouped by category, sorted by year DESC within each category
    DOC_TYPE_LABELS = {
        "dfr_report":      "IPEDS Data Feedback Report",
        "factbook":        "Factbook",
        "strategic_plan":  "Strategic Plan",
        "data_dictionary": "Data Dictionary",
        "data_definition": "Data Definitions",
        "glossary":        "Glossary",
        "annual_report":   "Annual Report",
        "cds_pdf":         "Common Data Set (PDF)",
        "dashboard_link":  "Dashboard",
        "other":           "Other",
    }
    DOC_TYPE_ORDER = list(DOC_TYPE_LABELS.keys())

    parts.append('<section><h2>Documents &amp; Reports</h2>')
    if documents or cds_links:
        # Build a unified pile: documents + cds_links (treated as "Common Data Set")
        bucket: dict[str, list[dict]] = {}
        for d in documents:
            doc_type = d.get("doc_type") or "other"
            bucket.setdefault(doc_type, []).append({
                "title": d.get("title") or "(untitled)",
                "year": d.get("year"),
                "url": d.get("doc_url"),
                "has_file": bool(d.get("storage_path")),
                "summary": d.get("summary"),
                "pages": d.get("page_count"),
            })
        for c in cds_links:
            bucket.setdefault("common_data_set", []).append({
                "title": f"Common Data Set {c.get('year') or ''}".strip(),
                "year": c.get("year"),
                "url": c.get("cds_url"),
                "has_file": bool(c.get("is_pdf")),
                "summary": None,
                "pages": None,
            })
        DOC_TYPE_LABELS["common_data_set"] = "Common Data Set"
        # Custom order with common_data_set right after factbook
        order = ["dfr_report", "factbook", "common_data_set", "data_dictionary",
                 "data_definition", "glossary", "strategic_plan", "annual_report",
                 "cds_pdf", "dashboard_link", "other"]

        for doc_type in order:
            if doc_type not in bucket:
                continue
            label = DOC_TYPE_LABELS.get(doc_type, doc_type)
            items = sorted(bucket[doc_type], key=lambda x: x.get("year") or 0, reverse=True)
            parts.append(f'<h3 style="margin-top:18px;font-size:15px;color:var(--accent)">{_esc(label)} <span style="color:#a0aec0;font-weight:400;font-size:12px">({len(items)})</span></h3>')
            parts.append('<table style="margin-top:6px"><tr><th style="width:80px">Year</th><th>Title</th><th>Link</th><th style="width:80px">Stored</th></tr>')
            for it in items:
                yr = it["year"] or "—"
                stored = '<span class="pill-good">file</span>' if it["has_file"] else '<span class="pill-warn">link only</span>'
                parts.append(
                    f'<tr><td><strong>{_esc(yr)}</strong></td>'
                    f'<td>{_esc(it["title"])}'
                    + (f'<div style="font-size:12px;color:#718096;margin-top:2px">{_esc((it["summary"] or "")[:160])}</div>' if it.get("summary") else "")
                    + f'</td>'
                    f'<td><a href="{_esc(it["url"])}" target="_blank">{_esc((it["url"] or "")[:60])}…</a></td>'
                    f'<td>{stored}</td></tr>'
                )
            parts.append('</table>')
    else:
        parts.append('<div class="empty">No documents discovered yet.</div>')
    parts.append('</section>')

    # NEW BLOCK — IR Jobs (from HigherEdJobs + institution careers)
    ir_jobs_list = [j for j in jobs if (j.get("category") or "").lower() == "ir"]
    parts.append(f'<section><h2>IR Job Openings at {_esc(name)}</h2>')
    if ir_jobs_list:
        parts.append('<table><tr><th>Title</th><th>Source</th><th>Apply Link</th></tr>')
        for j in ir_jobs_list[:20]:
            src = (j.get("source_board") or "").replace("_", " ").title() or "Institution Site"
            parts.append(f"<tr><td>{_esc(j.get('title'))}</td>"
                         f"<td>{_esc(src)}</td>"
                         f"<td><a href=\"{_esc(j.get('apply_url'))}\" target=\"_blank\">{_esc((j.get('apply_url') or '')[:60])}…</a></td></tr>")
        parts.append('</table>')
        parts.append(f'<p style="font-size:12px;color:#718096;margin-top:6px">{len(ir_jobs_list)} current IR-related opening(s). Source: HigherEdJobs + institution careers pages.</p>')
    else:
        parts.append('<div class="empty">No current IR job postings discovered (institution may not be hiring or may use a vendor portal we can\'t parse).</div>')
    parts.append('</section>')

    # NEW BLOCK — Grants (institution-published + SAM.gov opportunities/awards)
    parts.append(f'<section><h2>Grants &amp; Funding Opportunities</h2>')
    if grants_rows:
        # Group by source_system
        by_src: dict[str, list] = {}
        for g in grants_rows:
            by_src.setdefault(g.get("source_system") or "other", []).append(g)
        SRC_LABEL = {"sam_gov": "SAM.gov (Federal Opportunities/Awards)",
                     "institution_site": "Institution Sponsored Programs",
                     "grants_gov": "Grants.gov",
                     "other": "Other"}
        for src, items in by_src.items():
            label = SRC_LABEL.get(src, src)
            parts.append(f'<h3 style="margin-top:18px;font-size:15px;color:var(--accent)">{_esc(label)} <span style="color:#a0aec0;font-weight:400;font-size:12px">({len(items)})</span></h3>')
            parts.append('<table><tr><th>Title</th><th>Agency</th><th>Type</th><th>Amount</th><th>Link</th></tr>')
            for g in items[:15]:
                kind = (g.get("kind") or "").lower()
                pill = '<span class="pill-good">opportunity</span>' if kind == "opportunity" else ('<span class="pill-warn">award</span>' if kind == "award" else _esc(kind))
                amt = _money(g.get("amount_usd")) if g.get("amount_usd") else "—"
                parts.append(f"<tr><td>{_esc((g.get('title') or '')[:120])}</td>"
                             f"<td>{_esc(g.get('agency') or '—')}</td>"
                             f"<td>{pill}</td>"
                             f"<td>{amt}</td>"
                             f"<td><a href=\"{_esc(g.get('source_url'))}\" target=\"_blank\">open</a></td></tr>")
            parts.append('</table>')
    else:
        parts.append('<div class="empty">No grants discovered yet.</div>')
    parts.append('</section>')

    # BLOCK 08 — Notable Alumni
    parts.append('<section><h2>Notable Alumni</h2>')
    if notable_alumni:
        parts.append('<ul class="links">')
        for a in notable_alumni[:15]:
            url = a.get("wikipedia_url")
            label = _esc(a.get("name"))
            link = f'<a href="{_esc(url)}" target="_blank">{label}</a>' if url else label
            parts.append(f'<li>{link} <span style="color:#718096;font-size:13px">— {_esc((a.get("short_bio") or "")[:140])}</span></li>')
        parts.append('</ul>')
    else:
        parts.append('<div class="empty">No alumni list scraped yet.</div>')
    parts.append('</section>')

    parts.append(f"""<footer>
  Generated by Clema Landing Page Builder · slug <code>{_esc(row['slug'])}</code> · unitid {row['unitid']} · UEI {_esc(row.get('ueis'))}
</footer>
</body></html>""")
    return "".join(parts)


@click.command()
@click.option("--unitid", type=int, default=None)
@click.option("--slug", type=str, default=None)
@click.option("--output", type=click.Path(dir_okay=False, writable=True, path_type=Path), required=True)
def main(unitid: int | None, slug: str | None, output: Path) -> None:
    if not unitid and not slug:
        raise click.UsageError("provide --unitid or --slug")
    html = render(unitid=unitid, slug=slug)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html)
    click.echo(f"wrote {output} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()

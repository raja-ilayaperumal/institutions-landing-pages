"""Render landing pages for every pilot_cohort institution + an index page.

Usage:
    python scripts/render_all_pilots.py
    python scripts/render_all_pilots.py --output-dir docs/preview
"""
from __future__ import annotations

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.db.engine import get_conn  # noqa: E402

from render_page import render  # noqa: E402


def _index_html(rows: list[dict]) -> str:
    items = []
    for r in rows:
        items.append(f"""<li>
  <a href="{r['slug']}.html"><strong>{r['name']}</strong></a>
  <div style="color:#718096;font-size:12px;margin-top:2px">
    {r.get('city') or ''}, {r.get('state_name') or ''} ·
    {r.get('control_label') or ''} ·
    {r.get('carnegie_basic_label') or ''}
  </div>
</li>""")
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Clema Institution Landing Pages — Pilot</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 24px; color: #1a202c; }}
  h1 {{ color: #1a365d; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 14px 0; border-bottom: 1px solid #e2e8f0; }}
  a {{ color: #2c5282; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .meta {{ color: #718096; font-size: 13px; margin-top: 14px; }}
</style></head><body>
<h1>Clema Institution Landing Pages — Pilot ({len(rows)})</h1>
<p class="meta">Generated from <code>landing.mv_institution_page</code>.
Each page is fully populated from federal sources (IPEDS, College Scorecard,
NIH, NSF, USAspending) + scraped institutional pages.</p>
<ul>
{''.join(items)}
</ul>
</body></html>
"""


@click.command()
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), default="docs/preview")
def main(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT li.unitid, li.slug, mv.institution_name AS name,
                   mv.city, mv.state_name, mv.control_label, mv.carnegie_basic_label
            FROM landing.institutions li
            JOIN landing.mv_institution_page mv USING (unitid)
            WHERE li.pilot_cohort
            ORDER BY mv.state_slug, li.unitid
        """)
        rows = cur.fetchall()

    for r in rows:
        try:
            html = render(unitid=r["unitid"])
            (output_dir / f"{r['slug']}.html").write_text(html)
            click.echo(f"  ✓ {r['slug']}.html")
        except Exception as e:  # noqa: BLE001
            click.echo(f"  ✗ {r['slug']}.html: {e}")

    (output_dir / "index.html").write_text(_index_html(rows))
    click.echo(f"\nIndex: {output_dir}/index.html  ({len(rows)} institutions)")


if __name__ == "__main__":
    main()

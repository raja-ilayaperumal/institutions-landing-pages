"""Backfill posted_at / inactive_at / response_deadline_at on existing
landing.grants rows by re-querying SAM.gov for each row whose dates we never
captured.

SAM.gov exposes an unauthenticated browser-API at
  https://sam.gov/api/prod/opps/v2/opportunities/{noticeId}
which returns JSON with `postedDate`, `data2.archive.date` (the "Original
Inactive Date" — the hard expiry) and `data2.solicitation.deadlines.response`
(the proposal response deadline). We extract the 32-char hex notice ID from
the existing source_url and call the API one row at a time with a small
delay so we stay polite.

Run:  python scripts/backfill_grants_dates.py            # all sam_gov rows missing dates
      python scripts/backfill_grants_dates.py --unitid 166683    # just MIT
      python scripts/backfill_grants_dates.py --dry-run          # show, don't write
"""
from __future__ import annotations

import re
import time
from datetime import datetime

import click
import httpx
import psycopg
from psycopg.rows import dict_row


NOTICE_ID_RE = re.compile(r"/opp/([0-9a-fA-F]{32})/")

DB_DSN = "host=localhost user=rajailayaperumal dbname=clema_landing"
API_TMPL = "https://sam.gov/api/prod/opps/v2/opportunities/{notice_id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (clema-landing backfill)",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://sam.gov",
    "Referer": "https://sam.gov/",
}


def _extract_notice_id(url: str | None) -> str | None:
    if not url:
        return None
    m = NOTICE_ID_RE.search(url)
    return m.group(1) if m else None


def _parse_iso_date(value) -> "datetime.date | None":
    if not value:
        return None
    s = str(value)
    # ISO-8601 with or without time zone, or plain YYYY-MM-DD
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def fetch_sam_dates(client: httpx.Client, notice_id: str) -> dict | None:
    """Return {'posted_at', 'inactive_at', 'response_deadline_at'} or None on failure."""
    try:
        r = client.get(API_TMPL.format(notice_id=notice_id), timeout=15.0)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    data2 = d.get("data2") or {}
    archive = data2.get("archive") or {}
    deadlines = (data2.get("solicitation") or {}).get("deadlines") or {}
    return {
        "posted_at": _parse_iso_date(d.get("postedDate")),
        "inactive_at": _parse_iso_date(archive.get("date")),
        "response_deadline_at": _parse_iso_date(deadlines.get("response")),
    }


@click.command()
@click.option("--unitid", type=int, default=None, help="Backfill only this institution")
@click.option("--dry-run", is_flag=True, help="Print what would update, don't write")
@click.option("--sleep", default=0.3, help="Seconds between API calls (politeness)")
def main(unitid: int | None, dry_run: bool, sleep: float) -> None:
    where_unit = "AND unitid = %s" if unitid is not None else ""
    params = (unitid,) if unitid is not None else ()

    with psycopg.connect(DB_DSN, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, unitid, source_url, title FROM landing.grants
                    WHERE source_system='sam_gov'
                      AND (posted_at IS NULL OR inactive_at IS NULL)
                      {where_unit}
                    ORDER BY unitid, id""",
                params,
            )
            rows = cur.fetchall()

        click.echo(f"Found {len(rows)} sam_gov rows missing dates")
        if not rows:
            return

        updated = skipped = failed = 0
        with httpx.Client(headers=HEADERS) as client:
            for r in rows:
                notice_id = _extract_notice_id(r["source_url"])
                if not notice_id:
                    click.echo(f"  ! id={r['id']} no notice_id in {r['source_url']}")
                    skipped += 1
                    continue

                dates = fetch_sam_dates(client, notice_id)
                if dates is None:
                    click.echo(f"  ✗ id={r['id']} API fetch failed for {notice_id}")
                    failed += 1
                    time.sleep(sleep)
                    continue

                tag = (
                    f"posted={dates['posted_at']} "
                    f"inactive={dates['inactive_at']} "
                    f"deadline={dates['response_deadline_at']}"
                )
                click.echo(f"  ✓ id={r['id']:>4} u={r['unitid']}  {tag}  {(r['title'] or '')[:50]}")

                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE landing.grants
                                  SET posted_at = COALESCE(%s, posted_at),
                                      inactive_at = COALESCE(%s, inactive_at),
                                      response_deadline_at = COALESCE(%s, response_deadline_at),
                                      fetched_at = NOW()
                                WHERE id = %s""",
                            (dates["posted_at"], dates["inactive_at"],
                             dates["response_deadline_at"], r["id"]),
                        )
                updated += 1
                time.sleep(sleep)

    click.echo(f"\nUpdated: {updated}  Failed: {failed}  Skipped: {skipped}")


if __name__ == "__main__":
    main()

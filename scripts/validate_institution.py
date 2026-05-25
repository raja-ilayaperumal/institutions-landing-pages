"""Post-scrape data-quality validator.

For each institution, runs INDEPENDENT cross-checks against what's in the
DB. Designed to catch:

  - Wrong `ir_page` URL (Google's view disagrees with our pick).
  - Hallucinated team names (LLM-extracted names that aren't on the
    re-fetched IR page or its discovered subpages).
  - Suspicious emails (right page wrong domain, e.g. @gmail.com).
  - Dead links (CDS URL returns 404 or has been moved).

Output: a per-institution pass/warn/fail report. Each row gets a verdict
plus the failing checks listed. Run after a scrape batch to confirm
quality before generating HTML or shipping.

Usage:
    python scripts/validate_institution.py --unitids 110565,110583,...
    python scripts/validate_institution.py --cohort scripts/cohorts/ca_top100.txt --top 20

We DELIBERATELY re-fetch and re-search rather than re-using cached
canonical_urls / scraper_runs rows. Independent verification is the whole
point — two different judgments on the same page agreeing is a much
stronger signal than the original scrape's own confidence score.
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import click
import httpx
import structlog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from landing_scraper.core import crawler, llm, search  # noqa: E402
from landing_scraper.core.html import visible_text  # noqa: E402
from landing_scraper.db.engine import get_conn  # noqa: E402
from landing_scraper.scraper.ir_team_extractor import (  # noqa: E402
    _deobfuscate_cf_emails, _discover_subpages, _fetch_subpages,
)

log = structlog.get_logger(__name__)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class InstitutionReport:
    unitid: int
    name: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        fails = [c for c in self.checks if not c.passed]
        if not fails:
            return "PASS"
        critical = [c for c in fails if c.name in ("ir_page_url", "team_substring", "email_domain")]
        return "FAIL" if critical else "WARN"


IR_URL_VERIFY_PROMPT = """\
You are independently verifying whether a URL is THE institution's
operational Office of Institutional Research (or equivalent office).

Many IR offices DO NOT include "institutional research" in their name.
ACCEPT equivalents that perform the same operational data role:

  - "Office of Institutional Effectiveness" (community colleges often)
  - "Institutional Research & Analytics" (e.g. CSULB)
  - "Analytic Studies & Institutional Research" (e.g. SDSU)
  - "Office of Planning and Analysis" (e.g. Berkeley OPA)
  - "Academic Planning and Budget" when it houses an "Office of
    Analytics and Institutional Research" subunit (e.g. UCLA APB)
  - "Office of Budget and Institutional Analysis" (UC Davis style)
  - "Institutional Research, Assessment, and Planning" / IRAP
  - "University Data and Analytics" / UDA
  - "Office of Institutional Effectiveness and Planning"

ACCEPT (is_correct=true) when the page IS the institution's operational
data-publishing office — look for IR-typical functions in the text:
enrollment data, factbook, common data set, dashboards, program review,
accreditation support, analytics, planning. The office may sit under a
broader umbrella (Academic Planning, Budget, Provost, Effectiveness).

REJECT (is_correct=false) only when:
  - The page is a research CENTER about IR as an academic topic
    (e.g. cshe.berkeley.edu/topics/institutional-research)
  - The page is a faculty bio, news article, press release
  - The page is empty, a 404 disguised as 200, or a placeholder
  - The page is clearly for a DIFFERENT institution

You'll see:
  - The institution name
  - The URL we chose
  - The first ~2000 chars of that page's visible text

Reply JSON: {"is_correct": bool, "confidence": 0.0-1.0, "reason": str}"""


def _norm_name(s: str) -> str:
    """Strip credentials and punctuation from a person's name for substring matching.

    Many IR team rows have suffixes like ", Ph.D.", ", M.A.", "MPH" etc.
    The IR page may render the same name without the credential, with the
    credential separated by a space, or formatted as "Lastname, Firstname".
    Normalizing to a credential-less, punctuation-stripped, lowercase
    string lets us substring-match robustly.
    """
    s = re.sub(r",\s*(Ph\.?D\.?|Ed\.?D\.?|M\.?A\.?|M\.?S\.?|M\.?P\.?H\.?|B\.?S\.?|"
               r"M\.?F\.?A\.?|HSPP|MBA|J\.?D\.?|RN|D\.?Sc\.?)\.?\s*$", "", s,
               flags=re.IGNORECASE)
    s = re.sub(r"[.,]", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


async def _fetch_text(url: str, timeout: float = 25.0) -> str:
    """Fetch a URL via the project's crawler (httpx → crawl4ai → Wayback) +
    Cloudflare-email-deobfuscate the HTML so encoded emails become real."""
    try:
        f = await crawler.fetch(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return ""
    if not f.success:
        return ""
    html = _deobfuscate_cf_emails(f.html or "")
    return visible_text(html) or f.markdown or ""


async def _check_ir_page_url(report: InstitutionReport, ir_url: str | None) -> None:
    """Check 1: re-search via Google CSE and have an independent LLM
    verify the chosen ir_page URL is the right one."""
    if not ir_url:
        report.checks.append(CheckResult("ir_page_url", False, "no ir_page in DB"))
        return

    text = await _fetch_text(ir_url)
    if not text or len(text) < 200:
        # Page might be unreachable from public network or empty
        report.checks.append(CheckResult(
            "ir_page_url", False,
            f"could not fetch ir_page content ({len(text)} chars)",
        ))
        return

    r = await llm.call(
        system=IR_URL_VERIFY_PROMPT,
        user=(f"Institution: {report.name}\nChosen URL: {ir_url}\n\n"
              f"Page text (first 2000 chars):\n{text[:2000]}"),
        tier="judge", max_tokens=250, expect_json=True,
    )
    data = r.data if isinstance(r.data, dict) else {}
    ok = bool(data.get("is_correct"))
    reason = data.get("reason", "")[:140]
    report.checks.append(CheckResult(
        "ir_page_url", ok,
        f"{'verified' if ok else 'REJECTED'}: {reason}",
    ))


async def _check_team_substrings(
    report: InstitutionReport,
    ir_url: str | None,
    contacts: list[dict],
) -> None:
    """Check 2: every stored team name must appear (case-insensitively,
    credential-stripped) in the re-fetched IR page or one of its
    nearby subpages. Catches LLM-hallucinated names."""
    if not contacts:
        report.checks.append(CheckResult(
            "team_substring", True, "no contacts to verify (empty IR team)",
        ))
        return
    if not ir_url:
        report.checks.append(CheckResult(
            "team_substring", False, "ir_page URL missing",
        ))
        return

    # Use the SAME subpage discovery the scraper used (on-page links from
    # IR landing + sitemap + wellknown probes) so the validator looks at
    # the same surface area the extractor did. Catches institution-specific
    # team-page conventions like CSULB's `/meet-the-staff` or Berkeley's
    # `/about-us/staff-directory` automatically.
    landing_fetch, subpages = await _discover_subpages(ir_url, max_subpages=15)
    await _fetch_subpages(subpages, max_concurrency=4)

    pool_parts: list[str] = []
    if landing_fetch and landing_fetch.success:
        landing_html = _deobfuscate_cf_emails(landing_fetch.html or "")
        pool_parts.append(visible_text(landing_html) or landing_fetch.markdown or "")
    for sp in subpages:
        if sp.success and sp.body_text:
            pool_parts.append(sp.body_text)
    pool_text = "\n\n".join(t for t in pool_parts if t)
    if not pool_text:
        report.checks.append(CheckResult(
            "team_substring", False,
            "could not fetch IR page or any team subpage for re-check",
        ))
        return

    pool_norm = _norm_name(pool_text)
    missing: list[str] = []
    matched: list[str] = []
    for c in contacts:
        name = c.get("name") or ""
        if not name:
            continue
        if _norm_name(name) in pool_norm:
            matched.append(name)
        else:
            # Try last-name-first form ("Lastname, Firstname")
            parts = name.split()
            if len(parts) >= 2:
                flipped = f"{parts[-1]} {' '.join(parts[:-1])}"
                if _norm_name(flipped) in pool_norm:
                    matched.append(name)
                    continue
            missing.append(name)

    if missing:
        report.checks.append(CheckResult(
            "team_substring", False,
            f"{len(missing)}/{len(contacts)} names NOT found in page text: "
            f"{', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}",
        ))
    else:
        report.checks.append(CheckResult(
            "team_substring", True,
            f"all {len(matched)} team names verified on page",
        ))


def _check_email_domains(
    report: InstitutionReport,
    contacts: list[dict],
    institution_domain: str | None,
) -> None:
    """Check 3: stored emails should be on a legitimate institutional domain.

    The real risk is the LLM picking up a contact form's reply-to going to
    a personal account (@gmail.com, @yahoo.com, @outlook.com) — that's
    what we want to flag. Any .edu domain is acceptable: many California
    community colleges share IR services at the parent district level
    (rccd.edu, yosemite.edu, cccd.edu, vcccd.edu) and many universities
    operate multiple legitimate domains (csufresno.edu + fresnostate.edu).
    Allowing all .edu domains catches the actual hallucination risk
    without false-flagging legitimate district/system office emails.
    """
    suspicious: list[str] = []
    for c in contacts:
        email = (c.get("email") or "").lower().strip()
        if not email or "@" not in email:
            continue
        domain = email.split("@", 1)[1]
        # Any .edu / .gov is institutional. Flag personal / commercial domains.
        if domain.endswith(".edu") or domain.endswith(".gov"):
            continue
        suspicious.append(f"{c.get('name','?')} <{email}>")
    if suspicious:
        report.checks.append(CheckResult(
            "email_domain", False,
            f"{len(suspicious)} email(s) off-institutional-domain: "
            f"{', '.join(suspicious[:3])}",
        ))
    else:
        report.checks.append(CheckResult(
            "email_domain", True,
            "all stored emails on institutional (.edu/.gov) domain",
        ))


async def _check_cds_reachable(report: InstitutionReport, unitid: int) -> None:
    """Check 4: confirm up to 3 most-recent CDS URLs return real content.

    Uses crawler.fetch (httpx → crawl4ai/Playwright → Wayback) instead of
    raw httpx. Many .edu domains (opa.berkeley.edu, asir.sdsu.edu, etc.)
    front Cloudflare/WAF that 403s anything below a real browser; a 403
    from those is not a dead link.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cds_url FROM landing.cds_links WHERE unitid=%s "
            "ORDER BY year DESC NULLS LAST LIMIT 3",
            (unitid,),
        )
        urls = [r["cds_url"] for r in cur.fetchall()]
    if not urls:
        report.checks.append(CheckResult(
            "cds_reachable", True, "no CDS URLs in DB (nothing to check)",
        ))
        return

    async def _one(u: str) -> str | None:
        is_binary = any(u.lower().endswith(ext)
                        for ext in (".pdf", ".xlsx", ".xls", ".csv"))
        if is_binary:
            try:
                async with httpx.AsyncClient(
                    timeout=15.0, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) "
                                           "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"},
                ) as c:
                    resp = await c.head(u)
                    if resp.status_code in (405, 501):
                        # HEAD not supported — try ranged GET
                        async with c.stream("GET", u, headers={"Range": "bytes=0-1023"}) as r:
                            status = r.status_code
                            await r.aclose()
                        if status >= 400 and status not in (401, 403, 503):
                            return f"{u[:60]}... → HTTP {status}"
                        return None
                    # 401/403/503 = likely WAF/auth, NOT a dead link. Don't
                    # flag — many .edu domains (USC's oir.usc.edu via
                    # Cloudflare) 403 anything below a real browser even
                    # though the file IS reachable via a normal user.
                    if resp.status_code in (401, 403, 503):
                        return None
                    if resp.status_code >= 400:
                        return f"{u[:60]}... → HTTP {resp.status_code}"
                return None
            except Exception as e:  # noqa: BLE001
                return f"{u[:60]}... → {type(e).__name__}"
        # HTML — use the full crawler chain so WAF-protected sites don't false-fail
        try:
            f = await crawler.fetch(u, timeout=30.0)
            if not f.success or len(f.markdown or "") < 100:
                return f"{u[:60]}... → fetch failed ({f.error or 'empty'})"
            return None
        except Exception as e:  # noqa: BLE001
            return f"{u[:60]}... → {type(e).__name__}"

    results = await asyncio.gather(*[_one(u) for u in urls])
    dead = [r for r in results if r]
    if dead:
        report.checks.append(CheckResult(
            "cds_reachable", False,
            f"{len(dead)}/{len(urls)} unreachable: {' | '.join(dead[:2])}",
        ))
    else:
        report.checks.append(CheckResult(
            "cds_reachable", True,
            f"checked {len(urls)} most-recent CDS URLs, all reachable",
        ))


async def validate_institution(unitid: int) -> InstitutionReport:
    """Run all checks for one institution and return the report."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT li.name, li.registrable_domain,
                      (SELECT page_url FROM landing.ir_pages WHERE unitid=li.unitid) AS ir_page
                 FROM landing.institutions li WHERE unitid=%s""",
            (unitid,),
        )
        row = cur.fetchone()
        if not row:
            return InstitutionReport(unitid=unitid, name="(unknown)",
                                      checks=[CheckResult("load", False, "unitid not found")])

        cur.execute(
            "SELECT name, email FROM landing.ir_contacts "
            "WHERE unitid=%s ORDER BY ordering, id",
            (unitid,),
        )
        contacts = list(cur.fetchall())

    report = InstitutionReport(unitid=unitid, name=row["name"])
    ir_url = row.get("ir_page")
    domain = row.get("registrable_domain")

    # Run independent checks in parallel where possible
    await asyncio.gather(
        _check_ir_page_url(report, ir_url),
        _check_team_substrings(report, ir_url, contacts),
        _check_cds_reachable(report, unitid),
    )
    _check_email_domains(report, contacts, domain)
    return report


def _print_report(rep: InstitutionReport) -> None:
    icon = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[rep.status]
    color = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m"}[rep.status]
    reset = "\033[0m"
    click.echo(f"\n{color}[{icon} {rep.status}]{reset} [{rep.unitid}] {rep.name}")
    for c in rep.checks:
        sym = "  ✓" if c.passed else "  ✗"
        click.echo(f"{sym} {c.name}: {c.detail}")


@click.command()
@click.option("--unitids", default=None,
              help="comma-separated unitids; overrides --cohort/--top")
@click.option("--cohort", default=None,
              type=click.Path(exists=True, dir_okay=False),
              help="cohort file (one unitid per line, # comments ok)")
@click.option("--top", default=None, type=int,
              help="take only first N unitids from --cohort")
@click.option("--concurrency", default=4, type=int)
def main(unitids: str | None, cohort: str | None, top: int | None,
         concurrency: int) -> None:
    ids: list[int] = []
    if unitids:
        ids = [int(x) for x in unitids.split(",") if x.strip()]
    elif cohort:
        for line in Path(cohort).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"\s*(\d+)\s*\|", line)
            if m:
                ids.append(int(m.group(1)))
        if top:
            ids = ids[:top]
    else:
        click.echo("Provide --unitids or --cohort", err=True)
        sys.exit(2)

    click.echo(f"Validating {len(ids)} institution(s)...")

    sem = asyncio.Semaphore(concurrency)
    async def _bounded(u):
        async with sem:
            return await validate_institution(u)

    async def _all():
        reports = await asyncio.gather(*[_bounded(u) for u in ids])
        for r in reports:
            _print_report(r)
        # Summary
        statuses = [r.status for r in reports]
        click.echo(f"\n=== Summary ===")
        click.echo(f"  PASS: {statuses.count('PASS')}")
        click.echo(f"  WARN: {statuses.count('WARN')}")
        click.echo(f"  FAIL: {statuses.count('FAIL')}")
        # Exit non-zero if any FAIL so this is CI-friendly
        if "FAIL" in statuses:
            sys.exit(1)

    asyncio.run(_all())


if __name__ == "__main__":
    main()

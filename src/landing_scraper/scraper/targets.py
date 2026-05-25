"""Target registry — one entry per scrapable page type.

A TargetSpec defines everything the pipeline needs to find + validate +
extract a given page type for an institution: search query templates,
crawl keywords, URL pattern signals, LLM prompts, and which landing.* table
to write to.

Add a new page type by adding a new entry to TARGETS. The pipeline picks it
up automatically; CLI gains a new --target option; the DB schema must already
have a content table OR you must accept that we only store provenance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class TargetSpec:
    """How to find + validate + extract one type of institutional page."""
    name: str                              # source_type (must exist in landing.source_types)
    description: str

    # ── Discovery ───────────────────────────────────────────────────────
    # Search query templates (lowercased). Placeholders: {name}, {city}, {state}
    # Each is run as `site:<registrable_domain> <query>`.
    search_queries: tuple[str, ...]
    # Extra non-site search queries (when site: is too restrictive or for cross-domain context)
    search_queries_open: tuple[str, ...] = ()
    # Keywords for crawl4ai BestFirstCrawlingStrategy scoring (deep crawl fallback)
    crawl_keywords: tuple[str, ...] = ()
    # Positive URL signals: regex that bumps score when matched
    url_patterns_positive: tuple[str, ...] = ()
    # Negative URL signals: regex that disqualifies (e.g. news article, login page)
    url_patterns_negative: tuple[str, ...] = (
        r"/news/", r"/press-release", r"/login", r"/search\?", r"\.(jpg|jpeg|png|gif|webp|svg|css|js)$",
    )
    # Whether to try sitemap.xml-based discovery (most useful for IR pages, factbook, CDS)
    use_sitemap: bool = True
    # Whether to fall back to crawl4ai deep crawl when search yields thin results
    use_deep_crawl: bool = True
    # Whether to fetch the validated URL and store raw payload (False for URL-only sources like cds)
    fetch_payload: bool = True

    # Well-known subdomain prefixes to probe (e.g. 'ir' → tries https://ir.<domain>/).
    # High precision; institutions follow strong naming conventions for IR pages.
    wellknown_subdomains: tuple[str, ...] = ()
    # Well-known root-relative paths to probe (e.g. '/institutional-research' tries every host
    # the institution uses, but typically just <canonical_root_url>/<path>).
    wellknown_paths: tuple[str, ...] = ()

    # ── Validation (LLM judge) ──────────────────────────────────────────
    # System prompt for the LLM judge. Must instruct JSON response with:
    #   { "is_match": bool, "confidence": float (0-1), "reason": str }
    validator_system: str = ""

    # ── Extraction ──────────────────────────────────────────────────────
    # System prompt for the LLM extractor. Must instruct JSON shape.
    extractor_system: str = ""
    # Max chars of page text to send to extractor (keep cost predictable)
    extractor_text_budget: int = 30_000

    # Cross-domain hosts allowed in open_search (e.g. higheredjobs.com for jobs)
    allow_external_hosts: tuple[str, ...] = ()

    # Allow disabling pieces when in a constrained environment
    enabled: bool = True


# ============================================================================
# REGISTRY
# ============================================================================

IR_PAGE_VALIDATOR = """\
You are picking the BEST URL to represent the institution's Office of
Institutional Research on a landing page directory. Equivalent offices count:
institutional effectiveness, institutional analytics, institutional planning,
university data and analytics (UDA), decision support, office of planning &
research.

ALWAYS prefer (in this order, higher wins):
  1. The OFFICE'S HOMEPAGE — the index/landing page of the office's web
     section, on the institution's own domain. Hostnames like
     uda.<inst>.edu, ir.<inst>.edu, oir.<inst>.edu, ie.<inst>.edu, or paths
     like /institutional-research/, /institutional-analytics/.
  2. A clear "About" / "Mission" page of that office.

NEVER pick (reject even if it mentions IR):
  - A single survey PDF, a dataset file, an old report PDF, a form
  - A faculty bio, a news article, a press release
  - A research-center page, a student handbook section
  - A page under /News/, /Surveys/, /Forms/, /Reports/<year>/

If you see both a deep PDF/leaf URL AND the office homepage in the
candidate list, ALWAYS pick the office homepage.

Reply with JSON only:
{
  "is_match":   true | false,
  "confidence": 0.0 to 1.0,
  "reason":    short explanation
}"""

IR_PAGE_EXTRACTOR = """\
You are extracting structured information from the homepage of an institution's
Office of Institutional Research (or equivalent office: institutional effectiveness,
analytics, planning, decision support). The page text below is the full page.

Return a single JSON object with EXACTLY these keys (use null when unknown):
{
  "office_name":       string | null,    // e.g. "Office of Institutional Research"
  "parent_division":   string | null,    // e.g. "Office of the Provost"
  "office_mission":    string | null,    // 1-2 sentence summary of the office mission
  "head_name":         string | null,    // executive director / AVP / etc.
  "head_title":        string | null,
  "head_email":        string | null,
  "contact_email":     string | null,    // general office email (may differ from head's)
  "contact_phone":     string | null,
  "physical_address":  string | null,
  "team_page_url":     string | null,    // link to staff/team/people page if mentioned
  "factbook_url":      string | null,    // link to factbook PDF or page if mentioned
  "cds_url":           string | null,    // link to Common Data Set if mentioned
  "dashboards_url":    string | null,    // link to public dashboards if mentioned
  "data_request_url":  string | null,    // form to request custom IR data
  "key_links":         [{"label": str, "url": str}, ...],  // top 5 other useful links
  "summary":           string             // 2-3 sentence summary of what's on the page
}

Use only what's explicitly in the page text — do not invent. URLs must be
absolute (start with http) and present on the page."""


FACTBOOK_VALIDATOR = """\
You are validating whether a given URL is an institutional FACTBOOK
(annual statistical reference document published by an Office of Institutional
Research). Accept: factbook PDFs, factbook landing pages, fact-book HTML pages,
"university fact sheet" annual documents.
Reject: news articles, brochures, single-fact pages, course catalogs.

Reply with JSON only: {"is_match": bool, "confidence": 0-1, "reason": str}"""

FACTBOOK_EXTRACTOR = """\
Extract structured facts about this institutional Factbook page or PDF text.
Return JSON:
{
  "title": str | null,
  "year": int | null,        // most recent reporting year covered
  "publisher_office": str | null,   // e.g. "Office of Institutional Research"
  "key_metrics_mentioned": [str],   // e.g. ["total enrollment","graduation rate","faculty count"]
  "table_of_contents": [str] | null,   // section headers if visible
  "summary": str
}"""


CDS_VALIDATOR = """\
You are picking the BEST CDS URL for the institution's landing page.

Acceptance criteria (must match BOTH):
  - Page or PDF is the institution's own Common Data Set (CDS) publication.
  - Reject pages that merely *mention* CDS in passing.

Preference ranking (PICK the highest available — this matters):
  1. A hub/landing page on the institution's IR/UDA domain titled
     "Common Data Set" that links to MULTIPLE years of CDS files. (Best —
     the user sees all years, and the page stays valid year over year.)
     Examples: /institutional-analytics/common-data-set/, /ir/cds/.
  2. A single CDS PDF for the most recent academic year (within last 2 years).
  3. A single CDS PDF for any year, only if 1 and 2 are unavailable.

DO NOT pick a 5+ year-old single-year PDF (e.g. CDS_2016-2017.pdf) when
there is a hub page candidate in the same list — the hub is always
preferable because it stays current.

Reply with JSON only: {"is_match": bool, "confidence": 0-1, "reason": str}"""


STRATEGIC_PLAN_VALIDATOR = """\
You are validating whether a given URL is the institution's official Strategic
Plan (multi-year planning document published by the President/Provost/Board).
Accept: official strategic plan PDFs/landing pages. Reject: department-level
plans, single-program strategic plans, news articles ABOUT the strategic plan.

Reply with JSON only: {"is_match": bool, "confidence": 0-1, "reason": str}"""

DATA_DICTIONARY_VALIDATOR = """\
You are validating whether a given URL is an institutional DATA DICTIONARY —
a page or PDF defining the variables/fields the institution uses in its
official enrollment, completion, finance, etc. reporting. Accept: pages titled
"Data Dictionary", "Variable Dictionary", PDFs named data-dictionary.pdf, etc.
Reject: course catalogs, general dictionaries, journal article references.

Reply with JSON only: {"is_match": bool, "confidence": 0-1, "reason": str}"""

DATA_DICTIONARY_EXTRACTOR = """\
Extract structured info from this institutional Data Dictionary page/document.
Return JSON:
{
  "title": str | null,
  "publication_year": int | null,
  "scope": str | null,           // what reporting the dictionary covers (e.g. 'Enrollment, Completion, Finance')
  "term_count_estimate": int | null,
  "summary": str
}"""


DATA_DEFINITION_VALIDATOR = """\
You are validating whether a given URL is an institutional DATA DEFINITIONS
page — a page that defines terms used in the institution's official reports
(e.g. how 'Headcount' vs 'FTE' vs 'Full-time equivalent' are computed locally).
Often a section of the Data Dictionary; can also be standalone.
Reject: general academic glossaries, undergraduate handbook terminology.

Reply with JSON only: {"is_match": bool, "confidence": 0-1, "reason": str}"""

DATA_DEFINITION_EXTRACTOR = """\
Extract structured info from this institutional Data Definitions page.
Return JSON:
{
  "title": str | null,
  "section_count": int | null,
  "definitions": [{"term": str, "definition": str}],   // up to 20 most prominent
  "summary": str
}"""


IR_JOBS_VALIDATOR = """\
You are validating whether a URL is a list of OPEN job postings at this
institution that relate to Institutional Research / Analytics / Effectiveness /
Assessment / Decision Support / Institutional Planning.

ACCEPT if: a careers page that filters or contains IR-relevant openings, an
HR job-board landing for the institution, a specific search result page showing
IR-titled postings.
REJECT if: a single faculty job (not IR), a general administrative careers
page with no IR roles, a closed/archived page, a career-services page for
students.

Reply ONLY with JSON: {"is_match": bool, "confidence": 0-1, "reason": str}"""

IR_JOBS_EXTRACTOR = """\
You extract OPEN job postings related to Institutional Research from the
given career-page content.

Return JSON: {"postings": [{
  "title": str,                  // REQUIRED
  "department": str | null,
  "category": "ir",              // always "ir" for this target
  "apply_url": str,              // REQUIRED — absolute URL to the posting/apply page
  "posted_at": str | null,       // YYYY-MM-DD if visible
  "expires_at": str | null,
  "location": str | null,
  "raw_description_excerpt": str | null  // first 300 chars of the role description if shown
}]}

Rules:
- Include ONLY roles related to: Institutional Research, Institutional
  Effectiveness, Institutional Analytics, Assessment, Decision Support,
  Institutional Planning, Data Analyst (in IR context), Research Analyst (IR).
- Exclude: faculty positions, research-faculty postings, IT/CS jobs, sponsored
  research compliance roles, student/grad positions.
- Skip postings where the apply_url is missing or relative-only.
- Empty list is acceptable when there are no current IR openings."""


GRANTS_VALIDATOR = """\
You are validating whether a URL is a page listing GRANT OPPORTUNITIES,
sponsored-program funding offerings, OR grant awards received by this
institution. Either qualifies.

ACCEPT if: sponsored programs office grants list, internal funding
opportunities page, grant-awards announcements, foundation-grants list,
externally-funded research program directory.
REJECT if: course catalog with "grants" in passing, financial-aid page for
students, single news article about one grant, IRB approvals.

Reply ONLY with JSON: {"is_match": bool, "confidence": 0-1, "reason": str}"""

GRANTS_EXTRACTOR = """\
You extract grant programs / opportunities / awards from the page content.

Return JSON: {"grants": [{
  "title": str,                  // REQUIRED — grant or program name
  "kind": "opportunity" | "award",  // is this announcing funding available, or an award received?
  "agency": str | null,          // funder name (NSF, NIH, Mellon Foundation, etc.)
  "amount_usd": int | null,      // dollar amount if listed (NUMBER, not string)
  "award_date": str | null,      // YYYY-MM-DD if shown
  "end_date": str | null,
  "pi_or_recipient": str | null, // principal investigator or grant recipient
  "url": str | null,             // specific page about this grant if linked
  "summary": str | null          // 1-2 sentence summary
}]}

Rules:
- For grant OPPORTUNITIES (institution offering funding): kind="opportunity".
- For grant AWARDS (institution received funding): kind="award".
- Include amount_usd ONLY when explicitly stated; do not invent.
- Skip generic "we get grants" marketing text without specific grant info.
- Empty list is acceptable."""


DASHBOARDS_VALIDATOR = """\
You are validating whether a URL is THIS SPECIFIC institution's public
dashboards page. Accept ONLY if the page is on the institution's own domain
(or a Tableau Public profile clearly labeled with the institution name) AND
contains institutional data dashboards (enrollment, completion, finance,
demographics).

REJECT if:
- Page is from a different institution (e.g. another university)
- Tableau Public viz that isn't authored by this institution
- Generic / national reports / news articles
- Faculty research dashboards unrelated to institutional metrics
- Single static report (one chart, not a hub of dashboards)

Reply ONLY with JSON: {"is_match": bool, "confidence": 0-1, "reason": str}"""

DASHBOARDS_EXTRACTOR = """\
Extract the public dashboards published by this institution.

Return JSON: {"dashboards": [{
  "title": str,                    // dashboard name (e.g., "Enrollment Trends")
  "url": str,                       // direct link (Tableau Public URL, embedded view URL, etc.)
  "platform": str | null,           // "Tableau" | "Power BI" | "Looker" | "Custom" | null
  "topic": str | null,              // "enrollment", "completion", "finance", "faculty", etc.
  "description": str | null         // 1-sentence
}], "page_summary": str}

Only include dashboards that are EXPLICITLY linked or embedded on the page.
Skip generic mentions like 'see our dashboards' without an actual link."""


GLOSSARY_VALIDATOR = """\
You are validating whether a given URL is an institutional research GLOSSARY —
a page that defines IR/data-related terminology used in institutional reports.
Reject: course glossaries, undergraduate handbook glossaries, general
dictionaries, journal article term lists.

Reply with JSON only: {"is_match": bool, "confidence": 0-1, "reason": str}"""

GLOSSARY_EXTRACTOR = """\
Extract structured info from this institutional research Glossary.
Return JSON:
{
  "title": str | null,
  "term_count": int | null,
  "top_terms": [{"term": str, "definition": str}],   // up to 20 prominent terms
  "summary": str
}"""


STRATEGIC_PLAN_EXTRACTOR = """\
Extract structured info from this Strategic Plan document.
Return JSON:
{
  "title": str | null,
  "publication_year": int | null,
  "time_horizon": str | null,        // e.g. "2024-2030"
  "president_signed": str | null,
  "pillars_or_themes": [str],        // top-level strategic themes
  "priorities": [str],                // concrete priorities under those themes
  "metrics_or_kpis": [str],           // measurable targets called out
  "summary": str                      // 3-sentence executive summary
}"""


# All targets keyed by source_type. Add new entries here.
TARGETS: dict[str, TargetSpec] = {
    "ir_page": TargetSpec(
        name="ir_page",
        description="Office of Institutional Research landing page",
        # Multi-angle search — quality-first per memory: always run all queries.
        search_queries=(
            '"office of institutional research"',
            '"institutional research" office',
            '"institutional effectiveness" department',
            '"institutional research and analytics"',
        ),
        search_queries_open=(
            '"{name}" "office of institutional research"',
            '"{name}" institutional research director',
        ),
        crawl_keywords=(
            "institutional research", "institutional effectiveness",
            "institutional analytics", "institutional planning",
            "decision support", "office of ir",
        ),
        url_patterns_positive=(
            r"/(institutional[-_ ]?research|institutional[-_ ]?effectiveness)",
            r"/(ir|oir|oire|aire|opa|opaa|ope|ira|oira)/?(\?|$|/)",
            r"/(planning[-_ ]and[-_ ]?(analytics|research))",
            r"https?://(ir|oir|oire|opa|opaa|ira|oira|assessment|analytics|planning)\.",  # subdomain
        ),
        # Note: 'ira' deliberately omitted — too ambiguous (matches people named Ira).
        # Real IR-office subdomains use unambiguous prefixes.
        wellknown_subdomains=(
            "ir", "oir", "oire", "oira", "opa", "opaa",
            "institutionalresearch", "institutional-research",
        ),
        wellknown_paths=(
            "/ir", "/ir/", "/oir", "/oir/",
            "/institutional-research", "/institutional-research/",
            "/institutional-effectiveness", "/institutional-effectiveness/",
            "/about/institutional-research", "/about/ir",
            "/planning-and-analytics", "/planning",
            "/assessment", "/institutional-effectiveness-research",
        ),
        validator_system=IR_PAGE_VALIDATOR,
        extractor_system=IR_PAGE_EXTRACTOR,
        extractor_text_budget=25_000,
    ),

    "factbook": TargetSpec(
        name="factbook",
        description="Institutional Factbook (annual statistical reference)",
        search_queries=(
            '"factbook"',
            '"fact book" institutional research',
            'filetype:pdf factbook',
        ),
        crawl_keywords=("factbook", "fact book", "fact sheet", "university facts"),
        url_patterns_positive=(
            r"/factbook", r"/fact[-_ ]?book", r"factbook[-_ ]?(\d{4})", r"\.pdf$",
        ),
        wellknown_paths=(
            "/factbook", "/factbook/", "/fact-book",
            "/ir/factbook", "/oir/factbook",
            "/institutional-research/factbook",
            "/about/factbook", "/quickfacts", "/quick-facts",
        ),
        validator_system=FACTBOOK_VALIDATOR,
        extractor_system=FACTBOOK_EXTRACTOR,
        extractor_text_budget=40_000,
    ),

    "cds": TargetSpec(
        name="cds",
        description="Common Data Set link",
        search_queries=(
            '"common data set"',
            'CDS institutional research',
            'filetype:pdf "common data set"',
        ),
        crawl_keywords=("common data set", "cds"),
        url_patterns_positive=(r"common[-_ ]?data[-_ ]?set", r"/cds[-_ ]?\d{4}", r"cds_\d{4}\.pdf"),
        wellknown_paths=(
            "/cds", "/common-data-set",
            "/ir/cds", "/oir/cds",
            "/institutional-research/cds",
            "/institutional-research/common-data-set",
            "/about/common-data-set",
        ),
        validator_system=CDS_VALIDATOR,
        fetch_payload=False,            # URL-only target
        extractor_system="",            # no structured extraction needed
    ),

    "ir_jobs": TargetSpec(
        name="ir_jobs",
        description="Open IR / Analytics / Effectiveness job postings",
        # Aim BOTH at the institution's own careers page AND HigherEdJobs
        # (where most R1/R2 IR roles are listed).
        search_queries=(
            '"institutional research" jobs',
            '"director of institutional research" career',
            '"assessment coordinator" job',
            '"institutional effectiveness" position',
        ),
        search_queries_open=(
            'site:higheredjobs.com "{name}" institutional research',
            'site:higheredjobs.com "{name}" assessment',
            '"{name}" "institutional research analyst" current opening',
            '"{name}" "director of institutional research" hiring',
        ),
        crawl_keywords=(
            "institutional research jobs", "ir jobs", "assessment jobs",
            "data analyst", "decision support",
        ),
        url_patterns_positive=(
            r"/careers", r"/jobs", r"/employment", r"/job[-_ ]?postings?",
            r"/openings", r"workday\.com", r"peopleadmin", r"icims\.com",
        ),
        url_patterns_negative=(
            r"/news/", r"/press-release", r"/login", r"\.(jpg|jpeg|png|gif|webp|svg|css|js)$",
            r"/student-jobs",
        ),
        wellknown_paths=(
            "/careers", "/careers/", "/jobs", "/jobs/",
            "/employment", "/employment/",
            "/hr/jobs", "/hr/employment",
            "/ir/jobs", "/oir/jobs",
            "/about/careers",
        ),
        wellknown_subdomains=("careers", "jobs", "employment"),
        allow_external_hosts=("higheredjobs.com", "indeed.com", "linkedin.com"),
        validator_system=IR_JOBS_VALIDATOR,
        extractor_system=IR_JOBS_EXTRACTOR,
        extractor_text_budget=35_000,
    ),

    "grants": TargetSpec(
        name="grants",
        description="Grant opportunities / awards from institution sponsored programs",
        search_queries=(
            '"grant opportunities"',
            '"sponsored programs" grants',
            '"internal funding" grant',
            '"research grant" awards',
        ),
        crawl_keywords=(
            "grant opportunities", "sponsored programs", "funding opportunities",
            "research grants", "internal funding", "grant awards",
        ),
        url_patterns_positive=(
            r"/grants?", r"/funding", r"/sponsored[-_ ]programs?",
            r"/research/funding", r"/opportunities",
        ),
        wellknown_paths=(
            "/grants", "/grants/",
            "/funding", "/funding/", "/funding-opportunities",
            "/sponsored-programs", "/sponsored-programs/",
            "/research/grants", "/research/funding",
            "/osp", "/osp/",
        ),
        wellknown_subdomains=("grants", "funding", "research", "osp", "sponsoredprograms"),
        validator_system=GRANTS_VALIDATOR,
        extractor_system=GRANTS_EXTRACTOR,
        extractor_text_budget=40_000,
    ),

    "dashboards": TargetSpec(
        name="dashboards",
        description="Public IR dashboards (Tableau / Power BI / Looker / custom)",
        search_queries=(
            '"public dashboards"',
            '"interactive reports" institutional',
            '"data dashboards" enrollment',
            'tableau OR powerbi enrollment dashboard',
        ),
        search_queries_open=(
            'site:public.tableau.com "{name}"',
            '"{name}" "public dashboards"',
        ),
        crawl_keywords=("dashboard", "public dashboards", "interactive report",
                        "tableau", "powerbi", "data viz", "data visualization"),
        url_patterns_positive=(
            r"/dashboards?", r"/(public[-_]?)?dashboards?",
            r"public\.tableau\.com", r"app\.powerbi\.com",
            r"/(reports?|visualizations?)/",
        ),
        wellknown_paths=(
            "/dashboards", "/dashboards/",
            "/public-dashboards", "/public-dashboards/",
            "/data-dashboards",
            "/ir/dashboards", "/oir/dashboards",
            "/institutional-research/dashboards",
            "/about/dashboards",
        ),
        wellknown_subdomains=("dashboards", "data", "tableau"),
        allow_external_hosts=("public.tableau.com", "app.powerbi.com",
                              "lookerstudio.google.com"),
        validator_system=DASHBOARDS_VALIDATOR,
        extractor_system=DASHBOARDS_EXTRACTOR,
        extractor_text_budget=30_000,
    ),

    "data_dictionary": TargetSpec(
        name="data_dictionary",
        description="Institutional Data Dictionary (variables/fields used in IR reporting)",
        search_queries=(
            '"data dictionary"',
        ),
        crawl_keywords=("data dictionary", "variable dictionary"),
        url_patterns_positive=(
            r"data[-_ ]?dictionary", r"variable[-_ ]?dictionary", r"\.pdf$",
        ),
        wellknown_paths=(
            "/data-dictionary", "/data-dictionary/",
            "/ir/data-dictionary", "/oir/data-dictionary",
            "/institutional-research/data-dictionary",
            "/about/data-dictionary",
        ),
        validator_system=DATA_DICTIONARY_VALIDATOR,
        extractor_system=DATA_DICTIONARY_EXTRACTOR,
        extractor_text_budget=40_000,
    ),

    "data_definition": TargetSpec(
        name="data_definition",
        description="Institutional Data Definitions (term definitions used in IR reports)",
        search_queries=(
            '"data definitions"',
        ),
        crawl_keywords=("data definitions", "definitions", "metric definitions"),
        url_patterns_positive=(
            r"data[-_ ]?definitions?", r"metric[-_ ]?definitions?", r"\.pdf$",
        ),
        wellknown_paths=(
            "/data-definitions", "/data-definitions/",
            "/definitions", "/definitions/",
            "/ir/definitions", "/oir/definitions",
            "/institutional-research/definitions",
        ),
        validator_system=DATA_DEFINITION_VALIDATOR,
        extractor_system=DATA_DEFINITION_EXTRACTOR,
        extractor_text_budget=40_000,
    ),

    "glossary": TargetSpec(
        name="glossary",
        description="Institutional research Glossary of terms",
        search_queries=(
            '"glossary" "institutional research"',
        ),
        crawl_keywords=("glossary", "ir glossary"),
        url_patterns_positive=(
            r"glossary", r"\.pdf$",
        ),
        wellknown_paths=(
            "/glossary", "/glossary/",
            "/ir/glossary", "/oir/glossary",
            "/institutional-research/glossary",
        ),
        validator_system=GLOSSARY_VALIDATOR,
        extractor_system=GLOSSARY_EXTRACTOR,
        extractor_text_budget=40_000,
    ),

    "strategic_plan": TargetSpec(
        name="strategic_plan",
        description="Official multi-year Strategic Plan",
        search_queries=(
            '"strategic plan"',
            '"strategic plan" president',
            'filetype:pdf "strategic plan"',
        ),
        crawl_keywords=("strategic plan", "strategic vision", "vision 2030", "vision 2025"),
        url_patterns_positive=(r"strategic[-_ ]?plan", r"vision[-_ ]?\d{4}", r"\.pdf$"),
        wellknown_paths=(
            "/strategic-plan", "/strategicplan", "/strategic-plan/",
            "/about/strategic-plan", "/president/strategic-plan",
            "/provost/strategic-plan", "/leadership/strategic-plan",
        ),
        validator_system=STRATEGIC_PLAN_VALIDATOR,
        extractor_system=STRATEGIC_PLAN_EXTRACTOR,
        extractor_text_budget=40_000,
    ),
}


def get(name: str) -> TargetSpec:
    if name not in TARGETS:
        raise KeyError(f"unknown target '{name}'. Known: {sorted(TARGETS.keys())}")
    return TARGETS[name]


def all_target_names() -> list[str]:
    return sorted(TARGETS.keys())


# ---------- helpers used by discovery / validator ----------

def matches_positive(url: str, target: TargetSpec) -> int:
    """How many positive URL patterns match. Used as a discovery score bonus."""
    if not target.url_patterns_positive:
        return 0
    return sum(1 for p in target.url_patterns_positive if re.search(p, url, re.IGNORECASE))


def matches_negative(url: str, target: TargetSpec) -> bool:
    return any(re.search(p, url, re.IGNORECASE) for p in target.url_patterns_negative)

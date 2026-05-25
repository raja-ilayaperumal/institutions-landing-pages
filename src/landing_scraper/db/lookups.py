"""Canonical slug + label mappings shared by seed scripts and runtime code.

Carnegie slugs match the SEO research's URL examples (e.g. 'research-1',
'research-2', 'research-colleges-universities'). State slugs are
lowercased-dashed full state names ('new-york').
"""
from __future__ import annotations

import re

# c21basic → SEO-friendly slug.
#
# IPEDS has two Carnegie vintages mixed in c21basic:
#  * 2025 codes 15-46 (current Carnegie Classifications)
#  * 2021 codes  1-14 (legacy; not yet re-classified into the 2025 codes)
# Codes 1-14 describe the same categories as the corresponding 2025 codes —
# we map them to the SAME slug so institutions cluster correctly in
# /institutions/<carnegie-slug>/ hub pages regardless of which vintage
# their c21basic was recorded under.
#
# Mapping (legacy → equivalent 2025):
#   1=24, 2=25, 3=26, 4=27, 5=28, 6=29, 7=30, 8=31, 9=32,
#   10=33, 11=34, 12=35, 13=36, 14≈23 (Baccalaureate/Associate's)
CARNEGIE_SLUG: dict[int, str] = {
    -2: "not-classified",
    # Legacy 2021 codes (same slug as 2025 equivalent)
    1:  "associates-high-transfer-traditional",        # = 2025 code 24
    2:  "associates-high-transfer-mixed",              # = 2025 code 25
    3:  "associates-high-transfer-nontraditional",     # = 2025 code 26
    4:  "associates-mixed-traditional",                # = 2025 code 27
    5:  "associates-mixed-mixed-traditional",          # = 2025 code 28
    6:  "associates-mixed-nontraditional",             # = 2025 code 29
    7:  "associates-career-technical-traditional",     # = 2025 code 30
    8:  "associates-career-technical-mixed",           # = 2025 code 31
    9:  "associates-career-technical-nontraditional",  # = 2025 code 32
    10: "special-focus-2yr-health",                    # = 2025 code 33
    11: "special-focus-2yr-technical",                 # = 2025 code 34
    12: "special-focus-2yr-arts",                      # = 2025 code 35
    13: "special-focus-2yr-other",                     # = 2025 code 36
    14: "bachelors-associates-mixed",                  # = 2025 code 23
    # 2025 codes
    15: "research-1",
    16: "research-2",
    17: "doctoral-professional",
    18: "masters-larger",
    19: "masters-medium",
    20: "masters-small",
    21: "bachelors-arts-sciences",
    22: "bachelors-diverse-fields",
    23: "bachelors-associates-mixed",
    24: "associates-high-transfer-traditional",
    25: "associates-high-transfer-mixed",
    26: "associates-high-transfer-nontraditional",
    27: "associates-mixed-traditional",
    28: "associates-mixed-mixed-traditional",
    29: "associates-mixed-nontraditional",
    30: "associates-career-technical-traditional",
    31: "associates-career-technical-mixed",
    32: "associates-career-technical-nontraditional",
    33: "special-focus-2yr-health",
    34: "special-focus-2yr-technical",
    35: "special-focus-2yr-arts",
    36: "special-focus-2yr-other",
    37: "special-focus-4yr-faith",
    38: "special-focus-4yr-medical",
    39: "special-focus-4yr-health-professions",
    40: "special-focus-4yr-engineering",
    41: "special-focus-4yr-technology",
    42: "special-focus-4yr-business",
    43: "special-focus-4yr-arts-music-design",
    44: "special-focus-4yr-law",
    45: "special-focus-4yr-other",
    46: "tribal-colleges",
}

# Human-readable labels for legacy 2021 codes (mirror of 2025 equivalents).
# Used by the seed script to fill carnegie_basic_label when
# ipeds.carnegie_codes doesn't have the legacy code.
CARNEGIE_LEGACY_LABEL: dict[int, str] = {
    1:  "Associate's Colleges: High Transfer-High Traditional",
    2:  "Associate's Colleges: High Transfer-Mixed Traditional/Nontraditional",
    3:  "Associate's Colleges: High Transfer-High Nontraditional",
    4:  "Associate's Colleges: Mixed Transfer/Career & Technical-High Traditional",
    5:  "Associate's Colleges: Mixed Transfer/Career & Technical-Mixed Traditional/Nontraditional",
    6:  "Associate's Colleges: Mixed Transfer/Career & Technical-High Nontraditional",
    7:  "Associate's Colleges: High Career & Technical-High Traditional",
    8:  "Associate's Colleges: High Career & Technical-Mixed Traditional/Nontraditional",
    9:  "Associate's Colleges: High Career & Technical-High Nontraditional",
    10: "Special Focus Two-Year: Health Professions",
    11: "Special Focus Two-Year: Technical Professions",
    12: "Special Focus Two-Year: Arts & Design",
    13: "Special Focus Two-Year: Other Fields",
    14: "Baccalaureate/Associate's Colleges: Associate's Dominant",
}

CONTROL_SLUG: dict[int, str] = {
    1: "public",
    2: "private-np",
    3: "private-fp",
    -3: "not-available",
}


_SLUG_NONWORD = re.compile(r"[^a-z0-9]+")
# Detect '&' between short tokens (1-3 chars each) like 'A&M', 'A & M', 'T&T'
# In that case replace '&' with a separator (no "and"), so 'A & M' → 'a-m'.
_AMP_ACRONYM = re.compile(r"\b(\w{1,3})\s*&\s*(\w{1,3})\b", re.IGNORECASE)


def slugify(text: str | None) -> str:
    """Convert to URL-safe slug.

    Smart '&' handling:
      - between short tokens (acronyms like A&M, T&T) → '-' (no "and")
      - elsewhere (between words) → "and"
    """
    if not text:
        return ""
    s = text.lower().strip()
    # Acronym '&' first: 'a & m' → 'a-m'
    s = _AMP_ACRONYM.sub(r"\1-\2", s)
    # Any remaining '&' between full words → 'and'
    s = s.replace("&", "and")
    s = _SLUG_NONWORD.sub("-", s)
    return s.strip("-")


def state_slug(state_name: str | None) -> str:
    """'New York' → 'new-york', 'District of Columbia' → 'district-of-columbia'."""
    return slugify(state_name)


def carnegie_slug(code: int | None, label: str | None = None) -> str | None:
    if code is None:
        return None
    if code in CARNEGIE_SLUG:
        return CARNEGIE_SLUG[code]
    # Fallback: slugify the full label
    return slugify(label) if label else f"carnegie-{code}"


def control_slug(code: int | None) -> str | None:
    if code is None:
        return None
    return CONTROL_SLUG.get(code, f"control-{code}")

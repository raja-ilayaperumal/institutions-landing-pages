"""BeautifulSoup helpers for cheap rule-based extraction."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"
)


def to_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def visible_text(html: str, *, separator: str = "\n", strip: bool = True) -> str:
    soup = to_soup(html)
    for el in soup(["script", "style", "noscript", "template"]):
        el.decompose()
    return soup.get_text(separator=separator, strip=strip)


def extract_emails(html_or_text: str) -> list[str]:
    return sorted(set(EMAIL_RE.findall(html_or_text)))


def extract_phones(html_or_text: str) -> list[str]:
    return sorted(set(PHONE_RE.findall(html_or_text)))


def absolute_links(html: str, base_url: str, *, same_domain: bool = True) -> list[str]:
    base_host = urlparse(base_url).netloc
    soup = to_soup(html)
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        abs_url = urljoin(base_url, href)
        if same_domain and urlparse(abs_url).netloc and urlparse(abs_url).netloc != base_host:
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        out.append(abs_url)
    return out


def find_pdf_links(html: str, base_url: str) -> list[str]:
    """Find links that look like PDFs (case-insensitive .pdf suffix or content-disposition hint)."""
    soup = to_soup(html)
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if "pdf" in href.lower():
            out.append(urljoin(base_url, href))
    return list(dict.fromkeys(out))

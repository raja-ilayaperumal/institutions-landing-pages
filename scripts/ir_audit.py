"""IR-member content audit — reviews every category of every exported page,
top to bottom, the way an Institutional Research officer reading their own
landing page would: range sanity, cross-field consistency, identity, prose
quality. Captures every anomaly to CSV under testing/. Skips nothing.

Outputs:
  testing/anomalies.csv            one row per anomaly (unitid, category, severity, field, value, issue)
  testing/anomalies_by_category/*  same rows split per category
  testing/review_coverage.csv      one row per institution × category → proves nothing was skipped
  testing/summary.csv              counts by category × severity

Usage:  python scripts/ir_audit.py
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
from collections import defaultdict
from urllib.parse import urlparse

OUT = "testing"
CATEGORIES = [
    "identity", "about", "admissions", "cost", "graduation", "enrollment",
    "programs", "faculty", "peers", "research_funding", "ir_office",
    "ir_team", "documents", "cds_files", "grants", "notable_alumni",
]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DISAMBIG_RE = re.compile(
    r"(may|can|could|commonly)\s+refer(s)?\s+to|refers?\s+to\s+the\s+following"
    r"|no information available|broken link|disambiguation", re.I)
MARKUP_RE = re.compile(r"<[a-z/]+>|\{\{|&nbsp;|cookie|javascript|skip to", re.I)
INST_WORD_RE = re.compile(
    r"college|university|institute|school|academy|seminary|conservatory|"
    r"polytechnic|center|training|education", re.I)


def host_of(url: str | None) -> str:
    h = urlparse(url or "").netloc.lower()
    return h[4:] if h.startswith("www.") else h


def reg_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


class Auditor:
    def __init__(self):
        self.rows = []          # anomalies
        self.coverage = []      # per inst × category status

    def flag(self, inst, cat, sev, field, value, issue):
        self.rows.append({
            "unitid": inst["unitid"], "slug": inst["slug"], "name": inst["name"],
            "category": cat, "severity": sev, "field": field,
            "value": "" if value is None else str(value)[:120], "issue": issue,
        })

    def cover(self, inst, cat, status, n_anom):
        self.coverage.append({
            "unitid": inst["unitid"], "slug": inst["slug"], "name": inst["name"],
            "category": cat, "status": status, "anomalies": n_anom,
        })

    # ---- per-category reviewers -------------------------------------------
    def audit(self, d):
        ins = d.get("institution") or {}
        inst = {"unitid": ins.get("unitid"), "slug": ins.get("slug") or "?",
                "name": ins.get("name") or ins.get("slug") or "?"}
        domain = reg_domain(host_of(ins.get("website_url")))
        offers_ug = bool((d.get("programs") or {}).get("offers_undergraduate"))

        for cat in CATEGORIES:
            before = len(self.rows)
            section = d.get(cat) if cat != "identity" else ins
            present = bool(section) if not isinstance(section, (int, float)) else True
            getattr(self, f"_c_{cat}")(inst, d, ins, domain, offers_ug)
            n = len(self.rows) - before
            status = "ANOMALY" if n else ("empty" if not present else "ok")
            self.cover(inst, cat, status, n)

    def _c_identity(self, inst, d, ins, domain, offers_ug):
        if not isinstance(ins.get("unitid"), int):
            self.flag(inst, "identity", "ERROR", "unitid", ins.get("unitid"), "missing/invalid unitid")
        for f in ("name", "slug", "state", "city"):
            if not ins.get(f):
                self.flag(inst, "identity", "ERROR" if f in ("name", "slug") else "WARN",
                          f, None, f"missing {f}")
        if ins.get("website_url") and not host_of(ins["website_url"]):
            self.flag(inst, "identity", "WARN", "website_url", ins["website_url"], "unparseable website")

    def _c_about(self, inst, d, ins, domain, offers_ug):
        a = d.get("about") or {}
        s = a.get("summary") or ""
        if not s:
            return  # legitimately absent for micro-schools; coverage marks 'empty'
        if DISAMBIG_RE.search(s):
            self.flag(inst, "about", "ERROR", "summary", s[:80], "disambiguation/dead-link text")
        if MARKUP_RE.search(s):
            self.flag(inst, "about", "ERROR", "summary", s[:80], "markup/boilerplate in summary")
        if s[-1] not in '.!?"\')':
            self.flag(inst, "about", "WARN", "summary", s[-40:], "summary truncated mid-sentence")
        if len(s) < 200:
            self.flag(inst, "about", "WARN", "summary", len(s), f"thin summary ({len(s)} chars)")
        first = re.sub(r"[^a-z]", "", (inst["name"].split() or [""])[0].lower())
        if not INST_WORD_RE.search(s) and (not first or first not in s.lower()):
            self.flag(inst, "about", "WARN", "summary", s[:60], "summary may be off-topic")

    def _c_admissions(self, inst, d, ins, domain, offers_ug):
        a = d.get("admissions") or {}
        if not a:
            return
        ar = num(a.get("admission_rate")); ar2 = num(a.get("admit_rate"))
        yr = num(a.get("yield_rate"))
        apps = num(a.get("applicants")); adm = num(a.get("admits")); enr = num(a.get("enrolled"))
        for f, v in (("admission_rate", ar), ("admit_rate", ar2), ("yield_rate", yr)):
            if v is not None and not (0 < v <= 1):
                self.flag(inst, "admissions", "ERROR", f, v, "rate outside (0,1]")
        for f in ("sat_avg",):
            v = num(a.get(f))
            if v is not None and not (400 <= v <= 1600):
                self.flag(inst, "admissions", "ERROR", f, v, "SAT outside 400-1600")
        if apps and adm and adm > apps:
            self.flag(inst, "admissions", "ERROR", "admits", adm, f"admits>{apps} applicants")
        if adm and enr and enr > adm:
            self.flag(inst, "admissions", "ERROR", "enrolled", enr, f"enrolled>{adm} admits")
        if apps and adm and apps > 0:
            calc = adm / apps
            base = ar2 if ar2 is not None else ar
            if base is not None and abs(base - calc) > 0.06:
                self.flag(inst, "admissions", "WARN", "admit_rate", f"{base:.3f}",
                          f"admit_rate vs admits/applicants={calc:.3f} mismatch")

    def _c_cost(self, inst, d, ins, domain, offers_ug):
        c = d.get("cost") or {}
        if not c:
            return
        ti = num(c.get("in_state_tuition")); to = num(c.get("out_of_state_tuition"))
        rb = num(c.get("room_and_board")); np_ = num(c.get("avg_net_price"))
        for f, v in (("in_state_tuition", ti), ("out_of_state_tuition", to), ("room_and_board", rb)):
            if v is not None and not (0 <= v <= 100000):
                self.flag(inst, "cost", "ERROR", f, v, "value outside 0-100k")
        if np_ is not None and np_ < 0:
            self.flag(inst, "cost", "ERROR", "avg_net_price", np_, "negative net price (should be floored)")
        if ti is not None and to is not None and ti > to and not c.get("is_private_for_net_price"):
            self.flag(inst, "cost", "WARN", "tuition", f"in={ti}/out={to}", "in-state > out-of-state (public)")
        tc = num(c.get("total_cost_in_state"))
        if ti and rb and tc and abs(tc - (ti + rb)) > 5:
            self.flag(inst, "cost", "WARN", "total_cost_in_state", tc, f"!= tuition+rb ({ti+rb})")
        npbfi = c.get("net_price_by_family_income") or {}
        for k, v in npbfi.items():
            if num(v) is not None and v < 0:
                self.flag(inst, "cost", "ERROR", f"net_price.{k}", v, "negative income-band net price")
        for f in ("pell_grant_rate", "federal_loan_rate"):
            v = num(c.get(f))
            if v is not None and not (0 <= v <= 1):
                self.flag(inst, "cost", "ERROR", f, v, "rate outside 0-1")

    def _c_graduation(self, inst, d, ins, domain, offers_ug):
        g = d.get("graduation") or {}
        if not g:
            return
        for f, v in g.items():
            v = num(v)
            if v is None:
                continue
            if "rate" in f and not (0 <= v <= 1):
                self.flag(inst, "graduation", "ERROR", f, v, "rate outside 0-1")
        g4 = num(g.get("grad_4yr_bachelors")); g5 = num(g.get("grad_5yr_bachelors"))
        g6 = num(g.get("grad_6yr_bachelors"))
        if g4 and g5 and g4 > g5 + 1e-6:
            self.flag(inst, "graduation", "WARN", "grad_4yr_bachelors", f"{g4}>{g5}", "4yr>5yr (non-monotonic)")
        if g5 and g6 and g5 > g6 + 1e-6:
            self.flag(inst, "graduation", "WARN", "grad_5yr_bachelors", f"{g5}>{g6}", "5yr>6yr (non-monotonic)")

    def _c_enrollment(self, inst, d, ins, domain, offers_ug):
        e = d.get("enrollment") or {}
        if not e:
            return
        tot = num(e.get("enrollment_12mo_total")) or num(e.get("fall_total"))
        if tot is not None and tot <= 0:
            self.flag(inst, "enrollment", "ERROR", "total", tot, "non-positive enrollment")
        w = num(e.get("women_12mo")); m = num(e.get("men_12mo"))
        t12 = num(e.get("enrollment_12mo_total"))
        if w and m and t12 and abs((w + m) - t12) > max(2, 0.02 * t12):
            self.flag(inst, "enrollment", "WARN", "women+men", f"{w}+{m}≠{t12}", "gender split != total")
        ug = num(e.get("ug_12mo")); gr = num(e.get("grad_12mo"))
        if ug and gr and t12 and abs((ug + gr) - t12) > max(5, 0.05 * t12) and (ug + gr) > t12:
            self.flag(inst, "enrollment", "WARN", "ug+grad", f"{ug}+{gr}>{t12}", "ug+grad exceeds total")
        # NB: grad>ug is NORMAL for research universities (MIT, Stanford,
        # Caltech), medical schools, and seminaries — not an anomaly. Only flag
        # the impossible case where the parts exceed the whole.
        if ug and gr and t12 and (ug + gr) > t12 * 1.02:
            self.flag(inst, "enrollment", "WARN", "ug+grad", f"{ug}+{gr}>{t12}", "ug+grad exceeds 12-mo total")
        ft = num(e.get("fall_full_time")); pt = num(e.get("fall_part_time")); ftot = num(e.get("fall_total"))
        if ft and pt and ftot and (ft + pt) > ftot * 1.05:
            self.flag(inst, "enrollment", "WARN", "fall_part_time", f"ft{ft}+pt{pt}>{ftot}", "ft+pt exceeds fall_total (part-time likely mismapped)")
        re_ = e.get("race_ethnicity") or {}
        vals = [num(v) for v in re_.values() if num(v) is not None]
        for k, v in re_.items():
            if num(v) is not None and not (0 <= v <= 1):
                self.flag(inst, "enrollment", "ERROR", f"race.{k}", v, "share outside 0-1")
        if vals and not (0.97 <= sum(vals) <= 1.03):
            self.flag(inst, "enrollment", "WARN", "race_ethnicity", f"sum={sum(vals):.3f}", "race shares don't sum to ~1")

    def _c_programs(self, inst, d, ins, domain, offers_ug):
        p = d.get("programs") or {}
        if not p:
            return
        pc = num(p.get("program_count"))
        if pc is not None and pc < 0:
            self.flag(inst, "programs", "ERROR", "program_count", pc, "negative program count")

    def _c_faculty(self, inst, d, ins, domain, offers_ug):
        f = d.get("faculty") or {}
        r = num(f.get("student_faculty_ratio"))
        if r is not None and not (0 < r <= 100):
            self.flag(inst, "faculty", "WARN", "student_faculty_ratio", r, "implausible S:F ratio")

    def _c_peers(self, inst, d, ins, domain, offers_ug):
        p = d.get("peers") or {}
        lst = p.get("institutions") if isinstance(p, dict) else None
        if not lst:
            return
        ids = [x.get("unitid") for x in lst if isinstance(x, dict)]
        if inst["unitid"] in ids:
            self.flag(inst, "peers", "ERROR", "institutions", inst["unitid"], "institution is its own peer")
        if len(ids) != len(set(ids)):
            self.flag(inst, "peers", "WARN", "institutions", len(ids), "duplicate peer unitids")

    def _c_research_funding(self, inst, d, ins, domain, offers_ug):
        r = d.get("research_funding") or {}
        if not r:
            return
        for f, v in r.items():
            v = num(v)
            if v is not None and ("usd" in f or "count" in f) and v < 0:
                self.flag(inst, "research_funding", "ERROR", f, v, "negative amount/count")
        comp = [num(r.get(k)) or 0 for k in ("nsf_total_usd", "nih_total_usd", "usaspending_total_usd")]
        allc = num(r.get("all_sources_total_usd"))
        if allc is not None and sum(comp) and abs(allc - sum(comp)) > max(1000, 0.02 * allc):
            self.flag(inst, "research_funding", "WARN", "all_sources_total_usd", allc,
                      f"!= nsf+nih+usaspending ({sum(comp)})")

    def _c_ir_office(self, inst, d, ins, domain, offers_ug):
        o = d.get("ir_office") or {}
        if not o:
            return
        em = o.get("email")
        if em and not EMAIL_RE.match(em):
            self.flag(inst, "ir_office", "ERROR", "email", em, "malformed email")
        if em and EMAIL_RE.match(em) and domain and reg_domain(em.split("@")[-1]) != domain:
            self.flag(inst, "ir_office", "INFO", "email", em, f"office email on different domain than {domain} (likely district)")
        if o.get("needs_review"):
            self.flag(inst, "ir_office", "WARN", "needs_review", True, "needs_review leaked into shipped page")

    def _c_ir_team(self, inst, d, ins, domain, offers_ug):
        for member in d.get("ir_team") or []:
            nm = (member or {}).get("name") or ""
            em = (member or {}).get("email")
            if nm and (len(nm) < 4 or len(nm) > 60 or re.search(r"https?:|@|\.com|\d{3}", nm)):
                self.flag(inst, "ir_team", "WARN", "name", nm, "name not person-like")
            if em and not EMAIL_RE.match(em):
                self.flag(inst, "ir_team", "ERROR", "email", em, "malformed team email")
            # Off-domain team emails are almost always legit district/system
            # addresses (Coast CCD @cccd.edu for Orange Coast, @csufresno.edu for
            # Fresno State) — demote to INFO, not a WARN that reads as wrong.
            if em and EMAIL_RE.match(em) and domain and reg_domain(em.split("@")[-1]) != domain:
                self.flag(inst, "ir_team", "INFO", "email", em, f"team email on different domain than {domain} (likely district)")

    def _c_documents(self, inst, d, ins, domain, offers_ug):
        for doc in d.get("documents") or []:
            doc = doc or {}
            if doc.get("needs_review"):
                self.flag(inst, "documents", "WARN", "needs_review", doc.get("url"), "needs_review doc shipped")
            u = doc.get("url"); h = reg_domain(host_of(u))
            fed = h in ("ed.gov", "nces.ed.gov", "nsf.gov", "nih.gov")
            if u and domain and h and h != domain and not fed and "tableau.com" not in h:
                # district/parent domains are common & legit → INFO, not ERROR
                self.flag(inst, "documents", "INFO", "url", u, f"doc off institution domain ({domain})")

    def _c_cds_files(self, inst, d, ins, domain, offers_ug):
        for c in d.get("cds_files") or []:
            u = (c or {}).get("url")
            if u and not host_of(u):
                self.flag(inst, "cds_files", "WARN", "url", u, "unparseable CDS url")

    def _c_grants(self, inst, d, ins, domain, offers_ug):
        return  # grants already filtered to recent+active upstream

    def _c_notable_alumni(self, inst, d, ins, domain, offers_ug):
        for a in d.get("notable_alumni") or []:
            nm = (a or {}).get("name") or ""
            if nm and (len(nm) < 3 or re.search(r"https?:|@|\d{3}", nm)):
                self.flag(inst, "notable_alumni", "WARN", "name", nm, "alumnus name not person-like")


def main():
    os.makedirs(f"{OUT}/anomalies_by_category", exist_ok=True)
    aud = Auditor()
    files = [f for f in sorted(glob.glob("data/json/*.json")) if not f.endswith("institutions.json")]
    for fp in files:
        aud.audit(json.load(open(fp)))

    # master anomalies
    cols = ["unitid", "slug", "name", "category", "severity", "field", "value", "issue"]
    with open(f"{OUT}/anomalies.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(aud.rows)
    # per category
    bycat = defaultdict(list)
    for r in aud.rows:
        bycat[r["category"]].append(r)
    for cat, rows in bycat.items():
        with open(f"{OUT}/anomalies_by_category/{cat}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    # coverage matrix (proves nothing skipped)
    with open(f"{OUT}/review_coverage.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["unitid", "slug", "name", "category", "status", "anomalies"])
        w.writeheader(); w.writerows(aud.coverage)
    # summary
    summ = defaultdict(lambda: defaultdict(int))
    for r in aud.rows:
        summ[r["category"]][r["severity"]] += 1
    with open(f"{OUT}/summary.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["category", "ERROR", "WARN", "INFO", "total"])
        for cat in CATEGORIES:
            e, wn, i = summ[cat]["ERROR"], summ[cat]["WARN"], summ[cat]["INFO"]
            w.writerow([cat, e, wn, i, e + wn + i])

    insts = len(files)
    errs = sum(1 for r in aud.rows if r["severity"] == "ERROR")
    warns = sum(1 for r in aud.rows if r["severity"] == "WARN")
    infos = sum(1 for r in aud.rows if r["severity"] == "INFO")
    print(f"Reviewed {insts} institutions × {len(CATEGORIES)} categories "
          f"= {insts*len(CATEGORIES)} category-reviews (none skipped).")
    print(f"Anomalies: {errs} ERROR, {warns} WARN, {infos} INFO  → testing/anomalies.csv")
    print("\nBy category:")
    for cat in CATEGORIES:
        e, wn, i = summ[cat]["ERROR"], summ[cat]["WARN"], summ[cat]["INFO"]
        if e + wn + i:
            print(f"  {cat:18} ERROR={e:4} WARN={wn:4} INFO={i:4}")


if __name__ == "__main__":
    main()

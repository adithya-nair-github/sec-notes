"""Read the text of 10-Ks and 10-Qs for finance-operations pain, in the company's own words.

Sections read (the table of contents is skipped by taking the longest copy of each section):
  10-K  Item 1 (Human Capital: employee count), Item 1A Risk Factors, Item 7 MD&A, Item 9A Controls,
        Item 14 Principal Accountant Fees (when the 10-K carries it instead of the proxy)
  10-Q  Item 2 MD&A, Item 4 Controls, Part II Item 1A
Only sentences that say something is happening are kept: hypotheticals ("if we fail to...",
"could", "may") are dropped for the project-type patterns. Quotes are exact (they come from the filing).
"""
import re

from . import eightk

VERSION = 3

SECTIONS_10K = {
    "business": r"item\s*1\.?\s*business",
    "risk": r"item\s*1a\.?\s*risk\s*factors",
    "mdna": r"item\s*7\.?\s*management'?s\s*discussion",
    "controls": r"item\s*9a\.?\s*controls\s*and\s*procedures",
    "fees": r"item\s*14\.?\s*principal\s*account",
}
SECTIONS_10Q = {
    "mdna": r"item\s*2\.?\s*management'?s\s*discussion",
    "controls": r"item\s*4\.?\s*controls\s*and\s*procedures",
    "risk": r"item\s*1a\.?\s*risk\s*factors",
}
ANY_ITEM = re.compile(r"\bitem\s*\d{1,2}[a-c]?\.?\s+[A-Z]", re.I)

FILING_PATTERNS = {
    "filing_erp_project": (re.compile(
        r"(implement\w*|transition\w*|migrat\w*|upgrad\w*|convert\w*|deploy\w*|replac\w*|go[- ]live)[^.]{0,60}"
        r"(\berp\b|enterprise resource planning|new (accounting|financial|finance|reporting|consolidation) (system|platform|software)|"
        r"s/?4 ?hana|oracle (cloud|fusion)|workday financials|netsuite)|"
        r"(\berp\b|enterprise resource planning)[^.]{0,60}(implementation|migration|upgrade|conversion|go[- ]live)", re.I),
        ("mdna", "controls", "risk")),
    "filing_lean_accounting": (re.compile(
        r"(lack|insufficient|limited|inadequate|not (maintain|have) (a )?sufficient|did not (maintain|have)|small number of)[^.]{0,60}"
        r"(accounting|finance|financial reporting|gaap|technical accounting)[^.]{0,30}(personnel|staff|resources|expertise|professionals|employees|team)|"
        r"(complement|number) of (accounting |finance |qualified )?(personnel|staff|professionals|employees)[^.]{0,100}(accounting|financial reporting|gaap)|"
        r"(accounting|finance|financial reporting) (team|staff|function|department)s? (is|are) (leanly|thinly|small|limited)|leanly staffed|"
        r"segregation of duties[^.]{0,140}(limited|small|few|lack of|insufficient|number of) (accounting )?(personnel|staff|employees|people|size)|"
        r"(limited|small|lack of|insufficient) (number of )?(accounting )?(personnel|staff|employees)[^.]{0,140}segregation of duties|"
        r"(due to|because of|given) (our|the company's|its|the) (small size|limited (number of )?(accounting )?(personnel|staff|employees))", re.I),
        ("controls", "risk")),
    "filing_outsourced_reporting": (re.compile(
        r"(third[- ]party|outside|external|independent)\s+(consultant|accountant|service provider|firm|advisor)s?[^.]{0,80}"
        r"(financial reporting|sec reporting|financial statements|xbrl|technical accounting|period[- ]end)|"
        r"(rely|relies|relied|depend\w*) (on|upon)[^.]{0,40}(consultant|outsourc|third part|external service)[^.]{0,60}(reporting|accounting|financial statements)", re.I),
        ("controls", "risk", "mdna")),
    "filing_remediation": (re.compile(
        r"remediation (plan|efforts|measures|activities)|plan(s)? to remediate|(are|is) (in the process of )?remediating|"
        r"(has|have) not (yet )?been (fully )?remediated|not yet (been )?remediated|until (the|such|these) material weakness(es)? (is|are) remediated", re.I),
        ("controls",)),
}
# sentences that match a pattern but do not mean what we want
NOT_THIS = {
    "filing_erp_project": re.compile(
        r"periodically|from time to time|difficult(y|ies) (in|with)|risks? (associated|related) (with|to)|inherent risks|such as (our|the)|"
        r"seek to|may (implement|in the future)|and may implement|including implementing new|\bany (new|future)\b|failure to|disruption", re.I),
    "filing_lean_accounting": re.compile(
        r"user access|privileged|information technology|\bit general|itgc|change management|\bsystems? access|truck|driver|production labor|"
        r"attract (and|or) retain|retain qualified|fleet|clinical|scientific|(has|have|was|were|had) (been )?(fully )?remediated|"
        r"remediated as of|\b(19|20)(0\d|1\d|2[0-3])\b(?![^.]*\b202[4-9]\b)|business combination|product candidate|"
        r"manage (the|this|our) (expansion|simultaneous|growth)|recruit and train|development (process|program)|access to capital", re.I),
    "filing_outsourced_reporting": re.compile(r"(has|have|was|were) (been )?(fully )?remediated", re.I),
    "filing_remediation": re.compile(
        r"environmental|(has|have|was|were|had) (been )?(fully )?remediated|remediated as of|r emediated|completed (our |the )?(testing|remediation)|"
        r"concluded that[^.]{0,120}(is|are|was|were) effective|if remediation|remediation (is|was) complete", re.I),
}
ACTUAL = re.compile(r"\b(are|is|were|was) (currently )?(in the process of |in the midst of )?(implementing|transitioning|migrating|upgrading|converting|deploying)|"
                    r"\bin the (midst|process) of\b|multi-?year (process|project|implementation|program)|\bcontinu\w+ (to implement|the implementation|to deploy|to migrate|to upgrade)|"
                    r"\b(began|begun|commenced|completed|implemented|went live|go[- ]live|plan(s|ned)? to|will (implement|transition|migrate|replace)|expected to (be completed|go live))", re.I)
HYPOTHETICAL = re.compile(r"^\s*if\b|\bif we\b|\bcould\b|\bmay\b|\bmight\b|\bwould\b|\bany failure\b|there can be no|no assurance", re.I)
DONE = re.compile(r"\b(completed|implemented|went live|replaced)\b", re.I)
IN_PROGRESS = re.compile(r"in the (process|midst) of|multi-?year|continu\w+|implementing|upgrading|migrating|expected to|will |plan(s|ned)? to|phase", re.I)
TRUNCATED = re.compile(r"\b(in|of|the|to|and|as|with|item \d+[a-c]?|part ii)\.$", re.I)
EMPLOYEES = re.compile(r"(?:had|employ(?:ed)?|approximately|total of)\s+(?:approximately\s+)?([\d,]{1,9})\s+(?:full[- ]time\s+)?(?:employees|team members|associates|people)", re.I)


def section(text, pattern):
    """The longest stretch that starts at a heading match and runs to the next Item heading."""
    best = ""
    for m in re.finditer(pattern, text, re.I):
        rest = text[m.end(): m.end() + 400000]
        nxt = ANY_ITEM.search(rest, 1)
        body = rest[: nxt.start()] if nxt else rest[:150000]
        if len(body) > len(best):
            best = body
    best = re.sub(r"^\W*(and analysis( of financial condition and results of operations)?)\W*", "", best, flags=re.I)
    return best


ABBREV = re.compile(r"\b(inc|corp|co|ltd|no|u\.s|mr|ms|dr|approx|e\.g|i\.e|vs)\.$", re.I)


def sentences(text):
    """Split on sentence ends; glue back pieces cut after common abbreviations ("Inc.", "U.S.")."""
    out, buf = [], ""
    for piece in re.split(r"(?<=[.;!?])\s+", text):
        buf = (buf + " " + piece).strip() if buf else piece.strip()
        if ABBREV.search(buf):
            continue
        if 40 <= len(buf) <= 700:
            out.append(buf)
        buf = ""
    return out


def erp_is_real(s, year=None):
    """An ERP project that is happening (or just finished), not risk-factor boilerplate or one done years ago."""
    if not ACTUAL.search(s) or (HYPOTHETICAL.search(s) and not re.search(r"\b(we|the company)\b[^.]{0,40}\b(are|is|began|completed|implemented)\b", s, re.I)):
        return False
    yrs = [int(y) for y in re.findall(r"\b(20[0-3]\d)\b", s)]
    if year and yrs and re.search(r"final phase|completed|operationaliz\w+|went live|concluded", s, re.I) and max(yrs) < year - 1:
        return False          # "completed in 2024", read in a 2026 filing
    return True


def read_filing(raw_html, form, year=None):
    """{hits:{type:{quote, section[, status]}}, employees:int|None, fees_text:str}"""
    text = eightk.to_text(raw_html or "")
    secs = SECTIONS_10K if form.startswith("10-K") else SECTIONS_10Q
    parts = {k: section(text, p) for k, p in secs.items()}
    hits = {}
    for typ, (rx, where) in FILING_PATTERNS.items():
        for sec in where:
            for s in sentences(parts.get(sec, "")):
                m = rx.search(s)
                if not m or NOT_THIS[typ].search(s) or TRUNCATED.search(s.strip()) or len(s) < 60:
                    continue
                if typ == "filing_erp_project" and not erp_is_real(s, year):
                    continue
                if typ in ("filing_lean_accounting", "filing_outsourced_reporting") and re.match(r"\s*if\b", s, re.I):
                    continue
                hits[typ] = {"quote": s[:420], "section": sec}
                if typ == "filing_erp_project":
                    hits[typ]["status"] = "done" if DONE.search(s) and not IN_PROGRESS.search(s) else "in_progress"
                break
            if typ in hits:
                break
    emp = None
    m = EMPLOYEES.search(parts.get("business", "")) if parts.get("business") else None
    if m:
        try:
            emp = int(m.group(1).replace(",", ""))
        except ValueError:
            emp = None
    return {"hits": hits, "employees": emp, "fees_text": parts.get("fees", "")[:20000]}


SECTION_NAME = {"mdna": "MD&A", "controls": "Controls and Procedures", "risk": "Risk Factors", "business": "Business"}

"""Read the free earnings materials a company files with the SEC (8-K Item 2.02 / 7.01 exhibits:
press release, slide deck, prepared remarks) and pull out sentences that show finance pain.

Sentence-level matching only: every hit keeps the exact sentence so a human (or the drafting task)
can quote it. Anything negated ("no restructuring") is dropped.
"""
import re

from . import eightk

# bump this whenever the filters change: old cached results are then re-read with the new rules
VERSION = 3

# type -> regex that must match inside ONE sentence
PATTERNS = {
    "earnings_cost_program": re.compile(
        r"(cost|expense|opex|g&a|sg&a)[a-z\s\-]{0,25}(reduction|savings|take[- ]?out|program|initiative|discipline|containment)|"
        r"restructuring|workforce reduction|reduction in force|headcount reduction|reduc\w+ (our )?(headcount|workforce)|"
        r"efficiency (program|initiative|plan)|streamlin\w+ (our )?(cost|operations)|reduce (our )?(operating )?(costs|expenses)", re.I),
    "earnings_systems_project": re.compile(
        r"(implement|adopt|stabili[sz]|migrat|upgrad|transition|go[- ]live|new|rollout|roll-out)\w*[^.]{0,90}\berp\b|\berp\b[^.]{0,40}(implement|adopt|stabili[sz]|migrat|upgrad|transition|go[- ]live|rollout|roll-out)|system(s)? (implementation|conversion|migration|upgrade|transformation)|finance transformation|"
        r"(implement|migrat|upgrad|transition)\w* (to )?(a )?new (erp|financial|accounting|reporting|finance) (system|platform|software)|"
        r"digital transformation of (our )?finance|automat\w+ (of )?(our )?(financial )?(close|reporting)", re.I),
    "earnings_finance_ops": re.compile(
        r"(financial|month[- ]end|quarter[- ]end|year[- ]end) close|close process|sec reporting|"
        r"remediat\w+ (of )?(the |our |a )?(material weakness|control)|internal control (deficienc|weakness)|"
        r"(delay|delayed|late)\w* (in )?(the )?(filing )?(of )?our (10-k|10-q|annual report|quarterly report)|audit fees? (increase|rose|higher|jump)|change in (our )?(independent )?auditor", re.I),
}
# legal / accounting boilerplate: never news
BOILERPLATE = re.compile(
    r"non-?gaap|forward[- ]looking|defined as|excluding|exclude[sd]?\b|\bsee (item|note|part)|risk factors|"
    r"reconcil|adjusted (ebitda|net income|earnings)|safe harbor|undue reliance|could differ materially|"
    r"no assurance|ability to|may not be able|if we (fail|are unable)|\bnotes? to\b|"
    r"form 10-[kq]|annual report|press release|webcast|conference call|"
    r"non-comparable|\bffo\b|estimates are based|current expectations|subject to completion|preliminary results|"
    r"pric(e|ing)|reimburs|payers?|regulator|clinical|\bfda\b|\b(ind|bla|nda)\b|^\(\d+\)|^\s*\W", re.I)
# the company itself must be the one acting
COMPANY_VOICE = re.compile(r"\b(we|our|us)\b|the company|management|company'?s own", re.I)
NEGATION = re.compile(r"\b(no|not|without|none|never|n't)\b[^.]{0,30}$", re.I)


def pain_lines(text, max_per_type=1):
    """{type: sentence} for the strongest hit of each type found in text."""
    out = {}
    sentences = re.split(r"(?<=[.;!?])\s+", text or "")
    for typ, rx in PATTERNS.items():
        for s in sentences:
            if len(s) < 40 or len(s) > 600:
                continue
            if BOILERPLATE.search(s) or not COMPANY_VOICE.search(s):
                continue
            m = rx.search(s)
            if not m:
                continue
            if NEGATION.search(s[: m.start()]):
                continue
            out[typ] = s.strip()[:320]
            break
    return out


def exhibit_names(index_json):
    """Names of the 8-K exhibit files likely to hold earnings text (EX-99.x htm files)."""
    items = ((index_json or {}).get("directory") or {}).get("item") or []
    names = [i["name"] for i in items if i.get("name", "").lower().endswith((".htm", ".html"))]
    ex = [n for n in names if re.search(r"(^|[^a-z])(ex|exh|exhibit)[-_]?99", n, re.I)]
    return sorted(ex)[:3]


def scan_filing(http, cik, adsh):
    """Fetch the filing's exhibit files and return {type: sentence}."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}"
    idx = http.get(base + "/index.json", want="json")
    merged = {}
    for name in exhibit_names(idx):
        raw = http.get(f"{base}/{name}")
        if not raw:
            continue
        for typ, sent in pain_lines(eightk.to_text(raw)).items():
            merged.setdefault(typ, sent)
    return merged

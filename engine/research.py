"""Extra SEC data per company, beyond 8-Ks: audit fees, activist 13Ds and financial trends,
plus turning everything the researcher found into engine signals.

  audit_fees(text)        "Audit Fees $2,150,000 $1,890,000" -> current vs prior year (proxy DEF 14A, or 10-K Item 14)
  activist_13d(...)       Schedule 13D filed about the company by a known activist or with activist intent
  fin_trends(facts)       revenue, SG&A, net income, operating cash flow and cash from SEC XBRL company facts
  signals_for_company()   ready-made signals the private engine adds as they are
"""
import datetime as dt
import re

# ------------------------------------------------------------ audit fees
MONEY = r"\$?\s*\(?([\d]{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\)?\s*(thousand|million|k\b)?"
AUDIT_ROW = re.compile(r"\baudit\s+fees\b(?:\s*\(\s*\d\s*\))?[^$\d]{0,60}" + MONEY + r"[^$\d]{0,40}" + MONEY, re.I)
YEARS = re.compile(r"\b(20\d\d)\b")


def _amount(num, unit, table_unit):
    v = float(num.replace(",", ""))
    u = (unit or table_unit or "").lower()
    if u.startswith("million"):
        v *= 1_000_000
    elif u in ("thousand", "k"):
        v *= 1_000
    return v


def audit_fees(text):
    """{current, prior, year_cur, year_prior, change} or None. Picks the fee table row that has two amounts."""
    if not text:
        return None
    for m in AUDIT_ROW.finditer(text):
        before = text[max(0, m.start() - 600): m.start()]
        table_unit = "thousand" if re.search(r"in thousands|\(\$?000s?\)|\$ in 000", before, re.I) else (
            "million" if re.search(r"in millions", before, re.I) else "")
        a = _amount(m.group(1), m.group(2), table_unit)
        b = _amount(m.group(3), m.group(4), table_unit)
        if a < 10_000 or b < 10_000 or a > 500_000_000 or b > 500_000_000:
            continue
        yrs = [int(y) for y in YEARS.findall(before)[-4:]]
        first_is_current = True
        if len(yrs) >= 2 and yrs[-2] < yrs[-1]:
            first_is_current = False  # header reads "2024 2025": the second column is current
        cur, prior = (a, b) if first_is_current else (b, a)
        ycur = max(yrs[-2:]) if len(yrs) >= 2 else None
        return {"current": cur, "prior": prior, "year_cur": ycur, "year_prior": (ycur - 1) if ycur else None,
                "change": (cur - prior) / prior if prior else None}
    return None


# ------------------------------------------------------------ activists
ACTIVISTS = re.compile(
    r"elliott|starboard|icahn|trian|valueact|jana partners|third point|engaged capital|land (and|&) buildings|ancora|"
    r"legion partners|irenic|politan|mantle ridge|sachem head|corvex|pershing square|barington|blackwells|cannell|"
    r"impactive|anson|carronade|driver management|kent lake|bluebell|saba capital|scopia|praesidium|marathon partners|"
    r"stilwell|lone pine|cevian|inclusive capital|sarissa|d\. ?e\. ?shaw|voss capital|harbert discovery|"
    r"radoff|fourworld|macellum|alta fox|mangrove", re.I)
ACTIVIST_INTENT = re.compile(
    r"board (seat|representation|composition|nominee)|nominat\w+ (director|candidate)|proxy (contest|solicitation)|"
    r"strategic alternatives|undervalu|engage (in discussions )?with (management|the board)|"
    r"(operational|operating|margin|cost) (improvement|efficienc)|capital allocation|sale of the company|special meeting", re.I)
FORMS_13D = ("SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A")


def activist_from_text(text):
    """(is_activist, filer_hint, sentence) from a 13D's text."""
    t = text or ""
    name = ACTIVISTS.search(t[:20000])
    item4 = re.search(r"item\s*4\.?\s*purpose of (the )?transaction(.{0,4000})", t, re.I | re.S)
    body = item4.group(2) if item4 else ""
    intent = ACTIVIST_INTENT.search(body)
    sent = ""
    if intent:
        s0 = body.rfind(".", 0, intent.start()) + 1
        s1 = body.find(".", intent.end())
        sent = re.sub(r"\s+", " ", body[s0: s1 + 1 if s1 > 0 else intent.end() + 200]).strip()[:350]
    return bool(name or intent), (name.group(0) if name else ""), sent


# ------------------------------------------------------------ financial trends (XBRL company facts)
CONCEPTS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "sga": ["SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
}


def _annual(facts, names):
    """{fiscal-year-end date: value} for full-year (or instant, for cash) 10-K values of the first concept found."""
    gaap = ((facts or {}).get("facts") or {}).get("us-gaap") or {}
    for n in names:
        units = (gaap.get(n) or {}).get("units") or {}
        rows = units.get("USD") or []
        out = {}
        for r in rows:
            if not str(r.get("form", "")).startswith("10-K"):
                continue
            end = r.get("end")
            start = r.get("start")
            if start:
                days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
                if not 350 <= days <= 380:
                    continue
            out[end] = r.get("val")  # later filings overwrite earlier ones (restated values win)
        if out:
            return dict(sorted(out.items())[-4:])
    return {}


def fin_trends(facts):
    """Last fiscal years of revenue / SG&A / net income / operating cash flow / cash, and derived flags."""
    series = {k: _annual(facts, v) for k, v in CONCEPTS.items()}
    years = sorted(series["revenue"])[-3:]
    out = {"years": years, "series": {k: {y: v for y, v in s.items()} for k, s in series.items()}}
    def last2(s):
        ys = sorted(s)
        return (s[ys[-1]], s[ys[-2]], ys[-1]) if len(ys) >= 2 else (None, None, ys[-1] if ys else None)
    rev, rev_p, ry = last2(series["revenue"])
    sga, sga_p, _ = last2(series["sga"])
    flags = []
    if rev and rev_p:
        out["revenue_growth"] = (rev - rev_p) / abs(rev_p)
    if sga and sga_p:
        out["sga_change"] = (sga - sga_p) / abs(sga_p)
    if rev and sga:
        out["sga_pct_revenue"] = sga / rev
    ni = sorted(series["net_income"].items())
    out["loss_years"] = sum(1 for _y, v in ni[-3:] if v is not None and v < 0)
    ocf = sorted(series["ocf"].items())
    cash = sorted(series["cash"].items())
    if ocf and cash and ocf[-1][1] is not None and ocf[-1][1] < 0 and cash[-1][1]:
        out["runway_months"] = round(cash[-1][1] / (-ocf[-1][1]) * 12, 1)
    if out.get("sga_change") is not None and out["sga_change"] <= -0.08 and (out.get("revenue_growth") or 0) <= 0.05:
        flags.append("cost_cutting")
    if out.get("revenue_growth") is not None and out["revenue_growth"] <= -0.10:
        flags.append("revenue_down")
    if out.get("revenue_growth") is not None and out["revenue_growth"] >= 0.30 and (out.get("sga_change") or 0) < out["revenue_growth"] / 2:
        flags.append("growth_strain")
    if out.get("runway_months") is not None and out["runway_months"] < 18:
        flags.append("short_runway")
    out["flags"] = flags
    out["fy_end"] = ry
    return out


def _pct(x):
    return f"{x * 100:+.0f}%"


def _usd(v):
    if v is None:
        return "?"
    a = abs(v)
    s = f"${a / 1e9:.1f}B" if a >= 1e9 else f"${a / 1e6:.1f}M" if a >= 1e6 else f"${a / 1e3:.0f}K"
    return ("-" if v < 0 else "") + s


# ------------------------------------------------------------ signals
FILING_LABEL = {
    "filing_erp_project": "says it is moving to a new ERP / finance system",
    "filing_lean_accounting": "admits it is short of accounting staff or expertise",
    "filing_outsourced_reporting": "relies on outside help for its financial / SEC reporting",
    "filing_remediation": "is still remediating a control weakness",
}
SECTION_NAME = {"mdna": "MD&A", "controls": "Controls and Procedures", "risk": "Risk Factors", "business": "Business"}


def signals_for_company(doc, today, window_days=420):
    """Engine-ready signals from one company's research file (dates are the filing dates)."""
    out = []
    cik, name = doc["cik"], doc.get("company", "")
    cutoff = (today - dt.timedelta(days=window_days)).isoformat()
    # 10-K / 10-Q text: newest filing that says each thing
    used = set()
    for adsh, f in sorted(doc.get("filings", {}).items(), key=lambda kv: kv[1].get("date", ""), reverse=True):
        if f.get("date", "") < cutoff:
            continue
        for typ, h in (f.get("hits") or {}).items():
            if typ in used:
                continue
            used.add(typ)
            out.append({"type": typ, "cik": cik, "company": name, "date": f["date"],
                        "detail": f"Its {f['form']} filed {f['date']} ({SECTION_NAME.get(h.get('section'), h.get('section'))}) {FILING_LABEL[typ]}: \"{h['quote'].rstrip(' .;')}.\"",
                        "url": f.get("url", ""), "ref": "state", "source": "SEC filing text"})
    a = doc.get("audit") or {}
    if a.get("change") is not None and a["change"] >= 0.20 and a["current"] - a["prior"] >= 100_000 and a.get("date", "") >= cutoff:
        yr = f" for {a['year_cur']}" if a.get("year_cur") else ""
        out.append({"type": "audit_fee_jump", "cik": cik, "company": name, "date": a["date"],
                    "detail": f"Audit fees rose {_pct(a['change'])}{yr}, to {_usd(a['current'])} from {_usd(a['prior'])} ({a.get('source', 'proxy statement')}).",
                    "url": a.get("url", ""), "ref": "state", "source": "SEC proxy"})
    for act in doc.get("activists", []):
        if act.get("date", "") < cutoff:
            continue
        who = act.get("who") or "An investor"
        out.append({"type": "activist_investor", "cik": cik, "company": name, "date": act["date"],
                    "detail": f"{who} filed a Schedule 13D on {act['date']} with activist intent" + (f": \"{act['quote']}\"" if act.get("quote") else "."),
                    "url": act.get("url", ""), "ref": "state", "source": "SEC 13D"})
        break
    fin = doc.get("fin") or {}
    fl = fin.get("flags") or []
    if fl and fin.get("fy_end"):
        bits = []
        if fin.get("revenue_growth") is not None:
            bits.append(f"revenue {_pct(fin['revenue_growth'])}")
        if fin.get("sga_change") is not None:
            bits.append(f"SG&A {_pct(fin['sga_change'])}")
        if fin.get("loss_years"):
            bits.append(f"losses in {fin['loss_years']} of the last 3 years")
        if fin.get("runway_months") is not None:
            bits.append(f"about {fin['runway_months']:.0f} months of cash at the current burn")
        what = {"cost_cutting": "cutting overhead", "revenue_down": "revenue falling", "growth_strain": "growing fast on a lean overhead",
                "short_runway": "cash is tight"}
        typ = "fin_growth_strain" if fl == ["growth_strain"] else "fin_cost_pressure"
        out.append({"type": typ, "cik": cik, "company": name, "date": fin["fy_end"],
                    "detail": f"Financial trend (fiscal year ended {fin['fy_end']}): " + ", ".join(what[f] for f in fl) + " (" + ", ".join(bits) + ").",
                    "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K", "ref": "state", "source": "SEC XBRL data"})
    return out

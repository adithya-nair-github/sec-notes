"""Extra SEC data per company, beyond 8-Ks: audit fees, activist 13Ds and financial trends,
plus turning everything the researcher found into engine signals.

  audit_fees(text)        "Audit Fees $2,150,000 $1,890,000" -> current vs prior year (proxy DEF 14A, or 10-K Item 14)
  activist_13d(...)       Schedule 13D filed about the company by a known activist or with activist intent
  fin_trends(facts)       revenue, SG&A, net income, operating cash flow and cash from SEC XBRL company facts
  signals_for_company()   ready-made signals the private engine adds as they are
"""
import datetime as dt
import re

VERSION = 4   # bump to make the runner re-read audit fees, 13Ds and financials

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
        out = {"current": cur, "prior": prior, "year_cur": ycur, "year_prior": (ycur - 1) if ycur else None,
               "change": (cur - prior) / prior if prior else None}
        # A second, different "Audit Fees" row after this one usually means one table per audit firm (an auditor
        # change): one firm's table alone makes the switch look like a jump (hand check 20 Oct: RenovoRx read +47%,
        # real +7.6%). Fees are then not comparable, so report no change rather than a wrong one.
        rest = text[m.end(): m.end() + 15000]
        for m2 in AUDIT_ROW_ONE.finditer(rest):
            v = _amount(m2.group(1), m2.group(2), table_unit)
            if 10_000 <= v <= 500_000_000 and v not in (a, b):
                out.update(change=None, several_tables=True)
                break
        return out
    return None


AUDIT_ROW_ONE = re.compile(r"\baudit\s+fees\b(?:\s*\(\s*\d\s*\))?[^$\d]{0,60}" + MONEY, re.I)


# ------------------------------------------------------------ activists
ACTIVISTS = re.compile(   # firm names in full: a bare "Elliott" is often a founder's family name
    r"elliott (investment )?management|elliott associates|elliott international|starboard value|icahn (enterprises|capital|partners)|"
    r"carl c\.? icahn|trian (fund|partners)|valueact|jana partners|third point (llc|partners|management)|engaged capital|"
    r"land (and|&) buildings|ancora (advisors|holdings|catalyst)|legion partners|irenic capital|politan capital|mantle ridge|"
    r"sachem head|corvex|pershing square|barington capital|blackwells capital|cannell capital|impactive capital|"
    r"anson (funds|advisors)|carronade capital|driver management|kent lake|bluebell capital|saba capital|scopia capital|"
    r"praesidium|marathon partners|stilwell|cevian capital|inclusive capital|sarissa capital|voss capital|"
    r"harbert discovery|radoff|fourworld|macellum|alta fox|mangrove partners", re.I)
ACTIVIST_INTENT = re.compile(   # concrete pressure, not the standard "may engage in discussions" wording
    r"nominat\w+[^.]{0,40}(director|candidate|slate)|proxy (contest|solicitation|fight)|solicit\w* proxies|"
    r"cooperation agreement|standstill|seek\w* (to )?(replace|remove|elect)|letter (to|addressed to) the board|"
    r"call(ing)? (a|for a) special meeting|(urge|urging|call(ed|ing)? on|press\w*) (the )?(board|management|issuer)|"
    r"(explore|pursue|run) (a )?(sale of the (company|issuer)|strategic alternatives|sale process)|"
    r"significantly undervalued[^.]{0,160}(dialogue|discussions|engage)|board (seat|representation)", re.I)
NOT_ACTIVIST = re.compile(
    r"solely for investment purposes|voting (and support )?agreement|merger agreement|securities purchase agreement|"
    r"combined company|in connection with the (merger|offering|acquisition|business combination)|conversion of (the )?preferred|"
    r"(has been|was) granted the right to (appoint|designate)|underwriting agreement|registration rights|"
    r"customary standstill|may contain customary|confidentiality (or similar )?agreement|non-disclosure|\bnda\b", re.I)
FORMS_13D = ("SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A")


def activist_from_text(text):
    """(is_activist, filer_hint, sentence) from a 13D's text."""
    t = text or ""
    name = ACTIVISTS.search(t[:20000])
    item4 = re.search(r"item\s*4\.?\s*purpose of (the )?transaction(.{0,4000})", t, re.I | re.S)
    body = item4.group(2) if item4 else ""
    intent = ACTIVIST_INTENT.search(body)
    if intent and NOT_ACTIVIST.search(body):
        intent = None
    sent = ""
    if intent:
        s0 = body.rfind(".", 0, intent.start()) + 1
        s1 = body.find(".", intent.end())
        sent = re.sub(r"\s+", " ", body[s0: s1 + 1 if s1 > 0 else intent.end() + 200]).replace("Â", "").strip()[:350]
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
    best = {}
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
        if out and (not best or max(out) > max(best)):   # companies switch tags: use the one with the newest year
            best = out
    return dict(sorted(best.items())[-4:])


FINANCIAL_SIC = range(6000, 6800)   # banks, insurers, REITs, brokers: revenue / SG&A / burn mean something else


def fin_trends(facts, sic="", today=None):
    """Last fiscal years of revenue / SG&A / net income / operating cash flow / cash, and derived flags.
    Only the latest fiscal year counts (ended in the last ~16 months); tiny bases and odd jumps are ignored."""
    series = {k: _annual(facts, v) for k, v in CONCEPTS.items()}
    years = sorted(series["revenue"])[-3:]
    out = {"years": years, "series": {k: {y: v for y, v in s.items()} for k, s in series.items()}}
    def last2(s):
        ys = sorted(s)
        return (s[ys[-1]], s[ys[-2]], ys[-1]) if len(ys) >= 2 else (None, None, ys[-1] if ys else None)
    rev, rev_p, ry = last2(series["revenue"])
    sga, sga_p, _ = last2(series["sga"])
    flags = []
    out["fy_end"] = ry
    today = today or dt.date.today()
    try:
        financial = int(sic or 0) in FINANCIAL_SIC
    except ValueError:
        financial = False
    if financial or not ry or (today - dt.date.fromisoformat(ry)).days > 480:
        out["flags"], out["skipped"] = [], "financial company" if financial else "no recent fiscal year"
        return out
    if rev and rev_p and abs(rev_p) >= 5_000_000:
        out["revenue_growth"] = (rev - rev_p) / abs(rev_p)
    if sga and sga_p and abs(sga_p) >= 1_000_000 and abs((sga - sga_p) / abs(sga_p)) <= 3:
        out["sga_change"] = (sga - sga_p) / abs(sga_p)
    if rev and sga:
        out["sga_pct_revenue"] = sga / rev
    ni = sorted(series["net_income"].items())
    out["loss_years"] = sum(1 for _y, v in ni[-3:] if v is not None and v < 0)
    ocf = sorted(series["ocf"].items())
    cash = sorted(series["cash"].items())
    losing = bool(ni) and ni[-1][1] is not None and ni[-1][1] < 0
    if losing and ocf and cash and ocf[-1][1] is not None and ocf[-1][1] < 0 and cash[-1][1]:   # burning cash, not just working capital
        out["runway_months"] = round(cash[-1][1] / (-ocf[-1][1]) * 12, 1)
    if out.get("sga_change") is not None and out["sga_change"] <= -0.08 and (out.get("revenue_growth") or 0) <= 0.05:
        flags.append("cost_cutting")
    if out.get("revenue_growth") is not None and out["revenue_growth"] <= -0.10:
        flags.append("revenue_down")
    if (out.get("revenue_growth") is not None and out["revenue_growth"] >= 0.30 and abs(rev_p) >= 20_000_000
            and out.get("sga_change") is not None and out["sga_change"] < out["revenue_growth"] / 2):
        flags.append("growth_strain")
    if out.get("runway_months") is not None and out["runway_months"] < 12:
        flags.append("short_runway")
    out["flags"] = flags
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


def signals_for_company(doc, today, window_days=420, semantic_max=1):
    """Engine-ready signals from one company's research file (dates are the filing dates).
    Filing quotes the AI check dropped are skipped; plus at most `semantic_max` filing_semantic_pain."""
    out = []
    cik, name = doc["cik"], doc.get("company", "")
    cutoff = (today - dt.timedelta(days=window_days)).isoformat()
    # 10-K / 10-Q text. A control problem counts only if the newest filing still reports it
    # (risk-factor wording: the newest 10-K); an ERP project from any recent filing.
    fs = sorted(((k, f) for k, f in doc.get("filings", {}).items() if f.get("date", "") >= cutoff),
                key=lambda kv: kv[1].get("date", ""), reverse=True)
    newest = fs[0][0] if fs else None
    newest_10k = next((k for k, f in fs if f.get("form", "").startswith("10-K")), None)
    used = set()
    for adsh, f in fs:
        for typ, h in (f.get("hits") or {}).items():
            if typ in used or (h.get("judge") or {}).get("keep") is False:
                continue
            if typ != "filing_erp_project":
                ok = adsh == newest or (h.get("section") == "risk" and adsh == newest_10k)
                if not ok:
                    continue
            if re.search(r"\b(in|of|the|to|and|as|with|item \d+[a-c]?)\W*$", h["quote"].strip(), re.I):
                continue          # cut-off quote ("As previously disclosed in.")
            used.add(typ)
            label = FILING_LABEL[typ]
            if typ == "filing_erp_project" and h.get("status") == "done":
                label = "says it recently finished moving to a new ERP / finance system"
            out.append({"type": typ, "cik": cik, "company": name, "date": f["date"],
                        "detail": f"Its {f['form']} filed {f['date']} ({SECTION_NAME.get(h.get('section'), h.get('section'))}) {label}: \"{h['quote'].rstrip(' .;')}.\"",
                        "url": f.get("url", ""), "ref": "state", "source": "SEC filing text"})
    # pain said in other words (engine/semantic.py): newest filing first, never a theme or quote the rules already cover
    if semantic_max and any(f.get("sem") for _a, f in fs):
        from . import semantic
        rule_quotes = [h.get("quote", "") for _a, f in fs for h in list((f.get("hits") or {}).values()) + f.get("judged_out", [])]
        for f, s in semantic.pick([(f, f.get("sem")) for _a, f in fs], used, rule_quotes, semantic_max):
            out.append({"type": "filing_semantic_pain", "cik": cik, "company": name, "date": f["date"],
                        "detail": f"Its {f['form']} filed {f['date']} ({SECTION_NAME.get(s.get('section'), s.get('section'))}) says: "
                                  f"\"{s['quote'].rstrip(' .;')}.\" Theme: {semantic.theme_label(s['theme'])} "
                                  f"(found by meaning, similarity {s['sim']:.2f}).",
                        "url": f.get("url", ""), "ref": "state", "source": "SEC filing text"})
    a = doc.get("audit") or {}
    if (a.get("change") is not None and 0.20 <= a["change"] <= 3.0 and a["prior"] >= 100_000 and a["current"] - a["prior"] >= 100_000
            and a.get("date", "") >= cutoff and (not a.get("year_cur") or a["year_cur"] >= today.year - 1)):
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
    # financial trend: only the two that point at a stretched finance team. Revenue falling or cash running
    # out on their own are not buying signals (a company with months of cash left is not shopping for software).
    fin = doc.get("fin") or {}
    fl = fin.get("flags") or []
    if "short_runway" in fl and fin.get("fy_end"):
        # not a buying signal: the engine uses it only to dampen the score (weight 0, never shown in emails)
        out.append({"type": "fin_short_runway", "cik": cik, "company": name, "date": fin["fy_end"],
                    "detail": "Losses and cash burn suggest a short cash runway: may not be buying new software now.",
                    "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K", "ref": "state",
                    "source": "SEC XBRL data"})
    typ = "fin_cost_pressure" if "cost_cutting" in fl else "fin_growth_strain" if "growth_strain" in fl else None
    if typ and fin.get("fy_end"):
        bits = []
        if fin.get("revenue_growth") is not None and abs(fin["revenue_growth"]) >= 0.005:
            bits.append(f"revenue {_pct(fin['revenue_growth'])}")
        if fin.get("sga_change") is not None and abs(fin["sga_change"]) >= 0.005:
            bits.append(f"SG&A {_pct(fin['sga_change'])}")
        what = "cutting overhead while revenue is flat or down" if typ == "fin_cost_pressure" else "growing fast while overhead barely grows"
        out.append({"type": typ, "cik": cik, "company": name, "date": fin["fy_end"],
                    "detail": f"Financial trend (fiscal year ended {fin['fy_end']}): {what} (" + ", ".join(bits) + "). Context only: never cite their numbers.",
                    "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K", "ref": "state", "source": "SEC XBRL data"})
    return out

"""Thin wrappers around the free SEC EDGAR endpoints the engine uses."""
import datetime as dt
import re

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
DAILY_MASTER = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/master.{ymd}.idx"
QUARTER_MASTER = "https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{q}/master.idx"
FTS = "https://efts.sec.gov/LATEST/search-index"

PERIODIC = {"10-K", "10-Q", "20-F", "40-F", "10-KT", "10-QT"}


def pad(cik):
    return str(int(str(cik).strip())).zfill(10)


def short(cik):
    return str(int(str(cik).strip()))


def adsh_prefix(adsh):
    return adsh.split("-")[0].zfill(10)


def doc_url(cik, adsh, filename=None):
    base = f"https://www.sec.gov/Archives/edgar/data/{short(cik)}/{adsh.replace('-', '')}"
    return f"{base}/{filename}" if filename else f"{base}/{adsh}-index.htm"


def quarter(d):
    return (d.month - 1) // 3 + 1


# ---------------------------------------------------------------- submissions
def submissions(http, cik):
    """Company metadata + recent filings as a list of dicts (newest first)."""
    data = http.get(SUBMISSIONS.format(cik=pad(cik)), want="json")
    if not data:
        return None
    rec = data.get("filings", {}).get("recent", {})
    keys = list(rec.keys())
    n = len(rec.get("accessionNumber", []))
    filings = []
    for i in range(n):
        row = {k: (rec[k][i] if i < len(rec[k]) else None) for k in keys}
        filings.append(row)
    return {
        "cik": pad(cik),
        "name": data.get("name", ""),
        "tickers": data.get("tickers", []),
        "sic": data.get("sic", ""),
        "sicDescription": data.get("sicDescription", ""),
        "category": data.get("category", ""),
        "state": ((data.get("addresses") or {}).get("business") or {}).get("stateOrCountry", "") or "",
        "filings": filings,
    }


# ---------------------------------------------------------------- indexes
def _parse_master(text):
    rows = []
    started = False
    for line in text.splitlines():
        if not started:
            if line.startswith("-----"):
                started = True
            continue
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, name, form, date, fname = parts
        date = date.strip()
        if re.fullmatch(r"\d{8}", date):
            date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        m = re.search(r"(\d{10}-\d{2}-\d{6})", fname)
        if not m:
            continue
        rows.append({"cik": pad(cik), "name": name.strip(), "form": form.strip(),
                     "date": date, "adsh": m.group(1)})
    return rows


def daily_master(http, day):
    url = DAILY_MASTER.format(y=day.year, q=quarter(day), ymd=day.strftime("%Y%m%d"))
    text = http.get(url)
    return _parse_master(text) if text else None


def quarter_master(http, year, q):
    text = http.get(QUARTER_MASTER.format(y=year, q=q), timeout=120)
    return _parse_master(text) if text else []


# ---------------------------------------------------------------- full-text search
def fts(http, q, forms=None, startdt=None, enddt=None, cik=None, max_pages=10):
    """EDGAR full-text search. Returns normalized hits (primary docs + exhibits)."""
    out = []
    for page in range(max_pages):
        params = [("q", q)]
        if forms:
            params.append(("forms", forms))
        if startdt:
            params += [("dateRange", "custom"), ("startdt", startdt), ("enddt", enddt or dt.date.today().isoformat())]
        if cik:
            params.append(("ciks", pad(cik)))
        if page:
            params.append(("from", str(page * 100)))
        from urllib.parse import urlencode
        data = http.get(f"{FTS}?{urlencode(params)}", want="json")
        if not data:
            break
        hits = data.get("hits", {}).get("hits", [])
        for h in hits:
            src = h.get("_source", {})
            _id = h.get("_id", "")
            adsh, _, fname = _id.partition(":")
            ciks = [pad(c) for c in src.get("ciks", [])]
            file_type = src.get("file_type") or src.get("form") or ""
            out.append({
                "ciks": ciks,
                "names": src.get("display_names", []),
                "form": src.get("form") or (src.get("root_forms") or [""])[0],
                "file_type": file_type,
                "is_exhibit": file_type.upper().startswith("EX-"),
                "file_date": src.get("file_date", ""),
                "items": src.get("items") or [],
                "adsh": src.get("adsh") or adsh,
                "url": doc_url(ciks[0], src.get("adsh") or adsh, fname) if ciks else "",
            })
        total = data.get("hits", {}).get("total", {}).get("value", 0)
        if (page + 1) * 100 >= total or not hits:
            break
    return out


def clean_name(display_name):
    """'ZILLOW GROUP, INC.  (Z, ZG)  (CIK 0001617640)' -> 'ZILLOW GROUP, INC.'"""
    return re.split(r"\s{2,}\(|\s\(CIK", display_name)[0].strip()

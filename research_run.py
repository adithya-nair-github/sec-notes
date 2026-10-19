"""Filings researcher: 10-K/10-Q text, audit fees, activist 13Ds and financial trends per company.

Runs in the public repo with the private engine repo checked out in --data (same as calls_run.py).
Writes state/research/<cik>.json (with ready-made "signals") and output/research_report_shard<k>.md
into the private repo. The public log prints counts only.

  python research_run.py --data data --shard 0 --shards 6
"""
import argparse
import datetime as dt
import json
import os
import time

from calls_run import load, watchlist, watchlist_rows
from engine import ats, edgar, filings, research
from engine import http as httpmod

UA = os.environ.get("SEC_USER_AGENT") or "research-bot contact@example.com"
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def doc_url(cik, f):
    return edgar.doc_url(cik, f["accessionNumber"], f.get("primaryDocument", "")) if f.get("primaryDocument") else edgar.doc_url(cik, f["accessionNumber"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--budget-min", type=int, default=320)
    ap.add_argument("--max-companies", type=int, default=100000)
    ap.add_argument("--facts-every-days", type=int, default=30)
    a = ap.parse_args()
    t0, today = time.time(), dt.date.today()
    D = a.data
    http = httpmod.Http(UA)
    watch = watchlist(D)
    rows = watchlist_rows(D)
    private_boards = load(os.path.join(D, "state", "job_boards.json"), {})
    order = sorted(watch)
    mine = [c for i, c in enumerate(order) if i % a.shards == a.shard][: a.max_companies]
    out_dir = os.path.join(D, "state", "research")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(D, "output"), exist_ok=True)
    year_ago = (today - dt.timedelta(days=400)).isoformat()
    counts, lines, done = {}, [], 0

    def bump(k, n=1):
        counts[k] = counts.get(k, 0) + n

    for cik in mine:
        if (time.time() - t0) / 60 > a.budget_min:
            break
        sub = edgar.submissions(http, cik)
        if not sub:
            bump("no_submissions")
            continue
        done += 1
        company = watch.get(cik) or sub["name"]
        path = os.path.join(out_dir, f"{cik}.json")
        doc = load(path, {"cik": cik, "company": company, "filings": {}, "activists": []})
        if doc.get("rv") != research.VERSION:     # better rules: read audit fees, 13Ds, financials and job boards again
            doc.update(audit={}, activists=[], fin_checked="", jobs={}, rv=research.VERSION)
        doc["company"], doc["sic"] = company, sub.get("sic", "")
        fs = sub["filings"]

        # 1. 10-K / 10-Q text (latest 10-K + 10-Qs of the last ~13 months, each read once)
        periodic = [f for f in fs if f.get("form") in ("10-K", "10-Q") and f.get("filingDate", "") >= year_ago]
        tenk = [f for f in periodic if f["form"] == "10-K"][:1]
        tenq = [f for f in periodic if f["form"] == "10-Q"][:3]
        latest_10k_text = None
        for f in tenk + tenq:
            adsh = f["accessionNumber"]
            if adsh in doc["filings"] and doc["filings"][adsh].get("v") == filings.VERSION:
                continue
            raw = http.get(doc_url(cik, f))
            if not raw:
                bump("filing_fetch_failed")
                continue
            r = filings.read_filing(raw, f["form"], year=int(f["filingDate"][:4]))
            fees_text = r.pop("fees_text", "")
            if f["form"] == "10-K":
                latest_10k_text = fees_text
            doc["filings"][adsh] = {"form": f["form"], "date": f["filingDate"], "url": doc_url(cik, f), "v": filings.VERSION, **r}
            bump("filings_read")
            for typ in r["hits"]:
                bump(typ)
        keep = {f["accessionNumber"] for f in tenk + tenq}
        doc["filings"] = {k: v for k, v in doc["filings"].items() if k in keep}

        # 2. audit fees: latest proxy (DEF 14A), else the 10-K's Item 14
        proxy = [f for f in fs if f.get("form") in ("DEF 14A", "DEFM14A") and f.get("filingDate", "") >= year_ago][:1]
        a_old = doc.get("audit") or {}
        if proxy and a_old.get("adsh") != proxy[0]["accessionNumber"]:
            raw = http.get(doc_url(cik, proxy[0]))
            fees = research.audit_fees(filings.eightk.to_text(raw or ""))
            if fees:
                doc["audit"] = {**fees, "date": proxy[0]["filingDate"], "url": doc_url(cik, proxy[0]),
                                "adsh": proxy[0]["accessionNumber"], "source": "proxy statement"}
                bump("audit_fees_found")
            else:
                doc["audit"] = {"adsh": proxy[0]["accessionNumber"], "none": True}
                bump("audit_fees_not_parsed")
        elif not proxy and latest_10k_text:
            fees = research.audit_fees(latest_10k_text)
            if fees:
                doc["audit"] = {**fees, "date": tenk[0]["filingDate"], "url": doc_url(cik, tenk[0]),
                                "adsh": tenk[0]["accessionNumber"], "source": "10-K Item 14"}
                bump("audit_fees_found")

        # 3. activist 13Ds about the company (last ~13 months)
        seen = {x.get("adsh") for x in doc.get("activists", [])}
        for f in fs:
            if f.get("form") not in research.FORMS_13D or f.get("filingDate", "") < year_ago or f["accessionNumber"] in seen:
                continue
            raw = http.get(doc_url(cik, f))
            is_act, who, sent = research.activist_from_text(filings.eightk.to_text(raw or ""))
            doc.setdefault("activists", []).append({"adsh": f["accessionNumber"], "date": f["filingDate"], "form": f["form"],
                                                    "url": doc_url(cik, f), "activist": is_act, "who": who.title() if who else "", "quote": sent})
            bump("13d_read")
            if is_act:
                bump("activist_found")
        doc["activists"] = sorted([x for x in doc.get("activists", []) if x.get("date", "") >= year_ago],
                                  key=lambda x: x["date"], reverse=True)[:6]
        doc["activists_signal"] = [x for x in doc["activists"] if x.get("activist")]

        # 4. financial trends (XBRL company facts), refreshed monthly
        if (doc.get("fin_checked") or "") < (today - dt.timedelta(days=a.facts_every_days)).isoformat():
            facts = http.get(FACTS.format(cik=edgar.pad(cik)), want="json")
            if facts:
                doc["fin"] = research.fin_trends(facts, sic=doc.get("sic", ""), today=today)
                bump("fin_trends")
                for fl in doc["fin"].get("flags", []):
                    bump("fin_" + fl)
            doc["fin_checked"] = today.isoformat()

        # 5. SEC-reporting job ads on Workday / iCIMS / SmartRecruiters (+ boards the careers page links to).
        #    Skipped when the daily engine already reads this company's Greenhouse/Lever/Ashby board.
        job_sigs = []
        pb = private_boards.get(cik) or {}
        if not pb.get("ats") and rows.get(cik, {}).get("domain"):
            j = doc.get("jobs") or {}
            age = (today - dt.date.fromisoformat(j["checked"])).days if j.get("checked") else 999
            if age >= (90 if j.get("board") else 30):
                j = {"board": ats.discover(http, rows[cik]["domain"]), "checked": today.isoformat()}
                bump("job_board_found" if j["board"] else "job_board_none")
            if j.get("board") and not ats.board_owner_ok(j["board"], company, rows[cik]["domain"]):
                j["board"], j["hits"] = None, []
                bump("job_board_not_theirs")
            if j.get("board"):
                try:
                    hits = ats.classify(ats.SEARCH[j["board"]["ats"]](http, j["board"], today))
                except Exception:
                    hits = []
                    bump("job_search_error")
                j["hits"], j["searched"] = hits, today.isoformat()
                job_sigs = ats.signals(cik, company, j["board"]["ats"], hits, today)
                bump("job_ads_found", len(hits))
            doc["jobs"] = j

        sig_doc = dict(doc, activists=doc["activists_signal"])
        doc["signals"] = research.signals_for_company(sig_doc, today) + job_sigs
        doc["updated"] = today.isoformat()
        json.dump(doc, open(path, "w"), indent=1)
        if doc["signals"]:
            lines.append(f"## {company}")
            lines += [f"- [{s['type']}] {s['detail']} {s['url']}" for s in doc["signals"]]
            lines.append("")

    mins = round((time.time() - t0) / 60)
    head = [f"# Filings research {today} · shard {a.shard + 1} of {a.shards}", f"{done} companies checked in {mins} min."]
    head += [f"- {k}: {v}" for k, v in sorted(counts.items())] + [""]
    open(os.path.join(D, "output", f"research_report_shard{a.shard}.md"), "w").write("\n".join(head + lines))
    print(f"research shard {a.shard + 1}/{a.shards}: {done} companies in {mins} min; " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()

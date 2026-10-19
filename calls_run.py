"""Earnings-call transcriber runner.

Runs in its own (public) repo. The private engine repo is checked out into --data;
everything we read (watchlist, signals, signers) and write (call files, transcripts,
reports) lives there. The public run log prints counts only.

  python calls_run.py --data data --mode backfill --shard 0 --shards 12 --budget-min 320
  python calls_run.py --data data --mode weekly   --shard 0 --shards 12
  python calls_run.py --data data --mode pilot    --max-events 10

Modes
  pilot     latest earnings call of the top companies (to see where the funnel leaks)
  weekly    earnings 8-Ks from the last 21 days not done yet, plus one retry of recent misses
  backfill  every earnings 8-K of the last ~13 months (4 calls per company), best companies first
Rolling window: each company keeps its newest 4 calls; older ones and their transcripts are dropped.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import time

from engine import calls, edgar, http as httpmod

UA = os.environ.get("SEC_USER_AGENT") or "research-bot contact@example.com"


def load(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def watchlist_rows(data):
    """{cik: {company, domain}} from the private repo's watchlist."""
    out = {}
    with open(os.path.join(data, "data", "watchlist.csv"), newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            c = (r.get("cik") or "").strip()
            if c:
                out[edgar.pad(c)] = {"company": r.get("company", ""), "domain": (r.get("domain") or "").strip()}
    return out


def watchlist(data):
    out = {}
    with open(os.path.join(data, "data", "watchlist.csv"), newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            c = (r.get("cik") or "").strip()
            if c:
                out[edgar.pad(c)] = r.get("company", "")
    return out


def earnings_events(sub, since, cik, company):
    """Earnings 8-Ks (Item 2.02) since a date, each with the 8-Ks filed 0-5 days after it."""
    eights = [f for f in sub["filings"] if str(f.get("form", "")).startswith("8-K")]
    out = []
    for f in eights:
        d = f.get("filingDate", "")
        if d < since or "2.02" not in str(f.get("items", "")) or f.get("form") == "8-K/A":
            continue
        d0 = dt.date.fromisoformat(d)
        follow = [g["accessionNumber"] for g in eights if g is not f and g.get("filingDate", "") >= d
                  and dt.date.fromisoformat(g["filingDate"]) <= d0 + dt.timedelta(days=5)
                  and any(x in str(g.get("items", "")) for x in ("7.01", "8.01", "9.01"))]
        out.append({"cik": cik, "company": company, "adsh": f["accessionNumber"], "date": d, "followups": follow[:3]})
    return out


def due(rec, mode, today):
    """Should we (re)try this event?"""
    if not rec:
        return True
    if rec.get("v", 0) < calls.VERSION:   # better rules: read the call again (text sources are quick; audio ones too, once)
        return True
    if rec.get("stage") == "done":
        return False
    tries = rec.get("tries", 1)
    last = rec.get("last_try", rec.get("date", ""))
    age = (today - dt.date.fromisoformat(rec["date"])).days
    since_last = (today - dt.date.fromisoformat(last)).days if last else 99
    # replays often go up a day or two late: retry recent misses once, a week apart
    return tries < 2 and age <= 45 and since_last >= 6 and mode != "pilot"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--mode", choices=["pilot", "weekly", "backfill"], default="weekly")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--max-events", type=int, default=100000)
    ap.add_argument("--budget-min", type=int, default=320)
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--fix-model", default="small.en")
    ap.add_argument("--keep", type=int, default=4)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-dataset", action="store_true")
    a = ap.parse_args()

    t0, today = time.time(), dt.date.today()
    D = a.data
    http = httpmod.Http(UA)
    watch = watchlist(D)
    rows = watchlist_rows(D)
    signals = load(os.path.join(D, "state", "signals.json"), {})
    meta = load(os.path.join(D, "state", "meta.json"), {})
    signers = meta.get("signers", {})
    score = {}
    for s in (signals.values() if isinstance(signals, dict) else signals):
        if s.get("cik") in watch:
            score[s["cik"]] = score.get(s["cik"], 0) + 1
    order = sorted(watch, key=lambda c: (-score.get(c, 0), c))
    mine = [c for i, c in enumerate(order) if i % a.shards == a.shard]
    if a.mode == "pilot":
        mine = order          # walk down the list until max_events calls were tried

    calls_dir = os.path.join(D, "state", "calls")
    tr_dir = os.path.join(D, "state", "transcripts")
    os.makedirs(calls_dir, exist_ok=True)
    os.makedirs(os.path.join(D, "output"), exist_ok=True)
    # weekly: this season's calls first for everyone, then keep filling in the year (catch-up)
    passes = {"pilot": [100], "weekly": [21, 395], "backfill": [395]}[a.mode]
    subs, hf = {}, {}
    # transcripts you drop in yourself: state/transcripts_in/<cik>_<YYYY-MM-DD>.txt
    inbox = {}
    in_dir = os.path.join(D, "state", "transcripts_in")
    if os.path.isdir(in_dir):
        for fn in os.listdir(in_dir):
            m = re.match(r"(\d{1,10})[_-](\d{4}-\d{2}-\d{2})\.txt$", fn)
            if m:
                inbox.setdefault(edgar.pad(m.group(1)), []).append((m.group(2), os.path.join(in_dir, fn)))
    since_hf = (today - dt.timedelta(days=400)).isoformat()
    sources = {}

    funnel, lines, n_events = {}, [], 0
    for days in passes:
      since = (today - dt.timedelta(days=days)).isoformat()
      for cik in mine:
        if n_events >= a.max_events or (time.time() - t0) / 60 > a.budget_min:
            break
        if cik not in subs:
            subs[cik] = edgar.submissions(http, cik)
        sub = subs[cik]
        if not sub:
            continue
        company = watch.get(cik) or sub["name"]
        path = os.path.join(calls_dir, f"{cik}.json")
        doc = calls.load_doc(path, cik, company)
        sg = signers.get(cik) or signers.get(str(int(cik))) or {}
        doc["company"], doc["cfo_name"] = company, sg.get("pfo", doc.get("cfo_name", ""))
        evs = earnings_events(sub, since, cik, company)
        if evs and cik not in hf and not a.no_dataset:
            hf[cik] = calls.hf_calls(sub.get("tickers") or [], since_hf)
        for ev in evs:
            ev["domain"] = rows.get(cik, {}).get("domain", "")
            ev["hf"] = calls.hf_match(hf.get(cik), ev["date"])
            ev["inbox_file"] = calls.inbox_match(inbox.get(cik), ev["date"])
        if a.mode == "pilot":
            evs = evs[:1]
        evs = evs[: a.keep]
        changed = False
        for ev in evs:
            if n_events >= a.max_events or (time.time() - t0) / 60 > a.budget_min:
                break
            old = doc["events"].get(ev["adsh"])
            if not due(old, a.mode, today):
                continue
            n_events += 1
            try:
                rec, text = calls.process_event(http, ev, "/tmp/calls", cfo_name=doc["cfo_name"], ceo_name=sg.get("ceo", ""),
                                                model=a.model, fix_model=a.fix_model, use_browser=not a.no_browser)
            except Exception as e:  # one bad call never stops the shard
                rec, text = {"cik": cik, "adsh": ev["adsh"], "date": ev["date"], "stage": "error", "error": str(e)[:200], "hits": []}, None
            rec["tries"] = (old or {}).get("tries", 0) + 1
            rec["last_try"] = today.isoformat()
            if text:
                rec["transcript_file"] = os.path.relpath(calls.save_transcript(tr_dir, cik, ev["date"], text, ev["adsh"]), D)
            doc["events"][ev["adsh"]] = rec
            changed = True
            funnel[rec["stage"]] = funnel.get(rec["stage"], 0) + 1
            if rec.get("source"):
                sources[rec["source"]["kind"]] = sources.get(rec["source"]["kind"], 0) + 1
            lines.append(f"## {company} ({ev['date']}): {rec['stage']}")
            if rec.get("source"):
                lines.append(f"Source: {rec['source']['kind']} {rec['source']['url']} · {rec.get('minutes') or '?'} min · {rec.get('words', '?')} words")
            else:
                if rec.get("links"):
                    lines.append("Links in the press release: " + " | ".join(rec["links"][:4]))
                if rec.get("ir"):
                    lines.append(f"IR site: {rec['ir']['pages']} pages read, {rec['ir']['found']} items for this call")
                if rec.get("tried"):
                    lines.append("Tried: " + " | ".join(rec["tried"][:8]))
            for h in rec.get("hits", []):
                lines.append(f"- [{calls.mmss(h['at'])} {h['part']}, {h['speaker']}{', verified' if h.get('verified') else ''}] {h['type']}: \"{h['quote']}\"")
            lines.append("")
        if changed or "cfo_name" in doc:
            # make tries relative to absolute data dir for prune
            for r in doc["events"].values():
                if r.get("transcript_file") and not os.path.isabs(r["transcript_file"]):
                    r["_abs"] = os.path.join(D, r["transcript_file"])
            for adsh, r in sorted(doc["events"].items(), key=lambda kv: kv[1].get("date", ""), reverse=True)[a.keep:]:
                p = r.get("_abs")
                if p and os.path.exists(p):
                    os.remove(p)
                del doc["events"][adsh]
            for r in doc["events"].values():
                r.pop("_abs", None)
            doc["signals"] = calls.signals_for_company(doc, today)  # ready-made for the private engine
            doc["updated"] = today.isoformat()
            json.dump(doc, open(path, "w"), indent=1)

    mins = round((time.time() - t0) / 60)
    head = [f"# Earnings-call run {today} · mode {a.mode} · shard {a.shard + 1} of {a.shards}",
            f"{n_events} calls tried in {mins} min. Where each one ended:"]
    head += [f"- {k}: {v}" for k, v in sorted(funnel.items(), key=lambda x: -x[1])]
    head += [f"- source_{k}: {v}" for k, v in sorted(sources.items())]
    head.append("")
    notes = []
    if calls._HF["err"]:
        notes.append(f"- Transcript dataset problem: {calls._HF['err'][:160]}")
    if calls.YT["blocked"]:
        notes.append("- YouTube asked for a sign-in check, so YouTube was skipped for the rest of the run.")
    if notes:
        head += ["## Notes"] + notes + [""]
    open(os.path.join(D, "output", f"calls_report_{a.mode}_shard{a.shard}.md"), "w").write("\n".join(head + lines))
    # public log: counts only, never company names or links
    print(f"shard {a.shard + 1}/{a.shards} mode={a.mode}: {n_events} calls tried in {mins} min; " +
          ", ".join(f"{k}={v}" for k, v in sorted(funnel.items())))


if __name__ == "__main__":
    main()

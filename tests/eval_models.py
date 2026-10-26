"""Real-model check of the AI quote check (engine/judge.py) and the meaning search (engine/semantic.py).

Downloads the models (first time) and scores a fixed set of hand-written sentences: real finance pain in many
wordings, and look-alikes (customers, products, gas pipes, sports, blank-check boilerplate). Prints precision and
recall at several thresholds so the defaults can be tuned. Nothing private is read or printed.

  python tests/eval_models.py
  python tests/eval_models.py --judge-models cross-encoder/nli-deberta-v3-xsmall,MoritzLaurer/deberta-v3-base-zeroshot-v2.0
Exit code 1 if a model cannot be loaded (so the GitHub run shows red).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import judge, semantic  # noqa: E402

# (sentence, rule type it was found under, should the quote be kept?)
JUDGE_SET = [
    # good quotes (paraphrases of real ones, company names removed)
    ("We continue to make progress on the transition of our ERP system to SAP S/4HANA, which we expect to go live next year.", "call_finance_systems", True),
    ("We are moving all of our regions onto a single ERP platform over the next two years.", "call_finance_systems", True),
    ("We successfully deployed our new 1CORE ERP system across the organization this quarter.", "call_finance_systems", True),
    ("We are taking actions to streamline our corporate cost structure and reduce overhead.", "call_cost_headcount", True),
    ("We remain on track with our cost transformation plan, which will lower our operating expenses by the end of the fiscal year.", "call_cost_headcount", True),
    ("We completed a workforce reduction that eliminated several hundred positions across the company.", "call_cost_headcount", True),
    ("We identified a material weakness related to revenue recognition and have started remediation.", "call_close_controls_audit", True),
    ("Our goal is to shorten our month-end close from ten days to five by the end of the year.", "call_close_controls_audit", True),
    ("We are preparing to comply with the California climate disclosure rules starting next year.", "call_reporting_regs", True),
    ("We completed the acquisition and are integrating the acquired business into our systems and processes.", "call_mna_integration", True),
    ("Our accounting function is leanly staffed, and a small number of employees prepare our financial statements.", "filing_lean_accounting", True),
    ("We rely on an outside consulting firm to prepare our SEC filings and handle technical accounting matters.", "filing_outsourced_reporting", True),
    ("We are in the process of remediating the material weakness by adding review controls and hiring additional staff.", "filing_remediation", True),
    ("We are in the process of implementing a new enterprise resource planning system, which we expect to complete in 2027.", "filing_erp_project", True),
    # look-alikes the rules let through
    ("Viewership was up sharply during the NHL layoffs, which drove record advertising for our sports networks.", "call_cost_headcount", False),
    ("We are investing in system upgrades to replace aging gas pipes and improve safety across our distribution network.", "call_finance_systems", False),
    ("As a blank check company, we have limited resources and rely on our officers, who are not accounting professionals.", "filing_lean_accounting", False),
    ("Our clients announced workforce reductions, which reduced demand for our staffing services.", "call_cost_headcount", False),
    ("Customers continue to invest in finance transformation and ERP upgrades, which drives demand for our consulting.", "call_finance_systems", False),
    ("We are the leading ERP for small online sellers, and our new release adds AI features.", "call_finance_systems", False),
    ("We integrate with all of the major systems, like Workday, SAP and Oracle.", "call_finance_systems", False),
    ("We completed the remediation of the punch list items at the new plant ahead of schedule.", "call_close_controls_audit", False),
    ("We terminated the former chief executive officer after the board's investigation of his conduct.", "call_close_controls_audit", False),
    ("We entered into an amended and restated credit agreement that extends our maturities to 2031.", "call_close_controls_audit", False),
    ("Layoffs at our service providers created some delays in deliveries this quarter.", "call_cost_headcount", False),
    ("Our proprietary ERP gives our franchise partners real-time visibility into their stores.", "call_finance_systems", False),
]

# (sentence, theme it should be found under, or None for a look-alike)
SEM_SET = [
    ("Our accounting team is stretched thin.", "stretched_team"),
    ("Honestly, the finance group has been doing double duty since the acquisition closed.", "stretched_team"),
    ("We have a small team in accounting and everybody wears a lot of hats.", "stretched_team"),
    ("We're still closing the books largely in spreadsheets.", "manual_close"),
    ("It takes us almost three weeks to close each month, and we want to cut that in half.", "manual_close"),
    ("A lot of our consolidation still happens by hand at quarter end.", "manual_close"),
    ("We're evaluating a few new tools to help us put together our SEC filings.", "reporting_tools"),
    ("This year we plan to move our external reporting onto a modern cloud platform.", "reporting_tools"),
    ("The audit took longer this year.", "audit_pain"),
    ("Our audit bill went up quite a bit as the auditors did more testing.", "audit_pain"),
    ("Our auditors flagged a few issues in our controls that we're working through.", "audit_pain"),
    ("We're getting ready for the new climate disclosure rules, which will be a lot of work for the team.", "new_requirements"),
    ("As we cross into large accelerated filer status, our filing deadlines will get tighter.", "new_requirements"),
    ("We're in the middle of moving the whole company onto one new system for finance and supply chain.", "finance_systems"),
    ("The cutover to our new general ledger happened in July and caused some delays.", "finance_systems"),
    ("We're taking a hard look at overhead and have cut corporate costs by ten percent.", "cost_pressure"),
    ("We've flattened the organization and removed a layer of management.", "cost_pressure"),
    ("We are combining our back-office teams into one shared service center to save money.", "cost_pressure"),
    # look-alikes
    ("Our customers are stretched thin and are delaying purchases.", None),
    ("Our platform helps finance teams close their books in days instead of weeks.", None),
    ("Our clients are evaluating new reporting tools, which is driving demand for our services.", None),
    ("We are upgrading the pipeline system to replace aging cast iron mains.", None),
    ("The NHL playoffs drove record ratings for our sports networks.", None),
    ("Our sales team is stretched across a lot of new accounts.", None),
    ("As a blank check company, we have limited resources until we complete a business combination.", None),
    ("Revenue grew eight percent and gross margin expanded.", None),
    ("The market remains challenging, with higher interest rates weighing on demand.", None),
    ("We launched a new version of our accounting software for small businesses.", None),
    ("Our engineering team is working nights to finish the product launch.", None),
    ("Store labor was tight during the holidays.", None),
    ("General and administrative expenses increased because of higher salaries and bonuses.", None),
    ("We expect our effective tax rate to be about twenty-four percent next year.", None),
]


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def eval_judge(name):
    J = judge.Judge({"model": name}, log=print)
    t = time.time()
    raw = J.raw([(s, k) for s, k, _y in JUDGE_SET])
    if raw is None:
        return None
    secs = time.time() - t
    print(f"\n=== Judge {name}: {len(JUDGE_SET)} rule quotes scored in {secs:.1f}s "
          f"({secs / (len(JUDGE_SET) * 5) * 1000:.0f} ms per sentence/statement pair, first call includes warm-up)")
    print(f"{'keep?':6} {'pos':>5} {'neg':>5}  sentence")
    for (s, k, y), (pos, neg) in zip(JUDGE_SET, raw):
        print(f"{'yes' if y else 'no':6} {pos:5.2f} {neg:5.2f}  [{k}] {s[:110]}")
    print("\nrule threshold -> precision / recall / F1 of kept quotes (look-alikes dropped / good quotes lost)")
    best = None
    for thr in (0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70):
        J.cfg["rule_threshold"] = thr
        tp = fp = fn = dropped_bad = 0
        for (_s, _k, y), (pos, neg) in zip(JUDGE_SET, raw):
            keep = J.label(pos, neg) == "pain"
            tp += keep and y
            fp += keep and not y
            fn += (not keep) and y
            dropped_bad += (not keep) and not y
        p, r, f = prf(tp, fp, fn)
        mark = " (default)" if thr == judge.DEFAULTS["rule_threshold"] else ""
        print(f"  {thr:4.2f}: P {p:.2f}  R {r:.2f}  F1 {f:.2f}   look-alikes dropped {dropped_bad}/{sum(1 for x in JUDGE_SET if not x[2])}, "
              f"good lost {fn}/{sum(1 for x in JUDGE_SET if x[2])}{mark}")
        if best is None or (f, r) > best[1:]:
            best = (thr, f, r)
    print(f"  best F1 at rule threshold {best[0]:.2f}")
    return J


def eval_semantic(J, embed_model):
    S = semantic.Semantic({"model": embed_model, "sim_cutoff": -1.0, "top_k": 999}, judge=J, log=print)
    if not S.available:
        return False
    import numpy as np
    t = time.time()
    lib, cols, neg = S._lib
    x = semantic._unit(np, S._encode([s for s, _t in SEM_SET]))
    sims = x @ lib.T
    themes = list(cols)
    best = np.stack([sims[:, cols[th]].max(axis=1) for th in themes], axis=1)
    nsim = (x @ neg.T).max(axis=1)
    rows = []
    for i, (s, want) in enumerate(SEM_SET):
        j = int(best[i].argmax())
        rows.append((s, want, themes[j], float(best[i, j]), float(nsim[i])))
    raw = J.raw([(s, th) for s, _w, th, _sim, _n in rows])
    print(f"\n=== Meaning search {embed_model} + judge {J.cfg['model']}: {len(SEM_SET)} sentences in {time.time() - t:.1f}s")
    print(f"{'want':16} {'got':16} {'sim':>5} {'neg':>5} {'pos':>5} {'nneg':>5}  sentence")
    for (s, want, th, sim, ns), (pos, nn) in zip(rows, raw):
        print(f"{str(want):16} {th:16} {sim:5.2f} {ns:5.2f} {pos:5.2f} {nn:5.2f}  {s[:90]}")
    n_pos = sum(1 for _s, w in SEM_SET if w)
    print("\nsimilarity cut-off x semantic judge threshold -> precision / recall (a find counts only with the right theme)")
    out = []
    for cut in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        cells = []
        for thr in (0.40, 0.50, 0.60, 0.70, 0.80):
            J.cfg["semantic_threshold"] = thr
            tp = fp = 0
            for (s, want, th, sim, ns), (pos, nn) in zip(rows, raw):
                if sim >= cut and ns < sim and J.label(pos, nn, strict=True) == "pain":
                    if want == th:
                        tp += 1
                    else:
                        fp += 1
            p, r, f = prf(tp, fp, n_pos - tp)
            cells.append(f"{thr:.2f}: P {p:.2f} R {r:.2f}")
            out.append((f, p, r, cut, thr))
        print(f"  cut {cut:.2f} | " + " | ".join(cells))
    out.sort(reverse=True)
    f, p, r, cut, thr = out[0]
    print(f"  defaults: cut {semantic.DEFAULTS['sim_cutoff']:.2f}, judge {judge.DEFAULTS['semantic_threshold']:.2f}; "
          f"best F1 {f:.2f} (P {p:.2f}, R {r:.2f}) at cut {cut:.2f}, judge {thr:.2f}")
    sims_pos = [sim for _s, w, th, sim, _n in rows if w]
    sims_neg = [sim for _s, w, th, sim, _n in rows if not w]
    print(f"  similarity: pain sentences {min(sims_pos):.2f}-{max(sims_pos):.2f} (mean {sum(sims_pos) / len(sims_pos):.2f}), "
          f"look-alikes {min(sims_neg):.2f}-{max(sims_neg):.2f} (mean {sum(sims_neg) / len(sims_neg):.2f})")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge-models", default=judge.DEFAULTS["model"])
    ap.add_argument("--embed-model", default=semantic.DEFAULTS["model"])
    a = ap.parse_args()
    ok = True
    for i, name in enumerate([m.strip() for m in a.judge_models.split(",") if m.strip()]):
        J = eval_judge(name)
        if J is None:
            ok = False
            continue
        if i == 0 and not eval_semantic(J, a.embed_model):
            ok = False
    print("\nOK" if ok else "\nA model could not be loaded (see above).")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

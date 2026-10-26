"""Offline tests for the AI quote check (engine/judge.py) and the meaning search (engine/semantic.py).
Models are mocked: no torch, no downloads. Run:  python -m unittest discover -s tests -p "test_*.py" -v"""
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import calls, filings, judge, research, semantic  # noqa: E402

TODAY = dt.date(2026, 10, 20)

TRANSCRIPT = """Operator

Good morning and welcome to the third quarter call. After the presentation there will be a question-and-answer session.

Jane Smith -- Chief Financial Officer

Thanks. We are on track to complete our move to SAP S/4HANA by the end of next year across all regions. Honestly, our accounting team is stretched thin after a very busy year for the whole company. Our customers are going through workforce reductions of their own, which hurt demand.

John Doe -- Chief Executive Officer

Viewership was up sharply during the NHL layoffs, which drove record advertising for our sports networks this quarter.

Operator

Our first question comes from Sam Lee with Big Bank Securities. I think your accounting team is stretched thin with all of the reporting work you have.

Sam Lee -- Big Bank Securities -- Analyst

It sounds like your accounting team is stretched thin given all the reporting work this year and last.

Jane Smith -- Chief Financial Officer

Sure. We still close the books in spreadsheets at quarter end, and that takes a lot of the team's time each month.
"""

FILING = """<html><body>
<p>Item 7. Management's Discussion and Analysis</p>
<p>We are in the process of implementing a new enterprise resource planning (ERP) system, which we expect to go live in 2027 across our segments. Our audit took longer this year and our audit fees rose as the auditors performed more testing on our accounts. We could need more accounting staff if the audit took longer in the future.</p>
<p>Item 8. Financial Statements</p>
<p>Item 9A. Controls and Procedures</p>
<p>We have limited accounting and finance resources as a shell company with no operations and only two officers in total. Our accounting team is leanly staffed, and a small number of employees prepare our financial statements each quarter. Our disclosure controls and procedures were effective as of the end of the period covered by this report.</p>
<p>Item 9B. Other Information</p>
</body></html>"""

BAD = re.compile(r"NHL|shell company|blank check|gas pipe", re.I)
GOOD = re.compile(r"S/4HANA|leanly|stretched|spreadsheet|audit|ERP|material weakness|overhead", re.I)


def fake_scorer(calls_log=None):
    """P(entailment) by keywords: look-alikes low, real pain high, customers -> 'about customers'."""
    def score(pairs):
        if calls_log is not None:
            calls_log.append(len(pairs))
        out = []
        for premise, hyp in pairs:
            if hyp in judge.NEG_HYPOTHESES:
                out.append(0.92 if re.search(r"customer|client", premise, re.I) else 0.02)
            elif BAD.search(premise):
                out.append(0.03)
            elif GOOD.search(premise):
                out.append(0.91)
            else:
                out.append(0.30)
        return out
    return score


GROUPS = [r"stretched|short-handed", r"spreadsheet|by hand", r"audit", r"overhead|cost structure", r"customer|pipeline|playoff"]


def fake_encoder(texts):
    import numpy as np
    rows = []
    for t in texts:
        rows.append([1.0 if re.search(g, t, re.I) else 0.0 for g in GROUPS] + [0.05])
    return np.asarray(rows, dtype="float32")


THEMES = {
    "stretched_team": {"label": "stretched finance team", "covered_by": ["filing_lean_accounting"],
                       "examples": ["Our accounting team is stretched thin.", "We are short-handed in accounting."]},
    "manual_close": {"label": "manual or slow close", "covered_by": ["call_close_controls_audit"],
                     "examples": ["We close the books in spreadsheets."]},
    "audit_pain": {"label": "audit pain", "covered_by": ["filing_remediation"], "examples": ["The audit took longer this year."]},
    "cost_pressure": {"label": "G&A / overhead cost pressure", "covered_by": ["call_cost_headcount"],
                      "examples": ["We are cutting overhead."]},
}
NEGATIVES = ["Our customers are going through a tough time."]


def make(logs=None, cache=None, judge_cfg=None, scorer=None):
    J = judge.Judge(judge_cfg, cache=cache, log=(logs.append if logs is not None else (lambda m: None)), scorer=scorer or fake_scorer())
    S = semantic.Semantic(None, judge=J, log=(logs.append if logs is not None else (lambda m: None)), encoder=fake_encoder,
                          themes=THEMES, negatives=NEGATIVES)
    return J, S


def no_model(*_a, **_k):
    raise ImportError("No module named 'torch'")


def attributed():
    return calls.attribute(calls.text_segments(TRANSCRIPT), "Jane Smith", "John Doe")


class JudgeTests(unittest.TestCase):
    def test_drops_false_positive_keeps_good_quote(self):
        J, _S = make()
        dropped = []
        hits, topics = calls.analyze(attributed(), judge=J, dropped=dropped)
        quotes = " | ".join(h["quote"] for h in hits)
        self.assertIn("S/4HANA", quotes)
        self.assertNotIn("NHL", quotes)
        self.assertEqual([d["type"] for d in dropped], ["call_cost_headcount"])
        self.assertIn("NHL", dropped[0]["quote"])
        self.assertEqual(dropped[0]["judge"]["label"], "not_pain")
        self.assertNotIn("call_cost_headcount", topics)     # a rejected quote is not counted as raised
        s4 = next(h for h in hits if "S/4HANA" in h["quote"])
        self.assertEqual(s4["judge"]["label"], "pain")
        self.assertTrue(s4["judge"]["keep"])
        # without the judge the look-alike gets through (today's behaviour)
        plain, _t = calls.analyze(attributed())
        self.assertIn("NHL", " | ".join(h["quote"] for h in plain))

    def test_filing_next_candidate_wins_when_first_is_rejected(self):
        J, _S = make()
        plain = filings.read_filing(FILING, "10-K", year=2026)
        self.assertIn("shell company", plain["hits"]["filing_lean_accounting"]["quote"])
        r = filings.read_filing(FILING, "10-K", year=2026, judge=J)
        self.assertIn("leanly staffed", r["hits"]["filing_lean_accounting"]["quote"])
        self.assertEqual(r["hits"]["filing_lean_accounting"]["judge"]["label"], "pain")
        self.assertIn("shell company", r["judged_out"][0]["quote"])
        self.assertIn("filing_erp_project", r["hits"])

    def test_customer_sentence_is_not_ours(self):
        J, _S = make()
        v = J.verdicts([("Our customers announced workforce reductions that cut demand for our staffing services.", "call_cost_headcount")])[0]
        self.assertEqual(v["label"], "not_ours")
        self.assertFalse(v["keep"])

    def test_shadow_mode_scores_but_keeps_rule_hits(self):
        J, _S = make(judge_cfg={"mode": "shadow"})
        hits, _t = calls.analyze(attributed(), judge=J)
        nhl = next(h for h in hits if "NHL" in h["quote"])
        self.assertEqual(nhl["judge"]["label"], "not_pain")
        self.assertTrue(nhl["judge"]["keep"])
        # semantic finds are never shadow-kept
        self.assertFalse(J.verdicts([("Viewership was up during the NHL layoffs this quarter.", "cost_pressure")], strict=True)[0]["keep"])

    def test_fail_open_when_model_missing(self):
        logs = []
        J = judge.Judge(None, log=logs.append, loader=no_model)
        S = semantic.Semantic(None, judge=J, log=logs.append, encoder=fake_encoder, themes=THEMES, negatives=NEGATIVES)
        att = attributed()
        self.assertEqual(calls.analyze(att, judge=J), calls.analyze(att))
        self.assertEqual(filings.read_filing(FILING, "10-K", 2026, judge=J), filings.read_filing(FILING, "10-K", 2026))
        self.assertFalse(J.available)
        self.assertIsNone(J.verdicts([("x" * 80, "call_erp")]))
        self.assertIsNone(S.scan_call(att))
        self.assertEqual(len(logs), 1)               # one line, however often it is asked
        self.assertIn("keeping rule results", logs[0])
        # the embedding model missing: semantic off, judge still fine
        logs2 = []
        J2 = judge.Judge(None, log=logs2.append, scorer=fake_scorer())
        S2 = semantic.Semantic(None, judge=J2, log=logs2.append, loader=no_model)
        self.assertIsNone(S2.scan_call(att))
        self.assertFalse(S2.available)
        self.assertEqual(len(logs2), 1)
        self.assertTrue(J2.available)

    def test_scorer_crash_mid_run_fails_open(self):
        def boom(_pairs):
            raise RuntimeError("out of memory")
        logs = []
        J = judge.Judge(None, log=logs.append, scorer=boom)
        att = attributed()
        self.assertEqual(calls.analyze(att, judge=J), calls.analyze(att))
        self.assertEqual(len(logs), 1)

    def test_cache_by_sentence_hash(self):
        with tempfile.TemporaryDirectory() as d:
            n = []
            J = judge.Judge(None, cache=judge.Cache(d, "calls", 3, TODAY), scorer=fake_scorer(n))
            s = "We are on track to complete our move to SAP S/4HANA by the end of next year."
            J.verdicts([(s, "call_finance_systems")])
            J.verdicts([("  we are on track to complete our move to SAP S/4HANA   by the end of next year. ", "call_finance_systems")])
            self.assertEqual(len(n), 1)                       # same sentence (case / spaces aside): judged once
            self.assertEqual(J.stats["cached"], 1)
            J.save_cache()
            self.assertEqual(os.listdir(d), ["calls_3.json"])
            with open(os.path.join(d, "calls_3.json")) as fh:
                stored = json.load(fh)["entries"]
            self.assertEqual(len(stored), 1)
            self.assertEqual(list(stored.values())[0][2], TODAY.isoformat())
            # another shard reads every file of the job: no model call; a new threshold re-labels from the cache
            n2 = []
            J2 = judge.Judge({"rule_threshold": 0.95}, cache=judge.Cache(d, "calls", 7, TODAY), scorer=fake_scorer(n2))
            v = J2.verdicts([(s, "call_finance_systems")])[0]
            self.assertEqual(n2, [])
            self.assertEqual(v["label"], "not_pain")
            J2.save_cache()
            self.assertEqual(sorted(os.listdir(d)), ["calls_3.json", "calls_7.json"])
            # a research job does not read the calls files, and a different question is a different key
            self.assertIsNone(judge.Cache(d, "research", 0, TODAY).get(judge.cache_key(J.cfg["model"], "call_finance_systems", s)))
            self.assertNotEqual(judge.cache_key("m", "call_finance_systems", s), judge.cache_key("m", "cost_pressure", s))

    def test_old_cache_entries_expire(self):
        with tempfile.TemporaryDirectory() as d:
            c = judge.Cache(d, "calls", 0, dt.date(2026, 1, 1))
            c.put("old", 0.9, 0.1)
            c.save()
            c2 = judge.Cache(d, "calls", 0, TODAY, keep_days=30)
            c2.put("new", 0.5, 0.1)
            c2.save()
            with open(os.path.join(d, "calls_0.json")) as fh:
                self.assertEqual(list(json.load(fh)["entries"]), ["new"])


class SemanticTests(unittest.TestCase):
    def test_skips_analyst_operator_and_rule_sentences(self):
        att = attributed()
        cands = semantic.call_candidates(att)
        texts = [c["quote"] for c in cands]
        roles = {c["speaker"] for c in cands}
        self.assertTrue(roles <= {"cfo", "ceo", "management"})
        self.assertFalse(any("S/4HANA" in t for t in texts))           # a rule matches it: never repeated here
        self.assertFalse(any("It sounds like your accounting" in t for t in texts))   # analyst
        self.assertFalse(any("I think your accounting" in t for t in texts))          # operator
        self.assertFalse(any("customers" in t for t in texts))         # NOT_OURS
        J, S = make()
        found = S.scan_call(att)
        self.assertEqual({f["theme"] for f in found}, {"stretched_team", "manual_close"})
        team = next(f for f in found if f["theme"] == "stretched_team")
        self.assertEqual(team["speaker"], "cfo")
        self.assertEqual(team["part"], "prepared remarks")
        close = next(f for f in found if f["theme"] == "manual_close")
        self.assertEqual(close["part"], "Q&A")
        self.assertGreaterEqual(team["sim"], 0.9)
        self.assertEqual(team["judge"]["label"], "pain")

    def test_judge_rejects_semantic_lookalike(self):
        J, S = make()
        att = [(None, "Viewership was strong and our audit of the NHL season schedule went well for the networks.", "ceo", "")]
        self.assertEqual(S.scan_call(att), [])

    def test_filing_candidates_filters(self):
        r = filings.read_filing(FILING, "10-K", year=2026, keep_sections=True)
        cands = semantic.filing_candidates(r["sections"])
        texts = " | ".join(c["quote"] for c in cands)
        self.assertIn("Our audit took longer this year", texts)
        self.assertNotIn("could need", texts)          # hypothetical
        self.assertNotIn("were effective", texts)      # controls boilerplate
        self.assertNotIn("leanly staffed", texts)      # a rule matches it
        self.assertNotIn("enterprise resource planning", texts)
        J, S = make()
        found = S.scan_filing(r["sections"])
        self.assertEqual([f["theme"] for f in found], ["audit_pain"])
        self.assertEqual(found[0]["section"], "mdna")

    def test_at_most_n_per_company_and_no_rule_repeats(self):
        def find(theme, quote, sim=0.8):
            return {"quote": quote, "theme": theme, "sim": sim, "rank": sim, "at": 65, "part": "Q&A", "speaker": "cfo",
                    "label": "", "verified": True, "judge": {"score": 0.9, "neg": 0.0, "label": "pain", "keep": True}}
        rule = {"type": "call_cost_headcount", "quote": "We reduced corporate overhead by ten percent through a restructuring program.",
                "at": None, "part": "prepared remarks", "speaker": "cfo", "label": "", "verified": True}
        doc = {"cik": "0000000001", "company": "Acme", "cfo_name": "Jane Smith", "events": {
            "a": {"stage": "done", "date": "2026-10-01", "source": {"kind": "hf_transcript", "url": "https://x/a"},
                  "hits": [rule], "topics": {"call_cost_headcount": 1},
                  "sem": [find("cost_pressure", "We are trimming corporate overhead wherever we can this year, across every function."),
                          find("stretched_team", "Our accounting team is stretched thin after a very busy year for the whole company.", 0.7)]},
            "b": {"stage": "done", "date": "2026-07-01", "source": {"kind": "hf_transcript", "url": "https://x/b"}, "hits": [],
                  "sem": [find("manual_close", "We still close the books in spreadsheets at quarter end, which takes the team days."),
                          find("audit_pain", rule["quote"])]},
            "c": {"stage": "done", "date": "2026-06-01", "source": {"kind": "hf_transcript", "url": "https://x/c"}, "hits": [],
                  "sem": [find("audit_pain", "The audit took longer this year and the auditors asked for a lot more support.")]}}}
        for n, want in ((0, 0), (1, 1), (2, 2), (5, 3)):
            sig = [s for s in calls.signals_for_company(doc, TODAY, semantic_max=n) if s["type"] == "call_semantic_pain"]
            self.assertEqual(len(sig), want)
            self.assertEqual(len({s["detail"].split("Theme: ")[1] for s in sig}), want)    # one per theme
            for s in sig:
                self.assertNotIn("overhead", s["detail"])     # theme covered by call_cost_headcount
                self.assertNotIn("restructuring", s["detail"])   # a rule quote is never repeated
        first = [s for s in calls.signals_for_company(doc, TODAY, semantic_max=1) if s["type"] == "call_semantic_pain"][0]
        self.assertIn("stretched thin", first["detail"])      # newest call first
        self.assertEqual(first["date"], "2026-10-01")

    def test_signal_shape_and_detail(self):
        f = {"quote": "Our accounting team is stretched thin after a very busy year for the whole company.", "theme": "stretched_team",
             "sim": 0.634, "rank": 0.8, "at": 2470, "part": "Q&A", "speaker": "cfo", "label": "", "verified": False,
             "judge": {"score": 0.91, "neg": 0.0, "label": "pain", "keep": True}}
        doc = {"cik": "0000000001", "company": "Acme", "cfo_name": "Jane Smith", "events": {
            "a": {"stage": "done", "date": "2026-10-02", "source": {"kind": "audio_file", "url": "https://www.youtube.com/watch?v=abc"},
                  "hits": [], "sem": [f]}}}
        (s,) = calls.signals_for_company(doc, TODAY)
        self.assertEqual(set(s), {"type", "cik", "company", "date", "detail", "url", "ref", "source"})
        self.assertEqual((s["type"], s["ref"], s["source"], s["date"]), ("call_semantic_pain", "state", "Earnings call", "2026-10-02"))
        self.assertEqual(s["detail"], 'On its Oct 2, 2026 earnings call (Q&A, 41:10), the CFO, Jane Smith said: "Our accounting team is '
                                      'stretched thin after a very busy year for the whole company." Theme: stretched finance team '
                                      '(found by meaning, similarity 0.63). [machine transcript: paraphrase, don\'t quote word for word]')
        self.assertEqual(s["url"], "https://www.youtube.com/watch?v=abc&t=2470s")
        # filings
        rdoc = {"cik": "0000000001", "company": "Acme", "filings": {"x": {
            "form": "10-Q", "date": "2026-08-05", "url": "https://sec/x", "hits": {},
            "sem": [{"quote": "Our audit took longer this year and our audit fees rose.", "section": "mdna", "theme": "audit_pain",
                     "sim": 0.612, "rank": 0.7, "judge": {"score": 0.9, "neg": 0.0, "label": "pain", "keep": True}}]}}}
        (r,) = research.signals_for_company(rdoc, TODAY)
        self.assertEqual(set(r), {"type", "cik", "company", "date", "detail", "url", "ref", "source"})
        self.assertEqual((r["type"], r["source"], r["url"]), ("filing_semantic_pain", "SEC filing text", "https://sec/x"))
        self.assertEqual(r["detail"], 'Its 10-Q filed 2026-08-05 (MD&A) says: "Our audit took longer this year and our audit fees rose." '
                                      'Theme: audit pain (longer audit, higher fees or auditor findings) (found by meaning, similarity 0.61).')
        self.assertEqual(research.signals_for_company(rdoc, TODAY, semantic_max=0), [])


class FlowTests(unittest.TestCase):
    def test_process_event_offline_text_source(self):
        J, S = make()
        paras = [(sp, tx) for sp, tx in [(lab.split(" — ")[0] if lab else "", t) for _s, t, lab in calls.text_segments(TRANSCRIPT)]]
        ev = {"cik": "0000000001", "company": "Acme", "adsh": "0000000001-26-000001", "date": "2026-10-01",
              "hf": {"symbol": "ACME", "fy": 2026, "q": 3, "report_date": "2026-10-01", "paras": paras}}
        rec, text = calls.process_event(None, ev, "/tmp/x", cfo_name="Jane Smith", ceo_name="John Doe", judge=J, semantic=S)
        self.assertEqual(rec["stage"], "done")
        self.assertEqual(rec["jv"], J.signature)
        self.assertEqual(rec["sv"], S.signature)
        self.assertFalse(any("NHL" in h["quote"] for h in rec["hits"]))
        self.assertIn("NHL", rec["judged_out"][0]["quote"])
        self.assertTrue(rec["sem"] and all(f["verified"] for f in rec["sem"]))
        self.assertFalse(calls.needs_reread(rec, J, S))

    def test_reread_from_saved_transcript(self):
        att = attributed()
        hits, topics = calls.analyze(att)                      # stored before the AI check existed
        calls.finish_hits(hits, "hf_transcript")
        saved = "\n".join(f"({label or role}) {text}" for _s, text, role, label in att)
        rec = {"stage": "done", "date": "2026-10-01", "source": {"kind": "hf_transcript", "url": "u"}, "hits": hits, "topics": topics}
        J, S = make()
        self.assertTrue(calls.needs_reread(rec, J, S))
        self.assertTrue(calls.reread_event(rec, saved, "Jane Smith", "John Doe", judge=J, semantic=S))
        self.assertFalse(any("NHL" in h["quote"] for h in rec["hits"]))
        self.assertEqual(rec["judged_out"][0]["type"], "call_cost_headcount")
        self.assertEqual({f["theme"] for f in rec["sem"]}, {"stretched_team", "manual_close"})
        self.assertFalse(calls.needs_reread(rec, J, S))
        self.assertFalse(calls.reread_event(rec, saved, "Jane Smith", "John Doe", judge=J, semantic=S))   # nothing new
        # machine-transcript call: its (re-transcribed) quotes are judged in place, never re-read from the base text
        audio = {"stage": "done", "date": "2026-07-01", "source": {"kind": "audio_file", "url": "u"},
                 "hits": [dict(h, verified=False) for h in hits], "topics": topics}
        calls.reread_event(audio, saved, "Jane Smith", "John Doe", judge=J, semantic=S)
        self.assertEqual(len(audio["hits"]), len(hits))
        nhl = next(h for h in audio["hits"] if "NHL" in h["quote"])
        self.assertFalse(nhl["judge"]["keep"])
        doc = {"cik": "1", "company": "A", "events": {"x": audio}}
        self.assertFalse(any("NHL" in s["detail"] for s in calls.signals_for_company(doc, TODAY)))

    def test_saved_transcript_round_trip_keeps_roles(self):
        att = attributed()
        saved = "\n".join(f"({label or role}) {text}" for _s, text, role, label in att)
        again = calls.attribute(calls.segments_from_saved(saved), "Jane Smith", "John Doe")
        self.assertEqual([(t, r) for _s, t, r, _l in att], [(t, r) for _s, t, r, _l in again])
        # audio-style: no labels, times in the line
        segs = [(0, "Good afternoon and welcome to the Acme earnings call."), (30, "I will now turn the call over to Jane Smith, our Chief Financial Officer."),
                (65, "Thanks. Our accounting team is stretched thin this year."), (3725, "Our next question comes from Sam Lee."),
                (3740, "What about margins next year, can you comment?"), (3760, "Margins should improve as our new plant ramps up.")]
        a1 = calls.attribute(segs, "Jane Smith")
        saved = "\n".join(f"[{calls.mmss(s)}] ({label or role}) {t}" for s, t, role, label in a1)
        a2 = calls.attribute(calls.segments_from_saved(saved), "Jane Smith")
        self.assertEqual(a1, a2)

    def test_stored_filing_rejudge_and_signals_skip_dropped(self):
        r = filings.read_filing(FILING, "10-K", year=2026)
        frec = {"form": "10-K", "date": "2026-03-01", "url": "u", "v": filings.VERSION, **{k: v for k, v in r.items() if k != "fees_text"}}
        doc = {"cik": "0000000001", "company": "Acme", "filings": {"x": frec}}
        self.assertIn("filing_lean_accounting", [s["type"] for s in research.signals_for_company(doc, TODAY)])
        J, _S = make()
        self.assertTrue(filings.rejudge(frec, J))
        self.assertEqual(frec["jv"], J.signature)
        self.assertFalse(frec["hits"]["filing_lean_accounting"]["judge"]["keep"])
        types = [s["type"] for s in research.signals_for_company(doc, TODAY)]
        self.assertNotIn("filing_lean_accounting", types)
        self.assertIn("filing_erp_project", types)
        # judge unavailable: nothing changes
        frec2 = {k: v for k, v in filings.read_filing(FILING, "10-K", year=2026).items() if k != "fees_text"}
        self.assertFalse(filings.rejudge(frec2, judge.Judge(None, log=lambda m: None, loader=no_model)))
        self.assertNotIn("judge", frec2["hits"]["filing_lean_accounting"])

    def test_runner_wiring_flags(self):
        import argparse
        ap = argparse.ArgumentParser()
        semantic.add_args(ap)
        with tempfile.TemporaryDirectory() as d:
            J, S = semantic.build(ap.parse_args([]), d, "calls", 2, TODAY, log=lambda m: None)
            self.assertEqual(J.cache.path, os.path.join(d, "state", "judge_cache", "calls_2.json"))
            self.assertIs(S.judge, J)
            self.assertEqual(S.max_per_company, 1)
            J, S = semantic.build(ap.parse_args(["--no-semantic", "--judge-threshold", "0.3", "--judge-mode", "shadow"]), d, "research", 0, TODAY)
            self.assertIsNone(S)
            self.assertEqual((J.cfg["rule_threshold"], J.cfg["mode"]), (0.3, "shadow"))
            J, S = semantic.build(ap.parse_args(["--no-judge"]), d, "calls", 0, TODAY)
            self.assertEqual((J, S), (None, None))


if __name__ == "__main__":
    unittest.main()

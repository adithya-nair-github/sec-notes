"""Finance pain said in other words (Project W).

The rules only catch wording we wrote down. Here a small sentence-embedding model (all-MiniLM-L6-v2, CPU) turns
each management sentence into a "meaning fingerprint" and compares it with a library of hand-written example
sentences per theme (below). The closest candidates (above a similarity cut-off, and closer to a theme than to
the distractor examples) then go through the Project F judge (engine/judge.py, strict bar). What survives is
stored per call / per filing and becomes, at most once per company per window:

  call_semantic_pain    a management sentence from an earnings call (prepared remarks or management answers)
  filing_semantic_pain  a sentence from the 10-K / 10-Q MD&A, Controls or Risk Factors sections

Never: analyst questions or the operator (calls.attribute roles), sentences a rule pattern already matches
(that is the rules' job, so a semantic signal never repeats a rule quote), sentences about customers / the
market / a product (calls.NOT_OURS), boilerplate, hypotheticals ("could", "may") and denials ("no changes").

Cost: each call / filing is scanned once per settings version (stamp "sv" stored next to it); judge verdicts are
cached by sentence hash (engine/judge.py). Weekly runs only read new calls and filings.
FAIL OPEN: without the models (or without the judge) nothing is emitted and the rules work as before.
"""
import hashlib
import json
import os
import re

from . import calls, earnings, filings
from .judge import MODEL_DIR, record

VERSION = 1   # bump when the example library, filters or selection change (calls / filings are then scanned again)

DEFAULTS = {
    "enabled": True,
    # 22M parameters, ~90 MB, 384-dim vectors; several hundred sentences per second on a 4-core GitHub runner
    "model": "sentence-transformers/all-MiniLM-L6-v2",
    "sim_cutoff": 0.50,     # best cosine similarity to one of the theme's examples
    "top_k": 5,             # best candidates per call / filing that the judge reads
    "max_per_doc": 2,       # finds kept per call / filing (one per theme)
    "max_per_company": 1,   # call_semantic_pain / filing_semantic_pain signals per company per window
    "batch": 64,
}

# theme -> label used in signal details, rule types that already cover it, example sentences (company voice)
THEMES = {
    "stretched_team": {
        "label": "stretched finance team",
        "covered_by": ["filing_lean_accounting", "filing_outsourced_reporting"],
        "examples": [
            "Our accounting team is stretched thin.",
            "Our finance team is small and is carrying a heavy workload.",
            "We are running the finance function with a very lean team.",
            "People in our finance organization are wearing multiple hats.",
            "Our controller is also running FP&A and treasury.",
            "We have had turnover in the accounting department and are short-handed.",
            "The finance team has been working nights and weekends to get the quarter closed.",
            "We do not have enough people in accounting to keep up with the growth of the business.",
            "Our team is doing double duty while we backfill open finance roles.",
            "We have a limited number of people who can handle technical accounting matters.",
            "With a small finance staff, a lot of the reporting work falls on a few individuals.",
            "We lost several key members of the accounting team this year.",
            "Our finance group has not grown at the same pace as the company.",
            "We are relying on a handful of people to prepare our SEC filings.",
            "We need more capacity in our financial reporting group.",
            "The accounting staff is at full capacity.",
            "Our CFO is also acting as our principal accounting officer.",
            "We have been operating without a full-time controller.",
            "Hiring and keeping qualified accounting staff has been hard for us.",
            "The workload on our finance and accounting team increased significantly this year.",
            "Our reporting team is small relative to the complexity of our business.",
            "Key finance positions remained open for most of the year.",
            "We have leaned on temporary staff to get through the close.",
            "Our finance organization is under a lot of strain.",
        ]},
    "manual_close": {
        "label": "manual or slow close",
        "covered_by": ["call_close_controls_audit"],
        "examples": [
            "Our month-end close is still very manual.",
            "We are still closing the books largely in spreadsheets.",
            "It takes us too long to close the quarter.",
            "A lot of our consolidation work is done by hand in Excel.",
            "Our reporting process relies on manual reconciliations.",
            "We spend a lot of time pulling numbers together from different systems at quarter end.",
            "Our close process takes several weeks.",
            "Preparing our quarterly filings involves a lot of manual effort.",
            "We want to get our close down from fifteen days to five.",
            "Too much of our financial reporting is done with spreadsheets and email.",
            "We have multiple ledgers that have to be combined manually each quarter.",
            "Our quarter-end process is time consuming and error prone.",
            "Each reporting cycle requires a large amount of manual data gathering.",
            "We are working to shorten the time it takes to close our books.",
            "Our financial statements are assembled manually from several sources.",
            "The team spends days tying out numbers before each filing.",
            "Late adjustments at quarter end have delayed our reporting.",
            "We need to make our reporting process faster and less manual.",
            "Our reconciliations are performed in spreadsheets outside the accounting system.",
            "The close calendar keeps slipping because of manual steps.",
            "Getting the quarterly report out the door is a scramble every quarter.",
            "We still rely on manual journal entries for much of the close.",
        ]},
    "reporting_tools": {
        "label": "looking at reporting / disclosure tools",
        "covered_by": [],
        "examples": [
            "We are evaluating new tools for our financial reporting.",
            "We are looking at software to help prepare our SEC filings.",
            "We are selecting a new disclosure management platform.",
            "We plan to bring XBRL tagging in house with new software.",
            "We are reviewing vendors for our external reporting process.",
            "We are investing in technology to automate our financial reporting.",
            "We are replacing the tool we use to prepare our 10-K and 10-Q.",
            "We are moving our SEC reporting onto a new cloud platform.",
            "We have started a project to modernize our reporting tools.",
            "We are considering changing our filing agent.",
            "We are implementing new software for our consolidation and reporting.",
            "We are adding reporting software to reduce the manual work in our filings.",
            "We are looking at better tools for board reporting and disclosures.",
            "We will roll out a new reporting platform for the finance team next year.",
            "We are piloting automation tools in our financial reporting group.",
            "We are upgrading the software we use to draft and review our filings.",
            "We have budgeted for new financial reporting technology this year.",
            "We are evaluating how to automate our XBRL and SEC filing process.",
            "We are rethinking the tools we use to produce our annual filing.",
            "We are investing in our reporting infrastructure.",
        ]},
    "audit_pain": {
        "label": "audit pain (longer audit, higher fees or auditor findings)",
        "covered_by": ["call_close_controls_audit", "filing_remediation"],
        "examples": [
            "The audit took longer this year.",
            "Our audit fees went up significantly.",
            "Our auditors identified several control deficiencies.",
            "The year-end audit was more demanding than in prior years.",
            "We incurred higher audit and accounting fees.",
            "Professional fees increased due to additional audit procedures.",
            "Our auditors required additional testing this year.",
            "We received comments from our auditors on our controls.",
            "The audit of our financial statements was delayed.",
            "Audit costs rose as our business became more complex.",
            "Our new auditor needed more time to complete the audit.",
            "We spent more on outside accounting and audit support this year.",
            "The audit process required significant time from our finance team.",
            "Our auditors raised findings that we are addressing.",
            "Higher audit fees contributed to the increase in general and administrative expenses.",
            "We changed audit firms this year.",
            "We needed extra time to respond to our auditor's requests.",
            "Our first integrated audit added cost and work for the team.",
            "Increased audit scrutiny added to our professional fees.",
            "The inspection of our auditor led to more audit work for us.",
            "Compliance costs related to our audit and Sarbanes-Oxley increased.",
        ]},
    "new_requirements": {
        "label": "new reporting requirements",
        "covered_by": ["call_reporting_regs"],
        "examples": [
            "We are preparing for new climate disclosure requirements.",
            "New reporting rules will add work for our finance team.",
            "We are getting ready for the new segment disclosure rules.",
            "We will have to report our greenhouse gas emissions starting next year.",
            "We are preparing to comply with California's climate laws.",
            "The new accounting standard will require additional disclosures from us.",
            "We are working on our first sustainability report.",
            "We will become subject to Sarbanes-Oxley auditor attestation next year.",
            "As we become a large accelerated filer, our reporting deadlines get shorter.",
            "We are preparing for the expanded income tax disclosures.",
            "European sustainability reporting rules will apply to our subsidiaries.",
            "We expect higher compliance costs from new reporting requirements.",
            "We are adopting the new expense disaggregation disclosures.",
            "Our reporting obligations are increasing as we grow.",
            "We will need to file our first annual report as a public company.",
            "We are preparing for the move from smaller reporting company to accelerated filer.",
            "New disclosure requirements are adding complexity to our filings.",
            "We are building processes to collect data for new disclosure rules.",
            "We are preparing our first report under the new sustainability standards.",
            "The new rules require more detailed disclosures in our financial statements.",
        ]},
    "finance_systems": {
        "label": "finance systems / ERP project",
        "covered_by": ["call_finance_systems", "filing_erp_project"],
        "examples": [
            "We are implementing a new ERP system.",
            "We are moving to SAP S/4HANA.",
            "We are consolidating onto a single global ERP platform.",
            "We are replacing our legacy accounting system.",
            "We went live on our new financial system this quarter.",
            "We are upgrading our finance systems.",
            "We are migrating our general ledger to the cloud.",
            "We are rolling out Oracle Cloud across our business units.",
            "We are standardizing our finance processes on one system.",
            "The conversion to our new accounting platform is under way.",
            "We are moving our acquired businesses onto our core systems.",
            "We are in the middle of a multi-year systems transformation.",
            "Our new enterprise system will give us better data.",
            "We are investing in new back-office technology.",
            "The next phase of our systems rollout covers our international operations.",
            "We had some disruption from the cutover to our new system.",
            "We are modernizing our order-to-cash and financial systems.",
            "We are implementing NetSuite across our subsidiaries.",
            "We are moving off of several legacy systems.",
            "Our systems implementation remains on schedule.",
            "We are investing in a new financial planning and reporting system.",
            "We have started the design phase of our new ERP.",
        ]},
    "cost_pressure": {
        "label": "G&A / overhead cost pressure",
        "covered_by": ["call_cost_headcount"],
        "examples": [
            "We are streamlining our corporate cost structure.",
            "We are reducing our general and administrative expenses.",
            "We are taking costs out of the back office.",
            "We are cutting overhead across the company.",
            "We reduced our workforce to lower our operating costs.",
            "We eliminated management layers to simplify the organization.",
            "We are consolidating corporate functions to save costs.",
            "We are focused on lowering our SG&A as a percentage of sales.",
            "We announced a reduction of our salaried headcount.",
            "We are rightsizing our cost base.",
            "We are tightening spending across corporate functions.",
            "We are holding back on hiring in support functions.",
            "We have launched a program to reduce corporate overhead.",
            "Our transformation plan includes lowering our operating expenses.",
            "We are moving some finance work to a shared services center.",
            "We closed offices and reduced staff to cut costs.",
            "We are looking at every dollar of overhead spend.",
            "We expect annual savings from simplifying our organization.",
            "We are reducing the number of people in administrative roles.",
            "We are focused on cost discipline in our support functions.",
            "We are combining functions to run the company more efficiently.",
            "We are doing more with fewer people.",
        ]},
}
# look-alikes: a candidate closer to one of these than to its best theme is dropped before the judge
NEGATIVES = [
    "Our customers are facing pressure to cut costs.",
    "Our clients are going through their own system upgrades.",
    "Our software helps finance teams close their books faster.",
    "Our platform automates financial reporting for our customers.",
    "We sell ERP software to mid-sized companies.",
    "Demand for our consulting services was strong as companies modernize their finance systems.",
    "The market environment remains challenging.",
    "Consumers are pulling back on discretionary spending.",
    "We are upgrading the pipeline system to improve safety.",
    "Our utility is investing in grid and system upgrades.",
    "We completed the upgrade of our manufacturing plant.",
    "Viewership was strong during the playoffs.",
    "Our clinical team is enrolling patients in the trial.",
    "As a blank check company we have limited operations and limited resources.",
    "Our sales team is stretched across many new accounts.",
    "Our engineering team is focused on the new product launch.",
    "Our stores were short-staffed during the holiday season.",
    "The industry is seeing consolidation among competitors.",
    "Interest rates and inflation remain headwinds.",
    "Our tenants are reducing their office space.",
    "Revenue grew in every segment.",
    "We expanded our gross margin this quarter.",
    "Our network upgrades improved service reliability for subscribers.",
    "Our hospitals were short of nurses.",
]

# cheap first cut: the sentence must touch finance / reporting / people / cost / systems at all
DOMAIN = re.compile(
    r"\b(account\w*|financ\w*|audit\w*|close|closing|books|ledger|report\w*|disclos\w*|control\w*|sox|sarbanes|erp|"
    r"systems?|software|platform|tools?|spreadsheets?|excel|manual\w*|automat\w*|staff\w*|teams?|headcount|personnel|"
    r"people|employees?|hires?|hiring|workforce|bandwidth|capacity|stretched|lean\w*|costs?|expens\w*|g&a|sg&a|overhead|"
    r"administrative|restructur\w*|efficien\w*|streamlin\w*|consultants?|outsourc\w*|xbrl|sec|requirements?|standards?|"
    r"asu|csrd|issb|climate|sustainab\w*|esg|pcaob|weakness\w*|remediat\w*|restat\w*|filings?|10-k|10-q|controller|cfo|"
    r"treasury|tax|back[- ]office|shared services?)\b", re.I)
# denials: "there were no changes", "we have no plans to", "did not identify any"
DENIAL = re.compile(
    r"\b(no|not|never|none)\b[^.;]{0,50}\b(plans?|intend\w*|intention|changes?|impact\w*|issues?|problems?|concerns?|"
    r"material weakness\w*|deficienc\w*|layoffs?|reductions?|need\w*|expect\w*|significant)\b|"
    r"\bdid not (identify|experience|result|record)\b|\bthere (were|was|have been|has been) no\b|"
    r"\bwithout (any|a) (significant|material)\b", re.I)
# standard 10-K / 10-Q wording that is never news
FILING_BOILER = re.compile(
    r"\b(were|was|are|is) effective\b|no changes? in (our|the company'?s) internal control|inherent limitations|"
    r"reasonable assurance|cost[- ]benefit|evaluat\w+ (of )?the effectiveness|rules? 13a-15|15d-15|"
    r"participation of (our|the) (management|chief)|forward[- ]looking|in (all )?material respects|attestation report|"
    r"emerging growth company|smaller reporting company", re.I)
# filings: about someone else (calls use calls.NOT_OURS)
NOT_OURS_FILING = re.compile(
    r"\b(customers?|clients?|consumers?|patients?|tenants?|franchisees?|the economy|economic conditions|"
    r"market conditions|competitors?|borrowers?|policyholders|subscribers)\b", re.I)
FILING_SECTIONS = ("mdna", "controls", "risk")


def settings(overrides=None):
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return cfg


def theme_label(theme):
    return (THEMES.get(theme) or {}).get("label", theme.replace("_", " "))


def _sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def qkey(s):
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def is_dup(quote, keys):
    k = qkey(quote)
    return any(k and o and (k[:80] == o[:80] or k in o or o in k) for o in keys)


# ------------------------------------------------------------ candidate sentences
def call_candidates(attributed):
    """Management sentences of a call (prepared remarks + management answers), filtered like the rules."""
    qa_at = calls.qa_index(attributed)
    out, seen = [], set()
    for i, (sec, text, role, label) in enumerate(attributed):
        if role in ("analyst", "operator"):
            continue
        for s in calls.clean_sentences(text):
            s = s.strip()
            if len(s) < 60 or len(s) > 500 or earnings.BOILERPLATE.search(s) or s.endswith("?"):
                continue
            if any(rx.search(s) for rx in calls.CALL_PATTERNS.values()):
                continue          # a rule pattern matches: the rules' territory, never repeated here
            if calls.NOT_OURS.search(s) or DENIAL.search(s) or earnings.NEGATION.search(s.rstrip(" .;!")):
                continue
            if not DOMAIN.search(s):
                continue
            k = qkey(s)
            if k in seen:
                continue
            seen.add(k)
            out.append({"quote": s[:360], "at": sec, "part": "Q&A" if qa_at is not None and i >= qa_at else "prepared remarks",
                        "speaker": role, "label": label})
    return out


def filing_candidates(sections):
    """Factual sentences from the MD&A, Controls and Risk Factors sections (filings.read_filing split them)."""
    out, seen = [], set()
    for sec in FILING_SECTIONS:
        for s in filings.sentences((sections or {}).get(sec, "")):
            s = s.strip()
            if len(s) < 60 or len(s) > 600 or filings.TRUNCATED.search(s):
                continue
            if filings.HYPOTHETICAL.search(s) or earnings.BOILERPLATE.search(s) or FILING_BOILER.search(s):
                continue
            if any(rx.search(s) for rx, _where in filings.FILING_PATTERNS.values()):
                continue
            if NOT_OURS_FILING.search(s) or DENIAL.search(s) or earnings.NEGATION.search(s.rstrip(" .;")):
                continue
            if not DOMAIN.search(s):
                continue
            k = qkey(s)
            if k in seen:
                continue
            seen.add(k)
            out.append({"quote": s[:420], "section": sec})
    return out


# ------------------------------------------------------------ the model
def load_embedder(name, batch=64):
    """fn([text]) -> unit-length vectors (numpy), sentence-transformers on CPU."""
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(name, device="cpu", cache_folder=MODEL_DIR)

    def encode(texts):
        return model.encode(list(texts), batch_size=batch, normalize_embeddings=True, convert_to_numpy=True,
                            show_progress_bar=False)
    return encode


def _unit(np, x):
    x = np.asarray(x, dtype="float32")
    if x.ndim == 1:
        x = x.reshape(1, -1)
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.maximum(n, 1e-9)


class Semantic:
    """scan_call(attributed) / scan_filing(sections) -> [finds] ([] = nothing, None = models not available)."""

    def __init__(self, cfg=None, judge=None, log=print, encoder=None, loader=None, themes=None, negatives=None):
        self.cfg = settings(cfg)
        self.judge = judge
        self.log = log
        self.themes = themes or THEMES
        self.negatives = NEGATIVES if negatives is None else negatives
        self._encode = encoder          # tests inject a fake: fn([text]) -> vectors
        self._loader = loader or load_embedder
        self._failed = False
        self._lib = None
        self.stats = {"scanned": 0, "candidates": 0, "to_judge": 0, "found": 0}

    @property
    def max_per_company(self):
        return int(self.cfg["max_per_company"])

    @property
    def signature(self):
        c = self.cfg
        lib = _sha(json.dumps([[k, v["examples"]] for k, v in sorted(self.themes.items())] + [self.negatives]))[:8]
        base = f"{VERSION}|{c['model']}|{c['sim_cutoff']}|{c['top_k']}|{c['max_per_doc']}|{lib}|{self.judge.signature if self.judge else '-'}"
        return f"s{VERSION}-" + _sha(base)[:10]

    def _fail(self, e):
        self._failed = True
        self.log(f"semantic: meaning search not available ({type(e).__name__}: {str(e)[:120]}); rules only")

    def _ensure(self):
        if not self.cfg["enabled"] or self._failed:
            return False
        try:
            import numpy as np
            if self._encode is None:
                self._encode = self._loader(self.cfg["model"], self.cfg["batch"])
            if self._lib is None:
                ex, owner = [], []
                for t, spec in self.themes.items():
                    for s in spec["examples"]:
                        ex.append(s)
                        owner.append(t)
                lib = _unit(np, self._encode(ex))
                neg = _unit(np, self._encode(self.negatives)) if self.negatives else None
                cols = {t: [j for j, o in enumerate(owner) if o == t] for t in self.themes}
                self._lib = (lib, cols, neg)
        except Exception as e:
            self._fail(e)
            return False
        return True

    @property
    def available(self):
        # the judge first: without it nothing could be confirmed, so the embedding model is not even loaded
        return self.judge is not None and self.judge.available and self._ensure()

    def rank(self, cands):
        """Candidates closest to a theme: [cand + theme, sim], best first (above the cut-off, nearer a theme than a look-alike)."""
        if not cands:
            return []
        import numpy as np
        lib, cols, neg = self._lib
        x = _unit(np, self._encode([c["quote"] for c in cands]))
        sims = x @ lib.T
        themes = list(cols)
        best = np.stack([sims[:, cols[t]].max(axis=1) for t in themes], axis=1)
        nsim = (x @ neg.T).max(axis=1) if neg is not None and len(neg) else np.full(len(cands), -1.0)
        out = []
        for i, c in enumerate(cands):
            j = int(best[i].argmax())
            sim = float(best[i, j])
            if sim < self.cfg["sim_cutoff"] or float(nsim[i]) >= sim:
                continue
            out.append(dict(c, theme=themes[j], sim=round(sim, 3)))
        out.sort(key=lambda c: -c["sim"])
        return out

    def scan(self, cands):
        if not self.available:
            return None
        self.stats["scanned"] += 1
        self.stats["candidates"] += len(cands)
        try:
            ranked = self.rank(cands)[: int(self.cfg["top_k"])]
        except Exception as e:
            self._fail(e)
            return None
        if not ranked:
            return []
        self.stats["to_judge"] += len(ranked)
        vs = self.judge.verdicts([(c["quote"], c["theme"]) for c in ranked], strict=True)
        if vs is None:
            return None
        good = []
        for c, v in zip(ranked, vs):
            if v["label"] == "pain":
                good.append(dict(c, judge=record(v), rank=round((c["sim"] + v["score"]) / 2, 3)))
        good.sort(key=lambda c: -c["rank"])
        out, themes = [], set()
        for c in good:
            if c["theme"] in themes:
                continue
            themes.add(c["theme"])
            out.append(c)
            if len(out) >= int(self.cfg["max_per_doc"]):
                break
        self.stats["found"] += len(out)
        return out

    def scan_call(self, attributed):
        return self.scan(call_candidates(attributed)) if self.available else None

    def scan_filing(self, sections):
        return self.scan(filing_candidates(sections)) if self.available else None


# ------------------------------------------------------------ into the signals
def pick(groups, rule_types=(), rule_quotes=(), max_n=1, themes=None):
    """groups = [(call or filing record, [finds])], newest first -> [(record, find)].
    Skips themes a rule signal of the company already covers, quotes a rule already used, and repeats a theme."""
    themes = themes or THEMES
    rule_types = set(rule_types)
    covered = {t for t, spec in themes.items() if set(spec.get("covered_by", ())) & rule_types}
    keys = [qkey(q) for q in rule_quotes if q]
    out, used = [], set()
    if max_n <= 0:
        return out
    for cont, finds in groups:
        for f in sorted(finds or [], key=lambda f: -f.get("rank", f.get("sim", 0))):
            t = f.get("theme")
            if t in covered or t in used or is_dup(f.get("quote", ""), keys):
                continue
            used.add(t)
            out.append((cont, f))
            if len(out) >= max_n:
                return out
    return out


# ------------------------------------------------------------ runner wiring (calls_run.py / research_run.py)
def add_args(ap):
    g = ap.add_argument_group("AI checks (engine/judge.py = quote check, engine/semantic.py = meaning search)")
    g.add_argument("--no-judge", action="store_true", help="rules only (also turns the meaning search off)")
    g.add_argument("--no-semantic", action="store_true", help="no call_semantic_pain / filing_semantic_pain")
    g.add_argument("--judge-model", default=None)
    g.add_argument("--judge-mode", choices=["drop", "shadow"], default=None)
    g.add_argument("--judge-threshold", type=float, default=None, help="rule hits (default 0.15)")
    g.add_argument("--judge-semantic-threshold", type=float, default=None, help="semantic finds (default 0.60)")
    g.add_argument("--judge-neg-threshold", type=float, default=None, help="'about customers / product / market' (default 0.60)")
    g.add_argument("--embed-model", default=None)
    g.add_argument("--sim-cutoff", type=float, default=None)
    g.add_argument("--semantic-max", type=int, default=None, help="semantic signals per company per window (default 1)")


def build(a, data_dir, job, shard, today=None, log=print):
    """(judge, semantic) for a run, or None for each that is switched off. Models load on first use."""
    from . import judge as judgemod
    J = S = None
    if not getattr(a, "no_judge", False) and judgemod.DEFAULTS["enabled"]:
        J = judgemod.Judge({"model": getattr(a, "judge_model", None), "mode": getattr(a, "judge_mode", None),
                            "rule_threshold": getattr(a, "judge_threshold", None),
                            "semantic_threshold": getattr(a, "judge_semantic_threshold", None),
                            "neg_threshold": getattr(a, "judge_neg_threshold", None)},
                           cache=judgemod.Cache(os.path.join(data_dir, "state", "judge_cache"), job, shard, today), log=log)
    if J is not None and not getattr(a, "no_semantic", False) and DEFAULTS["enabled"]:
        S = Semantic({"model": getattr(a, "embed_model", None), "sim_cutoff": getattr(a, "sim_cutoff", None),
                      "max_per_company": getattr(a, "semantic_max", None)}, judge=J, log=log)
    return J, S

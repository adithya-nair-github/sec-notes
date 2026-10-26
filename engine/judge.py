"""Second check on quotes (Project F): does this sentence really show a finance / reporting pain AT THIS COMPANY?

The rules (calls.CALL_PATTERNS, filings.FILING_PATTERNS) shortlist sentences by wording. Look-alikes slip through
("NHL layoffs", gas-pipe "system upgrades", a blank-check company's "limited resources", a customer's ERP project,
an ERP the company sells). This module asks a small open-source NLI model (zero-shot, CPU) two questions:

  pos  does the sentence entail "we (the company) have <this kind of pain>"?   (best of 1-2 statements per kind)
  neg  does it entail "this is about our customers / a product we sell / the market"?

  label "pain"      pos >= threshold (lenient for rule hits, strict for semantic finds) and not "not_ours"
        "not_ours"  neg >= neg_threshold and neg > pos
        "not_pain"  otherwise
Rule hits are dropped only when the label is not "pain" (mode "drop"); mode "shadow" scores but never drops them.

Raw scores are cached by sentence hash in <data>/state/judge_cache/<job>_<shard>.json (one file per parallel job,
so shards never edit the same file; every file of the job is read). Thresholds are applied after the cache,
so changing a threshold re-labels without re-running the model.

FAIL OPEN: no torch / transformers, no internet, a bad model name -> one log line, the judge reports itself
unavailable and every caller keeps today's rule behaviour.
"""
import datetime as dt
import hashlib
import json
import os
import re

VERSION = 1   # bump when the statements or the decision rule change (stored hits are then judged again, from cache where possible)

# Settings. The runners can override each one (calls_run.py / research_run.py --help).
DEFAULTS = {
    "enabled": True,
    # ~70M parameters (DeBERTa-v3-xsmall backbone), Apache-2.0, 3-way NLI (MNLI-mismatched 87.8%, as good as the
    # 2x larger "small"). About 10-20 ms per sentence/statement pair on a 4-core GitHub runner.
    # Bigger step up if precision disappoints: "MoritzLaurer/deberta-v3-base-zeroshot-v2.0" (~184M, ~3x slower).
    "model": "cross-encoder/nli-deberta-v3-xsmall",
    "rule_threshold": 0.15,      # rule hits: drop only when the model is fairly sure it is NOT this pain
    "semantic_threshold": 0.60,  # semantic finds (no rule behind them): must clearly state the pain
    "neg_threshold": 0.60,       # "about customers / a product / the market" wins above this (and above pos)
    "mode": "drop",              # "drop" or "shadow" (score + label kept on every hit, nothing dropped)
    "batch": 16,
    "max_length": 192,
    "cache_days": 180,           # cache entries not used for this long are forgotten
}
MODEL_DIR = os.environ.get("SEC_NOTES_MODEL_DIR") or os.path.expanduser("~/.cache/sec-notes-models")

# what each rule type / semantic theme must show, in the company's own voice ("we" = the company)
KIND_HYPOTHESES = {
    # rule types: calls
    "call_finance_systems": ["We are implementing, replacing or upgrading our own ERP, accounting or finance system.",
                             "We are moving our business onto a new ERP system such as SAP S/4HANA, Oracle, NetSuite or Workday."],
    "call_close_controls_audit": ["We have an issue with our own accounting, internal controls, auditor, restatement or SEC filings.",
                                  "We are changing or speeding up how we close our books and report our results."],
    "call_reporting_regs": ["We must comply with new financial, climate or sustainability reporting requirements."],
    "call_cost_headcount": ["We are cutting our own costs, overhead or headcount.",
                            "We are reducing our own workforce or simplifying our organization to save money."],
    "call_mna_integration": ["We are integrating a business that we acquired."],
    "call_leadership": ["Our chief financial officer or another finance leader is changing."],
    # rule types: 10-K / 10-Q text
    "filing_erp_project": ["We are implementing, replacing or upgrading our own ERP, accounting or finance system."],
    "filing_lean_accounting": ["We do not have enough accounting or finance staff or expertise.",
                               "Our accounting team is small or thinly staffed."],
    "filing_outsourced_reporting": ["We rely on outside consultants or firms for our accounting or financial reporting."],
    "filing_remediation": ["We are still fixing a weakness in our internal control over financial reporting."],
    # semantic themes (engine/semantic.py THEMES)
    "stretched_team": ["Our finance or accounting team is understaffed, overworked or stretched.",
                       "We do not have enough people in finance or accounting."],
    "manual_close": ["Our financial close or reporting process is slow, manual or relies on spreadsheets."],
    "reporting_tools": ["We are evaluating, buying or adopting new software for financial reporting, disclosure or SEC filings."],
    "audit_pain": ["Our audit became longer, harder or more expensive, or our auditor found problems.",
                   "We are paying more for audit and accounting fees."],
    "new_requirements": ["We face new financial, climate or sustainability reporting requirements."],
    "finance_systems": ["We are implementing, replacing or upgrading our own ERP, accounting or finance system."],
    "cost_pressure": ["We are cutting our own overhead, G&A or corporate costs.",
                      "We are reducing our own headcount to save money."],
}
GENERIC = ["We have a finance, accounting, reporting, controls or cost problem at our own company."]
NEG_HYPOTHESES = ["This is about our customers or clients.",
                  "This describes a product or service that we sell.",
                  "This is about the economy, the market or our industry."]


def settings(overrides=None):
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return cfg


def hypotheses(kind):
    return KIND_HYPOTHESES.get(kind) or GENERIC


def norm(sentence):
    return re.sub(r"\s+", " ", sentence or "").strip().lower()


def _sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def cache_key(model, kind, sentence):
    """Sentence hash + what it was asked: editing a statement or switching model never reuses old scores."""
    asked = _sha("\n".join(hypotheses(kind) + NEG_HYPOTHESES))[:8]
    return _sha(f"{model}\0{asked}\0{norm(sentence)}")[:20]


def record(v):
    """What is stored on a hit / semantic find."""
    return {"score": v["score"], "neg": v["neg"], "label": v["label"], "keep": v["keep"]}


def kept(hit):
    """False only for a hit the judge dropped (unjudged hits count as kept: fail open)."""
    return (hit.get("judge") or {}).get("keep") is not False


# ------------------------------------------------------------ cache
class Cache:
    """Judge scores by sentence hash. One file per job and shard: parallel shards never write the same file
    (no git conflicts when they push); all files of the job are read, so a company that moved shard still hits."""

    def __init__(self, folder, job="calls", shard=0, today=None, keep_days=None):
        self.folder = folder
        self.path = os.path.join(folder, f"{job}_{shard}.json") if folder else None
        self.today = (today or dt.date.today()).isoformat()
        self.keep_days = keep_days or DEFAULTS["cache_days"]
        self.all, self.mine, self.dirty = {}, {}, False
        if folder and os.path.isdir(folder):
            for fn in sorted(os.listdir(folder)):
                if not (fn.startswith(job + "_") and fn.endswith(".json")):
                    continue
                try:
                    with open(os.path.join(folder, fn)) as fh:
                        ents = (json.load(fh) or {}).get("entries") or {}
                except (OSError, ValueError, AttributeError):
                    continue
                for k, v in ents.items():
                    if isinstance(v, list) and len(v) == 3 and (k not in self.all or v[2] > self.all[k][2]):
                        self.all[k] = v
                if os.path.join(folder, fn) == self.path:
                    self.mine = {k: v for k, v in ents.items() if isinstance(v, list) and len(v) == 3}

    def get(self, key):
        v = self.all.get(key)
        if not v:
            return None
        if v[2] != self.today or key not in self.mine:   # used today: keep it alive in this shard's file
            v = [v[0], v[1], self.today]
            self.all[key] = self.mine[key] = v
            self.dirty = True
        return v[0], v[1]

    def put(self, key, pos, neg):
        self.all[key] = self.mine[key] = [pos, neg, self.today]
        self.dirty = True

    def save(self):
        if not self.path or not self.dirty:
            return
        cutoff = (dt.date.fromisoformat(self.today) - dt.timedelta(days=self.keep_days)).isoformat()
        os.makedirs(self.folder, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"v": 1, "entries": {k: v for k, v in sorted(self.mine.items()) if v[2] >= cutoff}}, f, separators=(",", ":"))
        self.dirty = False


# ------------------------------------------------------------ the model
def load_nli(name, batch=16, max_length=192):
    """fn([(premise, hypothesis)]) -> [P(entailment)] with a Hugging Face NLI model on CPU.
    Works with 3-way (contradiction/entailment/neutral) and binary (entailment/not_entailment) models."""
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    try:
        tok = AutoTokenizer.from_pretrained(name, cache_dir=MODEL_DIR)
    except Exception:  # DeBERTa-v3 fast-tokenizer conversion can fail without protobuf: the slow one works
        tok = AutoTokenizer.from_pretrained(name, cache_dir=MODEL_DIR, use_fast=False)
    model = AutoModelForSequenceClassification.from_pretrained(name, cache_dir=MODEL_DIR)
    model.eval()
    labels = {int(i): str(l).lower() for i, l in model.config.id2label.items()}
    ent = [i for i, l in labels.items() if "entail" in l and not l.startswith("not")]
    if len(ent) != 1:
        raise ValueError(f"model has no single entailment label: {labels}")
    ent = ent[0]

    def score(pairs):
        out = []
        for k in range(0, len(pairs), batch):
            chunk = pairs[k: k + batch]
            enc = tok([p for p, _h in chunk], [h for _p, h in chunk], padding=True, truncation="only_first",
                      max_length=max_length, return_tensors="pt")
            with torch.inference_mode():
                logits = model(**enc).logits
            out += torch.softmax(logits.float(), dim=-1)[:, ent].tolist()
        return out
    return score


class Judge:
    """judge.verdicts([(sentence, kind)], strict=False) -> [{score, neg, label, keep}] or None when unavailable."""

    def __init__(self, cfg=None, cache=None, log=print, scorer=None, loader=None):
        self.cfg = settings(cfg)
        self.cache = cache
        self.log = log
        self._scorer = scorer          # tests inject a fake: fn([(premise, hypothesis)]) -> [P(entailment)]
        self._loader = loader or load_nli
        self._failed = False
        self.stats = {"judged": 0, "cached": 0, "kept": 0, "dropped": 0}

    @property
    def signature(self):
        c = self.cfg
        base = f"{VERSION}|{c['model']}|{c['rule_threshold']}|{c['semantic_threshold']}|{c['neg_threshold']}|{c['mode']}"
        return f"j{VERSION}-" + _sha(base)[:10]

    def _ensure(self):
        if not self.cfg["enabled"] or self._failed:
            return None
        if self._scorer is None:
            try:
                self._scorer = self._loader(self.cfg["model"], self.cfg["batch"], self.cfg["max_length"])
            except Exception as e:  # no torch, no internet, wrong name: keep the rules as they are
                self._fail(e)
        return self._scorer

    def _fail(self, e):
        self._failed, self._scorer = True, None
        self.log(f"judge: AI quote check not available ({type(e).__name__}: {str(e)[:120]}); keeping rule results as they are")

    @property
    def available(self):
        return self._ensure() is not None

    def raw(self, items):
        """[(sentence, kind)] -> [(pos, neg)], cached by sentence hash. None when the model is not available."""
        scorer = self._ensure()
        if scorer is None:
            return None
        out, todo = [None] * len(items), []
        for i, (s, kind) in enumerate(items):
            key = cache_key(self.cfg["model"], kind, s)
            got = self.cache.get(key) if self.cache else None
            if got:
                out[i] = got
                self.stats["cached"] += 1
            else:
                todo.append((i, s, kind, key))
        if todo:
            pairs, index = [], []
            for j, (_i, s, kind, _k) in enumerate(todo):
                for h in hypotheses(kind):
                    pairs.append((s, h))
                    index.append((j, 0))
                for h in NEG_HYPOTHESES:
                    pairs.append((s, h))
                    index.append((j, 1))
            try:
                probs = list(scorer(pairs))
                if len(probs) != len(pairs):
                    raise ValueError("scorer returned the wrong number of scores")
            except Exception as e:
                self._fail(e)
                return None
            best = [[0.0, 0.0] for _ in todo]
            for (j, side), p in zip(index, probs):
                best[j][side] = max(best[j][side], float(p))
            for j, (i, _s, _kind, key) in enumerate(todo):
                out[i] = (round(best[j][0], 4), round(best[j][1], 4))
                if self.cache:
                    self.cache.put(key, *out[i])
            self.stats["judged"] += len(todo)
        return out

    def label(self, pos, neg, strict=False):
        c = self.cfg
        if neg >= c["neg_threshold"] and neg > pos:
            return "not_ours"
        return "pain" if pos >= (c["semantic_threshold"] if strict else c["rule_threshold"]) else "not_pain"

    def verdicts(self, items, strict=False):
        """strict=True for semantic finds (stricter bar, never shadow-kept)."""
        if not items:
            return []
        raw = self.raw(items)
        if raw is None:
            return None
        res = []
        for pos, neg in raw:
            lab = self.label(pos, neg, strict)
            keep = lab == "pain" or (self.cfg["mode"] == "shadow" and not strict)
            if not strict:
                self.stats["kept" if keep else "dropped"] += 1
            res.append({"score": round(pos, 3), "neg": round(neg, 3), "label": lab, "keep": keep})
        return res

    def save_cache(self):
        if self.cache:
            try:
                self.cache.save()
            except OSError as e:
                self.log(f"judge: cache not saved ({type(e).__name__})")

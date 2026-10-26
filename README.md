# sec-notes

Small experiments with public SEC EDGAR data and public audio. Code only; nothing is stored here.

Optional AI checks run on CPU with small open-source models from Hugging Face (downloaded once, then cached):
a natural-language-inference model re-checks rule-matched sentences (engine/judge.py), and a sentence-embedding
model looks for the same topics in other words (engine/semantic.py). Offline tests: `python -m unittest discover -s tests -p "test_*.py"`.

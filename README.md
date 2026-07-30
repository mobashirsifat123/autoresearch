# AutoResearch

Provider-first, evidence-preserving research discovery and literature review.
The first milestone accepts one seed paper, retrieves up to 20 recent related
papers, downloads legally accessible PDFs, performs structured analysis, ranks
the top five, and writes an evidence-cited research-gap report.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
autoresearch discover --seed-paper paper.pdf --limit 20
autoresearch review --project PROJECT_ID --top-k 5
```

API keys are read only from environment-variable names declared in
`.autoresearch/config.toml`. Tests use deterministic provider doubles and do
not contact paid services.

# AutoResearch

Provider-first, evidence-preserving conversational research and coding agent.
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

Start the backend and then launch the terminal client:

```bash
cd /Users/mobashirsifat/autoresearch
export AUTORESEARCH_AGENT_ROOTS=/Users/mobashirsifat/autoresearch:/Users/mobashirsifat/autoresearch-cli
autoresearch-api

cd /Users/mobashirsifat/autoresearch-cli
npx --yes bun@1.3.14 run --cwd packages/autoresearch-cli dev
```

The CLI opens in `Default Agent` mode. Ask normal coding questions directly;
use `/tools`, `/mode`, `/use`, `/auto`, `/discover`, `/review`, `/invent`, or
`/experiment` when a specialized capability is wanted. `/reinvent` remains
honestly disabled until its Python backend pipeline exists.

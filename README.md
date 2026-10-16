# AutoResearch

An evidence-backed research cycle in five skills:

```
review → novelty → experiment → manuscript
        └───────── workflow (runs all four in a loop) ─────────┘
```

- **review** — discovers real literature (Semantic Scholar, OpenAlex,
  Crossref, arXiv, OpenReview), extracts verbatim-verified evidence, and
  surfaces a cited research question with counter-evidence.
- **novelty** — generates candidate methods grounded in the reviewed
  evidence, checks them against prior art, critiques them adversarially, and
  freezes the selected methodology (versioned, checksummed).
- **experiment** — turns the frozen methodology into working research code
  (generated + statically verified), plans a falsifiable spec, and executes
  it only under an explicit, exact-checksum approval. Measurements are
  ingested, identity-checked, recomputed from raw outputs where possible,
  and analyzed deterministically. Negative and null results are valid
  outcomes.
- **manuscript** — assembles the validated lineage into an editable,
  cited manuscript (Markdown + LaTeX + BibTeX + tables + manifest). Every
  number traces to a measurement artifact; missing evidence produces an
  explicitly incomplete draft, never a fabricated one.
- **workflow** — one durable loop over all four stages, reusing completed
  work and stopping where asked.

Every analyzed outcome is persisted to a shared **Findings Memory**
(including failures and refutations) that conditions later runs, so the
system does not repeatedly rediscover unsuccessful approaches.

## Install

```bash
pip install -e .
```

## Configure

Copy `.autoresearch/config.example.toml` to `.autoresearch/config.toml` and
point it at your provider. Secrets are never inlined: name an environment
variable (`api_key_env`). Inside an agent session the key resolves
automatically (session/connector credentials) — no separate export needed.

## Use

```bash
autoresearch skill workflow --seed-paper <pdf|doi|url> --until design
autoresearch skill experiment approve --candidate-id <id> --accept   # your approval gate
autoresearch skill workflow --project <project_id> --until manuscript

# or each skill separately:
autoresearch skill review     --project <id>
autoresearch skill novelty    --project <id>
autoresearch skill experiment plan --candidate-id <id>
autoresearch skill manuscript --project <id>
```

`autoresearch skill install --target opencode` (or `claude`) installs the
five skills into your agent host as explicit slash commands
(`/review`, `/novelty`, `/experiment`, `/manuscript`, `/workflow`).

## Honesty rules (enforced in code, not prompts)

- No fabricated citations, measurements, hardware facts, or outcomes — ever.
- Execution requires an explicit approval bound to the exact spec checksum.
- Fixture-labelled artifacts cannot pass scientific-evidence gates.
- Numerical authority is deterministic; model-assisted manuscript prose is
  audited (every number, citation, and overclaim checked) and falls back to
  the deterministic draft on any violation.
- An incomplete result stays visibly incomplete.

## Verify

```bash
pip install pytest anyio
pytest tests/ -q   # offline end-to-end smoke of the five-skill loop
```

# AutoResearch

An evidence-backed research cycle in five skills:

```
review → novelty → experiment → manuscript
        └───────── workflow (runs all four in a loop) ─────────┘
```

- **review** — discovers real literature (Semantic Scholar, OpenAlex,
  Crossref, arXiv, OpenReview), extracts verbatim-verified evidence, and
  surfaces a cited research question with counter-evidence. It writes a
  review corpus and citation contexts, a **review audit** with bounded
  cross-paper conflict candidates (heuristic, explicitly not verified
  contradictions) and a missing-evidence ledger, and reports publication
  gaps when fewer than 20 papers are reviewed.
- **novelty** — generates candidate methods grounded in the reviewed
  evidence, checks them against prior art, critiques them adversarially, and
  freezes the selected methodology (versioned, checksummed) with a
  publication-design slice: contribution, novelty delta, datasets,
  baselines, ablations, robustness, and power.
- **experiment** — turns the frozen methodology into working research code
  (generated + statically verified), plans a falsifiable spec, and executes
  it only under an explicit, exact-checksum approval. It computes dataset
  checksum and baseline commit from the real target, reports
  publication-design gaps before execution, and ingests identity-checked
  measurements. Negative and null results are valid outcomes.
  During execution, an optional pre-registered **monitor contract** is
  enforced deterministically: generated code emits
  `AUTORESEARCH_PROBE:{"name": ..., "value": ...}` lines; NaN/Inf, bound
  breaches, persistent trend violations, stalls, and guard events terminate
  the run early. Suspect observations are retained as evidence. When a
  verified implementation artifact exists, the executed script must byte-match
  it or execution is refused.
- **recovery** — every failed approved execution (and measurement-ingestion
  failure) produces a checksummed `recovery_decision` (fast loop: retry /
  repair / parameter adjustment; slow loop: redesign / return to novelty /
  return to review / stop) with a rationale and evidence ids. The failure is
  also persisted to Findings Memory. Expensive follow-up work is recommended,
  never auto-executed without a fresh approval or explicit resume.
- **manuscript** — assembles the validated lineage into an editable,
  cited manuscript (Markdown + LaTeX + BibTeX + tables + manifest) behind a
  publication-readiness gate. It distinguishes a complete evidence chain
  from a submission-ready paper: provenance/simulation detection, a
  contribution contract, a sentence-level claim ledger, a full-corpus
  literature synthesis, a venue profile, and a deterministic reviewer panel.
  Every number traces to a measurement artifact; missing evidence produces
  an explicitly incomplete draft or a gated `draft`, never a fabricated
  submission.
- **workflow** — one durable loop over all four stages, reusing completed
  work and stopping where asked. At the end of every invocation it writes
  `research_state.json` (current question, novelty/methodology/experiment
  lineage, latest decision, pending recovery, conflicts, manuscript status)
  and exposes the exact path in the JSON result. `autoresearch skill state
  --project <id>` prints the same snapshot on demand.

Every analyzed outcome is persisted to a shared **Findings Memory**
(including failures and refutations) that conditions later runs, so the
system does not repeatedly rediscover unsuccessful approaches.

## Quality width (parallel, latency-neutral)

Quality scales by *width, not depth*: the four skills add parallel fan-out
where coverage is the ceiling — more tokens, same wall-clock, no new serial
hops. All knobs default ON and degrade gracefully under partial failure;
tune them under `[research]` in `.autoresearch/config.toml` (the boolean and
perspective knobs also accept `AUTORESEARCH_*` env equivalents; the
anchors/neighbors bounds are config-file only):

- `discovery_query_perspectives` — parallel complementary perspectives for
  search-strategy generation (topic/method, application/domain,
  limitation/counter-evidence); the union is deduplicated.
- `discovery_citation_expansion` + `_anchors`/`_neighbors` — deterministic,
  zero-token OpenAlex citation chaining: forward/backward neighbors of the
  top-ranked candidates join the candidate pool and are re-ranked with it.
- `review_ensemble` — dual-leg parallel extraction per paper (second leg
  sees reversed chunk order), merged with claim deduplication; a failing
  leg degrades to the survivor.
- Prior-art checking (novelty) now fans out across **all** sources with six
  complementary queries per candidate, including cross-domain probes
  grounded in the seed's topic and datasets.

## Install

```bash
pip install -e .
```

## Configure

Inside an agent session, the skills automatically use the session's own AI
model and API key (auto-detected from the OpenCode credential store) — no
config file required. `AUTORESEARCH_AUTO_PROVIDER=true` or `[research]
auto_provider = true` forces session-following even when a config file
exists.

Otherwise copy `.autoresearch/config.example.toml` to
`.autoresearch/config.toml` and point it at your provider. Secrets are never
inlined: name an environment variable (`api_key_env`), or rely on ambient
resolution (OpenCode credential store for recognized endpoints,
`ANTHROPIC_API_KEY` / connector settings for Anthropic-protocol providers).

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

# publication track: 20-40 reviewed papers, early publication gaps, venue gate
autoresearch skill workflow --seed-paper <pdf|doi|url> \
  --limit 40 --top-k 20 --publication --venue generic_journal --until manuscript
```

The publication track writes `publication_contract.json` and
`publication_gaps.json` after review, methodology freeze, and experiment
design. It stops before the next expensive stage when the review corpus,
novelty design, or experiment provenance cannot support a submission-grade
paper. Without `--publication`, the workflow still records the contract and
gaps but continues in exploratory mode.

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
- Runtime perception and recovery are deterministic; no LLM is called at
  stream rate, and a suspect observation never aborts a run by itself.
- Cross-paper conflicts are labelled candidates for expert verification, not
  asserted contradictions.

## Verify

```bash
pip install pytest anyio
pytest tests/ -q   # offline end-to-end smoke of the five-skill loop
```


## Where project state lives

Each research project stores its state **in its own directory** — the
directory you run the skills from when the project is created:

- The project's database, artifacts, and downloads live in
  `<project-dir>/.autoresearch/`.
- The project remembers that directory (exact database path recorded at
  creation, plus a machine-local registry at
  `~/.autoresearch/project-registry.json`).
- Resuming with `--project <id>` works **from any working directory** —
  the skills re-open the project's own database instead of starting a fresh
  one. Four projects in four folders stay four separate stores; nothing
  lands in the source checkout.

**Rule of thumb:** run `/workflow` (or the individual skills) from inside
the project's folder the first time; after that, resume from anywhere.

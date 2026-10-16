"""End-to-end offline verification of the completed result path:

    execution → measurement ingestion → statistical analysis → report → manuscript.

Engineering fixtures only: the worker script computes deterministic pseudo
measurements and (in the fixture-labelled variant) exercises the
fail-closed integrity gates. These tests verify orchestration and honesty
properties, NOT any scientific claim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pypdf import PdfWriter

from autoresearch.application.manuscript import ManuscriptService
from autoresearch.application.services import (
    DiscoveryService,
    ExperimentService,
    LiteratureReviewService,
    NoveltyService,
    ReinventionService,
)
from autoresearch.application.workflow import WorkflowService
from autoresearch.providers.budget import ProviderBudgetGuard
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.config import ModelAssignment
from autoresearch.providers.registry import ProviderRegistry
from autoresearch.providers.router import ModelRouter
from autoresearch.providers.testing import FakeEmbeddingProvider, FakeLLMProvider
from autoresearch.storage import ResearchRepository
from autoresearch.workers.local_typed import LocalTypedWorker

from test_workflow_coordination import DeterministicMetadataSource, DeterministicPdfResolver, _responder as _stock_responder

_WORKSPACE = str(Path.cwd())

# Test-controlled behavior for the implementation-generator responder branch.
_IMPLEMENT_QUALITY = {"mode": "good"}
# Test-controlled behavior for the model-assisted manuscript writer.
_WRITER_QUALITY = {"mode": "good"}


def _writer_responses(prompt: str) -> str:
    """Responder for the manuscript writer: 'good' copies the evidence pack's
    table/decision verbatim (a compliant model); 'bad' hallucinates numbers
    and overclaims, exercising the deterministic audit fallback."""
    import re as _re

    table = ""
    match = _re.search(r"RESULTS TABLE \(embed VERBATIM\):\n(.*?)\nDECISION", prompt, flags=_re.DOTALL)
    if match:
        table = match.group(1).strip()
    decision = "supported"
    decision_match = _re.search(r"DECISION \(use this word verbatim\): (\w+)", prompt)
    if decision_match:
        decision = decision_match.group(1)
    keys = _re.findall(r"PAPER \[([A-Za-z0-9_-]+)\]", prompt)
    citations = " ".join(f"[@{key}]" for key in keys)
    if _WRITER_QUALITY["mode"] == "bad":
        return (
            f"# A Groundbreaking Result\n\nOur method achieves a 99.7% improvement, which is "
            f"state-of-the-art and unprecedented. We observed 1234 samples across 56 datasets. "
            f"Citing [@paper999]. Decision: much better than expected.\n\n{table}"
        )
    return (
        f"# Evidence-Backed Evaluation\n\n## Abstract\n\nWe investigated the question using the frozen "
        f"methodology; the deterministic decision gates classified the hypothesis as {decision}.\n\n"
        f"## Results\n\n{table}\n\nThe decision is {decision}.\n\n## Related Work\n\n{citations}\n\n"
        f"## Limitations\n\nReported limitations from the analyzed experiment apply.\n"
    )


def _generated_script(prompt: str) -> str:
    """A responder 'model' that follows the implementation contract: copies the
    IDENTITY literal, iterates the declared seeds, names every required
    artifact type, and implements the methodology components as named functions."""
    identity_match = re.search(r"IDENTITY = (\{.*?\})\n", prompt, flags=re.DOTALL)
    identity = identity_match.group(1) if identity_match else "{}"
    seeds_match = re.search(r"Random seeds \(iterate ALL of these\): \[([0-9, ]+)\]", prompt)
    seeds = "[" + seeds_match.group(1) + "]" if seeds_match else "[42]"
    artifacts_match = re.search(r"\(types: \[(.*?)\]\)", prompt)
    artifact_types = artifacts_match.group(1) if artifacts_match else "'seed_metrics'"
    if _IMPLEMENT_QUALITY["mode"] == "broken":
        return "print('hello world')\n"
    return f'''
import json, pathlib, random

IDENTITY = {identity}

def encode_features(values):
    return [v for v in values]

def estimate_domain_statistics(values):
    return sum(values) / max(len(values), 1), (max(values) - min(values)) or 1e-6

def recalibrate_features(values):
    mean, spread = estimate_domain_statistics(values)
    return [(v - mean) / spread for v in values]

def decode_head(value):
    return min(max(value, 0.0), 1.0)

run_dir = pathlib.Path("run_outputs")
run_dir.mkdir(exist_ok=True)
seed_rows = []
for seed in {seeds}:
    rng = random.Random(seed)
    base_features = encode_features([rng.random() for _ in range(8)])
    baseline = decode_head(sum(base_features) / len(base_features) + 0.45)
    recalibrated = recalibrate_features(base_features)
    candidate = decode_head(sum(recalibrated) / len(recalibrated) + 0.45 + 0.03)
    seed_rows.append({{"seed": seed, "baseline": {{"accuracy": round(baseline, 6)}}, "candidate": {{"accuracy": round(candidate, 6)}}}})
artifact_rows = []
for artifact_type in [{artifact_types}]:
    path = run_dir / (artifact_type + ".txt")
    path.write_text("generated artifact: " + artifact_type + chr(10))
    artifact_rows.append({{"artifact_type": artifact_type, "path": str(path)}})
print(json.dumps({{"identity": IDENTITY, "seeds": seed_rows, "artifacts": artifact_rows}}))
'''


def _responder(messages):
    prompt = str(messages[-1]["content"])
    if "implementation-generator-v1" in prompt:
        return _generated_script(prompt)
    if "manuscript-writer-v2-model-outline" in prompt:
        return "1. Introduction: the question and why it matters. 2. Method. 3. Results with the provided table. 4. Discussion anchored on the decision. 5. Limitations."
    writer_prompt = next(
        (str(message.get("content")) for message in messages if "manuscript-writer-v2-model-draft" in str(message.get("content", ""))),
        None,
    )
    if writer_prompt is not None:
        return _writer_responses(writer_prompt)
    if "experiment-planner" in prompt:
        return {
            "hypothesis": "H1", "causal_mechanism": "C1", "falsification_condition": "F1", "repository": _WORKSPACE,
            "baseline_commit": "main", "dataset_identity": "data", "dataset_checksum": "checksum", "split_definition": "split",
            "baseline_configuration": "base", "proposed_change": "change", "independent_variables": [], "controlled_variables": [],
            "metrics": ["accuracy"], "random_seeds": [42, 43, 44], "required_seed_count": 3,
            "minimum_meaningful_effect": {"accuracy": 0.01}, "metric_direction": {"accuracy": "maximize"},
            "ablations": [], "robustness_tests": [], "statistical_plan": "exact Wilcoxon signed-rank across seeds",
            "compute_budget": "budget", "stopping_conditions": [], "expected_outputs": ["scripts/run_experiment.py"], "artifact_contract": "contract",
            "interpretation_rules": [
                "positive: accuracy gate passes",
                "negative: accuracy gate fails",
                "null: no difference",
                "failed: execution or artifact-validation failure",
            ],
        }
    return _stock_responder(messages)


def _measurement_script(lift: float, fixture: bool, tamper: bool = False) -> str:
    """A worker script that emits the measurement-document contract: computed
    per-seed values, all eight required artifact files, and identity fields.
    `fixture=True` labels the artifacts FIXTURE to exercise the fail-closed gate.
    `tamper=True` emits raw predictions/labels that contradict the claimed
    accuracy to exercise independent metric recomputation."""
    return f"""
import json, pathlib, random

run_dir = pathlib.Path("run_outputs")
run_dir.mkdir(exist_ok=True)
rng = random.Random(20261015)
seeds = []
for seed in (42, 43, 44):
    baseline = 0.50 + rng.random() * 0.02
    candidate = baseline + {lift!r} + rng.random() * 0.01
    row = {{"seed": seed, "baseline": {{"accuracy": round(baseline, 6)}}, "candidate": {{"accuracy": round(candidate, 6)}}}}
    if {tamper!r}:
        labels = [0, 1, 0, 1, 0, 1, 0, 1]
        candidate_predictions = [0, 1, 0, 1, 0, 1, 0, 0]
        row["raw_outputs"] = {{"labels": labels, "predictions": candidate_predictions}}
    seeds.append(row)
artifacts = []
for artifact_type in ("implementation_diff", "training_log", "checkpoint", "seed_metrics", "parameter_profile", "flop_profile", "latency_profile", "evaluation_report"):
    path = run_dir / (artifact_type + ".txt")
    path.write_text("engineering fixture artifact: " + artifact_type + chr(10))
    entry = {{"artifact_type": artifact_type, "path": str(path)}}
    if {fixture!r}:
        entry["provenance_type"] = "FIXTURE"
    artifacts.append(entry)
print(json.dumps({{"identity": {{"method_id": "fixture-method", "repository_commit": "none", "implementation_hash": "fixture-impl", "config_hash": "fixture-config", "metric_version": "accuracy-v1"}}, "seeds": seeds, "artifacts": artifacts}}))
"""


def _build_router(repository, workspace: str, provider):
    global _WORKSPACE
    _WORKSPACE = workspace
    registry = ProviderRegistry()
    registry.register_llm("fake", provider)
    registry.register_embeddings("fake_embeddings", FakeEmbeddingProvider())
    assignments = {
        ModelRole.LITERATURE_EXTRACTION: ModelAssignment(ModelRole.LITERATURE_EXTRACTION, "fake", "fake-model"),
        ModelRole.RESEARCH_REASONING: ModelAssignment(ModelRole.RESEARCH_REASONING, "fake", "fake-model"),
        ModelRole.SCIENTIFIC_CRITIC: ModelAssignment(ModelRole.SCIENTIFIC_CRITIC, "fake", "fake-model"),
        ModelRole.EMBEDDINGS: ModelAssignment(ModelRole.EMBEDDINGS, "fake_embeddings", "fake-embedding-v1"),
    }
    return ModelRouter(registry, assignments, usage_sink=repository.record_provider_usage, budget_guard=ProviderBudgetGuard(repository))


def _router(repository, workspace: str):
    return _build_router(repository, workspace, FakeLLMProvider(responder=_responder))


def _harness(tmp_path: Path, *, lift: float = 0.05, fixture: bool = False, write_script: bool = True, tamper: bool = False):
    from autoresearch.papers.search_pipeline import StudySearchPipeline

    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    repository.migrate()
    workspace = tmp_path / "workspace"
    (workspace / "scripts").mkdir(parents=True, exist_ok=True)
    if write_script:
        (workspace / "scripts" / "run_experiment.py").write_text(_measurement_script(lift, fixture, tamper))
    worker = LocalTypedWorker(repository, workspace)
    provider = FakeLLMProvider(responder=_responder)
    router = _build_router(repository, str(workspace), provider)
    pipeline = StudySearchPipeline(repository, [DeterministicMetadataSource()], DeterministicPdfResolver(), tmp_path / "downloads")
    workflow = WorkflowService(
        repository, router,
        discovery=DiscoveryService(repository, router, pipeline),
        review=LiteratureReviewService(repository, router),
        novelty=NoveltyService(repository, router, pipeline),
        experiment=ExperimentService(repository, router, workspace, worker=worker),
        reinvention=ReinventionService(repository, router),
        manuscript=ManuscriptService(repository, router),
    )
    seed = tmp_path / "seed.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=200, height=200); writer.add_metadata({"/Title": "Seed Adaptive Fusion"})
    with seed.open("wb") as handle:
        writer.write(handle)
    return repository, workflow, seed, provider


async def _run_to_manuscript(repository, workflow, seed):
    from uuid import uuid4

    done = await workflow.run(seed=str(seed), until="design")
    project = done["project_id"]
    candidate_id = next(s for s in done["stages"] if s["stage"] == "methodology")["candidate_id"]
    preview = workflow.experiment.approval_preview_for_candidate(candidate_id)
    saved = repository.save_exact_approval({
        "id": uuid4().hex, "run_id": preview["run_id"], "specification_checksum": str(preview["checksum"]),
        "approver": "test-user", "approved_scope": "workspace", "approved_target": str(preview["worker"]),
        "approved_budget": 60.0, "expires_at": "2099-01-01T00:00:00+00:00", "status": "pending",
    })
    repository.approve_exact(saved["id"], approver="test-user")
    result = await workflow.run(project_id=project, until="manuscript")
    return project, candidate_id, result


@pytest.mark.anyio
async def test_full_path_execution_analysis_and_manuscript(tmp_path):
    repository, workflow, seed, _provider = _harness(tmp_path, lift=0.05)
    project, _candidate_id, result = await _run_to_manuscript(repository, workflow, seed)
    stages = {s["stage"]: s for s in result["stages"]}
    assert stages["experiment"]["status"] == "completed", stages["experiment"]
    assert stages["manuscript"]["status"] == "complete", stages["manuscript"]

    analysis = repository.get_statistical_analysis(stages["experiment"]["spec_id"])
    assert analysis is not None, "execution must automatically produce the validated analysis"
    assert analysis.hypothesis_support_assessment == "SUPPORTED"

    record = repository.latest_manuscript(project)
    assert record is not None and record.status == "complete" and record.decision.lower() == "supported"
    markdown = Path(record.markdown_path).read_text(encoding="utf-8")
    assert "SUPPORTED" in markdown
    bundle = json.loads(Path(record.evidence_bundle_path).read_text(encoding="utf-8"))
    assert bundle["lineage"]["spec_id"] == stages["experiment"]["spec_id"]
    assert bundle["lineage"]["report_id"] == stages["experiment"]["report_id"]
    assert bundle["analysis"]["decision_gates"], "tables must trace to the deterministic analysis"
    bib = Path(record.bibtex_path).read_text(encoding="utf-8")
    assert "@misc{" in bib or "@article{" in bib
    manifest = json.loads(Path(record.manifest_path).read_text(encoding="utf-8"))
    assert manifest["files"], "manifest must checksum every emitted file"

    # Reuse: re-running to manuscript must not re-execute the approved worker job.
    jobs_before = repository.rows("SELECT COUNT(*) AS n FROM worker_jobs")[0]["n"]
    rerun = await workflow.run(project_id=project, until="manuscript")
    jobs_after = repository.rows("SELECT COUNT(*) AS n FROM worker_jobs")[0]["n"]
    assert jobs_before == jobs_after
    assert next(s for s in rerun["stages"] if s["stage"] == "manuscript")["status"] == "complete"


@pytest.mark.anyio
async def test_fixture_labeled_measurements_cannot_become_results(tmp_path):
    repository, workflow, seed, _provider = _harness(tmp_path, lift=0.05, fixture=True)
    _project, _candidate_id, result = await _run_to_manuscript(repository, workflow, seed)
    stages = {s["stage"]: s for s in result["stages"]}
    assert stages["experiment"]["status"] == "executed_analysis_pending", stages["experiment"]
    assert stages["manuscript"]["status"] == "blocked", stages["manuscript"]
    assert repository.get_statistical_analysis(stages["experiment"]["spec_id"]) is None, \
        "FIXTURE-labelled artifacts must never yield a statistical analysis"


@pytest.mark.anyio
async def test_manuscript_is_honestly_incomplete_without_evidence(tmp_path):
    repository, workflow, seed, _provider = _harness(tmp_path)
    done = await workflow.run(seed=str(seed), until="design")
    outcome = await workflow.manuscript.generate(done["project_id"])
    assert outcome["status"] == "incomplete"
    assert any("analysis" in gap for gap in outcome["evidence_gaps"]), outcome["evidence_gaps"]
    record = repository.latest_manuscript(done["project_id"])
    markdown = Path(record.markdown_path).read_text(encoding="utf-8")
    assert "INCOMPLETE" in markdown


@pytest.mark.anyio
async def test_null_result_is_a_valid_manuscript_outcome(tmp_path):
    repository, workflow, seed, _provider = _harness(tmp_path, lift=-0.05)  # candidate is worse: gates fail
    project, _candidate_id, result = await _run_to_manuscript(repository, workflow, seed)
    stages = {s["stage"]: s for s in result["stages"]}
    assert stages["experiment"]["status"] == "completed"
    assert stages["manuscript"]["status"] == "complete"
    record = repository.latest_manuscript(project)
    assert record.decision.lower() == "refuted"
    markdown = Path(record.markdown_path).read_text(encoding="utf-8")
    assert "REFUTED" in markdown and "valid scientific outcome" in markdown


@pytest.mark.anyio
async def test_implementation_is_generated_and_verified_when_script_missing(tmp_path):
    """The methodology→code gap closed: no user-supplied script; the workflow
    generates one through the router, the deterministic verifier passes it
    (mechanism coverage + measurement contract), and the GENERATED script then
    carries a real execution → analysis → manuscript path."""
    from uuid import uuid4

    repository, workflow, seed, _provider = _harness(tmp_path, write_script=False)
    first = await workflow.run(seed=str(seed), until="design")
    project = first["project_id"]
    candidate_id = next(s for s in first["stages"] if s["stage"] == "methodology")["candidate_id"]
    implementation = next(s for s in first["stages"] if s["stage"] == "implementation")
    assert implementation["status"] == "generated", implementation
    assert implementation["fidelity"] == "STATIC_VERIFIED"
    assert implementation["mechanism_coverage"] >= 0.75
    script = Path(str(implementation["script"]))
    assert script.is_file() and "IDENTITY" in script.read_text(encoding="utf-8")

    preview = workflow.experiment.approval_preview_for_candidate(candidate_id)
    saved = repository.save_exact_approval({
        "id": uuid4().hex, "run_id": preview["run_id"], "specification_checksum": str(preview["checksum"]),
        "approver": "test-user", "approved_scope": "workspace", "approved_target": str(preview["worker"]),
        "approved_budget": 60.0, "expires_at": "2099-01-01T00:00:00+00:00", "status": "pending",
    })
    repository.approve_exact(saved["id"], approver="test-user")
    result = await workflow.run(project_id=project, until="manuscript")
    # second pass reuses the verified implementation instead of regenerating
    reused = next(s for s in result["stages"] if s["stage"] == "implementation")
    assert reused["status"] == "reused", reused
    stages = {s["stage"]: s for s in result["stages"]}
    # The full scientific path ran on the GENERATED implementation
    assert stages["experiment"]["status"] == "completed", stages["experiment"]
    assert stages["manuscript"]["status"] == "complete", stages["manuscript"]
    analysis = repository.get_statistical_analysis(stages["experiment"]["spec_id"])
    assert analysis is not None and analysis.hypothesis_support_assessment in {"SUPPORTED", "REFUTED", "PARTIAL_SUPPORT"}
    # Verification artifact is persisted for audit
    verifications = repository.rows(
        "SELECT * FROM artifacts WHERE artifact_type='implementation_verification' AND run_id=?",
        (stages["experiment"]["spec_id"],),
    ) or repository.rows("SELECT * FROM artifacts WHERE artifact_type='implementation_verification'")
    assert verifications, "verification report must be persisted"
    # IMPLEMENTED_METHOD finding recorded with lineage
    statuses = {f.status.value for f in repository.findings_for_project(project)}
    assert "implemented_method" in statuses


@pytest.mark.anyio
async def test_findings_memory_records_outcomes_and_feeds_novelty(tmp_path):
    """The feedback loop: execution outcome → structured finding (exact
    lineage, observation separated from interpretation) → the NEXT novelty run
    retrieves it (including failures) in its generation context."""
    from autoresearch.domain.models import PipelineKind

    repository, workflow, seed, provider = _harness(tmp_path, lift=0.05)
    project, candidate_id, result = await _run_to_manuscript(repository, workflow, seed)
    stages = {s["stage"]: s for s in result["stages"]}
    assert stages["experiment"]["status"] == "completed"

    findings = repository.findings_for_project(project)
    assert findings, "the analyzed outcome must be persisted as a finding"
    outcome = [f for f in findings if f.status.value in {"confirmed_finding", "refuted_hypothesis", "inconclusive"}]
    assert outcome, [f.status.value for f in findings]
    top = outcome[0]
    assert top.spec_id == stages["experiment"]["spec_id"]
    assert top.analysis_id and top.question_id
    observed = json.loads(top.observed_outcome_json)
    assert "decision" in observed and "gates" in observed
    assert top.interpretation != top.lesson  # interpretation kept separate

    # The feedback loop: a NEW novelty run for the same question must include
    # the findings block (failures first) in its generation prompt.
    calls_before = len(provider.calls)
    portfolio_id = await workflow.novelty.generate_portfolio(project)
    assert portfolio_id
    prompted = [
        c for c in provider.calls[calls_before:]
        if any("Findings Memory" in str(m.get("content", "")) for m in c.get("messages", []))
    ]
    assert prompted, "the next novelty generation must be conditioned on Findings Memory"
    # and the next-action policy is inspectable
    actions = workflow.experiment.findings.next_actions(project)
    assert actions and all(a["priority"] == "heuristic" for a in actions)


@pytest.mark.anyio
async def test_model_writer_produces_audited_argument(tmp_path):
    """Model-assisted mode: the draft is a scientific argument (not field
    formatting), and the deterministic audit passes only because every number
    and citation traces to the evidence pack."""
    repository, workflow, seed, _provider = _harness(tmp_path, lift=0.05)
    workflow.manuscript.writer_mode = "model"
    project, _candidate_id, result = await _run_to_manuscript(repository, workflow, seed)
    stages = {s["stage"]: s for s in result["stages"]}
    assert stages["manuscript"]["status"] == "complete"
    record = repository.latest_manuscript(project)
    markdown = Path(record.markdown_path).read_text(encoding="utf-8")
    assert "deterministic decision gates" in markdown  # the model argument, not the template
    report = json.loads(Path(record.evidence_bundle_path).parent.joinpath("writing_audit.json").read_text())
    assert report["mode"] == "model" and report["used_model_draft"] is True
    assert report["audit"]["fallback"] is None
    # deterministic companion preserved for diffing/audit
    assert Path(record.evidence_bundle_path).parent.joinpath("manuscript_deterministic.md").is_file()


@pytest.mark.anyio
async def test_model_writer_hallucinations_fall_back_to_deterministic(tmp_path):
    """The audit is the authority: fabricated numbers, unknown citations, and
    overclaims surviving revision force the deterministic draft."""
    _WRITER_QUALITY["mode"] = "bad"
    try:
        repository, workflow, seed, _provider = _harness(tmp_path, lift=0.05)
        workflow.manuscript.writer_mode = "model"
        project, _candidate_id, result = await _run_to_manuscript(repository, workflow, seed)
        record = repository.latest_manuscript(project)
        report = json.loads(Path(record.evidence_bundle_path).parent.joinpath("writing_audit.json").read_text())
        assert report["used_model_draft"] is False
        assert report["audit"]["fallback"] == "deterministic"
        violations = report["audit"]["attempts"][-1]["violations"]
        assert violations["numbers"], "fabricated numbers must be flagged"
        assert violations["citations"], "unknown citation keys must be flagged"
        assert violations["overclaims"], "overclaim phrases must be flagged"
        markdown = Path(record.markdown_path).read_text(encoding="utf-8")
        assert "99.7" not in markdown and "deterministic decision gates" in markdown
    finally:
        _WRITER_QUALITY["mode"] = "good"


@pytest.mark.anyio
async def test_tampered_metrics_rejected_by_independent_recomputation(tmp_path):
    """A checksum proves file identity, not experiment legitimacy: when the
    implementation emits raw predictions/labels, claimed metrics are
    recomputed independently; a contradiction fails closed."""
    repository, workflow, seed, _provider = _harness(tmp_path, tamper=True)
    _project, _candidate_id, result = await _run_to_manuscript(repository, workflow, seed)
    stages = {s["stage"]: s for s in result["stages"]}
    assert stages["experiment"]["status"] == "executed_analysis_pending", stages["experiment"]
    assert repository.get_statistical_analysis(stages["experiment"]["spec_id"]) is None
    findings = repository.findings_for_project(_project)
    assert any(f.status.value == "implementation_failure" and "recomputation" in f.lesson.lower() for f in findings), \
        [f.lesson[:80] for f in findings]


@pytest.mark.anyio
async def test_unverifiable_generation_is_reported_honestly(tmp_path):
    """When generation cannot pass deterministic verification after the
    bounded repair loop, the failure is recorded as a finding — never masked
    by a silently degraded 'implementation'."""
    _IMPLEMENT_QUALITY["mode"] = "broken"
    try:
        repository, workflow, seed, _provider = _harness(tmp_path, write_script=False)
        done = await workflow.run(seed=str(seed), until="design")
        stages = {s["stage"]: s for s in done["stages"]}
        implementation = stages["implementation"]
        assert implementation["status"] == "failed", implementation
        reasons = implementation.get("error") or ""
        verification_rows = repository.rows("SELECT * FROM artifacts WHERE artifact_type='implementation_verification'")
        assert verification_rows, "failed verification must still be persisted"
        findings = repository.findings_for_project(done["project_id"])
        assert any(f.status.value == "implementation_failure" for f in findings), \
            [f.status.value for f in findings]
    finally:
        _IMPLEMENT_QUALITY["mode"] = "good"

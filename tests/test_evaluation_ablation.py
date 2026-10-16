"""Offline evaluation harness: mechanism ablations measured on the fixture
end-to-end path (engineering smoke — NOT evidence of research quality).

Measures per configuration:
- wall time for the full path (seed → … → manuscript),
- model calls (provider invocations, counted exactly via FakeLLMProvider),
- key mechanism dispositions (implementation verified, findings recorded,
  manuscript mode).

Variants (mechanism on/off):
A. baseline      — hand-supplied script (pre-implementation-generator world),
                   deterministic manuscript writer.
B. +generation   — no script; implementation generated + verified (this is the
                   new default path), deterministic writer.
C. +model writer — as B with manuscript writer_mode="model" (adds
                   outline/draft/revision calls; audit still enforced).

The written JSON report keeps raw measurements inspectable; assertions here
check structural sanity only (monotone call counts, statuses), never quality.
Run: .venv/bin/python -m pytest tests/test_evaluation_ablation.py -q
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pypdf import PdfWriter

from autoresearch.application.services import (
    DiscoveryService,
    ExperimentService,
    LiteratureReviewService,
    NoveltyService,
    ReinventionService,
)
from autoresearch.application.manuscript import ManuscriptService
from autoresearch.application.workflow import WorkflowService
from autoresearch.papers.search_pipeline import StudySearchPipeline
from autoresearch.providers.budget import ProviderBudgetGuard
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.config import ModelAssignment
from autoresearch.providers.registry import ProviderRegistry
from autoresearch.providers.router import ModelRouter
from autoresearch.providers.testing import FakeEmbeddingProvider, FakeLLMProvider
from autoresearch.storage import ResearchRepository
from autoresearch.workers.local_typed import LocalTypedWorker

from test_manuscript_path import _responder
from test_workflow_coordination import DeterministicMetadataSource, DeterministicPdfResolver


def _build(tmp_path: Path, *, write_script: bool, writer_mode: str):
    import test_manuscript_path

    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    repository.migrate()
    workspace = tmp_path / "workspace"
    (workspace / "scripts").mkdir(parents=True, exist_ok=True)
    test_manuscript_path._WORKSPACE = str(workspace.resolve())
    if write_script:
        from test_manuscript_path import _measurement_script
        (workspace / "scripts" / "run_experiment.py").write_text(_measurement_script(0.05, False))
    provider = FakeLLMProvider(responder=_responder)
    registry = ProviderRegistry()
    registry.register_llm("fake", provider)
    registry.register_embeddings("fake_embeddings", FakeEmbeddingProvider())
    assignments = {
        ModelRole.LITERATURE_EXTRACTION: ModelAssignment(ModelRole.LITERATURE_EXTRACTION, "fake", "fake-model"),
        ModelRole.RESEARCH_REASONING: ModelAssignment(ModelRole.RESEARCH_REASONING, "fake", "fake-model"),
        ModelRole.SCIENTIFIC_CRITIC: ModelAssignment(ModelRole.SCIENTIFIC_CRITIC, "fake", "fake-model"),
        ModelRole.EMBEDDINGS: ModelAssignment(ModelRole.EMBEDDINGS, "fake_embeddings", "fake-embedding-v1"),
    }
    router = ModelRouter(registry, assignments, usage_sink=repository.record_provider_usage, budget_guard=ProviderBudgetGuard(repository))
    pipeline = StudySearchPipeline(repository, [DeterministicMetadataSource()], DeterministicPdfResolver(), tmp_path / "downloads")
    manuscript = ManuscriptService(repository, router, writer_mode=writer_mode)
    workflow = WorkflowService(
        repository, router,
        discovery=DiscoveryService(repository, router, pipeline),
        review=LiteratureReviewService(repository, router),
        novelty=NoveltyService(repository, router, pipeline),
        experiment=ExperimentService(repository, router, workspace, worker=LocalTypedWorker(repository, workspace)),
        reinvention=ReinventionService(repository, router),
        manuscript=manuscript,
    )
    seed = tmp_path / "seed.pdf"
    writer = PdfWriter(); writer.add_blank_page(width=200, height=200); writer.add_metadata({"/Title": "Seed"})
    with seed.open("wb") as handle:
        writer.write(handle)
    return repository, workflow, seed, provider


async def _measure(tmp_path: Path, *, write_script: bool, writer_mode: str) -> dict:
    from uuid import uuid4

    repository, workflow, seed, provider = _build(tmp_path, write_script=write_script, writer_mode=writer_mode)
    started = time.perf_counter()
    first = await workflow.run(seed=str(seed), until="design")
    project = first["project_id"]
    candidate_id = next(s for s in first["stages"] if s["stage"] == "methodology")["candidate_id"]
    first_implementation = next((s for s in first["stages"] if s["stage"] == "implementation"), {})
    preview = workflow.experiment.approval_preview_for_candidate(candidate_id)
    saved = repository.save_exact_approval({
        "id": uuid4().hex, "run_id": preview["run_id"], "specification_checksum": str(preview["checksum"]),
        "approver": "eval", "approved_scope": "workspace", "approved_target": str(preview["worker"]),
        "approved_budget": 60.0, "expires_at": "2099-01-01T00:00:00+00:00", "status": "pending",
    })
    repository.approve_exact(saved["id"], approver="eval")
    result = await workflow.run(project_id=project, until="manuscript")
    elapsed = time.perf_counter() - started
    stages = {s["stage"]: s for s in result["stages"]}
    manuscript = repository.latest_manuscript(project)
    audit_path = Path(manuscript.evidence_bundle_path).parent / "writing_audit.json"
    return {
        "write_script": write_script,
        "writer_mode": writer_mode,
        "wall_seconds": round(elapsed, 3),
        "model_calls": len(provider.calls),
        "implementation": first_implementation.get("status"),
        "experiment": stages.get("experiment", {}).get("status"),
        "manuscript": stages.get("manuscript", {}).get("status"),
        "findings_total": len(repository.findings_for_project(project)),
        "writer_used_model": bool(audit_path.is_file() and json.loads(audit_path.read_text()).get("used_model_draft")),
    }


@pytest.mark.anyio
async def test_ablation_report(tmp_path):
    baseline = await _measure(tmp_path / "a", write_script=True, writer_mode="deterministic")
    generation = await _measure(tmp_path / "b", write_script=False, writer_mode="deterministic")
    model_writer = await _measure(tmp_path / "c", write_script=False, writer_mode="model")

    report = {
        "harness": "offline fixture e2e (engineering smoke; NOT research-quality evidence)",
        "variants": {"A_baseline": baseline, "B_plus_generation": generation, "C_plus_model_writer": model_writer},
        "deltas": {
            "generation_extra_calls_vs_baseline": generation["model_calls"] - baseline["model_calls"],
            "model_writer_extra_calls_vs_generation": model_writer["model_calls"] - generation["model_calls"],
        },
    }
    out = Path(__file__).parent.parent / ".autoresearch" / "evaluation" / "offline_ablation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Structural sanity only.
    for variant in (baseline, generation, model_writer):
        assert variant["experiment"] == "completed", variant
        assert variant["manuscript"] == "complete", variant
        assert variant["findings_total"] >= 1, "feedback loop must record outcomes"
    assert baseline["implementation"] == "reused"          # hand-supplied script
    assert generation["implementation"] == "generated"     # generated + verified
    assert generation["model_calls"] > baseline["model_calls"]          # generation costs bounded calls
    assert generation["model_calls"] - baseline["model_calls"] <= 3     # bounded repair loop
    assert model_writer["model_calls"] > generation["model_calls"]      # writer adds outline/draft(/revision)
    assert model_writer["writer_used_model"] is True

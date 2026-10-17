"""Workflow-loop resilience tests — the fixes for 'the workflow never finishes'.

Offline (fixture harness): stage-level transient retry, novelty second-sample
on honest zero-candidate outcome, stage-context notes on hard failures, and
orphaned-run retirement for novelty/manuscript.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoresearch.domain.models import PipelineKind, RunStatus
from autoresearch.providers.errors import ErrorKind, ProviderError
from test_smoke_e2e import _build_loop


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_stage_transient_error_retried_once(tmp_path):
    """A throttle on a stage's first attempt must not kill the loop."""
    repository, workflow, seed = _build_loop(tmp_path)
    real = workflow.novelty.generate_portfolio
    calls = {"count": 0}

    async def flaky_once(project, gap_id=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ProviderError("throttled", ErrorKind.RATE_LIMIT, "reasoning")
        return await real(project, gap_id=gap_id)

    workflow.novelty.generate_portfolio = flaky_once
    result = await workflow.run(seed=str(seed), until="hypotheses")
    hypotheses = next(s for s in result["stages"] if s["stage"] == "hypotheses")
    assert hypotheses["status"] == "completed"
    assert calls["count"] == 2


@pytest.mark.anyio
async def test_novelty_zero_candidates_gets_one_second_sample(tmp_path):
    """An honest 'no candidate passed the gates' outcome is model-sample
    dependent: one bounded regeneration is justified before reporting it."""
    repository, workflow, seed = _build_loop(tmp_path)
    real = workflow.novelty.generate_portfolio
    calls = {"count": 0}

    async def first_empty(project, gap_id=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("insufficient_evidence: novelty pipeline produced no candidate that passed the scientific gates.")
        return await real(project, gap_id=gap_id)

    workflow.novelty.generate_portfolio = first_empty
    result = await workflow.run(seed=str(seed), until="hypotheses")
    statuses = [s for s in result["stages"] if s["stage"] == "hypotheses"]
    assert statuses[0]["status"] == "retrying"
    assert statuses[-1]["status"] == "completed"
    assert calls["count"] == 2


@pytest.mark.anyio
async def test_hard_stage_failure_carries_stage_note(tmp_path):
    """A hard failure aborts with the ORIGINAL error (CLI exit codes stay
    meaningful) plus a stage-context note, and earlier stages stay reusable."""
    repository, workflow, seed = _build_loop(tmp_path)

    async def broken(project, gap_id=None):
        raise ValueError("boom")

    workflow.novelty.generate_portfolio = broken
    with pytest.raises(ValueError, match="boom") as exc:
        await workflow.run(seed=str(seed), until="hypotheses")
    notes = getattr(exc.value, "__notes__", [])
    assert any("workflow stage: hypotheses" in note for note in notes)


@pytest.mark.anyio
async def test_orphaned_novelty_run_retired_before_generation(tmp_path):
    repository, workflow, seed = _build_loop(tmp_path)
    done = await workflow.run(seed=str(seed), until="question")
    project = done["project_id"]
    stale = repository.create_run(project, PipelineKind.NOVELTY, {})
    assert repository.get_run(stale.id).status is RunStatus.RUNNING
    await workflow.run(project_id=project, until="hypotheses")
    assert repository.get_run(stale.id).status is RunStatus.CANCELLED


@pytest.mark.anyio
async def test_orphaned_manuscript_run_retired(tmp_path):
    repository, workflow, seed = _build_loop(tmp_path)
    first = await workflow.run(seed=str(seed), until="design")
    project = first["project_id"]
    candidate_id = next(s for s in first["stages"] if s["stage"] == "methodology")["candidate_id"]
    from uuid import uuid4

    preview = workflow.experiment.approval_preview_for_candidate(candidate_id)
    saved = repository.save_exact_approval({
        "id": uuid4().hex, "run_id": preview["run_id"], "specification_checksum": str(preview["checksum"]),
        "approver": "test", "approved_scope": "workspace", "approved_target": str(preview["worker"]),
        "approved_budget": 60.0, "expires_at": "2099-01-01T00:00:00+00:00", "status": "pending",
    })
    repository.approve_exact(saved["id"], approver="test")
    await workflow.run(project_id=project, until="experiment")
    stale = repository.create_run(project, PipelineKind.MANUSCRIPT, {})
    await workflow.run(project_id=project, until="manuscript")
    assert repository.get_run(stale.id).status is RunStatus.CANCELLED


@pytest.mark.anyio
async def test_planner_expands_seeds_to_required_count(tmp_path, monkeypatch):
    """Regression (live KAN stall): a spec demanding more seeds than it lists
    makes the analysis unreachable for any honest implementation. The planner
    must expand the seed list deterministically so the preregistration is
    self-consistent."""
    import test_smoke_e2e

    original = test_smoke_e2e._responder

    def wrapped(messages):
        prompt = str(messages[-1]["content"])
        if "experiment-planner" in prompt:
            payload = original(messages)
            payload["random_seeds"] = [42]
            payload["required_seed_count"] = 6
            return payload
        return original(messages)

    monkeypatch.setattr(test_smoke_e2e, "_responder", wrapped)
    repository, workflow, seed = _build_loop(tmp_path)
    result = await workflow.run(seed=str(seed), until="design")
    candidate_id = next(s for s in result["stages"] if s["stage"] == "methodology")["candidate_id"]
    spec = repository.get_experiment_spec(candidate_id)
    assert spec.required_seed_count == 6
    assert len(spec.random_seeds) == 6
    assert len(set(spec.random_seeds)) == 6

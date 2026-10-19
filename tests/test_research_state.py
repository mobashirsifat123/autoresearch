"""Consolidated research-state snapshot derived from durable rows."""

from __future__ import annotations

import json

import pytest

from autoresearch.application.research_state import build_research_state, write_research_state


@pytest.mark.anyio
async def test_snapshot_reports_the_exact_lineage_after_design(tmp_path):
    from test_smoke_e2e import _build_loop

    repository, workflow, seed = _build_loop(tmp_path)
    first = await workflow.run(seed=str(seed), until="design")
    project_id = first["project_id"]

    state = build_research_state(repository, project_id)
    assert state is not None
    assert state["state"] == "approval_pending"
    assert state["question"]["gap_id"]
    assert state["novelty"]["portfolio_id"]
    assert state["novelty"]["methodology_freeze"]["freeze_id"]
    assert state["experiment"]["spec_id"]
    assert state["experiment"]["job"] is None
    assert state["findings"]["count"] >= 0

    review_artifacts = [
        item for item in repository.artifacts_for_run(state["question"]["run_id"])
        if item.artifact_type == "review_audit"
    ]
    assert review_artifacts, "review must persist its audit artifact"
    audit = json.loads(open(review_artifacts[-1].path, encoding="utf-8").read())
    assert "cross_paper_conflicts" in audit
    assert "missing_evidence" in audit

    written = write_research_state(repository, project_id)
    assert written is not None
    path = written["artifact_path"]
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["state"] == "approval_pending"
    assert payload["experiment"]["spec_id"] == state["experiment"]["spec_id"]
    assert first["research_state"] is not None


@pytest.mark.anyio
async def test_completed_report_writes_recovery_and_snapshot(tmp_path):
    from test_smoke_e2e import _build_loop
    from uuid import uuid4

    repository, workflow, seed = _build_loop(tmp_path)
    first = await workflow.run(seed=str(seed), until="design")
    candidate_id = next(s for s in first["stages"] if s["stage"] == "methodology")["candidate_id"]
    preview = workflow.experiment.approval_preview_for_candidate(candidate_id)
    saved = repository.save_exact_approval({
        "id": uuid4().hex, "run_id": preview["run_id"],
        "specification_checksum": str(preview["checksum"]),
        "approver": "state-test", "approved_scope": "workspace",
        "approved_target": str(preview["worker"]), "approved_budget": 60.0,
        "expires_at": "2099-01-01T00:00:00+00:00", "status": "pending",
    })
    repository.approve_exact(saved["id"], approver="state-test")
    result = await workflow.run(project_id=first["project_id"], until="manuscript")
    stages = {item["stage"]: item for item in result["stages"]}
    assert stages["experiment"]["status"] == "completed"
    expected = {
        "supported": "advance_manuscript",
        "partially_supported": "advance_manuscript",
        "refuted": "revise_novelty",
        "insufficient_evidence": "redesign_experiment",
    }[stages["experiment"]["decision"]]
    assert stages["experiment"]["recovery_action"] == expected
    state = build_research_state(repository, first["project_id"])
    assert state is not None
    assert state["recovery"]["action"] == expected
    assert state["state"] in {"complete", "draft_gate_failed", "recovery_required"}
    assert state["findings"]["count"] >= 1


@pytest.mark.anyio
async def test_snapshot_advances_after_a_recovery_decision(tmp_path):
    from test_smoke_e2e import _build_loop

    repository, workflow, seed = _build_loop(tmp_path)
    first = await workflow.run(seed=str(seed), until="design")
    project_id = first["project_id"]
    state = build_research_state(repository, project_id)
    spec_id = state["experiment"]["spec_id"]

    from autoresearch.application.recovery import classify_report_decision, write_recovery_decision

    decision = classify_report_decision(
        project_id=project_id,
        run_id=state["experiment"]["run_id"],
        spec_id=spec_id,
        decision="refuted",
        report_id="report",
        analysis_id="analysis",
    )
    write_recovery_decision(repository, decision)
    refreshed = build_research_state(repository, project_id)
    assert refreshed is not None
    assert refreshed["recovery"]["action"] == "revise_novelty"
    assert refreshed["recovery"]["next_stage"] == "novelty"
    assert any("refuted" in item for item in refreshed["open_questions"])

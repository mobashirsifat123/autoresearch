"""Deterministic fast-loop / slow-loop recovery policy."""

from __future__ import annotations

from autoresearch.application.recovery import (
    classify_execution_failure,
    classify_report_decision,
    latest_recovery_decision,
    write_recovery_decision,
)
from autoresearch.domain.models import PipelineKind
from autoresearch.storage.repository import ResearchRepository


def _ids():
    return {"project_id": "p", "run_id": "r", "spec_id": "s"}


def test_operational_failures_are_fast_loop_and_do_not_revise_the_hypothesis():
    perception = classify_execution_failure(
        **_ids(), state="FAILED", error_category="PERCEPTION_ABORT",
        error_reason="loss=nan", attempt=1,
    )
    assert perception.loop == "fast"
    assert perception.action == "repair_implementation"
    assert perception.next_stage == "experiment"
    assert "not as evidence against" in perception.rationale

    timeout_first = classify_execution_failure(
        **_ids(), state="FAILED", error_category="TIMEOUT", error_reason="exceeded 60s", attempt=0,
    )
    assert timeout_first.action == "retry_same_spec"
    timeout_second = classify_execution_failure(
        **_ids(), state="FAILED", error_category="TIMEOUT", error_reason="exceeded 60s", attempt=2,
    )
    assert timeout_second.action == "adjust_parameters"

    cancelled = classify_execution_failure(**_ids(), state="CANCELED", attempt=1)
    assert cancelled.action == "stop"
    assert cancelled.next_stage == "none"


def test_integrity_failure_is_a_repair_not_a_retry():
    decision = classify_execution_failure(
        **_ids(), state="FAILED", error_category="WORKER_EXCEPTION",
        error_reason="implementation_integrity: script changed after verification", attempt=1,
    )
    assert decision.action == "repair_implementation"
    assert "Do not ingest edited bytes" in decision.rationale


def test_scientific_decisions_map_to_the_slow_loop():
    refuted = classify_report_decision(**_ids(), decision="refuted", report_id="rep")
    assert refuted.loop == "slow"
    assert refuted.action == "revise_novelty"
    assert refuted.next_stage == "novelty"

    null = classify_report_decision(**_ids(), decision="no_effect", report_id="rep")
    assert null.action == "redesign_experiment"
    assert null.next_stage == "experiment"

    insufficient = classify_report_decision(**_ids(), decision="insufficient_evidence", report_id="")
    assert insufficient.action == "redesign_experiment"
    assert insufficient.loop == "slow"

    data = classify_report_decision(**_ids(), decision="data_limitation", report_id="rep")
    assert data.action == "request_more_evidence"
    assert data.next_stage == "review"

    supported = classify_report_decision(**_ids(), decision="supported", report_id="rep")
    assert supported.action == "advance_manuscript"
    assert supported.requires_approval is False

    cost = classify_report_decision(**_ids(), decision="cost_prohibitive", report_id="rep")
    assert cost.action == "stop"


def test_artifact_failure_is_fast_maintenance_not_a_scientific_claim():
    decision = classify_report_decision(**_ids(), decision="implementation_failure", report_id="rep")
    assert decision.loop == "fast"
    assert decision.action == "repair_implementation"
    assert decision.next_stage == "experiment"


def test_cancellation_is_terminal_even_after_late_bookkeeping(tmp_path):
    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    project = repository.create_project("cancel")
    run = repository.create_run(project.id, PipelineKind.EXPERIMENT, {})
    repository.cancel_run(run.id)
    repository.finish_run(run.id, error="worker finished later")
    current = repository.get_run(run.id)
    assert current is not None
    assert current.status.value == "cancelled"


def test_reapproval_rebinds_a_regenerated_script_digest(tmp_path):
    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    project = repository.create_project("approval rebind")
    run = repository.create_run(project.id, PipelineKind.EXPERIMENT, {})
    base = {
        "id": "approval-1", "run_id": run.id, "specification_checksum": "same-spec",
        "approver": "test", "approved_scope": "workspace", "approved_target": "local",
        "approved_budget": 10.0, "expires_at": "2099-01-01T00:00:00+00:00",
        "status": "pending",
    }
    repository.save_exact_approval({**base, "approved_actions": {
        "actions": [], "implementation_checksum": "digest-a", "implementation_verified": True,
    }})
    updated = repository.save_exact_approval({**base, "id": "ignored", "approved_actions": {
        "actions": [], "implementation_checksum": "digest-b", "implementation_verified": True,
    }})
    assert updated["id"] == "approval-1"
    assert updated["approved_actions"]["implementation_checksum"] == "digest-b"
    assert updated["status"] == "pending"


def test_decision_is_persisted_and_read_back(tmp_path):
    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    project = repository.create_project("recovery")
    run = repository.create_run(project.id, PipelineKind.EXPERIMENT, {})
    decision = classify_report_decision(
        project_id=project.id, run_id=run.id, spec_id="spec",
        decision="refuted", report_id="report", analysis_id="analysis",
    )
    payload = write_recovery_decision(repository, decision)
    assert payload["action"] == "revise_novelty"
    loaded = latest_recovery_decision(repository, project_id=project.id, run_id=run.id)
    assert loaded is not None
    assert loaded["action"] == "revise_novelty"
    assert loaded["artifact_path"].endswith(".json")
    events = repository.api_events(run.id)
    assert any(event["event_type"] == "research.recovery_decision" for event in events)

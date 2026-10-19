"""End-to-end perception → recovery → Findings Memory integration.

The offline loop is driven to design, then the generated implementation is
replaced by an instrumented script that emits a fatal NaN probe. The exact
approval is still valid (the spec did not change), but the new script is
registered as the verified implementation. The workflow must stop the run
early, persist a recovery decision, and record an operational failure finding.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from autoresearch.domain.models import ArtifactReference, content_checksum


@pytest.mark.anyio
async def test_nan_probe_stops_run_and_workflow_records_recovery(tmp_path):
    from test_smoke_e2e import _build_loop

    repository, workflow, seed = _build_loop(tmp_path)
    first = await workflow.run(seed=str(seed), until="design")
    candidate_id = next(s for s in first["stages"] if s["stage"] == "methodology")["candidate_id"]
    spec = repository.get_experiment_spec(candidate_id)
    assert spec is not None
    script = Path(spec.repository) / "scripts" / "run_experiment.py"
    assert script.is_file()

    original = script.read_text(encoding="utf-8")
    instrumented = (
        "import json, time\n"
        "print('AUTORESEARCH_PROBE:' + json.dumps({'name': 'loss', 'value': float('nan'), 'step': 0}), flush=True)\n"
        "time.sleep(20)\n"
    ) + original
    script.write_text(instrumented, encoding="utf-8")
    # Fault-injection harness: simulate a regenerated, re-verified instrumented
    # implementation. The test's target is runtime perception at the worker
    # boundary, not the verifier (which has its own tests).
    verification = tmp_path / "instrumented-verification.json"
    verification.write_text(json.dumps({
        "spec_id": spec.id, "status": "generated", "level": "STATIC_VERIFIED",
        "reasons": [],
    }), encoding="utf-8")
    repository.save_artifact(ArtifactReference(
        uuid4().hex, project_id=spec.project_id, run_id=spec.run_id,
        artifact_type="experiment_implementation", path=str(script),
        source_evidence=(spec.id,), provider="deterministic", model="test",
        prompt_version="test", checksum=content_checksum(instrumented),
    ))
    repository.save_artifact(ArtifactReference(
        uuid4().hex, project_id=spec.project_id, run_id=spec.run_id,
        artifact_type="implementation_verification", path=str(verification),
        source_evidence=(spec.id,), provider="deterministic", model="test",
        prompt_version="test", checksum=content_checksum(verification.read_text()),
    ))

    preview = workflow.experiment.approval_preview_for_candidate(candidate_id)
    saved = repository.save_exact_approval({
        "id": uuid4().hex, "run_id": preview["run_id"],
        "specification_checksum": str(preview["checksum"]),
        "approver": "perception-test", "approved_scope": "workspace",
        "approved_target": str(preview["worker"]), "approved_budget": 20.0,
        "expires_at": "2099-01-01T00:00:00+00:00", "status": "pending",
    })
    repository.approve_exact(saved["id"], approver="perception-test")

    started = time.monotonic()
    result = await workflow.run(project_id=first["project_id"], until="experiment")
    elapsed = time.monotonic() - started
    stages = {item["stage"]: item for item in result["stages"]}

    assert elapsed < 12, "the fatal probe must stop the run, not wait for the 20s timeout"
    assert stages["experiment"]["status"] == "execution_failed"
    assert stages["experiment"]["recovery_action"] == "repair_implementation"
    assert stages["experiment"]["recovery_next_stage"] == "experiment"
    assert result["research_state"] == "recovery_required"
    assert result["pending_recovery"]["action"] == "repair_implementation"

    failures = [
        finding for finding in repository.findings_for_project(first["project_id"])
        if finding.failure_classification == "operational_execution_failure"
    ]
    assert failures, "an operational failure must be retained in Findings Memory"
    assert "PERCEPTION_ABORT" in failures[0].lesson
    assert failures[0].spec_id == spec.id

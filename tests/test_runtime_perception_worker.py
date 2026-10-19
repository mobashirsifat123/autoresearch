"""Runtime perception at the execution boundary.

These tests execute real local subprocesses (no model calls, no network) and
prove: a fatal probe kills the run before its natural end; a healthy streamed
run still completes; timeouts are terminal failures, not cancellations; the
executed script must match the statically verified implementation.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from autoresearch.domain.models import ArtifactProvenance, ArtifactReference, CandidateStatus, ExperimentSpec, NoveltyCandidate, PipelineKind, content_checksum
from autoresearch.experiments.policy import ActionType, ExecutionAction, specification_checksum
from autoresearch.storage.repository import ResearchRepository
from autoresearch.workers.local_typed import LocalTypedWorker


def _setup(tmp_path: Path, script: str, *, monitor_contract: dict | None = None):
    workspace = tmp_path / "workspace"
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "scripts" / "run_experiment.py").write_text(script, encoding="utf-8")
    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    project = repository.create_project("runtime perception")
    run = repository.create_run(project.id, PipelineKind.EXPERIMENT, {})
    candidate = NoveltyCandidate(
        id=uuid4().hex, project_id=project.id, run_id=run.id, gap_id="g",
        strategy="test", method="m", causal_mechanism="cm", target_limitation="tl",
        difference_from_existing="d", expected_effect="e", falsification_condition="f",
        minimum_validating_experiment="mv", required_ablations=(),
        implementation_complexity="low", expected_compute="low", risks_and_objections=(),
        status=CandidateStatus.GENERATED,
        provenance=ArtifactProvenance.create(
            project_id=project.id, run_id=run.id, source_evidence=(),
            provider="deterministic", model="test", prompt_version="test", content="c",
        ),
    )
    repository.save_novelty_candidate(candidate)
    spec = ExperimentSpec(
        id=uuid4().hex, project_id=project.id, run_id=run.id, candidate_id=candidate.id,
        hypothesis="h", causal_mechanism="cm", falsification_condition="f",
        repository=str(workspace.resolve()), baseline_commit="none",
        dataset_identity="synthetic", dataset_checksum="d", split_definition="s",
        baseline_configuration="b", proposed_change="p",
        independent_variables=(), controlled_variables=(), metrics=("loss",),
        random_seeds=(1,), ablations=(), robustness_tests=(),
        statistical_plan="paired", compute_budget="trivial",
        stopping_conditions=(), expected_outputs=("scripts/run_experiment.py",), artifact_contract="a",
        provenance=ArtifactProvenance.create(
            project_id=project.id, run_id=run.id, source_evidence=(),
            provider="deterministic", model="test", prompt_version="test", content="x",
        ),
        monitor_contract=monitor_contract or {},
    )
    repository.save_experiment_spec(spec)
    action = ExecutionAction(ActionType.RUN_PYTHON_SCRIPT, "scripts/run_experiment.py", ())
    approved = specification_checksum(spec, (action,), "local")
    return repository, workspace, spec, action, approved


def test_monitor_contract_round_trips_through_storage(tmp_path):
    from dataclasses import replace

    script = "print('{}')\n"
    contract = {"stall_seconds": 60, "monitors": [{"name": "loss", "healthy": {"max": 5}, "why": "divergence"}]}
    repository, _, spec, _, _ = _setup(tmp_path, script, monitor_contract=contract)
    loaded = repository.get_experiment_spec(spec.candidate_id)
    assert loaded is not None
    assert loaded.monitor_contract == contract

    # A declared artifact subset must survive a database round-trip; an
    # omitted/empty contract keeps the full standard set.
    declared = replace(spec, required_artifacts=("seed_metrics", "training_log"))
    repository.save_experiment_spec(declared)
    reloaded = repository.get_experiment_spec(spec.candidate_id)
    assert reloaded is not None
    assert reloaded.required_artifacts == ("seed_metrics", "training_log")


def test_nan_probe_aborts_before_the_script_finishes(tmp_path):
    # Without perception this would sleep for 30s. The fatal NaN must kill it.
    script = (
        "import json, time\n"
        "print('AUTORESEARCH_PROBE:' + json.dumps({'name': 'loss', 'value': 1.0, 'step': 0}), flush=True)\n"
        "print('AUTORESEARCH_PROBE:' + json.dumps({'name': 'loss', 'value': float('nan'), 'step': 1}), flush=True)\n"
        "time.sleep(30)\n"
        "print(json.dumps({'seeds': []}))\n"
    )
    repository, workspace, spec, action, approved = _setup(
        tmp_path, script,
        monitor_contract={"monitors": [{"name": "loss", "healthy": {"trend": "decreasing"}, "why": "divergence"}]},
    )
    worker = LocalTypedWorker(repository, workspace)
    started = time.monotonic()
    result = asyncio.run(worker.run(spec, (action,), approved_checksum=approved, timeout_seconds=20))
    elapsed = time.monotonic() - started

    assert elapsed < 10, "the fatal sample must terminate the process, not wait for the timeout"
    assert result["state"] == "FAILED"
    assert result["perception"]["category"] == "PERCEPTION_ABORT"
    assert "NaN" in result["perception"]["reason"] or "nan" in result["perception"]["reason"].lower()
    assert result["perception"]["fatal_verdicts"] == 1
    report_path = Path(str(result["monitor_report"]))
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["aborted"] is True
    assert report["first_fatal"]["signal"] == "loss"
    job = repository.worker_jobs_for_run(spec.run_id)[0]
    assert job["state"] == "FAILED"
    assert job["error"]["category"] == "PERCEPTION_ABORT"
    assert job["process_id"] is not None


def test_healthy_probe_run_completes_and_records_monitor_evidence(tmp_path):
    script = (
        "import json\n"
        "for step, value in enumerate([1.0, 0.7, 0.4, 0.2]):\n"
        "    print('AUTORESEARCH_PROBE:' + json.dumps({'name': 'loss', 'value': value, 'step': step}), flush=True)\n"
        "print(json.dumps({'identity': {}, 'seeds': [{'seed': 1, 'baseline': {'loss': 1.0}, 'candidate': {'loss': 0.5}}], 'artifacts': []}))\n"
    )
    repository, workspace, spec, action, approved = _setup(
        tmp_path, script,
        monitor_contract={"monitors": [{"name": "loss", "healthy": {"trend": "decreasing"}, "why": "divergence"}]},
    )
    result = asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved))
    assert result["state"] == "COMPLETED"
    assert result["perception"]["fatal_verdicts"] == 0
    assert Path(str(result["monitor_report"])).is_file()
    assert '"seeds"' in str(result["stdout"])

    from autoresearch.application.research_state import build_research_state

    state = build_research_state(repository, spec.project_id)
    assert state is not None
    assert state["state"] == "analysis_pending"


def test_timeout_is_a_terminal_failure_not_a_cancellation(tmp_path):
    repository, workspace, spec, action, approved = _setup(tmp_path, "import time\ntime.sleep(10)\n")
    result = asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved, timeout_seconds=1))
    assert result["state"] == "FAILED"
    assert result["perception"]["category"] == "TIMEOUT"
    job = repository.worker_jobs_for_run(spec.run_id)[0]
    assert job["state"] == "FAILED"
    assert job["error"]["category"] == "TIMEOUT"


def test_static_verifier_requires_declared_probes(tmp_path):
    from autoresearch.experiments.implementation import ImplementationVerifier, identity_for_spec

    repository, workspace, spec, _, _ = _setup(
        tmp_path, "print('x')\n",
        monitor_contract={"monitors": [{"name": "loss", "healthy": {"max": 5}, "why": "bound"}]},
    )
    verifier = ImplementationVerifier()
    identity = identity_for_spec(spec, None)
    without_probe = verifier.verify("print('nothing')\n", spec, {}, identity)
    assert without_probe.checks["monitor_contract_probed"] is False
    assert any("MONITOR_CONTRACT_NOT_PROBED" in reason for reason in without_probe.reasons)

    with_probe = (
        "import json\n"
        "print('AUTORESEARCH_PROBE:' + json.dumps({'name': 'loss', 'value': 1.0}), flush=True)\n"
    )
    verified = verifier.verify(with_probe, spec, {}, identity)
    assert verified.checks["monitor_contract_probed"] is True


def test_failed_generation_cannot_claim_the_verified_artifact_type(tmp_path):
    from autoresearch.experiments.implementation import ImplementationGenerator, VerificationReport

    repository, workspace, spec, _, _ = _setup(tmp_path, "print('failed verification')\n")
    generator = ImplementationGenerator(repository, None)
    script = workspace / "scripts" / "run_experiment.py"
    generator._record(
        spec, script, VerificationReport(level="STATIC_FAILED", reasons=["IDENTITY_BLOCK_MISSING"]),
        attempts=3, status="failed",
    )
    artifact_types = {item.artifact_type for item in repository.artifacts_for_run(spec.run_id)}
    assert "experiment_implementation_unverified" in artifact_types
    assert "experiment_implementation" not in artifact_types


def test_failed_static_verification_is_refused(tmp_path):
    repository, workspace, spec, action, approved = _setup(tmp_path, "print('not verified')\n")
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps({
        "spec_id": spec.id, "status": "failed", "level": "STATIC_FAILED",
        "reasons": ["IDENTITY_BLOCK_MISSING"],
    }), encoding="utf-8")
    repository.save_artifact(ArtifactReference(
        uuid4().hex, project_id=spec.project_id, run_id=spec.run_id,
        artifact_type="implementation_verification", path=str(verification),
        source_evidence=(spec.id,), provider="deterministic", model="test",
        prompt_version="test", checksum=content_checksum(verification.read_text()),
    ))
    with pytest.raises(RuntimeError, match="implementation_unverified"):
        asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved))


def test_reused_but_unverified_script_is_refused(tmp_path):
    repository, workspace, spec, action, approved = _setup(tmp_path, "print('reused but unverified')\n")
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps({
        "spec_id": spec.id, "status": "reused", "level": "STATIC_FAILED",
        "reasons": ["IDENTITY_BLOCK_MISSING"],
    }), encoding="utf-8")
    repository.save_artifact(ArtifactReference(
        uuid4().hex, project_id=spec.project_id, run_id=spec.run_id,
        artifact_type="implementation_verification", path=str(verification),
        source_evidence=(spec.id,), provider="deterministic", model="test",
        prompt_version="test", checksum=content_checksum(verification.read_text()),
    ))
    with pytest.raises(RuntimeError, match="implementation_unverified"):
        asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved))


def test_cancel_during_monitor_write_wins_the_terminal_state(tmp_path, monkeypatch):
    script = "print('{}')\n"
    repository, workspace, spec, action, approved = _setup(tmp_path, script)
    original = repository.save_artifact

    def cancel_on_monitor(artifact):
        if artifact.artifact_type == "execution_monitor":
            repository.execute(
                "UPDATE worker_jobs SET state='CANCELED' WHERE id=?",
                (artifact.source_evidence[1],),
            )
        return original(artifact)

    monkeypatch.setattr(repository, "save_artifact", cancel_on_monitor)
    result = asyncio.run(LocalTypedWorker(repository, workspace).run(
        spec, (action,), approved_checksum=approved
    ))
    assert result["state"] == "CANCELED"
    job = repository.worker_jobs_for_run(spec.run_id)[0]
    assert job["state"] == "CANCELED"


def test_heartbeat_does_not_resurrect_a_cancelled_job(tmp_path):
    repository, workspace, spec, action, approved = _setup(tmp_path, "print('x')\n")
    worker = LocalTypedWorker(repository, workspace)
    submission = worker.submit(spec, (action,), approved_checksum=approved)
    repository.execute("UPDATE worker_jobs SET state='CANCELED' WHERE id=?", (submission.job_id,))
    asyncio.run(worker.heartbeat(submission.job_id))
    job = repository.worker_job(submission.job_id)
    assert job is not None
    assert job["state"] == "CANCELED"


def test_approval_binding_rejects_a_changed_script(tmp_path):
    original = "print('approved bytes')\n"
    repository, workspace, spec, action, approved = _setup(tmp_path, original)
    saved = repository.save_exact_approval({
        "id": uuid4().hex, "run_id": spec.run_id, "specification_checksum": approved,
        "approver": "test", "approved_scope": "workspace", "approved_target": "local",
        "approved_budget": 10.0, "expires_at": "2099-01-01T00:00:00+00:00",
        "status": "pending",
        "approved_actions": {
            "actions": [], "implementation_checksum": content_checksum(original),
            "implementation_verified": True, "monitor_contract": {},
        },
    })
    repository.approve_exact(saved["id"], approver="test")
    script = workspace / "scripts" / "run_experiment.py"
    changed = "print('changed after approval')\n"
    script.write_text(changed, encoding="utf-8")
    with pytest.raises(RuntimeError, match="script changed after approval"):
        asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved))


def test_repeated_timeouts_increment_the_attempt_budget(tmp_path):
    repository, workspace, spec, action, approved = _setup(tmp_path, "import time\ntime.sleep(10)\n")
    first = asyncio.run(LocalTypedWorker(repository, workspace).run(
        spec, (action,), approved_checksum=approved, timeout_seconds=1
    ))
    second = asyncio.run(LocalTypedWorker(repository, workspace).run(
        spec, (action,), approved_checksum=approved, timeout_seconds=1
    ))
    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert first["perception"]["category"] == "TIMEOUT"
    assert second["perception"]["category"] == "TIMEOUT"


def test_tampered_stored_result_is_rejected_even_with_a_recomputed_checksum(tmp_path):
    from autoresearch.domain.models import content_checksum

    script = (
        "import json\n"
        "print(json.dumps({'seeds': [{'seed': 1, 'baseline': {'m': 1}, 'candidate': {'m': 2}}]}), flush=True)\n"
    )
    repository, workspace, spec, action, approved = _setup(tmp_path, script)
    first = asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved))
    assert first["state"] == "COMPLETED"
    path = workspace / "run_outputs" / f"worker_result_{first['job_id']}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stdout"] = json.dumps({"seeds": [{"seed": 1, "baseline": {"m": 99}, "candidate": {"m": 99}}]})
    body = {key: value for key, value in payload.items() if key != "result_checksum"}
    payload["result_checksum"] = content_checksum(body)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="artifact ledger"):
        asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved))


def test_a_long_single_line_does_not_crash_the_stream_reader(tmp_path):
    script = (
        "import json\n"
        "print('x' * 100000, flush=True)\n"
        "print(json.dumps({'seeds': [{'seed': 1, 'baseline': {'m': 1}, 'candidate': {'m': 2}}]}), flush=True)\n"
    )
    repository, workspace, spec, action, approved = _setup(tmp_path, script)
    result = asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved))
    assert result["state"] == "COMPLETED"
    assert '"seeds"' in str(result["stdout"])


def test_output_over_the_cap_preserves_a_large_tail_measurement(tmp_path):
    script = (
        "import json\n"
        "for i in range(20000):\n"
        "    print('progress-line-%06d-' % i + 'y' * 40, flush=True)\n"
        "payload = {'seeds': [{'seed': 1, 'baseline': {'m': 1}, 'candidate': {'m': 2}}],\n"
        "           'padding': 'z' * 300000}\n"
        "print(json.dumps(payload), flush=True)\n"
    )
    repository, workspace, spec, action, approved = _setup(tmp_path, script)
    result = asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved))
    assert result["state"] == "COMPLETED"
    assert result["output_truncated"] is True
    assert '"seeds"' in str(result["stdout"])
    assert "z" * 1000 in str(result["stdout"])


def test_edited_script_is_rejected_before_execution(tmp_path):
    original = "print('original')\n"
    repository, workspace, spec, action, approved = _setup(tmp_path, original)
    script = workspace / "scripts" / "run_experiment.py"
    repository.save_artifact(ArtifactReference(
        uuid4().hex, project_id=spec.project_id, run_id=spec.run_id,
        artifact_type="experiment_implementation", path=str(script),
        source_evidence=(spec.id,), provider="deterministic", model="test",
        prompt_version="test", checksum=content_checksum(original),
    ))
    script.write_text("print('edited after approval')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="implementation_integrity"):
        asyncio.run(LocalTypedWorker(repository, workspace).run(spec, (action,), approved_checksum=approved))
    job = repository.worker_jobs_for_run(spec.run_id)[0]
    assert job["state"] == "FAILED"
    assert job["error"]["category"] in {"WORKER_EXCEPTION", "UNKNOWN"}

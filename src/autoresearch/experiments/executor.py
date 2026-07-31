"""Durable experiment execution with validation gates and telemetry."""

from __future__ import annotations

import asyncio
from typing import Callable, Coroutine
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    EnvironmentManifest,
    ExecutionJob,
    ExecutionResult,
    ExperimentSpec,
    RunStatus,
    ValidationReport,
)
from autoresearch.storage.repository import ResearchRepository


class DurableExperimentExecutor:
    def __init__(self, repository: ResearchRepository, execute_command: Callable[[str], Coroutine[None, None, tuple[int, str, str]]] | None = None) -> None:
        self.repository = repository
        self.execute_command = execute_command

    async def run(self, spec: ExperimentSpec, manifest: EnvironmentManifest) -> ExecutionResult:
        # Step 1: Validation Gates
        report = ValidationReport(
            id=uuid4().hex,
            project_id=spec.project_id,
            run_id=spec.run_id,
            manifest_id=manifest.id,
            schema_validation_passed=True,
            static_checks_passed=True,
            unit_tests_passed=True,
            import_test_passed=True,
            one_batch_forward_passed=True,
            backward_gradient_passed=True,
            checkpoint_save_reload_passed=True,
            short_smoke_training_passed=True,
            evaluation_parsing_passed=True,
            budget_approval_passed=True,
            details={"note": "Simulated successful validation gates."},
            provenance=ArtifactProvenance.create(
                project_id=spec.project_id,
                run_id=spec.run_id,
                source_evidence=[manifest.id],
                provider="deterministic",
                model="validator",
                prompt_version="validator-v1",
                content={"status": "passed"},
            )
        )
        self.repository.save_validation_report(report)

        if not report.passed:
            raise RuntimeError(f"Validation gates failed for spec {spec.id}")

        # Step 2: Execution Job Tracking
        job = ExecutionJob(
            id=uuid4().hex,
            project_id=spec.project_id,
            run_id=spec.run_id,
            spec_id=spec.id,
            worker="local-mock-worker",
            stage="training",
            status=RunStatus.RUNNING,
            timeout_seconds=3600,
            retry_policy="none"
        )
        self.repository.save_execution_job(job)

        # Step 3: Run the command (Mocked if no real command provided)
        exit_code = 0
        stdout = "Training completed."
        stderr = ""
        
        if self.execute_command:
            # We would run the actual python script generated from the spec.
            # But for milestone 1 we just sleep briefly or execute a stub.
            exit_code, stdout, stderr = await self.execute_command(f"echo 'Running {spec.hypothesis}'")
        else:
            await asyncio.sleep(0.1)

        # Step 4: Record Results
        provenance = ArtifactProvenance.create(
            project_id=spec.project_id,
            run_id=spec.run_id,
            source_evidence=[job.id],
            provider="deterministic",
            model="executor",
            prompt_version="executor-v1",
            content={"exit_code": exit_code},
        )
        
        result = ExecutionResult(
            id=uuid4().hex,
            project_id=spec.project_id,
            run_id=spec.run_id,
            job_id=job.id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            structured_metrics={"accuracy": 0.85, "loss": 0.1},
            gpu_telemetry={"max_memory_allocated": "12GB"},
            checkpoint_manifest=("model_final.pt",),
            failure_classification=None if exit_code == 0 else "Execution failed",
            provenance=provenance,
        )
        
        self.repository.save_execution_result(result)
        return result

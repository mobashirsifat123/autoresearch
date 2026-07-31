"""Reproducible, isolated environment preparation."""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    EnvironmentManifest,
    ExperimentSpec,
)


class IsolatedEnvironmentBuilder:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    async def prepare(self, spec: ExperimentSpec) -> EnvironmentManifest:
        # In a real system, this would interact with Docker/Conda, 
        # verify the git worktree, parse dataset statistics, and ping GPUs.
        # For Milestone 1, we capture telemetry safely using standard libraries.
        
        # Determine paths
        experiment_dir = self.workspace_root / "experiments" / spec.run_id
        experiment_dir.mkdir(parents=True, exist_ok=True)
        
        # Hardware checks (mocked or basic OS probes)
        has_gpu = shutil.which("nvidia-smi") is not None
        
        provenance = ArtifactProvenance.create(
            project_id=spec.project_id,
            run_id=spec.run_id,
            source_evidence=[spec.id],
            provider="deterministic",
            model="system",
            prompt_version="env-builder-v1",
            content={"workspace": str(experiment_dir)},
        )

        return EnvironmentManifest(
            id=uuid4().hex,
            project_id=spec.project_id,
            run_id=spec.run_id,
            spec_id=spec.id,
            operating_system=platform.platform(),
            python_version=sys.version,
            environment_manager="local-venv-mock",
            dependency_lock_checksum="mock-lock-checksum-123",
            cuda_compatibility="CUDA-12.0" if has_gpu else "none",
            gpu_model="Mock-GPU-A100" if has_gpu else "none",
            gpu_memory="40GB" if has_gpu else "0GB",
            disk_space="100GB",
            dataset_paths=(f"{experiment_dir}/datasets/mock-data",),
            sample_counts={"train": 1000, "val": 100, "test": 100},
            splits=("train", "val", "test"),
            pretrained_checkpoint_identity="none",
            reproducibility_flags="--deterministic --seed 42",
            provenance=provenance,
        )

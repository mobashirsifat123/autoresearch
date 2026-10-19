"""Approval checksum compatibility for the additive monitor contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from uuid import uuid4

from autoresearch.domain.models import ArtifactProvenance, ExperimentSpec
from autoresearch.experiments.policy import ActionType, ExecutionAction, specification_checksum


def _spec(monitor_contract=None):
    return ExperimentSpec(
        id=uuid4().hex, project_id="p", run_id="r", candidate_id="c",
        hypothesis="h", causal_mechanism="cm", falsification_condition="f",
        repository="/tmp", baseline_commit="none", dataset_identity="d",
        dataset_checksum="dc", split_definition="s", baseline_configuration="b",
        proposed_change="change", independent_variables=(), controlled_variables=(),
        metrics=("accuracy",), random_seeds=(1,), ablations=(), robustness_tests=(),
        statistical_plan="p", compute_budget="c", stopping_conditions=(),
        expected_outputs=("scripts/run_experiment.py",), artifact_contract="a",
        provenance=ArtifactProvenance.create(
            project_id="p", run_id="r", source_evidence=(),
            provider="test", model="test", prompt_version="test", content="x",
        ),
        monitor_contract=monitor_contract or {},
    )


def test_empty_monitor_contract_preserves_the_pre_upgrade_checksum():
    spec = _spec()
    action = ExecutionAction(ActionType.RUN_PYTHON_SCRIPT, "scripts/run_experiment.py", ())
    old_payload = {"spec": asdict(spec), "actions": [asdict(action)], "worker": "local"}
    old_payload["spec"].pop("monitor_contract", None)
    expected = hashlib.sha256(
        json.dumps(old_payload, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert specification_checksum(spec, (action,), "local") == expected


def test_non_empty_monitor_contract_requires_a_new_approval():
    action = ExecutionAction(ActionType.RUN_PYTHON_SCRIPT, "scripts/run_experiment.py", ())
    empty = specification_checksum(_spec(), (action,), "local")
    monitored = specification_checksum(
        _spec({"monitors": [{"name": "loss", "why": "divergence"}]}), (action,), "local"
    )
    assert empty != monitored

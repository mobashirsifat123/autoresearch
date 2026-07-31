"""Experiment planning translating novelty into concrete specifications."""

from __future__ import annotations

import json
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    ExperimentSpec,
    NoveltyCandidate,
)
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "causal_mechanism": {"type": "string"},
        "falsification_condition": {"type": "string"},
        "repository": {"type": "string"},
        "baseline_commit": {"type": "string"},
        "dataset_identity": {"type": "string"},
        "dataset_checksum": {"type": "string"},
        "split_definition": {"type": "string"},
        "baseline_configuration": {"type": "string"},
        "proposed_change": {"type": "string"},
        "independent_variables": {"type": "array", "items": {"type": "string"}},
        "controlled_variables": {"type": "array", "items": {"type": "string"}},
        "metrics": {"type": "array", "items": {"type": "string"}},
        "random_seeds": {"type": "array", "items": {"type": "integer"}},
        "ablations": {"type": "array", "items": {"type": "string"}},
        "robustness_tests": {"type": "array", "items": {"type": "string"}},
        "statistical_plan": {"type": "string"},
        "compute_budget": {"type": "string"},
        "stopping_conditions": {"type": "array", "items": {"type": "string"}},
        "expected_outputs": {"type": "array", "items": {"type": "string"}},
        "artifact_contract": {"type": "string"},
    },
    "required": [
        "hypothesis", "causal_mechanism", "falsification_condition",
        "repository", "baseline_commit", "dataset_identity", "dataset_checksum",
        "split_definition", "baseline_configuration", "proposed_change",
        "independent_variables", "controlled_variables", "metrics",
        "random_seeds", "ablations", "robustness_tests", "statistical_plan",
        "compute_budget", "stopping_conditions", "expected_outputs",
        "artifact_contract"
    ],
    "additionalProperties": False,
}

PROMPT_VERSION = "experiment-planner-v1"


class ExperimentSpecGenerator:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def plan(self, candidate: NoveltyCandidate) -> ExperimentSpec:
        messages = [
            {"role": "system", "content": "You are a meticulous experimental designer. Translate the proposed novelty candidate into a highly-detailed, reproducible experiment specification. Specify random seeds, ablations, and exact stopping conditions."},
            {"role": "user", "content": f"Candidate Method: {candidate.method}\nExpected Effect: {candidate.expected_effect}\nAblations: {candidate.required_ablations}\nObjections: {candidate.risks_and_objections}"}
        ]
        
        payload = await self.router.structured_output(
            ModelRole.RESEARCH_REASONING,
            messages=messages,
            schema={"name": "experiment_spec", "schema": SPEC_SCHEMA},
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            operation="experiment_planning",
        )
        
        provider, model = self.router.identity(ModelRole.RESEARCH_REASONING)
        provenance = ArtifactProvenance.create(
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            source_evidence=[candidate.id],
            provider=provider,
            model=model,
            prompt_version=PROMPT_VERSION,
            content=payload,
        )
        
        return ExperimentSpec(
            id=uuid4().hex,
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            candidate_id=candidate.id,
            hypothesis=payload.get("hypothesis", ""),
            causal_mechanism=payload.get("causal_mechanism", ""),
            falsification_condition=payload.get("falsification_condition", ""),
            repository=payload.get("repository", ""),
            baseline_commit=payload.get("baseline_commit", ""),
            dataset_identity=payload.get("dataset_identity", ""),
            dataset_checksum=payload.get("dataset_checksum", ""),
            split_definition=payload.get("split_definition", ""),
            baseline_configuration=payload.get("baseline_configuration", ""),
            proposed_change=payload.get("proposed_change", ""),
            independent_variables=tuple(payload.get("independent_variables", [])),
            controlled_variables=tuple(payload.get("controlled_variables", [])),
            metrics=tuple(payload.get("metrics", [])),
            random_seeds=tuple(payload.get("random_seeds", [])),
            ablations=tuple(payload.get("ablations", [])),
            robustness_tests=tuple(payload.get("robustness_tests", [])),
            statistical_plan=payload.get("statistical_plan", ""),
            compute_budget=payload.get("compute_budget", ""),
            stopping_conditions=tuple(payload.get("stopping_conditions", [])),
            expected_outputs=tuple(payload.get("expected_outputs", [])),
            artifact_contract=payload.get("artifact_contract", ""),
            provenance=provenance,
        )

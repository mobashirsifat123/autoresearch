"""Scientific novelty criticism and scoring."""

from __future__ import annotations

import json
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    NoveltyCandidate,
    NoveltyCritique,
)
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "novelty_score": {"type": "number", "minimum": 0, "maximum": 1},
        "significance_score": {"type": "number", "minimum": 0, "maximum": 1},
        "causal_plausibility_score": {"type": "number", "minimum": 0, "maximum": 1},
        "falsifiability_score": {"type": "number", "minimum": 0, "maximum": 1},
        "experimental_discriminability_score": {"type": "number", "minimum": 0, "maximum": 1},
        "feasibility_score": {"type": "number", "minimum": 0, "maximum": 1},
        "implementation_risk_score": {"type": "number", "minimum": 0, "maximum": 1},
        "expected_information_gain_score": {"type": "number", "minimum": 0, "maximum": 1},
        "compute_efficiency_score": {"type": "number", "minimum": 0, "maximum": 1},
        "publication_potential_score": {"type": "number", "minimum": 0, "maximum": 1},
        "objections": {"type": "array", "items": {"type": "string"}},
        "failure_scenarios": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "novelty_score", "significance_score", "causal_plausibility_score",
        "falsifiability_score", "experimental_discriminability_score",
        "feasibility_score", "implementation_risk_score",
        "expected_information_gain_score", "compute_efficiency_score",
        "publication_potential_score", "objections", "failure_scenarios"
    ],
    "additionalProperties": False,
}

PROMPT_VERSION = "novelty-critic-v1"


class ScientificCritic:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def evaluate(self, candidate: NoveltyCandidate) -> NoveltyCritique:
        messages = [
            {"role": "system", "content": "You are a harsh but fair scientific reviewer for a top-tier machine learning conference. Score this candidate method strictly. Provide explicit objections and failure scenarios."},
            {"role": "user", "content": f"Method: {candidate.method}\nMechanism: {candidate.causal_mechanism}\nFalsification condition: {candidate.falsification_condition}\nExpected effect: {candidate.expected_effect}"}
        ]
        
        payload = await self.router.structured_output(
            ModelRole.SCIENTIFIC_CRITIC,
            messages=messages,
            schema={"name": "novelty_critique", "schema": CRITIQUE_SCHEMA},
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            operation="novelty_criticism",
        )
        
        provider, model = self.router.identity(ModelRole.SCIENTIFIC_CRITIC)
        provenance = ArtifactProvenance.create(
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            source_evidence=[],
            provider=provider,
            model=model,
            prompt_version=PROMPT_VERSION,
            content=payload,
        )
        
        return NoveltyCritique(
            id=uuid4().hex,
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            candidate_id=candidate.id,
            novelty_score=payload.get("novelty_score", 0.0),
            significance_score=payload.get("significance_score", 0.0),
            causal_plausibility_score=payload.get("causal_plausibility_score", 0.0),
            falsifiability_score=payload.get("falsifiability_score", 0.0),
            experimental_discriminability_score=payload.get("experimental_discriminability_score", 0.0),
            feasibility_score=payload.get("feasibility_score", 0.0),
            implementation_risk_score=payload.get("implementation_risk_score", 0.0),
            expected_information_gain_score=payload.get("expected_information_gain_score", 0.0),
            compute_efficiency_score=payload.get("compute_efficiency_score", 0.0),
            publication_potential_score=payload.get("publication_potential_score", 0.0),
            objections=tuple(payload.get("objections", [])),
            failure_scenarios=tuple(payload.get("failure_scenarios", [])),
            provenance=provenance,
        )

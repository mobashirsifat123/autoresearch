"""Novelty candidate generation using structured LLM output."""

from __future__ import annotations

import json
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    CandidateStatus,
    NoveltyCandidate,
    ResearchGap,
)
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "strategy": {"type": "string"},
        "method": {"type": "string"},
        "causal_mechanism": {"type": "string"},
        "target_limitation": {"type": "string"},
        "difference_from_existing": {"type": "string"},
        "expected_effect": {"type": "string"},
        "falsification_condition": {"type": "string"},
        "minimum_validating_experiment": {"type": "string"},
        "required_ablations": {"type": "array", "items": {"type": "string"}},
        "implementation_complexity": {"type": "string"},
        "expected_compute": {"type": "string"},
        "risks_and_objections": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "strategy", "method", "causal_mechanism", "target_limitation",
        "difference_from_existing", "expected_effect", "falsification_condition",
        "minimum_validating_experiment", "required_ablations",
        "implementation_complexity", "expected_compute", "risks_and_objections"
    ],
    "additionalProperties": False,
}

PROMPT_VERSION = "candidate-gen-v1"


class LLMCandidateGenerator:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def generate(self, gap: ResearchGap, count: int = 5) -> tuple[NoveltyCandidate, ...]:
        candidates: list[NoveltyCandidate] = []
        # We prompt the LLM for multiple strategies to generate diverse candidates.
        strategies = [
            "limitation root cause elimination",
            "cross-domain mechanism transfer",
            "contradiction resolution",
            "theoretical boundary extension",
            "computational bottleneck bypass",
        ]
        
        for i in range(min(count, len(strategies))):
            strategy = strategies[i]
            messages = [
                {"role": "system", "content": "You are a world-class AI researcher proposing a novel method to address a validated research gap. Return a precise, highly-detailed structured specification of your proposed method. Reject vague claims."},
                {"role": "user", "content": f"Research Gap:\n{gap.statement}\nWhy Unsolved: {gap.why_unsolved}\n\nGenerate a candidate using strategy: {strategy}."}
            ]
            
            payload = await self.router.structured_output(
                ModelRole.RESEARCH_REASONING,
                messages=messages,
                schema={"name": "novelty_candidate", "schema": CANDIDATE_SCHEMA},
                project_id=gap.project_id,
                run_id=gap.run_id,
                operation="novelty_generation",
            )
            
            provider, model = self.router.identity(ModelRole.RESEARCH_REASONING)
            provenance = ArtifactProvenance.create(
                project_id=gap.project_id,
                run_id=gap.run_id,
                source_evidence=gap.supporting_paper_ids,
                provider=provider,
                model=model,
                prompt_version=PROMPT_VERSION,
                content=payload,
            )
            
            candidate = NoveltyCandidate(
                id=uuid4().hex,
                project_id=gap.project_id,
                run_id=gap.run_id,
                gap_id=gap.id,
                strategy=payload.get("strategy", strategy),
                method=payload.get("method", ""),
                causal_mechanism=payload.get("causal_mechanism", ""),
                target_limitation=payload.get("target_limitation", ""),
                difference_from_existing=payload.get("difference_from_existing", ""),
                expected_effect=payload.get("expected_effect", ""),
                falsification_condition=payload.get("falsification_condition", ""),
                minimum_validating_experiment=payload.get("minimum_validating_experiment", ""),
                required_ablations=tuple(payload.get("required_ablations", [])),
                implementation_complexity=payload.get("implementation_complexity", ""),
                expected_compute=payload.get("expected_compute", ""),
                risks_and_objections=tuple(payload.get("risks_and_objections", [])),
                status=CandidateStatus.GENERATED,
                provenance=provenance,
            )
            candidates.append(candidate)
            
        return tuple(candidates)

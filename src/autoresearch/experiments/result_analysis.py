"""Statistical analysis aggregating experiment results."""

from __future__ import annotations

import json
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    ExecutionResult,
    ExperimentSpec,
    StatisticalAnalysis,
)
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "failure_case_analysis": {"type": "string"},
        "hypothesis_support_assessment": {"type": "string"},
    },
    "required": ["failure_case_analysis", "hypothesis_support_assessment"],
    "additionalProperties": False,
}

PROMPT_VERSION = "result-analyzer-v1"


class StatisticalAnalyzer:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def analyze(self, spec: ExperimentSpec, result: ExecutionResult) -> StatisticalAnalysis:
        # Step 1: Parse deterministic metrics from the ExecutionResult
        # In a real system, we'd aggregate across multiple seeds.
        baseline_metrics = {"accuracy": 0.80, "loss": 0.15}
        proposed_metrics = result.structured_metrics
        
        # Calculate effect sizes (mocked for milestone 1)
        effect_size = {"accuracy": proposed_metrics.get("accuracy", 0) - baseline_metrics.get("accuracy", 0)}
        
        # Step 2: Use LLM for scientific interpretation
        messages = [
            {"role": "system", "content": "You are a lead statistician. Analyze the metrics from an experiment. Assess whether the hypothesis is supported, contradicted, or if the results are inconclusive. Provide a failure case analysis if applicable."},
            {"role": "user", "content": f"Hypothesis: {spec.hypothesis}\n\nBaseline Metrics: {baseline_metrics}\nProposed Metrics: {proposed_metrics}\nEffect Size: {effect_size}\n\nAssess the results."}
        ]
        
        payload = await self.router.structured_output(
            ModelRole.RESEARCH_REASONING,
            messages=messages,
            schema={"name": "statistical_analysis", "schema": ANALYSIS_SCHEMA},
            project_id=spec.project_id,
            run_id=spec.run_id,
            operation="result_analysis",
        )
        
        provider, model = self.router.identity(ModelRole.RESEARCH_REASONING)
        provenance = ArtifactProvenance.create(
            project_id=spec.project_id,
            run_id=spec.run_id,
            source_evidence=[result.id],
            provider=provider,
            model=model,
            prompt_version=PROMPT_VERSION,
            content=payload,
        )
        
        return StatisticalAnalysis(
            id=uuid4().hex,
            project_id=spec.project_id,
            run_id=spec.run_id,
            spec_id=spec.id,
            baseline_metrics=baseline_metrics,
            proposed_metrics=proposed_metrics,
            effect_size=effect_size,
            confidence_intervals={"accuracy": (effect_size["accuracy"] - 0.01, effect_size["accuracy"] + 0.01)},
            seed_level_results=(proposed_metrics,),
            ablation_contributions={},
            parameter_count_diff=1000,
            flops_latency_diff=0.05,
            training_inference_cost=10.0,
            failure_case_analysis=payload.get("failure_case_analysis", ""),
            hypothesis_support_assessment=payload.get("hypothesis_support_assessment", ""),
            provenance=provenance,
        )

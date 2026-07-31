"""Scientific report generation summarizing the experiment."""

from __future__ import annotations

import json
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    ExperimentDecision,
    ExperimentReport,
    ExperimentSpec,
    StatisticalAnalysis,
)
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "literature_motivation": {"type": "string"},
        "research_gap": {"type": "string"},
        "implementation_diff": {"type": "string"},
        "uncertainty": {"type": "string"},
        "ablations": {"type": "string"},
        "robustness_tests": {"type": "string"},
        "limitations": {"type": "string"},
        "reproducibility_instructions": {"type": "string"},
        "decision": {
            "type": "string",
            "enum": [
                "supported", "partially_supported", "no_effect", 
                "implementation_failure", "optimization_failure", 
                "data_limitation", "evaluation_artifact", 
                "cost_prohibitive", "domain_specific", "mechanism_contradicted"
            ]
        },
        "decision_evidence": {"type": "string"},
    },
    "required": [
        "literature_motivation", "research_gap", "implementation_diff",
        "uncertainty", "ablations", "robustness_tests", "limitations",
        "reproducibility_instructions", "decision", "decision_evidence"
    ],
    "additionalProperties": False,
}

PROMPT_VERSION = "report-writer-v1"


class ScientificReportWriter:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def write(self, spec: ExperimentSpec, analysis: StatisticalAnalysis) -> ExperimentReport:
        messages = [
            {"role": "system", "content": "You are the final scientific author. Synthesize the experimental setup and results into a comprehensive, reproducible report. Explicitly make a decision on whether the hypothesis is supported."},
            {"role": "user", "content": f"Hypothesis: {spec.hypothesis}\nProposed Change: {spec.proposed_change}\n\nAssessment: {analysis.hypothesis_support_assessment}\nFailure analysis: {analysis.failure_case_analysis}"}
        ]
        
        payload = await self.router.structured_output(
            ModelRole.RESEARCH_REASONING,
            messages=messages,
            schema={"name": "experiment_report", "schema": REPORT_SCHEMA},
            project_id=spec.project_id,
            run_id=spec.run_id,
            operation="report_writing",
        )
        
        provider, model = self.router.identity(ModelRole.RESEARCH_REASONING)
        provenance = ArtifactProvenance.create(
            project_id=spec.project_id,
            run_id=spec.run_id,
            source_evidence=[analysis.id],
            provider=provider,
            model=model,
            prompt_version=PROMPT_VERSION,
            content=payload,
        )
        
        return ExperimentReport(
            id=uuid4().hex,
            project_id=spec.project_id,
            run_id=spec.run_id,
            analysis_id=analysis.id,
            literature_motivation=payload.get("literature_motivation", ""),
            research_gap=payload.get("research_gap", ""),
            hypothesis=spec.hypothesis,
            proposed_method=spec.proposed_change,
            implementation_diff=payload.get("implementation_diff", ""),
            lineage=f"Generated from candidate {spec.candidate_id}",
            dataset_split=spec.split_definition,
            exact_commands=("python train.py --config default",),
            baseline_results=analysis.baseline_metrics,
            aggregated_results=analysis.proposed_metrics,
            uncertainty=payload.get("uncertainty", ""),
            ablations=payload.get("ablations", ""),
            robustness_tests=payload.get("robustness_tests", ""),
            failed_runs=0,
            cost=analysis.training_inference_cost,
            limitations=payload.get("limitations", ""),
            reproducibility_instructions=payload.get("reproducibility_instructions", ""),
            decision=ExperimentDecision(payload.get("decision", "supported")),
            decision_evidence=payload.get("decision_evidence", ""),
            provenance=provenance,
        )

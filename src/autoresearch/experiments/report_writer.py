"""Scientific report generation summarizing the experiment.
Now with validity assessment and Bayesian analysis integration."""

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

VALIDITY_SCHEMA = {
    "type": "object",
    "properties": {
        "construct_validity": {"type": "string", "description": "Does the experiment actually test the hypothesis it claims to test?"},
        "internal_validity": {"type": "string", "description": "Are confounding variables controlled? Can we attribute effects to the intervention?"},
        "external_validity": {"type": "string", "description": "How well do results generalize beyond the specific experimental conditions?"},
        "statistical_conclusion_validity": {"type": "string", "description": "Are the statistical conclusions appropriate given the data and analysis methods?"},
    },
    "required": ["construct_validity", "internal_validity", "external_validity", "statistical_conclusion_validity"],
    "additionalProperties": False,
}

PROMPT_VERSION = "report-writer-v2"


class ScientificReportWriter:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def write(self, spec: ExperimentSpec, analysis: StatisticalAnalysis) -> ExperimentReport:
        # Step 1: Validity assessment
        validity = await self._assess_validity(spec, analysis)

        # Step 2: Generate the main report (with Bayesian context)
        bayesian_context = ""
        provenance_data = analysis.provenance.content if isinstance(analysis.provenance.content, dict) else {}
        bayesian_probs = provenance_data.get("bayesian_p_positive", {})
        if bayesian_probs:
            bayesian_context = "\nBayesian probabilities of positive effect:\n" + json.dumps(bayesian_probs, indent=2)
        cohens_d_vals = provenance_data.get("cohens_d", {})
        if cohens_d_vals:
            bayesian_context += "\nCohen's d effect sizes:\n" + json.dumps(cohens_d_vals, indent=2)

        messages = [
            {"role": "system", "content": "You are the final scientific author. Synthesize the experimental setup and results into a comprehensive, reproducible report. Explicitly make a decision on whether the hypothesis is supported. Consider both frequentist and Bayesian evidence."},
            {"role": "user", "content": f"Hypothesis: {spec.hypothesis}\nProposed Change: {spec.proposed_change}\n\nAssessment: {analysis.hypothesis_support_assessment}\nFailure analysis: {analysis.failure_case_analysis}\n{bayesian_context}\n\nValidity assessment:\n{json.dumps(validity, indent=2)}"}
        ]

        payload = await self.router.structured_output(
            ModelRole.RESEARCH_REASONING,
            messages=messages,
            schema={"name": "experiment_report", "schema": REPORT_SCHEMA},
            project_id=spec.project_id, run_id=spec.run_id,
            operation="report_writing",
        )

        provider, model = self.router.identity(ModelRole.RESEARCH_REASONING)
        provenance = ArtifactProvenance.create(
            project_id=spec.project_id, run_id=spec.run_id,
            source_evidence=[analysis.id],
            provider=provider, model=model,
            prompt_version=PROMPT_VERSION,
            content={**payload, "validity": validity},
        )

        return ExperimentReport(
            id=uuid4().hex, project_id=spec.project_id, run_id=spec.run_id,
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

    async def _assess_validity(self, spec: ExperimentSpec, analysis: StatisticalAnalysis) -> dict:
        """Assess construct, internal, external, and statistical conclusion validity."""
        messages = [
            {"role": "system", "content": "You are an experimental validity auditor. Assess the experiment's construct validity (does it test the right hypothesis?), internal validity (are confounds controlled?), external validity (does it generalize?), and statistical conclusion validity (are the analyses appropriate?). Be critical and specific."},
            {"role": "user", "content": f"Hypothesis: {spec.hypothesis}\nProposed Change: {spec.proposed_change}\nMetrics: {spec.metrics}\nStatistical Plan: {spec.statistical_plan}\nSeed count: {len(spec.random_seeds)}\nBaseline: {analysis.baseline_metrics}\nStatus: {analysis.hypothesis_support_assessment}"},
        ]

        payload = await self.router.structured_output(
            ModelRole.SCIENTIFIC_CRITIC,
            messages=messages,
            schema={"name": "validity_assessment", "schema": VALIDITY_SCHEMA},
            project_id=spec.project_id, run_id=spec.run_id,
            operation="validity_assessment",
        )
        return payload

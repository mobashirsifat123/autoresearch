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

        # Step 2: Generate the main report with standardized effect size interpretation (E5)
        provenance_data = getattr(analysis.provenance, "content", {}) if isinstance(getattr(analysis.provenance, "content", None), dict) else {}
        bayesian_probs = provenance_data.get("bayesian_p_positive", {})
        cohens_d_vals = provenance_data.get("cohens_d", {})
        holm_sig = provenance_data.get("holm_bonferroni_significant", {})

        effect_summary = _effect_interpretation(cohens_d_vals, bayesian_probs, holm_sig)
        bayesian_context = f"\nStandardized Effect Interpretation:\n{effect_summary}\n" if effect_summary else ""

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

        decision_str = payload.get("decision", "supported")
        decision_conf = _calibrate_decision_confidence(decision_str, bayesian_probs, len(spec.random_seeds))

        provider, model = self.router.identity(ModelRole.RESEARCH_REASONING)
        provenance = ArtifactProvenance.create(
            project_id=spec.project_id, run_id=spec.run_id,
            source_evidence=[analysis.id],
            provider=provider, model=model,
            prompt_version="report-writer-v3",
            content={**payload, "decision_confidence": decision_conf, "validity": validity},
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
            decision=ExperimentDecision(decision_str),
            decision_evidence=payload.get("decision_evidence", ""),
            provenance=provenance,
        )


def _effect_interpretation(cohens_d: dict, bayesian_probs: dict, holm_sig: dict) -> str:
    """Deterministic effect size interpretation framework (E5)."""
    lines = []
    for metric, d in cohens_d.items():
        mag = "negligible" if abs(d) < 0.2 else "small" if abs(d) < 0.5 else "medium" if abs(d) < 0.8 else "large"
        prob = bayesian_probs.get(metric, 0.5)
        certainty = "strong" if prob > 0.95 else "moderate" if prob > 0.80 else "weak" if prob > 0.60 else "inconclusive"
        sig_str = "statistically significant (Holm-Bonferroni corrected)" if holm_sig.get(metric) else "not significant after correction"
        lines.append(f"  - {metric}: Cohen's d={d:.3f} ({mag} effect), P(positive)={prob:.3f} ({certainty} Bayesian evidence), {sig_str}")
    return "\n".join(lines)


def _calibrate_decision_confidence(decision: str, bayesian_probs: dict, seed_count: int) -> float:
    """Calibrate decision confidence score between 0.0 and 1.0 (E7)."""
    avg_p = sum(bayesian_probs.values()) / max(len(bayesian_probs), 1) if bayesian_probs else 0.5
    base = avg_p if decision in ("supported", "partially_supported") else (1.0 - avg_p)
    sample_factor = min(1.0, seed_count / 10.0)
    return round(max(0.1, min(1.0, base * 0.6 + sample_factor * 0.4)), 4)

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

"""Deterministic, seed-aware experiment analysis with Bayesian estimation.
LLMs may explain the resulting record, but never calculate it."""

from __future__ import annotations

import math
from statistics import mean, stdev
from uuid import uuid4

from autoresearch.domain.models import ArtifactProvenance, ExecutionResult, ExperimentSpec, StatisticalAnalysis

# Bayesian prior constants
PRIOR_EFFECT_LOCATION = 0.0
PRIOR_EFFECT_SCALE = 0.5  # weak prior (medium effect size)
PRIOR_SAMPLE_SIZE = 2     # effective prior sample size (minimal)


def paired_effects(baseline: list[float], proposed: list[float]) -> dict[str, float | int | str]:
    """Return reproducible paired summary; insufficient samples stay explicit."""
    if len(baseline) != len(proposed):
        raise ValueError("Paired analysis requires equal seed counts")
    if not baseline:
        return {"sample_count": 0, "mean_difference": 0.0, "effect_size": 0.0, "status": "INSUFFICIENT_EVIDENCE"}
    differences = [b - a for a, b in zip(baseline, proposed)]
    avg = mean(differences)
    deviation = stdev(differences) if len(differences) > 1 else 0.0
    n = len(differences)
    cohens_d = avg / deviation if deviation else 0.0

    # Bayesian estimation: posterior mean with conjugate normal prior
    # Posterior = (prior_precision * prior_mean + data_precision * data_mean) / (prior_precision + data_precision)
    prior_precision = PRIOR_SAMPLE_SIZE / (PRIOR_EFFECT_SCALE ** 2) if PRIOR_EFFECT_SCALE > 0 else 0
    data_precision = n / (deviation ** 2) if deviation > 0 else 0
    posterior_mean = (prior_precision * PRIOR_EFFECT_LOCATION + data_precision * avg) / (prior_precision + data_precision) if (prior_precision + data_precision) > 0 else avg
    posterior_sd = math.sqrt(1.0 / (prior_precision + data_precision)) if (prior_precision + data_precision) > 0 else deviation

    # Bayesian probability of positive effect (P(effect > 0 | data))
    if posterior_sd > 0:
        p_positive = 1.0 - _normal_cdf(0, posterior_mean, posterior_sd)
    else:
        p_positive = 1.0 if posterior_mean > 0 else 0.0

    return {
        "sample_count": n,
        "mean_difference": round(avg, 6),
        "cohens_d": round(cohens_d, 6),
        "bayesian_posterior_mean": round(posterior_mean, 6),
        "bayesian_posterior_sd": round(posterior_sd, 6),
        "bayesian_p_positive": round(p_positive, 6),
        "status": "INSUFFICIENT_EVIDENCE" if n < 2 else "ESTIMATE",
    }


def _normal_cdf(x: float, mean: float, sd: float) -> float:
    """Standard normal CDF approximation (Abramowitz and Stegun)."""
    if sd <= 0:
        return 1.0 if x <= mean else 0.0
    z = (x - mean) / sd
    if z < -6:
        return 0.0
    if z > 6:
        return 1.0
    b0, b1, b2, b3, b4, b5, b6 = 0.2316419, 0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429, 0.9375
    t = 1.0 / (1.0 + b0 * abs(z))
    poly = t * (b1 + t * (b2 + t * (b3 + t * (b4 + t * (b5 + t * b6)))))
    cdf = 1.0 - 0.3989422804014327 * math.exp(-z * z / 2.0) * poly
    return cdf if z > 0 else 1.0 - cdf


class StatisticalAnalyzer:
    def __init__(self, router=None) -> None:
        self.router = router

    async def analyze(self, spec: ExperimentSpec, result: ExecutionResult) -> StatisticalAnalysis:
        proposed = result.structured_metrics
        if result.exit_code != 0:
            raise ValueError("evaluation_artifact: execution failed before metrics were produced")
        if not isinstance(proposed, dict) or not proposed or not isinstance(proposed.get("seeds"), list) or not proposed.get("seeds"):
            raise ValueError("metric_schema_invalid: expected non-empty structured metrics with seed observations")
        baseline = proposed.get("baseline", {}) if isinstance(proposed.get("baseline", {}), dict) else {}
        seed_rows = proposed.get("seeds", []) if isinstance(proposed.get("seeds", []), list) else []
        if seed_rows:
            seed_level = tuple(row for row in seed_rows if isinstance(row, dict))
        else:
            seed_level = (proposed,)
        metrics = set(baseline) | {k for k in proposed if isinstance(proposed.get(k), (int, float))}
        effects: dict[str, float] = {}
        intervals: dict[str, tuple[float, float]] = {}
        bayesian_p_positive: dict[str, float] = {}
        bayesian_posterior_mean: dict[str, float] = {}
        cohens_d: dict[str, float] = {}
        status = "INSUFFICIENT_EVIDENCE"
        for metric in sorted(metrics):
            before = [float(row["baseline"][metric]) for row in seed_level if isinstance(row.get("baseline"), dict) and metric in row["baseline"]]
            after = [float(row[metric]) for row in seed_level if metric in row and isinstance(row[metric], (int, float))]
            if before and len(before) == len(after):
                summary = paired_effects(before, after)
                effects[metric] = float(summary["mean_difference"])
                bayesian_p_positive[metric] = float(summary["bayesian_p_positive"])
                bayesian_posterior_mean[metric] = float(summary["bayesian_posterior_mean"])
                cohens_d[metric] = float(summary["cohens_d"])
                intervals[metric] = (
                    effects[metric] - 1.96 * abs(effects[metric]) / math.sqrt(len(after)),
                    effects[metric] + 1.96 * abs(effects[metric]) / math.sqrt(len(after)),
                ) if len(after) > 0 else (effects[metric], effects[metric])
                if len(after) >= 2:
                    status = "ESTIMATE"

        return StatisticalAnalysis(
            id=uuid4().hex, project_id=spec.project_id, run_id=spec.run_id, spec_id=spec.id,
            baseline_metrics=baseline, proposed_metrics=proposed, effect_size=effects,
            confidence_intervals=intervals, seed_level_results=seed_level,
            ablation_contributions={}, parameter_count_diff=0, flops_latency_diff=None,
            training_inference_cost=0.0, failure_case_analysis="",
            hypothesis_support_assessment=status,
            provenance=ArtifactProvenance.create(
                project_id=spec.project_id, run_id=spec.run_id,
                source_evidence=[result.id],
                provider="deterministic", model="statistics",
                prompt_version="stats-v3",
                content={
                    "status": status,
                    "bayesian_p_positive": bayesian_p_positive,
                    "bayesian_posterior_mean": bayesian_posterior_mean,
                    "cohens_d": cohens_d,
                },
            ),
        )

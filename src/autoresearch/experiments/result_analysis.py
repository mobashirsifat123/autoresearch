"""Deterministic, seed-aware experiment analysis.

LLMs may explain the resulting record, but never calculate it.
"""
from __future__ import annotations

import math
from statistics import mean, stdev
from uuid import uuid4

from autoresearch.domain.models import ArtifactProvenance, ExecutionResult, ExperimentSpec, StatisticalAnalysis


def paired_effects(baseline: list[float], proposed: list[float]) -> dict[str, float | int | str]:
    """Return reproducible paired summary; insufficient samples stay explicit."""
    if len(baseline) != len(proposed):
        raise ValueError("Paired analysis requires equal seed counts")
    if not baseline:
        return {"sample_count": 0, "mean_difference": 0.0, "effect_size": 0.0, "status": "INSUFFICIENT_EVIDENCE"}
    differences = [b - a for a, b in zip(baseline, proposed)]
    avg = mean(differences)
    deviation = stdev(differences) if len(differences) > 1 else 0.0
    return {
        "sample_count": len(differences),
        "mean_difference": avg,
        "effect_size": avg / deviation if deviation else 0.0,
        "status": "INSUFFICIENT_EVIDENCE" if len(differences) < 2 else "ESTIMATE",
    }


class StatisticalAnalyzer:
    def __init__(self, router=None) -> None:
        self.router = router

    async def analyze(self, spec: ExperimentSpec, result: ExecutionResult) -> StatisticalAnalysis:
        # Results may contain per-seed values under ``seeds``. A single value is
        # retained as an observation but is never called significant.
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
        status = "INSUFFICIENT_EVIDENCE"
        for metric in sorted(metrics):
            before = [float(row["baseline"][metric]) for row in seed_level if isinstance(row.get("baseline"), dict) and metric in row["baseline"]]
            after = [float(row[metric]) for row in seed_level if metric in row and isinstance(row[metric], (int, float))]
            if before and len(before) == len(after):
                summary = paired_effects(before, after)
                effects[metric] = float(summary["mean_difference"])
                intervals[metric] = (effects[metric], effects[metric]) if len(after) < 2 else (effects[metric] - 1.96 * abs(effects[metric]) / math.sqrt(len(after)), effects[metric] + 1.96 * abs(effects[metric]) / math.sqrt(len(after)))
                if len(after) >= 2:
                    status = "ESTIMATE"
        return StatisticalAnalysis(
            id=uuid4().hex, project_id=spec.project_id, run_id=spec.run_id, spec_id=spec.id,
            baseline_metrics=baseline, proposed_metrics=proposed, effect_size=effects,
            confidence_intervals=intervals, seed_level_results=seed_level,
            ablation_contributions={}, parameter_count_diff=0, flops_latency_diff=None,
            training_inference_cost=0.0, failure_case_analysis="",
            hypothesis_support_assessment=status,
            provenance=ArtifactProvenance.create(project_id=spec.project_id, run_id=spec.run_id, source_evidence=[result.id], provider="deterministic", model="statistics", prompt_version="stats-v2", content={"status": status}),
        )

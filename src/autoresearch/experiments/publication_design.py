"""Publication-design gate for experiment specifications.

Planning is allowed to be exploratory. It is not allowed to claim that a
one-baseline, no-ablation, no-power, model-supplied-checksum design is
submission-grade. This module reports exact gaps before execution.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from autoresearch.application.evidence_access import field
from autoresearch.application.publication_readiness import (
    baseline_names,
    dataset_names,
    power_established,
)
from autoresearch.application.venue_profiles import VenueProfile, get_venue_profile

_CHECKSUM = re.compile(r"^[0-9a-fA-F]{32,128}$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or len(set(lowered)) <= 2


def _checksum_ok(value: str) -> bool:
    return bool(_CHECKSUM.fullmatch(value.strip())) and not _placeholder(value)


def _commit_ok(value: str) -> bool:
    return bool(_COMMIT.fullmatch(value.strip())) and not _placeholder(value)


def publication_design_gaps(
    spec: Any,
    *,
    profile: VenueProfile | None = None,
    min_datasets: int | None = None,
) -> list[dict[str, Any]]:
    """Return exact publication-design gaps for a planned spec."""
    venue = profile or get_venue_profile("generic_journal")
    required_datasets = venue.min_datasets if min_datasets is None else min_datasets
    gaps: list[dict[str, Any]] = []

    def add(code: str, message: str, fatal: bool = True) -> None:
        gaps.append({
            "code": code,
            "stage": "experiment",
            "message": message,
            "fatal": fatal,
        })

    if spec is None:
        add("specification_missing", "no experiment specification exists")
        return gaps

    baselines = baseline_names(spec)
    if len(baselines) < venue.min_baselines:
        add("baselines_below_minimum", f"baselines {len(baselines)} < {venue.min_baselines}")
    ablations = list(field(spec, "ablations", ()) or ())
    if len(ablations) < venue.min_ablations:
        add("ablations_missing", f"ablations {len(ablations)} < {venue.min_ablations}")
    robustness = list(field(spec, "robustness_tests", ()) or ())
    if len(robustness) < venue.min_robustness:
        add("robustness_missing", f"robustness tests {len(robustness)} < {venue.min_robustness}")
    datasets = dataset_names(spec)
    if len(datasets) < required_datasets:
        add("datasets_below_minimum", f"datasets {len(datasets)} < {required_datasets}")
    if venue.require_power and not power_established(spec):
        add("power_not_established", "no established power analysis")
    checksum = str(field(spec, "dataset_checksum", "") or "")
    if not _checksum_ok(checksum):
        add("dataset_checksum_missing", "dataset checksum is missing or not a content hash")
    commit = str(field(spec, "baseline_commit", "") or "")
    if not _commit_ok(commit):
        add("baseline_commit_missing", "baseline commit is missing or not a commit hash")
    return gaps


def provenance_sources(repository: Any, spec: Any) -> dict[str, str]:
    """Read the planner's provenance-source record for this spec run."""
    run_id = str(field(spec, "run_id", "") or "unknown")
    path = Path(repository.artifact_root) / "experiments" / run_id / "provenance_sources.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    return {
        "dataset_checksum_source": str(payload.get("dataset_checksum_source") or "model"),
        "baseline_commit_source": str(payload.get("baseline_commit_source") or "model"),
    }


def write_provenance_sources(
    repository: Any, spec: Any, sources: dict[str, str]
) -> Path:
    run_id = str(field(spec, "run_id", "") or "unknown")
    root = Path(repository.artifact_root) / "experiments" / run_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / "provenance_sources.json"
    path.write_text(json.dumps(sources, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_publication_gaps(repository: Any, spec: Any, gaps: list[dict[str, Any]]) -> Path:
    """Persist gaps beside the experiment run artifacts."""
    run_id = str(field(spec, "run_id", "") or "unknown")
    root = Path(repository.artifact_root) / "experiments" / run_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / "publication_gaps.json"
    path.write_text(json.dumps({"gaps": gaps}, indent=2, sort_keys=True), encoding="utf-8")
    return path

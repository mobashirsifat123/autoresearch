"""Publication-readiness gate.

The manuscript stage must distinguish a complete evidence *chain* from a
submission-ready *paper*. This module makes that distinction explicit and
deterministic before any prose is written:

* simulation / synthetic / placeholder provenance is detected from the
  experiment source and artifacts, not inferred from prose;
* dataset checksum and implementation commit must be real identities for
  empirical work;
* seeds, power, external baselines, ablations, and related-work coverage are
  compared against the selected venue profile;
* a ``readiness`` state and exact ``blocking_issues`` are recorded.

The gate never fabricates evidence and never turns a simulation into an
empirical study. A simulated study remains publishable as a simulation study
(workshop / technical report), but it cannot be marked ``submission_ready``
for an empirical venue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from autoresearch.application.evidence_access import field as _field
from autoresearch.application.venue_profiles import VenueProfile, get_venue_profile

# Strong markers: the source says, in its own words, that the measurements are
# simulated or synthetic. These are treated as simulation evidence, not as
# style warnings.
_STRONG_SIMULATION_MARKERS = (
    # Explicit measurement-simulation phrases, not every use of the word
    # "simulation" (a real study may simulate an environment or use synthetic
    # training data). The AutoFID source says "Simulate GPT-5.2 ... on anchor
    # tasks" and "Simulated execution"; both are caught.
    "simulated execution",
    "simulate gpt",
    "simulated data",
    "synthetic measurements",
    "synthetic results",
    "mock measurements",
    "placeholder values",
    "synthesize a difficulty",
    "synthesized difficulty",
    "not_computed_no_checksum",
    "not_applicable_no_commit",
)

# Weak markers: common RNG usage. A legitimate experiment may also use these,
# so they only raise a provenance warning unless a strong marker is present.
_WEAK_SIMULATION_MARKERS = (
    # Continuous random draws used as measurements are suspicious. A bare
    # ``random.Random(seed)`` or ``torch.randn`` initialization is common in
    # legitimate experiment code and is not enough to call a study simulated.
    "random.gauss",
    "np.random.normal",
    "numpy.random.normal",
)

# A source that never reads data is suspicious even without an explicit
# "simulate" marker: the smoke fixture generates metrics from random values.
_HAS_DATA_IO_MARKERS = (
    "open(",
    "read_text(",
    "read_bytes(",
    "read_csv",
    "read_parquet",
    "np.load",
    "numpy.load",
    "torch.utils.data",
    "datasets.",
    "load_dataset",
    "DataLoader",
    "Image.open",
    "h5py",
    "sqlite3.connect",
    "zarr.",
)

_CHECKSUM_RE = re.compile(r"^[0-9a-fA-F]{32,128}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class ProvenanceReport:
    simulated: bool
    simulation_suspected: bool
    real_execution: bool
    dataset_checksum: str
    repository_commit: str
    source_path: str | None
    markers: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "simulated": self.simulated,
            "simulation_suspected": self.simulation_suspected,
            "real_execution": self.real_execution,
            "dataset_checksum": self.dataset_checksum,
            "repository_commit": self.repository_commit,
            "source_path": self.source_path,
            "markers": list(self.markers),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class EvidenceSufficiency:
    n_seeds: int
    power_established: bool
    external_baselines: int
    ablations: int
    robustness_tests: int
    related_work_coverage: int
    reference_count: int
    decision: str
    datasets: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_seeds": self.n_seeds,
            "power_established": self.power_established,
            "external_baselines": self.external_baselines,
            "ablations": self.ablations,
            "robustness_tests": self.robustness_tests,
            "related_work_coverage": self.related_work_coverage,
            "reference_count": self.reference_count,
            "decision": self.decision,
            "datasets": self.datasets,
        }


@dataclass
class ReadinessReport:
    project_id: str
    venue: str
    study_type: str
    readiness: str  # submission_ready | workshop_ready | technical_report | not_ready
    submission_ready: bool
    provenance: ProvenanceReport
    evidence: EvidenceSufficiency
    blocking_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "publication_readiness",
            "project_id": self.project_id,
            "venue": self.venue,
            "study_type": self.study_type,
            "readiness": self.readiness,
            "submission_ready": self.submission_ready,
            "provenance": self.provenance.to_dict(),
            "evidence": self.evidence.to_dict(),
            "blocking_issues": list(self.blocking_issues),
            "warnings": list(self.warnings),
        }


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _source_texts(spec: Any) -> tuple[list[str], str | None]:
    """Return scanned source/artifact texts and the script path (if found)."""
    repository = _field(spec, "repository", None)
    if not repository:
        return [], None
    root = Path(str(repository)).expanduser()
    texts: list[str] = []
    script_path: str | None = None
    script = root / "scripts" / "run_experiment.py"
    if script.is_file():
        script_path = str(script)
        texts.append(_read_text(script))
    outputs = root / "run_outputs"
    if outputs.is_dir():
        for path in sorted(outputs.glob("*.json"))[:20]:
            texts.append(_read_text(path))
    return texts, script_path


def _contains_marker(text: str, marker: str) -> bool:
    return marker.lower() in text.lower()


def _placeholder_hash(value: str) -> bool:
    """Reject format-valid but obviously fabricated identities.

    ``"0"*64`` and ``"a"*40`` satisfy a regex but cannot be a real content
    hash/commit. A real digest has many distinct characters.
    """
    lowered = value.strip().lower()
    if not lowered:
        return True
    if len(set(lowered)) <= 2:
        return True
    return lowered in {"0" * len(lowered), "1" * len(lowered)}


def _checksum_ok(value: str) -> bool:
    return bool(_CHECKSUM_RE.fullmatch(value.strip())) and not _placeholder_hash(value)


def _commit_ok(value: str) -> bool:
    return bool(_COMMIT_RE.fullmatch(value.strip())) and not _placeholder_hash(value)


def _baseline_names(spec: Any, report: Any) -> list[str]:
    """Distinct baseline identities, not metric names.

    ``ExperimentReport.baseline_results`` is production-shaped as
    ``analysis.baseline_metrics`` (metric -> value). Counting its keys would
    turn one baseline evaluated on two metrics into two baselines. Prefer the
    spec's ``baseline_configuration``; fall back to a structured report shape
    only when its values are themselves result mappings.
    """
    text = str(_field(spec, "baseline_configuration", "") or "").strip()
    if text:
        # The spec records one free-text baseline configuration. Count a
        # baseline only when a segment explicitly starts with a baseline/
        # control/comparison label; prose semicolons and "and" do not create
        # fake baselines.
        segments = [
            part.strip() for part in re.split(r"[;\n]\s*", text)
            if part.strip()
        ]
        labeled = [
            part for part in segments
            if re.match(r"^(?:baseline|control|comparison)\b", part, re.IGNORECASE)
        ]
        return list(dict.fromkeys(labeled)) or [text]
    value = _field(report, "baseline_results", None)
    if isinstance(value, Mapping):
        if any(isinstance(item, Mapping) for item in value.values()):
            return [str(key) for key, item in value.items() if str(key).strip() and item]
        if value:
            return ["configured baseline"]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _count_baselines(spec: Any, report: Any) -> int:
    return len(_baseline_names(spec, report))


def baseline_names(spec: Any, report: Any = None) -> list[str]:
    """Public accessor used by the publication contract."""
    return _baseline_names(spec, report)


def power_established(spec: Any, report: Any = None) -> bool:
    return _power_established(spec, report)


def power_text_established(text: Any) -> bool:
    lowered = str(text or "").lower()
    if "power" not in lowered:
        return False
    return not any(
        phrase in lowered
        for phrase in (
            "not established", "not computed", "not available",
            "not performed", "not conducted", "no power analysis",
            "power analysis was not",
        )
    )


def checksum_ok(value: Any) -> bool:
    return _checksum_ok(str(value or ""))


def commit_ok(value: Any) -> bool:
    return _commit_ok(str(value or ""))


def detect_study_type(spec: Any) -> str:
    """Cheap source-based study type for the shared contract."""
    texts, source_path = _source_texts(spec)
    joined = "\n".join(texts).lower()
    if any(marker.lower() in joined for marker in _STRONG_SIMULATION_MARKERS):
        return "simulation"
    if source_path and not any(marker.lower() in joined for marker in _HAS_DATA_IO_MARKERS):
        return "unverified"
    if any(marker.lower() in joined for marker in _WEAK_SIMULATION_MARKERS):
        return "unverified"
    return "empirical"


def dataset_names(spec: Any) -> list[str]:
    """Explicit datasets from a semicolon/newline-separated identity."""
    text = str(_field(spec, "dataset_identity", "") or "").strip()
    if not text:
        return []
    names = [part.strip() for part in re.split(r"[;\n]\s*", text) if part.strip()]
    return names or [text]


def _ablations_count(analysis: Any, report: Any) -> int:
    if analysis is not None:
        contributions = _field(analysis, "ablation_contributions", None) or {}
        if isinstance(contributions, Mapping) and contributions:
            return len(contributions)
    text = str(_field(report, "ablations", "") or "").lower() if report is not None else ""
    if not text or "not run" in text or "none" in text or "ablation_not_run" in text:
        return 0
    return 1


def _robustness_count(report: Any) -> int:
    if report is None:
        return 0
    text = str(_field(report, "robustness_tests", "") or "").lower()
    if not text or "not run" in text or "not claimed" in text or "none" in text:
        return 0
    return 1


def _power_established(spec: Any, report: Any) -> bool:
    text = " ".join(
        str(value or "")
        for value in (
            _field(spec, "statistical_plan", ""),
            _field(report, "uncertainty", ""),
            _field(report, "decision_evidence", ""),
        )
    ).lower()
    if "power" not in text:
        return False
    if any(
        phrase in text
        for phrase in (
            "not established",
            "not computed",
            "not available",
            "not performed",
            "not conducted",
            "no power analysis",
            "power analysis was not",
        )
    ):
        return False
    return True


def assess_publication_readiness(
    *,
    project_id: str,
    spec: Any | None,
    analysis: Any | None,
    report: Any | None,
    papers: list[dict[str, Any]],
    corpus_papers: list[dict[str, Any]],
    venue: VenueProfile | None = None,
    execution_verified: bool = False,
) -> ReadinessReport:
    """Assess whether the evidence chain supports a submission-grade paper.

    ``papers`` are the lineage-supporting papers; ``corpus_papers`` are the
    broader reviewed corpus. Neither is used to invent evidence.
    """
    profile = venue or get_venue_profile("generic_journal")
    texts, source_path = _source_texts(spec)
    joined = "\n".join(texts).lower()
    strong = tuple(marker for marker in _STRONG_SIMULATION_MARKERS if marker.lower() in joined)
    weak = tuple(marker for marker in _WEAK_SIMULATION_MARKERS if marker.lower() in joined)
    no_data_io = bool(source_path) and not any(
        marker.lower() in joined for marker in _HAS_DATA_IO_MARKERS
    )
    simulated = bool(strong)
    simulation_suspected = bool(strong or weak or no_data_io)

    checksum = str(_field(spec, "dataset_checksum", "") or "").strip()
    # ExperimentSpec stores this as ``baseline_commit``; older fixtures used
    # ``repository_commit``. Read both so production and tests agree.
    commit = str(
        _field(spec, "baseline_commit", "")
        or _field(spec, "repository_commit", "")
        or ""
    ).strip()
    checksum_ok = _checksum_ok(checksum)
    commit_ok = _commit_ok(commit)
    real_execution = bool(
        analysis is not None
        and report is not None
        and execution_verified
        and checksum_ok
        and commit_ok
        and source_path is not None
        and not simulation_suspected
    )

    warnings: list[str] = []
    if not source_path:
        warnings.append("no experiment source found; provenance is unverified")
    if not execution_verified:
        warnings.append("no execution result/worker job is bound to this analysis")
    if weak and not strong:
        warnings.append("possible simulation markers found (RNG usage); provenance requires human review")
    if no_data_io and not strong:
        warnings.append("experiment source never reads data; provenance requires human review")
    if not checksum_ok:
        warnings.append("dataset checksum is missing or not a content hash")
    if not commit_ok:
        warnings.append("repository commit is missing or not a commit hash")

    n_seeds = len(_field(analysis, "seed_level_results", ()) or ()) if analysis is not None else 0
    power = _power_established(spec, report)
    baselines = _count_baselines(spec, report)
    ablations = _ablations_count(analysis, report)
    robustness = _robustness_count(report)
    corpus_count = len({str(paper.get("id")) for paper in corpus_papers})
    reference_count = len({str(paper.get("id")) for paper in list(papers) + list(corpus_papers)})
    dataset_count = len(dataset_names(spec)) if spec is not None else 0
    decision = str(_field(report, "decision", "") or "INCOMPLETE") if report is not None else "INCOMPLETE"

    evidence = EvidenceSufficiency(
        n_seeds=n_seeds,
        power_established=power,
        external_baselines=baselines,
        ablations=ablations,
        robustness_tests=robustness,
        related_work_coverage=corpus_count,
        reference_count=reference_count,
        decision=decision,
        datasets=dataset_count,
    )

    blocking: list[str] = []
    if analysis is None or report is None:
        blocking.append("evidence_chain_incomplete")
    if simulated:
        blocking.append("simulated_evidence")
    if profile.require_real_execution:
        if not checksum_ok:
            blocking.append("dataset_checksum_missing")
        if not commit_ok:
            blocking.append("implementation_commit_missing")
        if not real_execution:
            blocking.append("real_execution_not_verified")
        if not execution_verified:
            blocking.append("execution_artifact_missing")
        if simulation_suspected and not simulated:
            # A real study may legitimately use continuous RNG for
            # augmentation; until a human verifies the source, it cannot be
            # marked submission-ready. This is a review gate, not a claim
            # that the study is simulated.
            blocking.append("provenance_unverified")
    if n_seeds < profile.min_seeds:
        blocking.append(f"seeds_below_minimum ({n_seeds} < {profile.min_seeds})")
    if profile.require_power and not power:
        blocking.append("power_not_established")
    if baselines < profile.min_baselines:
        blocking.append(f"external_baselines_missing ({baselines} < {profile.min_baselines})")
    if ablations < profile.min_ablations:
        blocking.append(f"ablations_missing ({ablations} < {profile.min_ablations})")
    if robustness < profile.min_robustness:
        blocking.append(f"robustness_missing ({robustness} < {profile.min_robustness})")
    if dataset_count < profile.min_datasets:
        blocking.append(f"datasets_below_minimum ({dataset_count} < {profile.min_datasets})")
    if corpus_count < profile.min_references:
        blocking.append(f"related_work_coverage_below_minimum ({corpus_count} < {profile.min_references})")

    submission_ready = not blocking
    if analysis is None or report is None:
        readiness = "not_ready"
    elif submission_ready:
        readiness = "submission_ready"
    elif simulated or (profile.require_real_execution and not real_execution) or (
        "provenance_unverified" in blocking
    ):
        readiness = "technical_report"
    else:
        readiness = "workshop_ready"

    return ReadinessReport(
        project_id=project_id,
        venue=profile.name,
        study_type="simulation" if simulated else ("unverified" if simulation_suspected else "empirical"),
        readiness=readiness,
        submission_ready=submission_ready,
        provenance=ProvenanceReport(
            simulated=simulated,
            simulation_suspected=simulation_suspected,
            real_execution=real_execution,
            dataset_checksum=checksum,
            repository_commit=commit,
            source_path=source_path,
            markers=strong + weak,
            warnings=tuple(warnings),
        ),
        evidence=evidence,
        blocking_issues=blocking,
        warnings=warnings,
    )

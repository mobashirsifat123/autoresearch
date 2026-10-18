"""Cross-stage publication contract.

Every skill writes one slice; every later skill reads the same contract. The
contract is deliberately explicit: a paper is not submission-ready because
the prose is polished, but because the review corpus, contribution, design,
and execution provenance satisfy the selected venue profile.

The contract never fabricates a missing field. It returns machine-readable
gaps with the stage that owns the fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from autoresearch.application.evidence_access import field as _field
from autoresearch.application.publication_readiness import (
    baseline_names,
    dataset_names,
    power_established,
)
from autoresearch.application.venue_profiles import VenueProfile, get_venue_profile


@dataclass(frozen=True)
class PublicationGap:
    code: str
    stage: str  # review | novelty | experiment | manuscript
    message: str
    fatal: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "fatal": self.fatal,
        }


@dataclass
class ContractSlice:
    stage: str
    complete: bool
    fields: dict[str, Any] = field(default_factory=dict)
    gaps: list[PublicationGap] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "complete": self.complete,
            "fields": dict(self.fields),
            "gaps": [gap.to_dict() for gap in self.gaps],
        }


@dataclass
class PublicationContract:
    project_id: str
    target_venue: str
    contribution_statement: str = ""
    novelty_delta: str = ""
    closest_prior_work: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()
    baselines: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    ablations: tuple[str, ...] = ()
    robustness_tests: tuple[str, ...] = ()
    power_analysis: str = ""
    power_established: bool = False
    required_seeds: int = 0
    related_work_coverage: int = 0
    full_text_ratio: float = 0.0
    execution_provenance: dict[str, Any] = field(default_factory=dict)
    reviewer_threshold: float = 0.0
    gaps: list[PublicationGap] = field(default_factory=list)

    @property
    def submission_ready(self) -> bool:
        return not any(gap.fatal for gap in self.gaps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "publication_contract",
            "project_id": self.project_id,
            "target_venue": self.target_venue,
            "contribution_statement": self.contribution_statement,
            "novelty_delta": self.novelty_delta,
            "closest_prior_work": list(self.closest_prior_work),
            "datasets": list(self.datasets),
            "baselines": list(self.baselines),
            "metrics": list(self.metrics),
            "ablations": list(self.ablations),
            "robustness_tests": list(self.robustness_tests),
            "power_analysis": self.power_analysis,
            "power_established": self.power_established,
            "required_seeds": self.required_seeds,
            "related_work_coverage": self.related_work_coverage,
            "full_text_ratio": self.full_text_ratio,
            "execution_provenance": dict(self.execution_provenance),
            "reviewer_threshold": self.reviewer_threshold,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "submission_ready": self.submission_ready,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PublicationContract":
        gaps = [
            PublicationGap(
                code=str(item.get("code") or ""),
                stage=str(item.get("stage") or ""),
                message=str(item.get("message") or ""),
                fatal=bool(item.get("fatal", True)),
            )
            for item in payload.get("gaps", [])
            if isinstance(item, Mapping) and item.get("code")
        ]
        return cls(
            project_id=str(payload.get("project_id") or ""),
            target_venue=str(payload.get("target_venue") or ""),
            contribution_statement=str(payload.get("contribution_statement") or ""),
            novelty_delta=str(payload.get("novelty_delta") or ""),
            closest_prior_work=tuple(str(item) for item in payload.get("closest_prior_work", [])),
            datasets=tuple(str(item) for item in payload.get("datasets", [])),
            baselines=tuple(str(item) for item in payload.get("baselines", [])),
            metrics=tuple(str(item) for item in payload.get("metrics", [])),
            ablations=tuple(str(item) for item in payload.get("ablations", [])),
            robustness_tests=tuple(str(item) for item in payload.get("robustness_tests", [])),
            power_analysis=str(payload.get("power_analysis") or ""),
            power_established=bool(payload.get("power_established")),
            required_seeds=int(payload.get("required_seeds") or 0),
            related_work_coverage=int(payload.get("related_work_coverage") or 0),
            full_text_ratio=float(payload.get("full_text_ratio") or 0.0),
            execution_provenance=dict(payload.get("execution_provenance") or {}),
            reviewer_threshold=float(payload.get("reviewer_threshold") or 0.0),
            gaps=gaps,
        )


def _methodology_payload(methodology: Any) -> dict[str, Any]:
    if methodology is None:
        return {}
    if isinstance(methodology, Mapping):
        block = methodology.get("methodology", methodology)
        return dict(block) if isinstance(block, Mapping) else {}
    return {}


def _review_closest_prior(review_corpus: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not review_corpus:
        return ()
    values = review_corpus.get("closest_prior_work") or review_corpus.get("top_paper_ids") or ()
    return tuple(str(item) for item in values)


def assess_contract(
    contract: PublicationContract, venue: VenueProfile | None = None
) -> list[PublicationGap]:
    """Return exact, stage-owned gaps. No gap is inferred from prose."""
    profile = venue or get_venue_profile(contract.target_venue or "generic_journal")
    gaps: list[PublicationGap] = []

    def add(code: str, stage: str, message: str, fatal: bool = True) -> None:
        gaps.append(PublicationGap(code, stage, message, fatal))

    if not contract.contribution_statement.strip():
        add("contribution_missing", "novelty", "The freeze has no one-sentence contribution statement.")
    if not contract.novelty_delta.strip():
        add("novelty_delta_missing", "novelty", "The freeze has no explicit delta from the closest prior work.")
    if not contract.closest_prior_work:
        add("closest_prior_work_missing", "review", "No closest prior work is recorded.")
    if contract.related_work_coverage < profile.min_references:
        add(
            "reviewed_papers_below_minimum",
            "review",
            f"reviewed papers {contract.related_work_coverage} < {profile.min_references}",
        )
    if contract.full_text_ratio and contract.full_text_ratio < 0.5:
        add(
            "full_text_ratio_below_threshold",
            "review",
            f"full-text ratio {contract.full_text_ratio:.2f} < 0.50",
        )
    if len(contract.baselines) < profile.min_baselines:
        add(
            "baselines_below_minimum",
            "experiment",
            f"baselines {len(contract.baselines)} < {profile.min_baselines}",
        )
    if len(contract.ablations) < profile.min_ablations:
        add(
            "ablations_missing",
            "experiment",
            f"ablations {len(contract.ablations)} < {profile.min_ablations}",
        )
    if len(contract.robustness_tests) < profile.min_robustness:
        add(
            "robustness_missing",
            "experiment",
            f"robustness tests {len(contract.robustness_tests)} < {profile.min_robustness}",
        )
    if profile.require_power and not contract.power_established:
        add("power_not_established", "experiment", "The design has no established power analysis.")
    if len(contract.datasets) < profile.min_datasets:
        add(
            "datasets_below_minimum",
            "experiment",
            f"datasets {len(contract.datasets)} < {profile.min_datasets}",
        )
    provenance = contract.execution_provenance or {}
    if profile.require_real_execution:
        if str(provenance.get("study_type") or "") == "simulation":
            add("simulated_study", "experiment", "Simulation evidence cannot satisfy an empirical venue.")
        if not provenance.get("dataset_checksum"):
            add("dataset_checksum_missing", "experiment", "No real dataset checksum is recorded.")
        if not provenance.get("baseline_commit"):
            add("baseline_commit_missing", "experiment", "No real baseline commit is recorded.")
        if not provenance.get("execution_verified"):
            add("execution_artifact_missing", "experiment", "No execution result is bound to the analysis.")
    return gaps


def build_contract(
    *,
    project_id: str,
    venue: VenueProfile | None = None,
    review_corpus: Mapping[str, Any] | None = None,
    candidate: Any | None = None,
    methodology: Any | None = None,
    spec: Any | None = None,
    analysis: Any | None = None,
    report: Any | None = None,
    execution_verified: bool = False,
    study_type: str = "empirical",
) -> PublicationContract:
    """Assemble one contract from the durable slices that exist."""
    profile = venue or get_venue_profile("generic_journal")
    block = _methodology_payload(methodology)
    contribution = str(block.get("contribution_statement") or _field(candidate, "method", "") or "")
    delta = str(block.get("novelty_delta") or _field(candidate, "difference_from_existing", "") or "")
    prior = _review_closest_prior(review_corpus)
    if not prior:
        prior = tuple(
            str(entry.get("paper_id"))
            for entry in block.get("closest_prior_methods", [])
            if isinstance(entry, Mapping) and entry.get("paper_id")
        )
    datasets = tuple(dataset_names(spec))
    if not datasets:
        configured = block.get("datasets")
        if isinstance(configured, list):
            datasets = tuple(str(item) for item in configured if str(item).strip())
    baselines = tuple(baseline_names(spec)) if spec is not None else ()
    if not baselines:
        configured = block.get("baselines")
        if isinstance(configured, list):
            baselines = tuple(str(item) for item in configured if str(item).strip())
    ablations = tuple(str(item) for item in (_field(spec, "ablations", ()) or ()) if str(item).strip())
    robustness = tuple(str(item) for item in (_field(spec, "robustness_tests", ()) or ()) if str(item).strip())
    if not ablations and isinstance(block.get("ablations"), list):
        ablations = tuple(str(item) for item in block["ablations"] if str(item).strip())
    if not robustness and isinstance(block.get("robustness_tests"), list):
        robustness = tuple(str(item) for item in block["robustness_tests"] if str(item).strip())
    metrics = tuple(str(item) for item in (_field(spec, "metrics", ()) or ()) if str(item).strip())
    required_seeds = int(_field(spec, "required_seed_count", 0) or 0)
    power_text = str(_field(spec, "statistical_plan", "") or block.get("power_analysis") or "")
    power = power_established(spec, report) if spec is not None else bool(block.get("power_analysis"))
    reviewed = int((review_corpus or {}).get("reviewed_papers") or 0)
    full_text_ratio = float((review_corpus or {}).get("full_text_ratio") or 0.0)
    provenance = {
        "execution_verified": bool(execution_verified),
        "analysis_id": str(_field(analysis, "id", "") or ""),
        "report_id": str(_field(report, "id", "") or ""),
        "dataset_checksum": str(_field(spec, "dataset_checksum", "") or ""),
        "baseline_commit": str(
            _field(spec, "baseline_commit", "") or _field(spec, "repository_commit", "") or ""
        ),
        "study_type": study_type,
    }
    contract = PublicationContract(
        project_id=project_id,
        target_venue=profile.name,
        contribution_statement=contribution,
        novelty_delta=delta,
        closest_prior_work=prior,
        datasets=datasets,
        baselines=baselines,
        metrics=metrics,
        ablations=ablations,
        robustness_tests=robustness,
        power_analysis=power_text,
        power_established=power,
        required_seeds=required_seeds,
        related_work_coverage=reviewed,
        full_text_ratio=full_text_ratio,
        execution_provenance=provenance,
        reviewer_threshold=profile.reviewer_threshold,
    )
    contract.gaps = assess_contract(contract, profile)
    return contract


def contract_slices(contract: PublicationContract) -> list[ContractSlice]:
    """Stage-owned slices for reporting and early-stop decisions."""
    stages = ("review", "novelty", "experiment", "manuscript")
    return [
        ContractSlice(
            stage=stage,
            complete=not any(gap.stage == stage and gap.fatal for gap in contract.gaps),
            fields={},
            gaps=[gap for gap in contract.gaps if gap.stage == stage],
        )
        for stage in stages
    ]

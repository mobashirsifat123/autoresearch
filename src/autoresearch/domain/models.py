"""Typed domain records shared by every AutoResearch pipeline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Sequence


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_checksum(value: Any) -> str:
    """Return a stable SHA-256 checksum for bytes, text, or JSON-compatible data."""
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class PipelineKind(StrEnum):
    DISCOVERY = "study_search"
    REVIEW = "deep_literature_review"
    NOVELTY = "novelty_generation"
    EXPERIMENT = "experiment_design_execution"
    REINVENTION = "result_conditioned_reinvention"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DownloadStatus(StrEnum):
    DOWNLOADED = "downloaded"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    FAILED = "failed"


class GapClassification(StrEnum):
    EVIDENCE_SUPPORTED = "evidence_supported"
    HYPOTHESIS = "hypothesis"


@dataclass(frozen=True)
class ArtifactProvenance:
    project_id: str
    run_id: str
    source_evidence: tuple[str, ...]
    provider: str
    model: str
    prompt_version: str
    created_at: str
    checksum: str

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        run_id: str,
        source_evidence: Sequence[str],
        provider: str,
        model: str,
        prompt_version: str,
        content: Any,
    ) -> "ArtifactProvenance":
        return cls(
            project_id=project_id,
            run_id=run_id,
            source_evidence=tuple(source_evidence),
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            created_at=utc_now(),
            checksum=content_checksum(content),
        )


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class PipelineRun:
    id: str
    project_id: str
    kind: PipelineKind
    status: RunStatus
    input_data: Mapping[str, Any]
    started_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SeedPaper:
    id: str
    project_id: str
    run_id: str
    input_type: str
    input_value: str
    title: str
    text: str
    topic: str
    research_question: str
    checksum: str
    provenance: ArtifactProvenance
    doi: str | None = None
    url: str | None = None
    methods: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class PaperRecord:
    id: str
    project_id: str
    run_id: str
    title: str
    abstract: str
    authors: tuple[str, ...]
    year: int | None
    publication_date: str | None
    doi: str | None
    arxiv_id: str | None
    url: str | None
    source_names: tuple[str, ...]
    open_access_pdf_url: str | None = None
    is_open_access: bool = False
    citation_count: int = 0
    venue: str = ""
    checksum: str = ""
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class SearchCandidate:
    id: str
    project_id: str
    run_id: str
    paper_id: str
    source: str
    strategy: str
    source_rank: int
    query: str
    raw_checksum: str
    retrieved_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class DownloadRecord:
    id: str
    project_id: str
    run_id: str
    paper_id: str
    status: DownloadStatus
    source: str
    url: str | None
    legal_basis: str | None
    license: str | None
    path: str | None
    reason: str | None
    checksum: str | None
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class EvidenceClaim:
    id: str
    project_id: str
    run_id: str
    paper_id: str
    claim_type: str
    statement: str
    excerpt: str
    page: int | None
    section: str | None
    confidence: float
    provenance: ArtifactProvenance

    def __post_init__(self) -> None:
        if not self.excerpt.strip():
            raise ValueError("Evidence claims require a non-empty source excerpt.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Evidence confidence must be between 0 and 1.")


@dataclass(frozen=True)
class PaperReview:
    id: str
    project_id: str
    run_id: str
    paper_id: str
    problem: str
    research_question: str
    method: str
    datasets: tuple[str, ...]
    experimental_protocol: str
    results: tuple[str, ...]
    ablations: tuple[str, ...]
    limitations: tuple[str, ...]
    failure_modes: tuple[str, ...]
    evidence_claim_ids: tuple[str, ...]
    confidence: float
    provenance: ArtifactProvenance

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Review confidence must be between 0 and 1.")
        if not self.evidence_claim_ids:
            raise ValueError("Paper reviews require at least one evidence claim.")


@dataclass(frozen=True)
class PriorityScore:
    id: str
    project_id: str
    run_id: str
    paper_id: str
    semantic_similarity: float
    methodological_similarity: float
    research_question_similarity: float
    recency: float
    influence: float
    evidence_quality: float
    total: float
    explanation: Mapping[str, Any]
    provenance: ArtifactProvenance

    def __post_init__(self) -> None:
        values = (self.semantic_similarity, self.methodological_similarity, self.research_question_similarity, self.recency, self.influence, self.evidence_quality, self.total)
        if any(not 0.0 <= value <= 1.0 for value in values):
            raise ValueError("Priority score components must be between 0 and 1.")


@dataclass(frozen=True)
class ResearchGap:
    id: str
    project_id: str
    run_id: str
    statement: str
    supporting_paper_ids: tuple[str, ...]
    supporting_claim_ids: tuple[str, ...]
    why_unsolved: str
    missing_experiment: str
    falsification_test: str
    confidence: float
    counter_evidence_claim_ids: tuple[str, ...]
    classification: GapClassification
    provenance: ArtifactProvenance

    def __post_init__(self) -> None:
        supported = bool(self.supporting_paper_ids and self.supporting_claim_ids)
        if self.classification is GapClassification.EVIDENCE_SUPPORTED and not supported:
            raise ValueError("An evidence-supported gap requires supporting papers and claims.")


@dataclass(frozen=True)
class ProviderUsage:
    id: str
    project_id: str | None
    run_id: str | None
    operation: str
    role: str
    provider: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    estimated_cost: float | None
    currency: str | None
    request_id: str | None
    latency_ms: float | None
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ArtifactReference:
    id: str
    project_id: str
    run_id: str
    artifact_type: str
    path: str
    source_evidence: tuple[str, ...]
    provider: str
    model: str
    prompt_version: str
    checksum: str
    created_at: str = field(default_factory=utc_now)


def dataclass_payload(value: Any) -> dict[str, Any]:
    """Serialize nested dataclasses and enums into a JSON-compatible mapping."""
    return json.loads(json.dumps(asdict(value), default=str))

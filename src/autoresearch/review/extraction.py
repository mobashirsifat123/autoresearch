"""Strict, evidence-backed per-paper structured review."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from autoresearch.domain.models import ArtifactProvenance, EvidenceClaim, PaperRecord, PaperReview
from autoresearch.ingestion.chunking import DocumentChunk
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

REVIEW_PROMPT_VERSION = "paper-review-v1"
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "problem": {"type": "string"},
        "research_question": {"type": "string"},
        "method": {"type": "string"},
        "datasets": _STRING_ARRAY,
        "experimental_protocol": {"type": "string"},
        "results": _STRING_ARRAY,
        "ablations": _STRING_ARRAY,
        "limitations": _STRING_ARRAY,
        "failure_modes": _STRING_ARRAY,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_spans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_type": {"type": "string"},
                    "statement": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "page": {"type": ["integer", "null"]},
                    "section": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["claim_type", "statement", "excerpt", "page", "section", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["problem", "research_question", "method", "datasets", "experimental_protocol", "results", "ablations", "limitations", "failure_modes", "evidence_spans", "confidence"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ReviewBundle:
    review: PaperReview
    claims: tuple[EvidenceClaim, ...]


class PaperReviewExtractor:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def extract(self, paper: PaperRecord, *, run_id: str, chunks: tuple[DocumentChunk, ...], fallback_text: str = "") -> ReviewBundle:
        source_text = "\n\n".join(f"[PAGE {chunk.page_start}; SECTION {chunk.section}]\n{chunk.text}" for chunk in chunks)
        if not source_text.strip():
            source_text = fallback_text or paper.abstract
        if not source_text.strip():
            raise ValueError(f"Paper '{paper.title}' has neither extractable PDF text nor abstract metadata.")
        payload = await self.router.structured_output(
            ModelRole.LITERATURE_EXTRACTION,
            messages=[
                {"role": "system", "content": "Act as a rigorous scientific reviewer. Every evidence item must quote an exact contiguous excerpt from the supplied source. Do not infer missing results."},
                {"role": "user", "content": f"Prompt version: {REVIEW_PROMPT_VERSION}\nPaper ID: {paper.id}\nTitle: {paper.title}\n\nSOURCE:\n{source_text[:100_000]}\n\nReturn one object with problem, research_question, method, datasets[], experimental_protocol, results[], ablations[], limitations[], failure_modes[], confidence, and evidence_spans[]. Each evidence span requires claim_type, statement, excerpt, page, section, confidence."},
            ],
            schema={"name": "paper_review", "schema": REVIEW_SCHEMA},
            project_id=paper.project_id,
            run_id=run_id,
            operation="paper_review",
        )
        provider, model = self.router.identity(ModelRole.LITERATURE_EXTRACTION)
        claims = self._claims(payload.get("evidence_spans"), paper, run_id, source_text, provider, model)
        if not claims:
            raise ValueError(f"Paper '{paper.title}' review did not contain a verifiable source excerpt.")
        review_content = {
            "problem": _required(payload, "problem"), "research_question": _required(payload, "research_question"),
            "method": _required(payload, "method"), "datasets": _strings(payload.get("datasets")),
            "experimental_protocol": _required(payload, "experimental_protocol"), "results": _strings(payload.get("results")),
            "ablations": _strings(payload.get("ablations")), "limitations": _strings(payload.get("limitations")),
            "failure_modes": _strings(payload.get("failure_modes")), "evidence_claim_ids": tuple(claim.id for claim in claims),
            "confidence": _confidence(payload.get("confidence")),
        }
        provenance = ArtifactProvenance.create(project_id=paper.project_id, run_id=run_id, source_evidence=tuple(claim.id for claim in claims), provider=provider, model=model, prompt_version=REVIEW_PROMPT_VERSION, content=review_content)
        review = PaperReview(uuid4().hex, paper.project_id, run_id, paper.id, review_content["problem"], review_content["research_question"], review_content["method"], review_content["datasets"], review_content["experimental_protocol"], review_content["results"], review_content["ablations"], review_content["limitations"], review_content["failure_modes"], review_content["evidence_claim_ids"], review_content["confidence"], provenance)
        return ReviewBundle(review, claims)

    @staticmethod
    def _claims(value: object, paper: PaperRecord, run_id: str, source_text: str, provider: str, model: str) -> tuple[EvidenceClaim, ...]:
        if not isinstance(value, list):
            return ()
        normalized_source = _normalize(source_text)
        claims: list[EvidenceClaim] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            excerpt = _string(raw.get("excerpt"))
            statement = _string(raw.get("statement"))
            if not excerpt or not statement or _normalize(excerpt) not in normalized_source:
                continue
            content = {"paper_id": paper.id, "statement": statement, "excerpt": excerpt, "page": raw.get("page"), "section": raw.get("section")}
            provenance = ArtifactProvenance.create(project_id=paper.project_id, run_id=run_id, source_evidence=(f"paper:{paper.id}",), provider=provider, model=model, prompt_version=REVIEW_PROMPT_VERSION, content=content)
            page = _page(raw.get("page"))
            claims.append(EvidenceClaim(uuid4().hex, paper.project_id, run_id, paper.id, _string(raw.get("claim_type")) or "finding", statement, excerpt, page, _string(raw.get("section")) or None, _confidence(raw.get("confidence")), provenance))
        return tuple(claims)


def _required(payload: dict[str, Any], key: str) -> str:
    value = _string(payload.get(key))
    if not value:
        raise ValueError(f"Paper review field '{key}' is required.")
    return value


def _string(value: object) -> str:
    return " ".join(str(value or "").split())


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for raw in value if (item := _string(raw)))


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.lower()))


def _confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _page(value: object) -> int | None:
    try:
        page = int(value)
        return page if page > 0 else None
    except (TypeError, ValueError):
        return None

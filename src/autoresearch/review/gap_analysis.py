"""Evidence-constrained research-gap generation and validation."""

from __future__ import annotations

import json
from typing import Sequence
from uuid import uuid4

from autoresearch.domain.models import ArtifactProvenance, EvidenceClaim, GapClassification, PaperRecord, PaperReview, PriorityScore, ResearchGap
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

GAP_PROMPT_VERSION = "evidence-gap-v1"
_ID_ARRAY = {"type": "array", "items": {"type": "string"}}
GAP_SCHEMA = {
    "type": "object",
    "properties": {
        "gap_statement": {"type": "string"},
        "supporting_paper_ids": _ID_ARRAY,
        "supporting_claim_ids": _ID_ARRAY,
        "why_existing_work_does_not_solve_it": {"type": "string"},
        "missing_experiment": {"type": "string"},
        "falsification_test": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "counter_evidence_claim_ids": _ID_ARRAY,
    },
    "required": ["gap_statement", "supporting_paper_ids", "supporting_claim_ids", "why_existing_work_does_not_solve_it", "missing_experiment", "falsification_test", "confidence", "counter_evidence_claim_ids"],
    "additionalProperties": False,
}


class GapAnalyzer:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def analyze(self, *, project_id: str, run_id: str, papers: Sequence[PaperRecord], reviews: Sequence[PaperReview], claims: Sequence[EvidenceClaim], scores: Sequence[PriorityScore]) -> ResearchGap:
        top_ids = {score.paper_id for score in scores}
        paper_map = {paper.id: paper for paper in papers if paper.id in top_ids}
        review_map = {review.paper_id: review for review in reviews if review.paper_id in top_ids}
        claim_map = {claim.id: claim for claim in claims if claim.paper_id in top_ids}
        if not paper_map or not claim_map:
            raise ValueError("Gap analysis requires ranked papers with validated evidence claims.")
        context = {
            "papers": [{"paper_id": paper.id, "title": paper.title, "year": paper.year, "review": {"method": review_map[paper.id].method, "limitations": review_map[paper.id].limitations, "failure_modes": review_map[paper.id].failure_modes}} for paper in paper_map.values() if paper.id in review_map],
            "claims": [{"claim_id": claim.id, "paper_id": claim.paper_id, "statement": claim.statement, "excerpt": claim.excerpt, "page": claim.page, "section": claim.section} for claim in claim_map.values()],
        }
        payload = await self.router.structured_output(
            ModelRole.SCIENTIFIC_CRITIC,
            messages=[
                {"role": "system", "content": "Identify one precise research gap using only the supplied paper and claim IDs. Include counter-evidence. Unsupported statements must be labeled as hypotheses."},
                {"role": "user", "content": f"Prompt version: {GAP_PROMPT_VERSION}\n{json.dumps(context, ensure_ascii=False)}\nReturn gap_statement, supporting_paper_ids[], supporting_claim_ids[], why_existing_work_does_not_solve_it, missing_experiment, falsification_test, confidence, counter_evidence_claim_ids[]."},
            ],
            schema={"name": "research_gap", "schema": GAP_SCHEMA},
            project_id=project_id,
            run_id=run_id,
            operation="gap_analysis",
        )
        provider, model = self.router.identity(ModelRole.SCIENTIFIC_CRITIC)
        return validate_gap_payload(payload, project_id=project_id, run_id=run_id, valid_papers=paper_map, valid_claims=claim_map, provider=provider, model=model)


def validate_gap_payload(payload: dict, *, project_id: str, run_id: str, valid_papers: dict[str, PaperRecord], valid_claims: dict[str, EvidenceClaim], provider: str, model: str) -> ResearchGap:
    paper_ids = tuple(dict.fromkeys(value for value in _ids(payload.get("supporting_paper_ids")) if value in valid_papers))
    claim_ids = tuple(dict.fromkeys(value for value in _ids(payload.get("supporting_claim_ids")) if value in valid_claims and valid_claims[value].paper_id in paper_ids))
    counter_ids = tuple(dict.fromkeys(value for value in _ids(payload.get("counter_evidence_claim_ids")) if value in valid_claims))
    classification = GapClassification.EVIDENCE_SUPPORTED if paper_ids and claim_ids else GapClassification.HYPOTHESIS
    content = {
        "statement": _required(payload, "gap_statement"), "supporting_paper_ids": paper_ids, "supporting_claim_ids": claim_ids,
        "why_unsolved": _required(payload, "why_existing_work_does_not_solve_it"), "missing_experiment": _required(payload, "missing_experiment"),
        "falsification_test": _required(payload, "falsification_test"), "counter_evidence_claim_ids": counter_ids, "classification": classification.value,
    }
    source_evidence = (*claim_ids, *counter_ids)
    provenance = ArtifactProvenance.create(project_id=project_id, run_id=run_id, source_evidence=source_evidence, provider=provider, model=model, prompt_version=GAP_PROMPT_VERSION, content=content)
    return ResearchGap(uuid4().hex, project_id, run_id, content["statement"], paper_ids, claim_ids, content["why_unsolved"], content["missing_experiment"], content["falsification_test"], _confidence(payload.get("confidence")), counter_ids, classification, provenance)


def _ids(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def _required(payload: dict, key: str) -> str:
    value = " ".join(str(payload.get(key) or "").split())
    if not value:
        raise ValueError(f"Gap field '{key}' is required.")
    return value


def _confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0

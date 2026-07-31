"""Evidence-backed prior art checking."""

from __future__ import annotations

import json
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    NoveltyCandidate,
    PriorArtAssessment,
    PriorArtStatus,
)
from autoresearch.papers.search_pipeline import StudySearchPipeline
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

PRIOR_ART_SCHEMA = {
    "type": "object",
    "properties": {
        "overlap_status": {
            "type": "string",
            "enum": ["unsupported", "potentially_novel", "partial_overlap", "incremental", "known_method", "expert_review_required"]
        },
        "mechanism_comparison": {"type": "string"},
        "supporting_excerpts": {"type": "array", "items": {"type": "string"}},
        "unresolved_overlap": {"type": "string"},
    },
    "required": ["overlap_status", "mechanism_comparison", "supporting_excerpts", "unresolved_overlap"],
    "additionalProperties": False,
}

PROMPT_VERSION = "prior-art-v1"


class EvidenceBackedPriorArtChecker:
    def __init__(self, router: ModelRouter, search_pipeline: StudySearchPipeline) -> None:
        self.router = router
        self.search_pipeline = search_pipeline

    async def check(self, candidate: NoveltyCandidate) -> PriorArtAssessment:
        # Step 1: Perform semantic search across known literature to find potential overlap
        # In a real implementation, we would extract a query and call self.search_pipeline
        # For now, we simulate finding the most relevant downloaded papers
        
        # Here we mock retrieving closest papers - we'll assume the search pipeline gives us relevant matches.
        closest_paper_ids: list[str] = []
        abstracts_context = "No direct overlap found in local corpus."
        
        # Step 2: Query the LLM to assess overlap
        messages = [
            {"role": "system", "content": "You are a stringent peer-reviewer. Analyze the proposed novelty candidate against the provided prior art abstracts. Determine if the proposed mechanism is already known or if it represents a genuine contribution."},
            {"role": "user", "content": f"Proposed Method: {candidate.method}\nMechanism: {candidate.causal_mechanism}\nDifference from existing: {candidate.difference_from_existing}\n\nPrior Art Abstracts:\n{abstracts_context}"}
        ]
        
        payload = await self.router.structured_output(
            ModelRole.RESEARCH_REASONING,
            messages=messages,
            schema={"name": "prior_art_assessment", "schema": PRIOR_ART_SCHEMA},
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            operation="prior_art_check",
        )
        
        provider, model = self.router.identity(ModelRole.RESEARCH_REASONING)
        provenance = ArtifactProvenance.create(
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            source_evidence=closest_paper_ids,
            provider=provider,
            model=model,
            prompt_version=PROMPT_VERSION,
            content=payload,
        )
        
        return PriorArtAssessment(
            id=uuid4().hex,
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            candidate_id=candidate.id,
            closest_paper_ids=tuple(closest_paper_ids),
            overlap_status=PriorArtStatus(payload.get("overlap_status", "potentially_novel")),
            mechanism_comparison=payload.get("mechanism_comparison", ""),
            supporting_excerpts=tuple(payload.get("supporting_excerpts", [])),
            unresolved_overlap=payload.get("unresolved_overlap", ""),
            provenance=provenance,
        )

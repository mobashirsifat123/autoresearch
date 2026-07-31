"""Evidence-backed prior art checking."""

from __future__ import annotations

import re
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
        # Search the project's verified evidence corpus. Ranking is deterministic
        # lexical retrieval; the model compares mechanisms only after provenance
        # records have been selected. Page/section references are never invented.
        repository = getattr(self.search_pipeline, "repository", None)
        terms = set(re.findall(r"[a-z0-9]{3,}", f"{candidate.method} {candidate.causal_mechanism} {candidate.difference_from_existing}".lower()))
        ranked: list[tuple[int, dict]] = []
        if repository is not None:
            for claim in repository.rows(
                "SELECT e.*,p.title,p.year,p.venue FROM evidence_claims e JOIN papers p ON p.id=e.paper_id WHERE e.project_id=?",
                (candidate.project_id,),
            ):
                evidence_terms = set(re.findall(r"[a-z0-9]{3,}", f"{claim['statement']} {claim['excerpt']} {claim.get('section') or ''}".lower()))
                ranked.append((len(terms & evidence_terms), claim))
        selected = [claim for score, claim in sorted(ranked, key=lambda item: (-item[0], item[1]["id"])) if score > 0][:12]
        closest_paper_ids = list(dict.fromkeys(claim["paper_id"] for claim in selected))
        evidence_context = "\n".join(
            f"[{claim['paper_id']}] {claim['title']} ({claim.get('year') or 'n.d.'}), section={claim.get('section') or 'unknown'}, page={claim.get('page') or 'unknown'}: {claim['excerpt']}"
            for claim in selected
        ) or "No matching verified evidence was retrieved from the project corpus. Retrieval coverage is incomplete."
        
        # Step 2: Query the LLM to assess overlap
        messages = [
            {"role": "system", "content": "You are a stringent prior-art analyst. Compare mechanisms only against the supplied verified excerpts. Preserve ambiguity and counter-evidence. Never claim exhaustive novelty."},
            {"role": "user", "content": f"Proposed Method: {candidate.method}\nMechanism: {candidate.causal_mechanism}\nDifference from existing: {candidate.difference_from_existing}\n\nPrior Art Abstracts and Section Evidence:\n{evidence_context}"}
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

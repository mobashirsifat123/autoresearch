"""Transparent top-k review ranking with router-only embeddings."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Sequence
from uuid import uuid4

from autoresearch.domain.models import ArtifactProvenance, PaperRecord, PaperReview, PriorityScore, SeedPaper
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

RANKING_VERSION = "priority-ranking-v1"
WEIGHTS = {
    "semantic_similarity": 0.35,
    "methodological_similarity": 0.20,
    "research_question_similarity": 0.15,
    "recency": 0.15,
    "influence": 0.10,
    "evidence_quality": 0.05,
}


class ReviewRanker:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def rank(self, *, seed: SeedPaper, papers: Sequence[PaperRecord], reviews: Sequence[PaperReview], run_id: str) -> list[PriorityScore]:
        review_by_paper = {review.paper_id: review for review in reviews}
        usable = [paper for paper in papers if paper.id in review_by_paper]
        if not usable:
            return []
        semantic_texts = [f"{seed.title} {seed.topic} {seed.text[:5000]}"] + [f"{paper.title} {paper.abstract} {review_by_paper[paper.id].problem}" for paper in usable]
        method_texts = [" ".join(seed.methods)] + [review_by_paper[paper.id].method for paper in usable]
        question_texts = [seed.research_question] + [review_by_paper[paper.id].research_question for paper in usable]
        embedded = await self.router.embed([*semantic_texts, *method_texts, *question_texts], project_id=seed.project_id, run_id=run_id, operation="review_priority_embeddings")
        count = len(usable) + 1
        semantic = embedded.vectors[:count]
        methods = embedded.vectors[count:count * 2]
        questions = embedded.vectors[count * 2:count * 3]
        maximum_citations = max((paper.citation_count for paper in usable), default=1) or 1
        current_year = datetime.now(timezone.utc).year
        provider, model = self.router.identity(ModelRole.EMBEDDINGS)
        scores: list[PriorityScore] = []
        for index, paper in enumerate(usable, start=1):
            review = review_by_paper[paper.id]
            components = {
                "semantic_similarity": _unit_cosine(semantic[0], semantic[index]),
                "methodological_similarity": _unit_cosine(methods[0], methods[index]),
                "research_question_similarity": _unit_cosine(questions[0], questions[index]),
                "recency": math.exp(-max(0, current_year - (paper.year or current_year - 10)) / 4.0),
                "influence": math.log1p(paper.citation_count) / math.log1p(maximum_citations),
                "evidence_quality": min(1.0, review.confidence * (0.6 + min(len(review.evidence_claim_ids), 4) * 0.1)),
            }
            components = {key: round(max(0.0, min(1.0, value)), 6) for key, value in components.items()}
            total = round(sum(components[key] * WEIGHTS[key] for key in WEIGHTS), 6)
            explanation = {"weights": WEIGHTS, "components": components, "formula": "weighted_sum"}
            provenance = ArtifactProvenance.create(project_id=seed.project_id, run_id=run_id, source_evidence=review.evidence_claim_ids, provider=provider, model=model, prompt_version=RANKING_VERSION, content={**components, "total": total})
            scores.append(PriorityScore(uuid4().hex, seed.project_id, run_id, paper.id, components["semantic_similarity"], components["methodological_similarity"], components["research_question_similarity"], components["recency"], components["influence"], components["evidence_quality"], total, explanation, provenance))
        return sorted(scores, key=lambda score: (-score.total, score.paper_id))


def _unit_cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return (max(-1.0, min(1.0, dot / (left_norm * right_norm))) + 1.0) / 2.0

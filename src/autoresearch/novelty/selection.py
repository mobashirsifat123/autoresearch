"""Portfolio selection mapping risk profiles to candidates."""

from __future__ import annotations

from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    CandidateStatus,
    NoveltyCandidate,
    NoveltyCritique,
    NoveltyPortfolio,
)
from autoresearch.storage.repository import ResearchRepository


class PortfolioSelector:
    def __init__(self, repository: ResearchRepository) -> None:
        self.repository = repository

    def select(self, project_id: str, run_id: str, candidates: list[NoveltyCandidate], critiques: list[NoveltyCritique]) -> NoveltyPortfolio:
        if not candidates:
            raise ValueError("Portfolio selection requires at least one candidate.")

        # Match candidates to their critiques
        scored_candidates = []
        for candidate in candidates:
            critique = next((c for c in critiques if c.candidate_id == candidate.id), None)
            if critique:
                scored_candidates.append((candidate, critique))

        if not scored_candidates:
            raise ValueError("No candidate passed the scientific gates.")

        # Sort by expected compute efficiency (proxy for low risk/fast execution)
        low_risk = sorted(scored_candidates, key=lambda x: x[1].compute_efficiency_score + x[1].feasibility_score, reverse=True)[0]
        
        # Sort by balance (publication potential + feasibility)
        balanced = sorted(scored_candidates, key=lambda x: x[1].publication_potential_score + x[1].feasibility_score, reverse=True)[0] if len(scored_candidates) > 1 else None
        
        # Sort by high risk (novelty + significance - feasibility)
        high_risk = sorted(scored_candidates, key=lambda x: x[1].novelty_score + x[1].significance_score - x[1].feasibility_score, reverse=True)[0] if len(scored_candidates) > 2 else None

        # Make sure they are unique if possible
        if balanced and balanced[0].id == low_risk[0].id:
            remaining = [c for c in scored_candidates if c[0].id != low_risk[0].id]
            if remaining:
                balanced = sorted(remaining, key=lambda x: x[1].publication_potential_score + x[1].feasibility_score, reverse=True)[0]

        if high_risk and high_risk[0].id in (low_risk[0].id, balanced[0].id if balanced else None):
            remaining = [c for c in scored_candidates if c[0].id not in (low_risk[0].id, balanced[0].id if balanced else None)]
            if remaining:
                high_risk = sorted(remaining, key=lambda x: x[1].novelty_score + x[1].significance_score - x[1].feasibility_score, reverse=True)[0]

        provenance = ArtifactProvenance.create(
            project_id=project_id,
            run_id=run_id,
            source_evidence=[c[0].id for c in scored_candidates],
            provider="deterministic",
            model="heuristic",
            prompt_version="portfolio-selector-v1",
            content={"strategy": "risk_balancing"},
        )

        portfolio = NoveltyPortfolio(
            id=uuid4().hex,
            project_id=project_id,
            run_id=run_id,
            low_risk_candidate_id=low_risk[0].id,
            balanced_candidate_id=balanced[0].id if balanced else None,
            high_risk_candidate_id=high_risk[0].id if high_risk else None,
            selection_reasons={
                "low_risk": "Highest feasibility and compute efficiency.",
                **({"balanced": "Strong trade-off between publication potential and feasibility."} if balanced else {}),
                **({"high_risk": "High information gain with explicitly higher risk."} if high_risk else {}),
            },
            provenance=provenance,
        )

        for candidate, _ in tuple(item for item in (low_risk, balanced, high_risk) if item is not None):
            self.repository.update_novelty_candidate_status(candidate.id, CandidateStatus.PORTFOLIO_SELECTED)

        return portfolio

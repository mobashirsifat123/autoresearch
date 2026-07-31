"""Orchestrator for the Novelty Pipeline."""

from __future__ import annotations

import logging

from autoresearch.domain.models import ResearchGap, NoveltyPortfolio
from autoresearch.novelty.candidate_generator import LLMCandidateGenerator
from autoresearch.novelty.novelty_critic import ScientificCritic
from autoresearch.novelty.prior_art_checker import EvidenceBackedPriorArtChecker
from autoresearch.novelty.selection import PortfolioSelector
from autoresearch.storage.repository import ResearchRepository

logger = logging.getLogger(__name__)


class NoveltyPipeline:
    def __init__(
        self,
        repository: ResearchRepository,
        generator: LLMCandidateGenerator,
        prior_art_checker: EvidenceBackedPriorArtChecker,
        critic: ScientificCritic,
        selector: PortfolioSelector,
    ) -> None:
        self.repository = repository
        self.generator = generator
        self.prior_art_checker = prior_art_checker
        self.critic = critic
        self.selector = selector

    async def generate_portfolio(self, gap: ResearchGap) -> NoveltyPortfolio:
        logger.info(f"Starting novelty generation for gap {gap.id}")
        
        # 1. Generate Candidates
        candidates = await self.generator.generate(gap, count=5)
        for candidate in candidates:
            self.repository.save_novelty_candidate(candidate)
            
        logger.info(f"Generated {len(candidates)} candidates.")

        valid_candidates = []
        critiques = []
        
        # 2. Check Prior Art and Criticize
        for candidate in candidates:
            assessment = await self.prior_art_checker.check(candidate)
            self.repository.save_prior_art_assessment(assessment)
            
            # Simple rejection for known method
            if assessment.overlap_status.value in ("known_method", "incremental"):
                logger.info(f"Candidate {candidate.id} rejected due to overlap: {assessment.overlap_status}")
                continue

            critique = await self.critic.evaluate(candidate)
            self.repository.save_novelty_critique(critique)
            
            # We could do revision loops here, but for milestone 1 we'll stick to a straight-through approach.
            if critique.novelty_score < 0.4:
                logger.info(f"Candidate {candidate.id} rejected by critic (novelty: {critique.novelty_score})")
                continue
                
            valid_candidates.append(candidate)
            critiques.append(critique)

        if len(valid_candidates) < 3:
            raise RuntimeError("Novelty pipeline failed to produce at least 3 valid candidates.")

        # 3. Portfolio Selection
        portfolio = self.selector.select(gap.project_id, gap.run_id, valid_candidates, critiques)
        self.repository.save_novelty_portfolio(portfolio)
        
        logger.info("Portfolio selected successfully.")
        return portfolio

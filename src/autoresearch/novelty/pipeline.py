"""Orchestrator for the Novelty Pipeline.
Now with ensemble critique, iterative refinement (up to 2 rounds),
and diversity-aware portfolio selection."""

from __future__ import annotations

import logging

from uuid import uuid4

from autoresearch.domain.models import ArtifactProvenance, NoveltyClaimLedger, ResearchGap, NoveltyPortfolio
from autoresearch.novelty.candidate_generator import LLMCandidateGenerator
from autoresearch.novelty.novelty_critic import ScientificCritic
from autoresearch.novelty.prior_art_checker import EvidenceBackedPriorArtChecker
from autoresearch.novelty.selection import PortfolioSelector
from autoresearch.storage.repository import ResearchRepository

logger = logging.getLogger(__name__)

# Maximum revision rounds for candidates with partial overlap
MAX_REVISION_ROUNDS = 2


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

        # 2. Check Prior Art and Criticize (with iterative refinement)
        for candidate in candidates:
            assessment = await self.prior_art_checker.check(candidate)
            self.repository.save_prior_art_assessment(assessment)

            # Confirmed known methods remain useful as baselines but cannot be
            # promoted as novelty candidates. Incremental work remains eligible.
            if assessment.overlap_status.value == "known_method":
                logger.info(f"Candidate {candidate.id} rejected due to overlap: {assessment.overlap_status}")
                continue

            critique = await self.critic.evaluate(candidate)
            self.repository.save_novelty_critique(critique)

            # Iterative refinement: up to MAX_REVISION_ROUNDS for partial overlap
            for revision_round in range(1, MAX_REVISION_ROUNDS + 1):
                if assessment.overlap_status.value != "partial_overlap":
                    break

                logger.info(f"Revision round {revision_round}/{MAX_REVISION_ROUNDS} for candidate {candidate.id}")
                original = candidate
                revised, revision = await self.generator.revise(
                    original, assessment, critique, revision_count=revision_round,
                )
                self.repository.save_novelty_candidate(revised)
                self.repository.save_candidate_revision(revision)
                reassessment = await self.prior_art_checker.check(revised)
                self.repository.save_prior_art_assessment(reassessment)
                candidate = revised
                assessment = reassessment
                critique = await self.critic.evaluate(revised)
                self.repository.save_novelty_critique(critique)

                # If no longer partial overlap, stop revising
                if assessment.overlap_status.value != "partial_overlap":
                    break

            hard_gates = (
                bool(candidate.difference_from_existing.strip()),
                bool(candidate.falsification_condition.strip()),
                bool(candidate.minimum_validating_experiment.strip()),
                assessment.overlap_status.value not in ("unsupported", "expert_review_required"),
                critique.falsifiability_score > 0,
                critique.experimental_discriminability_score > 0,
                critique.feasibility_score > 0,
            )
            if not all(hard_gates):
                logger.info("Candidate %s did not pass scientific gates", candidate.id)
                continue

            valid_candidates.append(candidate)
            critiques.append(critique)
            safe_wording = "Appears distinct under the current retrieved evidence; requires expert verification."
            ledger = NoveltyClaimLedger(
                id=uuid4().hex, project_id=candidate.project_id, run_id=candidate.run_id,
                candidate_id=candidate.id, contribution_claim=candidate.difference_from_existing,
                closest_methods=assessment.closest_paper_ids, overlap_status=assessment.overlap_status,
                supporting_evidence=assessment.supporting_excerpts,
                unresolved_uncertainty=assessment.unresolved_overlap,
                safe_wording=safe_wording,
                prohibited_wording=("proven novel", "definitively novel", "first-ever"),
                review_status="expert_review_required" if assessment.overlap_status.value == "expert_review_required" else "retrieval_assessed",
                provenance=ArtifactProvenance.create(
                    project_id=candidate.project_id, run_id=candidate.run_id,
                    source_evidence=[assessment.id, critique.id],
                    provider="deterministic", model="claim-policy",
                    prompt_version="claim-ledger-v1",
                    content={"safe_wording": safe_wording, "status": assessment.overlap_status.value},
                ),
            )
            self.repository.save_novelty_claim_ledger(ledger)

        if not valid_candidates:
            raise RuntimeError("Novelty pipeline produced no candidate that passed the scientific gates.")

        # 3. Portfolio Selection (diversity-aware)
        portfolio = self.selector.select(gap.project_id, gap.run_id, valid_candidates, critiques)
        self.repository.save_novelty_portfolio(portfolio)

        logger.info("Portfolio selected successfully.")
        return portfolio

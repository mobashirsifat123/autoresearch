from __future__ import annotations

import pytest

from autoresearch.domain.models import PaperRecord
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.config import ModelAssignment
from autoresearch.providers.registry import ProviderRegistry
from autoresearch.providers.router import ModelRouter
from autoresearch.providers.testing import FakeLLMProvider
from autoresearch.review.extraction import PaperReviewExtractor


@pytest.mark.asyncio
async def test_review_keeps_only_exact_source_excerpts():
    response = {
        "problem": "Robustness", "research_question": "Does adaptation help?", "method": "Adaptive fusion", "datasets": ["Dataset-A"],
        "experimental_protocol": "Matched evaluation", "results": ["Improved robustness"], "ablations": [], "limitations": ["One dataset"], "failure_modes": [], "confidence": 0.8,
        "evidence_spans": [
            {"claim_type": "result", "statement": "Improves robustness", "excerpt": "Adaptive fusion improves robustness.", "page": 1, "section": "Results", "confidence": 0.9},
            {"claim_type": "result", "statement": "Fabricated", "excerpt": "This sentence is absent.", "page": 1, "section": "Results", "confidence": 0.9},
        ],
    }
    provider = FakeLLMProvider(responses=[response])
    registry = ProviderRegistry(); registry.register_llm("fake", provider)
    router = ModelRouter(registry, {ModelRole.LITERATURE_EXTRACTION: ModelAssignment(ModelRole.LITERATURE_EXTRACTION, "fake", "fake-model")})
    paper = PaperRecord("paper", "project", "discovery", "Paper", "Adaptive fusion improves robustness.", (), 2025, "2025-01-01", None, None, None, ("test",))
    bundle = await PaperReviewExtractor(router).extract(paper, run_id="review", chunks=(), fallback_text=paper.abstract)
    assert len(bundle.claims) == 1
    claim = bundle.claims[0]
    assert claim.excerpt == "Adaptive fusion improves robustness."
    assert claim.provenance.project_id == "project"
    assert claim.provenance.run_id == "review"
    assert claim.provenance.provider == "fake"
    assert len(claim.provenance.checksum) == 64
    assert bundle.review.evidence_claim_ids == (claim.id,)


@pytest.mark.asyncio
async def test_hierarchical_groups_review_concurrently_and_survive_partial_group_failure():
    """Long-PDF groups must run in parallel (not one-by-one) and one group
    failing must not discard the evidence of the surviving groups."""
    import asyncio

    from autoresearch.ingestion.chunking import DocumentChunk

    marker = "Adaptive fusion improves robustness."
    filler = "Filler sentence padding the chunk past one group. " * 700  # ~32k chars
    response = {
        "problem": "Robustness", "research_question": "Does adaptation help?", "method": "Adaptive fusion", "datasets": ["Dataset-A"],
        "experimental_protocol": "Matched evaluation", "results": ["Improved robustness"], "ablations": [], "limitations": ["One dataset"], "failure_modes": [], "confidence": 0.8,
        "evidence_spans": [
            {"claim_type": "result", "statement": "Improves robustness", "excerpt": marker, "page": 1, "section": "Results", "confidence": 0.9},
        ],
    }

    class Tracking(FakeLLMProvider):
        def __init__(self, responder):
            super().__init__(responder=responder)
            self.inflight = 0
            self.max_inflight = 0

        async def complete(self, **kwargs):
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                await asyncio.sleep(0.02)
                return await super().complete(**kwargs)
            finally:
                self.inflight -= 1

    def responder(messages):
        user = next(item["content"] for item in messages if item["role"] == "user")
        if "[PAGE 2;" in user:  # the second source group's provider fails hard
            raise RuntimeError("group 2 provider failure")
        return response

    provider = Tracking(responder)
    registry = ProviderRegistry(); registry.register_llm("fake", provider)
    router = ModelRouter(registry, {ModelRole.LITERATURE_EXTRACTION: ModelAssignment(ModelRole.LITERATURE_EXTRACTION, "fake", "fake-model")})
    paper = PaperRecord("paper", "project", "discovery", "Paper", marker, (), 2025, "2025-01-01", None, None, None, ("test",))
    chunks = tuple(
        DocumentChunk(f"chunk-{page}", page, page, "Body", f"{marker} {filler}")
        for page in (1, 2)
    )
    bundle = await PaperReviewExtractor(router).extract(paper, run_id="review", chunks=chunks, hierarchical=True)
    assert bundle.claims, "surviving groups must still produce verbatim-backed evidence"
    assert all(claim.excerpt == marker for claim in bundle.claims)
    assert provider.max_inflight == 2, "one call per section, with independent sections overlapping"

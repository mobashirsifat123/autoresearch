from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pypdf import PdfWriter

from autoresearch.application.services import DiscoveryService, LiteratureReviewService
from autoresearch.domain.models import DownloadStatus, GapClassification, content_checksum
from autoresearch.papers.metadata_sources import MetadataPaper, SourceSearchResult
from autoresearch.papers.pdf_resolver import DownloadedPdf, PdfResolution
from autoresearch.papers.search_pipeline import StudySearchPipeline
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.config import ModelAssignment
from autoresearch.providers.registry import ProviderRegistry
from autoresearch.providers.router import ModelRouter
from autoresearch.providers.testing import FakeEmbeddingProvider, FakeLLMProvider
from autoresearch.storage import ResearchRepository


class DeterministicMetadataSource:
    name = "deterministic_source"

    async def search(self, query: str, *, limit: int, since_year: int | None = None) -> SourceSearchResult:
        papers = tuple(
            MetadataPaper(
                title="Seed Adaptive Fusion" if index == 0 else f"Adaptive Fusion Study {index}",
                abstract=f"Study {index} evaluates adaptive fusion on Dataset-A. Adaptive fusion improves robustness. Evaluation is limited to one dataset.",
                authors=(f"Researcher {index}",), year=2026 - (index % 4), publication_date=f"{2026 - (index % 4)}-01-01",
                doi=f"10.5555/study.{index}", url=f"https://example.org/study/{index}",
                open_access_pdf_url=f"https://oa.example.org/study-{index}.pdf", is_open_access=True,
                citation_count=index * 2, venue="Test Venue", source=self.name, external_id=str(index),
            )
            for index in range(25)
        )
        return SourceSearchResult(self.name, query, papers[:limit], content_checksum({"query": query, "count": len(papers[:limit])}))

    async def close(self) -> None:
        return None


class DeterministicPdfResolver:
    async def resolve(self, paper: MetadataPaper) -> PdfResolution:
        index = int((paper.doi or "0").rsplit(".", 1)[-1])
        if index % 2:
            return PdfResolution(False, None, "test_oa_registry", None, None, "No legal open-access PDF location")
        return PdfResolution(True, paper.open_access_pdf_url, "test_oa_registry", "Test fixture open-access declaration", "cc-by", None)

    async def download(self, resolution: PdfResolution, target: Path) -> DownloadedPdf:
        payload = b"%PDF-1.4\n% deterministic fixture\n%%EOF"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return DownloadedPdf(target, content_checksum(payload), len(payload))

    async def close(self) -> None:
        return None


def _responder(messages):
    prompt = str(messages[-1]["content"])
    if "seed-analysis-v1" in prompt:
        return {"title": "Seed Adaptive Fusion", "topic": "robust adaptive fusion", "research_question": "Can adaptive fusion improve cross-domain robustness?", "methods": ["adaptive fusion"], "datasets": ["Dataset-A"], "citations": [], "limitations": ["single-domain evaluation"], "keywords": ["robustness", "domain shift", "fusion"]}
    if "search-strategies-v1" in prompt:
        return {"strategies": [{"name": "topic", "query": "adaptive fusion robustness"}, {"name": "method", "query": "adaptive fusion domain shift"}, {"name": "question", "query": "cross-domain robust fusion"}, {"name": "limitation", "query": "single-domain evaluation limitation"}]}
    if "paper-review-v1" in prompt:
        return {
            "problem": "Cross-domain robustness", "research_question": "Does adaptive fusion improve robustness?", "method": "Adaptive fusion",
            "datasets": ["Dataset-A"], "experimental_protocol": "Single-dataset controlled evaluation", "results": ["Improved robustness"],
            "ablations": ["Fusion removed"], "limitations": ["Evaluation uses one dataset"], "failure_modes": ["Unverified domain transfer"], "confidence": 0.86,
            "evidence_spans": [
                {"claim_type": "result", "statement": "Adaptive fusion improves robustness.", "excerpt": "Adaptive fusion improves robustness.", "page": None, "section": "abstract", "confidence": 0.9},
                {"claim_type": "limitation", "statement": "Evaluation is limited to one dataset.", "excerpt": "Evaluation is limited to one dataset.", "page": None, "section": "abstract", "confidence": 0.95},
            ],
        }
    if "evidence-gap-v1" in prompt:
        claim_ids = re.findall(r'"claim_id":\s*"([a-f0-9]{32})"', prompt)
        paper_ids = list(dict.fromkeys(re.findall(r'"paper_id":\s*"([a-f0-9]{24})"', prompt)))
        return {"gap_statement": "Adaptive fusion lacks matched cross-domain validation.", "supporting_paper_ids": paper_ids[:2], "supporting_claim_ids": claim_ids[:2], "why_existing_work_does_not_solve_it": "The highest-priority studies evaluate only one dataset.", "missing_experiment": "A parameter-matched evaluation across three held-out domains.", "falsification_test": "Reject the gap if existing matched multi-domain evidence shows consistent gains across three domains.", "confidence": 0.88, "counter_evidence_claim_ids": claim_ids[2:3]}
    raise AssertionError(f"Unexpected provider prompt: {prompt[:120]}")


def _router(repository):
    registry = ProviderRegistry()
    registry.register_llm("fake", FakeLLMProvider(responder=_responder))
    registry.register_embeddings("fake_embeddings", FakeEmbeddingProvider())
    assignments = {
        ModelRole.LITERATURE_EXTRACTION: ModelAssignment(ModelRole.LITERATURE_EXTRACTION, "fake", "fake-model"),
        ModelRole.RESEARCH_REASONING: ModelAssignment(ModelRole.RESEARCH_REASONING, "fake", "fake-model"),
        ModelRole.SCIENTIFIC_CRITIC: ModelAssignment(ModelRole.SCIENTIFIC_CRITIC, "fake", "fake-model"),
        ModelRole.EMBEDDINGS: ModelAssignment(ModelRole.EMBEDDINGS, "fake_embeddings", "fake-embedding-v1"),
    }
    return ModelRouter(registry, assignments, usage_sink=repository.record_provider_usage)


def _seed_pdf(path: Path) -> None:
    writer = PdfWriter(); writer.add_blank_page(width=200, height=200); writer.add_metadata({"/Title": "Seed Adaptive Fusion"})
    with path.open("wb") as handle:
        writer.write(handle)


@pytest.mark.asyncio
async def test_complete_first_milestone_without_paid_api_keys(tmp_path):
    repository = ResearchRepository(tmp_path / "autoresearch.sqlite3", tmp_path / "artifacts")
    router = _router(repository)
    pipeline = StudySearchPipeline(repository, [DeterministicMetadataSource()], DeterministicPdfResolver(), tmp_path / "downloads")
    discovery_service = DiscoveryService(repository, router, pipeline)
    review_service = LiteratureReviewService(repository, router)
    seed = tmp_path / "seed.pdf"; _seed_pdf(seed)

    discovery = await discovery_service.discover(seed, limit=20, since_year=2023)
    assert discovery.retrieved == 20
    assert discovery.downloaded > 0
    assert discovery.downloaded + discovery.unavailable == 20
    assert repository.get_run(discovery.run_id).status.value == "completed"
    assert len(repository.search_events(discovery.run_id)) == 4
    assert len(repository.discovery_scores(discovery.run_id)) == 20
    discovery_report = json.loads(Path(discovery.artifact_path).read_text())
    assert discovery_report["timestamp"]
    assert discovery_report["source_evidence"]
    assert all(paper.title != "Seed Adaptive Fusion" for paper in repository.list_papers(discovery.project_id))

    review = await review_service.review(discovery.project_id, top_k=5)
    assert review.reviewed == 20
    assert len(review.top_paper_ids) == 5
    assert review.gap.classification is GapClassification.EVIDENCE_SUPPORTED
    assert review.gap.supporting_claim_ids
    assert Path(review.json_report).exists() and Path(review.markdown_report).exists()
    report = json.loads(Path(review.json_report).read_text())
    assert report["project_id"] == discovery.project_id
    assert report["run_id"] == review.run_id
    assert len(report["ranked_papers"]) == 5
    assert report["gap"]["supporting_claims"][0]["excerpt"]
    assert report["provenance"]["provider"] == "fake"
    assert len(report["provenance"]["checksum"]) == 64
    markdown = Path(review.markdown_report).read_text()
    assert "## Provenance" in markdown
    assert discovery.project_id in markdown and review.run_id in markdown
    assert repository.integrity_check() == "ok"
    assert repository.row("SELECT COUNT(*) AS n FROM provider_usage")["n"] >= 24
    assert repository.review_failures(review.run_id) == []
    await pipeline.close(); await router.close()

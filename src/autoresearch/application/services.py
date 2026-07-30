"""Durable orchestration for the first AutoResearch milestone."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from autoresearch.domain.models import ArtifactProvenance, ArtifactReference, DownloadStatus, PipelineKind, ResearchGap, SeedPaper, content_checksum, utc_now
from autoresearch.ingestion.chunking import DocumentChunk, chunk_document
from autoresearch.ingestion.document_parser import DocumentParser, ParsedDocument, ParsedPage
from autoresearch.ingestion.extraction import SEED_PROMPT_VERSION, SeedAnalysis, SeedAnalyzer
from autoresearch.papers.search_pipeline import DiscoveryResult, SearchStrategy, StudySearchPipeline
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter
from autoresearch.review.extraction import PaperReviewExtractor, ReviewBundle
from autoresearch.review.gap_analysis import GapAnalyzer
from autoresearch.review.similarity import ReviewRanker
from autoresearch.review.synthesis import ReviewSynthesisWriter
from autoresearch.storage.repository import ResearchRepository

SEARCH_PROMPT_VERSION = "search-strategies-v1"
SEARCH_STRATEGIES_SCHEMA = {
    "type": "object",
    "properties": {
        "strategies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "query": {"type": "string"}},
                "required": ["name", "query"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["strategies"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class DiscoveryOutcome:
    project_id: str
    run_id: str
    requested: int
    retrieved: int
    downloaded: int
    unavailable: int
    rejected: int
    failed: int
    artifact_path: str


@dataclass(frozen=True)
class ReviewOutcome:
    project_id: str
    run_id: str
    reviewed: int
    failed: int
    top_paper_ids: tuple[str, ...]
    gap: ResearchGap
    json_report: str
    markdown_report: str


class SearchStrategyGenerator:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def generate(self, analysis: SeedAnalysis, *, project_id: str, run_id: str) -> tuple[SearchStrategy, ...]:
        payload = await self.router.structured_output(
            ModelRole.RESEARCH_REASONING,
            messages=[
                {"role": "system", "content": "Generate complementary scholarly-search queries. Queries must be concise and grounded in the supplied seed analysis."},
                {"role": "user", "content": f"Prompt version: {SEARCH_PROMPT_VERSION}\nTopic: {analysis.topic}\nQuestion: {analysis.research_question}\nMethods: {list(analysis.methods)}\nDatasets: {list(analysis.datasets)}\nKeywords: {list(analysis.keywords)}\nReturn strategies[] with unique name and query. Include topic, method, research-question, and limitation strategies."},
            ],
            schema={"name": "search_strategies", "schema": SEARCH_STRATEGIES_SCHEMA},
            project_id=project_id,
            run_id=run_id,
            operation="search_strategy_generation",
        )
        strategies: list[SearchStrategy] = []
        seen: set[str] = set()
        for raw in payload.get("strategies") or []:
            if not isinstance(raw, dict):
                continue
            name = " ".join(str(raw.get("name") or "").split())
            query = " ".join(str(raw.get("query") or "").split())
            if name and query and query.lower() not in seen:
                strategies.append(SearchStrategy(name, query))
                seen.add(query.lower())
        if len(strategies) < 3:
            raise ValueError("Provider must return at least three distinct search strategies.")
        return tuple(strategies[:8])


class DiscoveryService:
    def __init__(self, repository: ResearchRepository, router: ModelRouter, search_pipeline: StudySearchPipeline, *, parser: DocumentParser | None = None) -> None:
        self.repository = repository
        self.router = router
        self.search_pipeline = search_pipeline
        self.parser = parser or DocumentParser()
        self.seed_analyzer = SeedAnalyzer(router)
        self.strategy_generator = SearchStrategyGenerator(router)

    async def discover(self, seed_input: str | Path, *, limit: int = 20, since_year: int | None = None) -> DiscoveryOutcome:
        parsed = self.parser.parse_seed(seed_input)
        project = self.repository.create_project(parsed.title)
        run = self.repository.create_run(project.id, PipelineKind.DISCOVERY, {"seed_input": str(seed_input), "limit": limit, "since_year": since_year})
        try:
            parsed = await self._hydrate_identifier_seed(parsed)
            analysis = await self.seed_analyzer.analyze(parsed, project_id=project.id, run_id=run.id)
            extraction_provider, extraction_model = self.router.identity(ModelRole.LITERATURE_EXTRACTION)
            analysis_content = {"title": analysis.title, "topic": analysis.topic, "research_question": analysis.research_question, "methods": analysis.methods, "datasets": analysis.datasets, "citations": analysis.citations, "limitations": analysis.limitations, "keywords": analysis.keywords}
            seed_provenance = ArtifactProvenance.create(project_id=project.id, run_id=run.id, source_evidence=(f"seed-checksum:{parsed.checksum}",), provider=extraction_provider, model=extraction_model, prompt_version=SEED_PROMPT_VERSION, content=analysis_content)
            seed = SeedPaper(id=uuid4().hex, project_id=project.id, run_id=run.id, input_type=parsed.input_type, input_value=parsed.input_value, title=analysis.title, text=parsed.text, topic=analysis.topic, research_question=analysis.research_question, checksum=parsed.checksum, provenance=seed_provenance, doi=parsed.doi, url=parsed.url, methods=analysis.methods, datasets=analysis.datasets, citations=analysis.citations, limitations=analysis.limitations)
            self.repository.save_seed(seed)
            strategies = await self.strategy_generator.generate(analysis, project_id=project.id, run_id=run.id)
            result = await self.search_pipeline.discover(project_id=project.id, run_id=run.id, seed=seed, strategies=strategies, limit=limit, since_year=since_year)
            artifact = self._write_discovery_artifact(seed, analysis, strategies, result)
            self.repository.finish_run(run.id)
            return DiscoveryOutcome(
                project.id, run.id, limit, len(result.papers), result.downloaded_count,
                sum(item.status is DownloadStatus.UNAVAILABLE for item in result.downloads),
                sum(item.status is DownloadStatus.REJECTED for item in result.downloads),
                sum(item.status is DownloadStatus.FAILED for item in result.downloads), artifact.path,
            )
        except Exception as exc:
            self.repository.finish_run(run.id, error=f"{type(exc).__name__}: {exc}")
            raise

    async def _hydrate_identifier_seed(self, parsed: ParsedDocument) -> ParsedDocument:
        if parsed.text:
            return parsed
        query = parsed.doi or parsed.url or parsed.input_value
        results = await asyncio.gather(*(source.search(query, limit=3) for source in self.search_pipeline.sources), return_exceptions=True)
        candidates = [paper for result in results if not isinstance(result, Exception) for paper in result.papers]
        if parsed.doi:
            exact = [paper for paper in candidates if paper.doi == parsed.doi]
            candidates = exact or candidates
        if not candidates:
            raise ValueError("Seed DOI/URL could not be resolved to scholarly metadata.")
        paper = max(candidates, key=lambda item: len(item.abstract))
        return ParsedDocument(parsed.input_type, parsed.input_value, paper.title or parsed.title, (ParsedPage(1, paper.abstract),), parsed.checksum, parsed.doi or paper.doi, parsed.url or paper.url)

    def _write_discovery_artifact(self, seed: SeedPaper, analysis: SeedAnalysis, strategies: Sequence[SearchStrategy], result: DiscoveryResult) -> ArtifactReference:
        provider, model = self.router.identity(ModelRole.RESEARCH_REASONING)
        payload = {
            "project_id": seed.project_id, "run_id": seed.run_id, "seed_id": seed.id,
            "provider": provider, "model": model, "prompt_version": SEARCH_PROMPT_VERSION,
            "timestamp": utc_now(),
            "source_evidence": [seed.id, *(paper.id for paper in result.papers)],
            "seed_analysis": analysis.__dict__, "strategies": [item.__dict__ for item in strategies],
            "requested": result.requested, "retrieved": len(result.papers), "downloaded": result.downloaded_count,
            "downloads": [{"paper_id": item.paper_id, "status": item.status.value, "source": item.source, "legal_basis": item.legal_basis, "reason": item.reason, "checksum": item.checksum} for item in result.downloads],
            "source_errors": result.source_errors,
        }
        root = self.repository.artifact_root / seed.project_id / seed.run_id
        root.mkdir(parents=True, exist_ok=True)
        path = root / "discovery.json"
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        path.write_text(text, encoding="utf-8")
        artifact = ArtifactReference(
            uuid4().hex,
            seed.project_id,
            seed.run_id,
            "discovery_summary",
            str(path),
            tuple(payload["source_evidence"]),
            provider,
            model,
            SEARCH_PROMPT_VERSION,
            content_checksum(text),
        )
        self.repository.save_artifact(artifact)
        return artifact


class LiteratureReviewService:
    def __init__(self, repository: ResearchRepository, router: ModelRouter, *, parser: DocumentParser | None = None) -> None:
        self.repository = repository
        self.router = router
        self.parser = parser or DocumentParser()
        self.extractor = PaperReviewExtractor(router)
        self.ranker = ReviewRanker(router)
        self.gap_analyzer = GapAnalyzer(router)
        self.writer = ReviewSynthesisWriter(repository)

    async def review(self, project_id: str, *, top_k: int = 5) -> ReviewOutcome:
        if not 1 <= top_k <= 15:
            raise ValueError("top_k must be between 1 and 15.")
        project = self.repository.get_project(project_id)
        seed = self.repository.get_seed(project_id)
        if not project or not seed:
            raise ValueError("Project or seed paper was not found.")
        papers = self.repository.list_papers(project_id)
        if not papers:
            raise ValueError("Run discovery before literature review.")
        run = self.repository.create_run(project_id, PipelineKind.REVIEW, {"top_k": top_k, "paper_count": len(papers)})
        try:
            results = await asyncio.gather(*(self._review_one(paper, run.id) for paper in papers), return_exceptions=True)
            bundles: list[ReviewBundle] = []
            for paper, result in zip(papers, results):
                if isinstance(result, Exception):
                    self.repository.record_review_failure(project_id=project_id, run_id=run.id, paper_id=paper.id, reason=f"{type(result).__name__}: {result}")
                else:
                    bundles.append(result)
                    for claim in result.claims:
                        self.repository.save_claim(claim)
                    self.repository.save_review(result.review)
            if len(bundles) < top_k:
                raise RuntimeError(f"Only {len(bundles)} papers produced evidence-backed reviews; {top_k} are required.")
            reviews = [bundle.review for bundle in bundles]
            claims = [claim for bundle in bundles for claim in bundle.claims]
            reviewed_papers = [paper for paper in papers if paper.id in {review.paper_id for review in reviews}]
            all_scores = await self.ranker.rank(seed=seed, papers=reviewed_papers, reviews=reviews, run_id=run.id)
            for score in all_scores:
                self.repository.save_score(score)
            top_scores = all_scores[:top_k]
            top_ids = {score.paper_id for score in top_scores}
            top_papers = [paper for paper in reviewed_papers if paper.id in top_ids]
            top_reviews = [review for review in reviews if review.paper_id in top_ids]
            top_claims = [claim for claim in claims if claim.paper_id in top_ids]
            gap = await self.gap_analyzer.analyze(project_id=project_id, run_id=run.id, papers=top_papers, reviews=top_reviews, claims=top_claims, scores=top_scores)
            self.repository.save_gap(gap)
            json_artifact, markdown_artifact = self.writer.write(project_id=project_id, run_id=run.id, papers=top_papers, reviews=top_reviews, scores=top_scores, claims=top_claims, gap=gap)
            self.repository.finish_run(run.id)
            return ReviewOutcome(project_id, run.id, len(reviews), len(results) - len(reviews), tuple(score.paper_id for score in top_scores), gap, json_artifact.path, markdown_artifact.path)
        except Exception as exc:
            self.repository.finish_run(run.id, error=f"{type(exc).__name__}: {exc}")
            raise

    async def _review_one(self, paper, run_id: str) -> ReviewBundle:
        chunks: tuple[DocumentChunk, ...] = ()
        download = self.repository.download_for_paper(paper.id)
        if download and download.status is DownloadStatus.DOWNLOADED and download.path:
            try:
                chunks = chunk_document(self.parser.parse_seed(download.path))
            except Exception:
                chunks = ()
        return await self.extractor.extract(paper, run_id=run_id, chunks=chunks, fallback_text=paper.abstract)

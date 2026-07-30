"""Concurrent, provenance-complete study-search pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from autoresearch.domain.models import (
    DownloadRecord,
    DownloadStatus,
    PaperRecord,
    SearchCandidate,
    SeedPaper,
    content_checksum,
)
from autoresearch.storage.repository import ResearchRepository

from .deduplication import SearchProvenance, SourcedCandidate, deduplicate, deduplication_key, normalized_title
from .metadata_sources import MetadataPaper, MetadataSource, metadata_payload
from .pdf_resolver import PdfResolver
from .ranking import CandidateScore, rank_candidates


@dataclass(frozen=True)
class SearchStrategy:
    name: str
    query: str


@dataclass(frozen=True)
class DiscoveryResult:
    project_id: str
    run_id: str
    requested: int
    papers: tuple[PaperRecord, ...]
    downloads: tuple[DownloadRecord, ...]
    source_errors: tuple[str, ...]

    @property
    def downloaded_count(self) -> int:
        return sum(record.status is DownloadStatus.DOWNLOADED for record in self.downloads)


class StudySearchPipeline:
    def __init__(self, repository: ResearchRepository, sources: Sequence[MetadataSource], pdf_resolver: PdfResolver, download_root: Path, *, search_concurrency: int = 8, download_concurrency: int = 4) -> None:
        if not sources:
            raise ValueError("At least one metadata source is required.")
        self.repository = repository
        self.sources = tuple(sources)
        self.pdf_resolver = pdf_resolver
        self.download_root = download_root
        self.search_semaphore = asyncio.Semaphore(max(1, search_concurrency))
        self.download_semaphore = asyncio.Semaphore(max(1, download_concurrency))

    async def discover(self, *, project_id: str, run_id: str, seed: SeedPaper, strategies: Sequence[SearchStrategy], limit: int, since_year: int | None = None) -> DiscoveryResult:
        if not 1 <= limit <= 100:
            raise ValueError("Discovery limit must be between 1 and 100.")
        if not strategies:
            raise ValueError("At least one search strategy is required.")
        tasks = [self._search(source, strategy, project_id, run_id, max(limit * 2, 20), since_year) for source in self.sources for strategy in strategies]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        sourced: list[SourcedCandidate] = []
        errors: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                errors.append(f"{type(result).__name__}: {result}")
                continue
            for rank, paper in enumerate(result.papers, start=1):
                if not paper.title.strip():
                    continue
                sourced.append(SourcedCandidate(paper, SearchProvenance(result.source, result.strategy.name, result.query, rank, result.raw_checksum)))
        if not sourced:
            raise RuntimeError("All scholarly searches failed or returned no usable candidates.")

        unique = deduplicate(sourced)
        seed_title = normalized_title(seed.title)
        unique = [
            candidate
            for candidate in unique
            if not (
                seed.doi
                and candidate.paper.doi
                and candidate.paper.doi.lower() == seed.doi.lower()
            )
            and normalized_title(candidate.paper.title) != seed_title
        ]
        ranked = rank_candidates(unique, seed_text=f"{seed.title} {seed.text[:8000]}", seed_methods=seed.methods)
        selected = ranked[:limit]
        records: list[PaperRecord] = []
        metadata_by_id: dict[str, MetadataPaper] = {}
        for candidate, score in selected:
            record = self._paper_record(project_id, run_id, candidate.paper)
            records.append(record)
            metadata_by_id[record.id] = candidate.paper
            self.repository.save_paper(record)
            self.repository.save_discovery_score(project_id=project_id, run_id=run_id, paper_id=record.id, components=score.components())
            for provenance in candidate.provenance:
                self.repository.save_candidate(SearchCandidate(uuid4().hex, project_id, run_id, record.id, provenance.source, provenance.strategy, provenance.source_rank, provenance.query, provenance.raw_checksum))

        downloads = await asyncio.gather(*(self._acquire(record, metadata_by_id[record.id]) for record in records))
        return DiscoveryResult(project_id, run_id, limit, tuple(records), tuple(downloads), tuple(errors))

    async def _search(self, source: MetadataSource, strategy: SearchStrategy, project_id: str, run_id: str, limit: int, since_year: int | None):
        async with self.search_semaphore:
            try:
                result = await source.search(strategy.query, limit=limit, since_year=since_year)
            except Exception as exc:
                self.repository.record_search_event(project_id=project_id, run_id=run_id, source=source.name, strategy=strategy.name, query=strategy.query, requested_limit=limit, result_count=0, raw_checksum=content_checksum(""), error=f"{type(exc).__name__}: {exc}")
                raise
        self.repository.record_search_event(project_id=project_id, run_id=run_id, source=source.name, strategy=strategy.name, query=strategy.query, requested_limit=limit, result_count=len(result.papers), raw_checksum=result.raw_checksum)
        return _StrategyResult(result.source, strategy, result.query, result.papers, result.raw_checksum)

    async def _acquire(self, paper: PaperRecord, metadata: MetadataPaper) -> DownloadRecord:
        async with self.download_semaphore:
            try:
                resolution = await self.pdf_resolver.resolve(metadata)
                if not resolution.available:
                    record = DownloadRecord(uuid4().hex, paper.project_id, paper.run_id, paper.id, DownloadStatus.UNAVAILABLE, resolution.source, resolution.url, resolution.legal_basis, resolution.license, None, resolution.reason, None)
                else:
                    target = self.download_root / paper.project_id / f"{paper.id}.pdf"
                    downloaded = await self.pdf_resolver.download(resolution, target)
                    record = DownloadRecord(uuid4().hex, paper.project_id, paper.run_id, paper.id, DownloadStatus.DOWNLOADED, resolution.source, resolution.url, resolution.legal_basis, resolution.license, str(downloaded.path), None, downloaded.checksum)
            except ValueError as exc:
                record = DownloadRecord(uuid4().hex, paper.project_id, paper.run_id, paper.id, DownloadStatus.REJECTED, metadata.source, metadata.open_access_pdf_url, None, None, None, str(exc), None)
            except Exception as exc:
                record = DownloadRecord(uuid4().hex, paper.project_id, paper.run_id, paper.id, DownloadStatus.FAILED, metadata.source, metadata.open_access_pdf_url, None, None, None, f"{type(exc).__name__}: {exc}", None)
        self.repository.save_download(record)
        return record

    @staticmethod
    def _paper_record(project_id: str, run_id: str, paper: MetadataPaper) -> PaperRecord:
        key = deduplication_key(paper)
        # A paper may occur in many independent projects. Scope its storage ID
        # to the project while keeping deduplication deterministic within it.
        identifier = content_checksum(f"{project_id}|{key}")[:24]
        payload = metadata_payload(paper)
        sources = tuple(sorted(filter(None, paper.source.split(","))))
        return PaperRecord(identifier, project_id, run_id, paper.title, paper.abstract, paper.authors, paper.year, paper.publication_date, paper.doi, paper.arxiv_id, paper.url, sources, paper.open_access_pdf_url, paper.is_open_access, paper.citation_count, paper.venue, content_checksum(payload))

    async def close(self) -> None:
        await asyncio.gather(*(source.close() for source in self.sources), return_exceptions=True)
        await self.pdf_resolver.close()


@dataclass(frozen=True)
class _StrategyResult:
    source: str
    strategy: SearchStrategy
    query: str
    papers: tuple[MetadataPaper, ...]
    raw_checksum: str

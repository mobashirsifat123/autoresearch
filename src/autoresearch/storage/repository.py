"""SQLite repository for durable, provenance-preserving pipeline state."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    ArtifactReference,
    DownloadRecord,
    DownloadStatus,
    EvidenceClaim,
    GapClassification,
    PaperRecord,
    PaperReview,
    PipelineKind,
    PipelineRun,
    PriorityScore,
    Project,
    ProviderUsage,
    ResearchGap,
    RunStatus,
    SearchCandidate,
    SeedPaper,
    utc_now,
)


SCHEMA_VERSION = 3


class ResearchRepository:
    def __init__(self, path: Path, artifact_root: Path) -> None:
        self.path = path.expanduser().resolve()
        self.artifact_root = artifact_root.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self._lock, self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            current = int(connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
            if current < 1:
                connection.executescript(_SCHEMA_V1)
                connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (1, utc_now()))
            if current < 2:
                connection.executescript(_SCHEMA_V2)
                connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (2, utc_now()))
            if current < 3:
                connection.executescript(_SCHEMA_V3)
                connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (3, utc_now()))
            latest = int(connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
            if latest != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported database schema v{latest}; expected v{SCHEMA_VERSION}.")

    def create_project(self, title: str) -> Project:
        project = Project(uuid4().hex, title.strip())
        if not project.title:
            raise ValueError("Project title is required.")
        self.execute("INSERT INTO projects(id,title,created_at) VALUES(?,?,?)", (project.id, project.title, project.created_at))
        return project

    def get_project(self, project_id: str) -> Project | None:
        row = self.row("SELECT * FROM projects WHERE id=?", (project_id,))
        return Project(**row) if row else None

    def list_projects(self) -> list[Project]:
        return [Project(**row) for row in self.rows("SELECT * FROM projects ORDER BY created_at DESC")]

    def create_run(self, project_id: str, kind: PipelineKind, input_data: dict[str, Any]) -> PipelineRun:
        run = PipelineRun(uuid4().hex, project_id, kind, RunStatus.RUNNING, input_data)
        self.execute(
            "INSERT INTO pipeline_runs(id,project_id,kind,status,input_json,started_at) VALUES(?,?,?,?,?,?)",
            (run.id, project_id, kind.value, run.status.value, _json(input_data), run.started_at),
        )
        return run

    def finish_run(self, run_id: str, *, error: str | None = None) -> None:
        status = RunStatus.FAILED if error else RunStatus.COMPLETED
        self.execute("UPDATE pipeline_runs SET status=?,completed_at=?,error=? WHERE id=?", (status.value, utc_now(), error, run_id))

    def resume_run(self, run_id: str) -> PipelineRun:
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Pipeline run '{run_id}' was not found.")
        self.execute(
            "UPDATE pipeline_runs SET status=?,completed_at=NULL,error=NULL WHERE id=?",
            (RunStatus.RUNNING.value, run_id),
        )
        resumed = self.get_run(run_id)
        assert resumed is not None
        return resumed

    def get_run(self, run_id: str) -> PipelineRun | None:
        row = self.row("SELECT * FROM pipeline_runs WHERE id=?", (run_id,))
        if not row:
            return None
        return PipelineRun(row["id"], row["project_id"], PipelineKind(row["kind"]), RunStatus(row["status"]), _loads(row["input_json"], {}), row["started_at"], row["completed_at"], row["error"])

    def list_runs(self, *, project_id: str | None = None, status: RunStatus | None = None) -> list[PipelineRun]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if status:
            clauses.append("status=?")
            params.append(status.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.rows(f"SELECT id FROM pipeline_runs{where} ORDER BY started_at DESC", tuple(params))
        return [self.get_run(row["id"]) for row in rows if self.get_run(row["id"])]

    def cancel_run(self, run_id: str) -> None:
        self.execute("UPDATE pipeline_runs SET status=?,completed_at=?,error=? WHERE id=? AND status IN (?,?)", (RunStatus.CANCELLED.value, utc_now(), "Cancelled by user.", run_id, RunStatus.RUNNING.value, RunStatus.PENDING.value))

    def latest_run(self, project_id: str, kind: PipelineKind) -> PipelineRun | None:
        row = self.row(
            "SELECT id FROM pipeline_runs WHERE project_id=? AND kind=? ORDER BY started_at DESC LIMIT 1",
            (project_id, kind.value),
        )
        return self.get_run(row["id"]) if row else None

    def begin_checkpoint(self, run_id: str, stage: str, item_key: str = "main") -> None:
        self.execute(
            """INSERT INTO pipeline_checkpoints(run_id,stage,item_key,status,payload_json,attempts,error,updated_at)
               VALUES(?,?,?,'running',NULL,1,NULL,?)
               ON CONFLICT(run_id,stage,item_key) DO UPDATE SET
                 status='running',attempts=pipeline_checkpoints.attempts+1,error=NULL,updated_at=excluded.updated_at""",
            (run_id, stage, item_key, utc_now()),
        )

    def complete_checkpoint(self, run_id: str, stage: str, item_key: str = "main", *, payload: Any = None) -> None:
        self.execute(
            """INSERT INTO pipeline_checkpoints(run_id,stage,item_key,status,payload_json,attempts,error,updated_at)
               VALUES(?,?,?,'completed',?,1,NULL,?)
               ON CONFLICT(run_id,stage,item_key) DO UPDATE SET
                 status='completed',payload_json=excluded.payload_json,error=NULL,updated_at=excluded.updated_at""",
            (run_id, stage, item_key, _json(payload) if payload is not None else None, utc_now()),
        )

    def fail_checkpoint(self, run_id: str, stage: str, item_key: str = "main", *, error: str) -> None:
        self.execute(
            """INSERT INTO pipeline_checkpoints(run_id,stage,item_key,status,payload_json,attempts,error,updated_at)
               VALUES(?,?,?,'failed',NULL,1,?,?)
               ON CONFLICT(run_id,stage,item_key) DO UPDATE SET
                 status='failed',error=excluded.error,updated_at=excluded.updated_at""",
            (run_id, stage, item_key, error, utc_now()),
        )

    def checkpoint(self, run_id: str, stage: str, item_key: str = "main") -> dict[str, Any] | None:
        row = self.row(
            "SELECT * FROM pipeline_checkpoints WHERE run_id=? AND stage=? AND item_key=?",
            (run_id, stage, item_key),
        )
        if row and row.get("payload_json"):
            row["payload"] = _loads(row["payload_json"], None)
        return row

    def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.rows(
            "SELECT * FROM pipeline_checkpoints WHERE run_id=? ORDER BY stage,item_key",
            (run_id,),
        )
        for row in rows:
            row["payload"] = _loads(row.get("payload_json"), None)
        return rows

    def save_seed(self, seed: SeedPaper) -> None:
        self.execute(
            """INSERT OR REPLACE INTO seed_papers
               (id,project_id,run_id,input_type,input_value,title,text,topic,research_question,doi,url,methods_json,datasets_json,citations_json,limitations_json,checksum,provenance_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (seed.id, seed.project_id, seed.run_id, seed.input_type, seed.input_value, seed.title, seed.text, seed.topic, seed.research_question, seed.doi, seed.url, _json(seed.methods), _json(seed.datasets), _json(seed.citations), _json(seed.limitations), seed.checksum, _json_provenance(seed.provenance), seed.created_at),
        )

    def get_seed(self, project_id: str) -> SeedPaper | None:
        row = self.row("SELECT * FROM seed_papers WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,))
        if not row:
            return None
        return SeedPaper(
            id=row["id"], project_id=row["project_id"], run_id=row["run_id"], input_type=row["input_type"], input_value=row["input_value"],
            title=row["title"], text=row["text"], topic=row["topic"], research_question=row["research_question"], checksum=row["checksum"], provenance=_provenance(row["provenance_json"]),
            doi=row["doi"], url=row["url"], methods=tuple(_loads(row["methods_json"], [])), datasets=tuple(_loads(row["datasets_json"], [])), citations=tuple(_loads(row["citations_json"], [])), limitations=tuple(_loads(row["limitations_json"], [])), created_at=row["created_at"],
        )

    def record_search_event(self, *, project_id: str, run_id: str, source: str, strategy: str, query: str, requested_limit: int, result_count: int, raw_checksum: str, error: str | None = None) -> str:
        identifier = uuid4().hex
        self.execute("INSERT INTO search_events(id,project_id,run_id,source,strategy,query,requested_limit,result_count,raw_checksum,error,retrieved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identifier, project_id, run_id, source, strategy, query, requested_limit, result_count, raw_checksum, error, utc_now()))
        return identifier

    def search_events(self, run_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM search_events WHERE run_id=? ORDER BY source,strategy", (run_id,))

    def save_paper(self, paper: PaperRecord) -> None:
        self.execute(
            """INSERT INTO papers
               (id,project_id,run_id,title,abstract,authors_json,year,publication_date,doi,arxiv_id,url,source_names_json,open_access_pdf_url,is_open_access,citation_count,venue,checksum,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title,abstract=excluded.abstract,authors_json=excluded.authors_json,year=excluded.year,
                 publication_date=excluded.publication_date,doi=COALESCE(excluded.doi,papers.doi),arxiv_id=COALESCE(excluded.arxiv_id,papers.arxiv_id),
                 url=COALESCE(excluded.url,papers.url),source_names_json=excluded.source_names_json,
                 open_access_pdf_url=COALESCE(excluded.open_access_pdf_url,papers.open_access_pdf_url),is_open_access=MAX(papers.is_open_access,excluded.is_open_access),
                 citation_count=MAX(papers.citation_count,excluded.citation_count),venue=COALESCE(NULLIF(excluded.venue,''),papers.venue),checksum=excluded.checksum""",
            (paper.id, paper.project_id, paper.run_id, paper.title, paper.abstract, _json(paper.authors), paper.year, paper.publication_date, paper.doi, paper.arxiv_id, paper.url, _json(paper.source_names), paper.open_access_pdf_url, int(paper.is_open_access), paper.citation_count, paper.venue, paper.checksum, paper.created_at),
        )

    def get_paper(self, paper_id: str) -> PaperRecord | None:
        row = self.row("SELECT * FROM papers WHERE id=?", (paper_id,))
        return _paper(row) if row else None

    def list_papers(self, project_id: str) -> list[PaperRecord]:
        return [_paper(row) for row in self.rows("SELECT * FROM papers WHERE project_id=? ORDER BY publication_date DESC,title", (project_id,))]

    def save_candidate(self, candidate: SearchCandidate) -> None:
        self.execute("INSERT OR IGNORE INTO search_candidates(id,project_id,run_id,paper_id,source,strategy,source_rank,query,raw_checksum,retrieved_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (candidate.id, candidate.project_id, candidate.run_id, candidate.paper_id, candidate.source, candidate.strategy, candidate.source_rank, candidate.query, candidate.raw_checksum, candidate.retrieved_at))

    def save_discovery_score(self, *, project_id: str, run_id: str, paper_id: str, components: dict[str, float]) -> None:
        self.execute(
            "INSERT OR REPLACE INTO discovery_scores(project_id,run_id,paper_id,components_json,total,created_at) VALUES(?,?,?,?,?,?)",
            (project_id, run_id, paper_id, _json(components), float(components["total"]), utc_now()),
        )

    def discovery_scores(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.rows("SELECT * FROM discovery_scores WHERE run_id=? ORDER BY total DESC,paper_id", (run_id,))
        for row in rows:
            row["components"] = _loads(row.pop("components_json"), {})
        return rows

    def save_download(self, record: DownloadRecord) -> None:
        self.execute("INSERT OR REPLACE INTO download_records(id,project_id,run_id,paper_id,status,source,url,legal_basis,license,path,reason,checksum,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (record.id, record.project_id, record.run_id, record.paper_id, record.status.value, record.source, record.url, record.legal_basis, record.license, record.path, record.reason, record.checksum, record.created_at))

    def downloads(self, project_id: str) -> list[DownloadRecord]:
        return [_download(row) for row in self.rows("SELECT * FROM download_records WHERE project_id=? ORDER BY created_at", (project_id,))]

    def download_for_paper(self, paper_id: str) -> DownloadRecord | None:
        row = self.row("SELECT * FROM download_records WHERE paper_id=? ORDER BY created_at DESC LIMIT 1", (paper_id,))
        return _download(row) if row else None

    def save_review(self, review: PaperReview) -> None:
        self.execute(
            """INSERT OR REPLACE INTO paper_reviews
               (id,project_id,run_id,paper_id,problem,research_question,method,datasets_json,experimental_protocol,results_json,ablations_json,limitations_json,failure_modes_json,evidence_claim_ids_json,confidence,provenance_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (review.id, review.project_id, review.run_id, review.paper_id, review.problem, review.research_question, review.method, _json(review.datasets), review.experimental_protocol, _json(review.results), _json(review.ablations), _json(review.limitations), _json(review.failure_modes), _json(review.evidence_claim_ids), review.confidence, _json_provenance(review.provenance)),
        )

    def record_review_failure(self, *, project_id: str, run_id: str, paper_id: str, reason: str) -> None:
        self.execute("INSERT INTO review_failures(id,project_id,run_id,paper_id,reason,created_at) VALUES(?,?,?,?,?,?)", (uuid4().hex, project_id, run_id, paper_id, reason, utc_now()))

    def clear_review_failures(self, run_id: str, paper_id: str) -> None:
        self.execute("DELETE FROM review_failures WHERE run_id=? AND paper_id=?", (run_id, paper_id))

    def review_failures(self, run_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM review_failures WHERE run_id=? ORDER BY paper_id", (run_id,))

    def reviews(self, project_id: str, run_id: str | None = None) -> list[PaperReview]:
        sql = "SELECT * FROM paper_reviews WHERE project_id=?"
        params: tuple[Any, ...] = (project_id,)
        if run_id:
            sql += " AND run_id=?"
            params = (project_id, run_id)
        return [_review(row) for row in self.rows(sql + " ORDER BY paper_id", params)]

    def review_for_paper(self, run_id: str, paper_id: str) -> PaperReview | None:
        row = self.row(
            "SELECT * FROM paper_reviews WHERE run_id=? AND paper_id=?",
            (run_id, paper_id),
        )
        return _review(row) if row else None

    def save_claim(self, claim: EvidenceClaim) -> None:
        self.execute("INSERT OR REPLACE INTO evidence_claims(id,project_id,run_id,paper_id,claim_type,statement,excerpt,page,section,confidence,provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (claim.id, claim.project_id, claim.run_id, claim.paper_id, claim.claim_type, claim.statement, claim.excerpt, claim.page, claim.section, claim.confidence, _json_provenance(claim.provenance)))

    def claims(self, project_id: str, *, ids: tuple[str, ...] | None = None) -> list[EvidenceClaim]:
        rows = self.rows("SELECT * FROM evidence_claims WHERE project_id=? ORDER BY id", (project_id,))
        claims = [_claim(row) for row in rows]
        return [claim for claim in claims if not ids or claim.id in ids]

    def save_score(self, score: PriorityScore) -> None:
        self.execute("INSERT OR REPLACE INTO priority_scores(id,project_id,run_id,paper_id,semantic_similarity,methodological_similarity,research_question_similarity,recency,influence,evidence_quality,total,explanation_json,provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (score.id, score.project_id, score.run_id, score.paper_id, score.semantic_similarity, score.methodological_similarity, score.research_question_similarity, score.recency, score.influence, score.evidence_quality, score.total, _json(score.explanation), _json_provenance(score.provenance)))

    def scores(self, project_id: str, run_id: str | None = None) -> list[PriorityScore]:
        sql = "SELECT * FROM priority_scores WHERE project_id=?"
        params: tuple[Any, ...] = (project_id,)
        if run_id:
            sql += " AND run_id=?"
            params = (project_id, run_id)
        return [_score(row) for row in self.rows(sql + " ORDER BY total DESC,paper_id", params)]

    def save_gap(self, gap: ResearchGap) -> None:
        self.execute("INSERT OR REPLACE INTO research_gaps(id,project_id,run_id,statement,supporting_paper_ids_json,supporting_claim_ids_json,why_unsolved,missing_experiment,falsification_test,confidence,counter_evidence_claim_ids_json,classification,provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (gap.id, gap.project_id, gap.run_id, gap.statement, _json(gap.supporting_paper_ids), _json(gap.supporting_claim_ids), gap.why_unsolved, gap.missing_experiment, gap.falsification_test, gap.confidence, _json(gap.counter_evidence_claim_ids), gap.classification.value, _json_provenance(gap.provenance)))

    def latest_gap(self, project_id: str) -> ResearchGap | None:
        row = self.row("SELECT * FROM research_gaps WHERE project_id=? ORDER BY rowid DESC LIMIT 1", (project_id,))
        return _gap(row) if row else None

    def gap_for_run(self, run_id: str) -> ResearchGap | None:
        row = self.row("SELECT * FROM research_gaps WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,))
        return _gap(row) if row else None

    def save_artifact(self, artifact: ArtifactReference) -> None:
        self.execute("INSERT OR REPLACE INTO artifacts(id,project_id,run_id,artifact_type,path,source_evidence_json,provider,model,prompt_version,checksum,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (artifact.id, artifact.project_id, artifact.run_id, artifact.artifact_type, artifact.path, _json(artifact.source_evidence), artifact.provider, artifact.model, artifact.prompt_version, artifact.checksum, artifact.created_at))

    def artifacts(self, project_id: str) -> list[ArtifactReference]:
        return [ArtifactReference(row["id"], row["project_id"], row["run_id"], row["artifact_type"], row["path"], tuple(_loads(row["source_evidence_json"], [])), row["provider"], row["model"], row["prompt_version"], row["checksum"], row["created_at"]) for row in self.rows("SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at", (project_id,))]

    def artifacts_for_run(self, run_id: str) -> list[ArtifactReference]:
        rows = self.rows("SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at", (run_id,))
        return [ArtifactReference(row["id"], row["project_id"], row["run_id"], row["artifact_type"], row["path"], tuple(_loads(row["source_evidence_json"], [])), row["provider"], row["model"], row["prompt_version"], row["checksum"], row["created_at"]) for row in rows]

    def record_provider_usage(self, usage: ProviderUsage) -> None:
        self.execute("INSERT INTO provider_usage(id,project_id,run_id,operation,role,provider,model,prompt_tokens,completion_tokens,estimated_cost,currency,request_id,latency_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (usage.id, usage.project_id, usage.run_id, usage.operation, usage.role, usage.provider, usage.model, usage.prompt_tokens, usage.completion_tokens, usage.estimated_cost, usage.currency, usage.request_id, usage.latency_ms, usage.created_at))

    def provider_spend(self, project_id: str) -> float:
        row = self.row("SELECT COALESCE(SUM(estimated_cost),0) AS total FROM provider_usage WHERE project_id=?", (project_id,))
        return float(row["total"] if row else 0.0)

    def set_project_budget(self, project_id: str, *, hard_stop: float | None, currency: str = "USD") -> None:
        self.execute("INSERT INTO project_budgets(project_id,hard_stop,currency,updated_at) VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET hard_stop=excluded.hard_stop,currency=excluded.currency,updated_at=excluded.updated_at", (project_id, hard_stop, currency, utc_now()))

    def get_project_budget(self, project_id: str) -> dict[str, Any] | None:
        return self.row("SELECT * FROM project_budgets WHERE project_id=?", (project_id,))

    def usage(self, *, project_id: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.rows(f"SELECT * FROM provider_usage{where} ORDER BY created_at", tuple(params))

    def append_api_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        with self._lock, self.connect() as connection:
            cursor = connection.execute("INSERT INTO api_events(run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)", (run_id, event_type, _json(payload), utc_now()))
            return int(cursor.lastrowid)

    def api_events(self, run_id: str, *, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.rows("SELECT id,run_id,event_type,payload_json,created_at FROM api_events WHERE run_id=? AND id>? ORDER BY id LIMIT ?", (run_id, after_id, limit))
        for row in rows:
            row["payload"] = _loads(row.pop("payload_json"), {})
        return rows

    def idempotency(self, key: str) -> dict[str, Any] | None:
        row = self.row("SELECT * FROM api_idempotency WHERE key=?", (key,))
        if row:
            row["response"] = _loads(row.pop("response_json"), {})
        return row

    def save_idempotency(self, key: str, operation: str, run_id: str, response: dict[str, Any]) -> None:
        self.execute("INSERT OR IGNORE INTO api_idempotency(key,operation,run_id,response_json,created_at) VALUES(?,?,?,?,?)", (key, operation, run_id, _json(response), utc_now()))

    def approval(self, run_id: str) -> dict[str, Any] | None:
        return self.row("SELECT * FROM run_approvals WHERE run_id=?", (run_id,))

    def set_approval(self, run_id: str, status: str, reason: str | None = None) -> None:
        self.execute("INSERT INTO run_approvals(run_id,status,reason,updated_at) VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,updated_at=excluded.updated_at", (run_id, status, reason, utc_now()))

    def integrity_check(self) -> str:
        row = self.row("PRAGMA integrity_check")
        return str(next(iter(row.values()))) if row else "unknown"

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(sql, params)

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock, self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def row(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_provenance(value: ArtifactProvenance) -> str:
    return _json({"project_id": value.project_id, "run_id": value.run_id, "source_evidence": value.source_evidence, "provider": value.provider, "model": value.model, "prompt_version": value.prompt_version, "created_at": value.created_at, "checksum": value.checksum})


def _provenance(value: str) -> ArtifactProvenance:
    data = _loads(value, {})
    data["source_evidence"] = tuple(data.get("source_evidence") or [])
    return ArtifactProvenance(**data)


def _paper(row: dict[str, Any]) -> PaperRecord:
    return PaperRecord(row["id"], row["project_id"], row["run_id"], row["title"], row["abstract"], tuple(_loads(row["authors_json"], [])), row["year"], row["publication_date"], row["doi"], row["arxiv_id"], row["url"], tuple(_loads(row["source_names_json"], [])), row["open_access_pdf_url"], bool(row["is_open_access"]), int(row["citation_count"]), row["venue"], row["checksum"], row["created_at"])


def _download(row: dict[str, Any]) -> DownloadRecord:
    return DownloadRecord(row["id"], row["project_id"], row["run_id"], row["paper_id"], DownloadStatus(row["status"]), row["source"], row["url"], row["legal_basis"], row["license"], row["path"], row["reason"], row["checksum"], row["created_at"])


def _review(row: dict[str, Any]) -> PaperReview:
    return PaperReview(row["id"], row["project_id"], row["run_id"], row["paper_id"], row["problem"], row["research_question"], row["method"], tuple(_loads(row["datasets_json"], [])), row["experimental_protocol"], tuple(_loads(row["results_json"], [])), tuple(_loads(row["ablations_json"], [])), tuple(_loads(row["limitations_json"], [])), tuple(_loads(row["failure_modes_json"], [])), tuple(_loads(row["evidence_claim_ids_json"], [])), float(row["confidence"]), _provenance(row["provenance_json"]))


def _claim(row: dict[str, Any]) -> EvidenceClaim:
    return EvidenceClaim(row["id"], row["project_id"], row["run_id"], row["paper_id"], row["claim_type"], row["statement"], row["excerpt"], row["page"], row["section"], float(row["confidence"]), _provenance(row["provenance_json"]))


def _score(row: dict[str, Any]) -> PriorityScore:
    return PriorityScore(row["id"], row["project_id"], row["run_id"], row["paper_id"], float(row["semantic_similarity"]), float(row["methodological_similarity"]), float(row["research_question_similarity"]), float(row["recency"]), float(row["influence"]), float(row["evidence_quality"]), float(row["total"]), _loads(row["explanation_json"], {}), _provenance(row["provenance_json"]))


def _gap(row: dict[str, Any]) -> ResearchGap:
    return ResearchGap(row["id"], row["project_id"], row["run_id"], row["statement"], tuple(_loads(row["supporting_paper_ids_json"], [])), tuple(_loads(row["supporting_claim_ids_json"], [])), row["why_unsolved"], row["missing_experiment"], row["falsification_test"], float(row["confidence"]), tuple(_loads(row["counter_evidence_claim_ids_json"], [])), GapClassification(row["classification"]), _provenance(row["provenance_json"]))


_SCHEMA_V1 = """
CREATE TABLE projects(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE pipeline_runs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),kind TEXT NOT NULL,status TEXT NOT NULL,input_json TEXT NOT NULL,started_at TEXT NOT NULL,completed_at TEXT,error TEXT);
CREATE INDEX pipeline_runs_project_idx ON pipeline_runs(project_id,started_at);
CREATE TABLE seed_papers(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),input_type TEXT NOT NULL,input_value TEXT NOT NULL,title TEXT NOT NULL,text TEXT NOT NULL,topic TEXT NOT NULL,research_question TEXT NOT NULL,doi TEXT,url TEXT,methods_json TEXT NOT NULL,datasets_json TEXT NOT NULL,citations_json TEXT NOT NULL,limitations_json TEXT NOT NULL,checksum TEXT NOT NULL,provenance_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE search_events(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),source TEXT NOT NULL,strategy TEXT NOT NULL,query TEXT NOT NULL,requested_limit INTEGER NOT NULL,result_count INTEGER NOT NULL,raw_checksum TEXT NOT NULL,error TEXT,retrieved_at TEXT NOT NULL);
CREATE TABLE papers(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),title TEXT NOT NULL,abstract TEXT NOT NULL,authors_json TEXT NOT NULL,year INTEGER,publication_date TEXT,doi TEXT,arxiv_id TEXT,url TEXT,source_names_json TEXT NOT NULL,open_access_pdf_url TEXT,is_open_access INTEGER NOT NULL,citation_count INTEGER NOT NULL,venue TEXT NOT NULL,checksum TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX papers_project_date_idx ON papers(project_id,publication_date);
CREATE INDEX papers_doi_idx ON papers(doi);
CREATE INDEX papers_arxiv_idx ON papers(arxiv_id);
CREATE TABLE search_candidates(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),paper_id TEXT NOT NULL REFERENCES papers(id),source TEXT NOT NULL,strategy TEXT NOT NULL,source_rank INTEGER NOT NULL,query TEXT NOT NULL,raw_checksum TEXT NOT NULL,retrieved_at TEXT NOT NULL,UNIQUE(run_id,paper_id,source,strategy));
CREATE TABLE discovery_scores(project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),paper_id TEXT NOT NULL REFERENCES papers(id),components_json TEXT NOT NULL,total REAL NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(run_id,paper_id));
CREATE TABLE download_records(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),paper_id TEXT NOT NULL REFERENCES papers(id),status TEXT NOT NULL,source TEXT NOT NULL,url TEXT,legal_basis TEXT,license TEXT,path TEXT,reason TEXT,checksum TEXT,created_at TEXT NOT NULL);
CREATE TABLE evidence_claims(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),paper_id TEXT NOT NULL REFERENCES papers(id),claim_type TEXT NOT NULL,statement TEXT NOT NULL,excerpt TEXT NOT NULL,page INTEGER,section TEXT,confidence REAL NOT NULL,provenance_json TEXT NOT NULL);
CREATE TABLE paper_reviews(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),paper_id TEXT NOT NULL REFERENCES papers(id),problem TEXT NOT NULL,research_question TEXT NOT NULL,method TEXT NOT NULL,datasets_json TEXT NOT NULL,experimental_protocol TEXT NOT NULL,results_json TEXT NOT NULL,ablations_json TEXT NOT NULL,limitations_json TEXT NOT NULL,failure_modes_json TEXT NOT NULL,evidence_claim_ids_json TEXT NOT NULL,confidence REAL NOT NULL,provenance_json TEXT NOT NULL,UNIQUE(run_id,paper_id));
CREATE TABLE review_failures(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),paper_id TEXT NOT NULL REFERENCES papers(id),reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE priority_scores(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),paper_id TEXT NOT NULL REFERENCES papers(id),semantic_similarity REAL NOT NULL,methodological_similarity REAL NOT NULL,research_question_similarity REAL NOT NULL,recency REAL NOT NULL,influence REAL NOT NULL,evidence_quality REAL NOT NULL,total REAL NOT NULL,explanation_json TEXT NOT NULL,provenance_json TEXT NOT NULL,UNIQUE(run_id,paper_id));
CREATE TABLE research_gaps(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),statement TEXT NOT NULL,supporting_paper_ids_json TEXT NOT NULL,supporting_claim_ids_json TEXT NOT NULL,why_unsolved TEXT NOT NULL,missing_experiment TEXT NOT NULL,falsification_test TEXT NOT NULL,confidence REAL NOT NULL,counter_evidence_claim_ids_json TEXT NOT NULL,classification TEXT NOT NULL,provenance_json TEXT NOT NULL);
CREATE TABLE artifacts(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),run_id TEXT NOT NULL REFERENCES pipeline_runs(id),artifact_type TEXT NOT NULL,path TEXT NOT NULL,source_evidence_json TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,prompt_version TEXT NOT NULL,checksum TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE provider_usage(id TEXT PRIMARY KEY,project_id TEXT,run_id TEXT,operation TEXT NOT NULL,role TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,prompt_tokens INTEGER,completion_tokens INTEGER,estimated_cost REAL,currency TEXT,request_id TEXT,latency_ms REAL,created_at TEXT NOT NULL);
CREATE TABLE project_budgets(project_id TEXT PRIMARY KEY REFERENCES projects(id),hard_stop REAL,currency TEXT NOT NULL,updated_at TEXT NOT NULL);
"""

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS pipeline_checkpoints(
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id),
  stage TEXT NOT NULL,
  item_key TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(run_id,stage,item_key)
);
CREATE INDEX IF NOT EXISTS pipeline_checkpoints_status_idx ON pipeline_checkpoints(run_id,status,stage);
CREATE INDEX IF NOT EXISTS download_records_project_paper_idx ON download_records(project_id,paper_id,created_at);
CREATE INDEX IF NOT EXISTS paper_reviews_project_paper_idx ON paper_reviews(project_id,paper_id);
"""

_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS api_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id),
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS api_events_run_idx ON api_events(run_id,id);
CREATE TABLE IF NOT EXISTS api_idempotency(
  key TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id),
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_approvals(
  run_id TEXT PRIMARY KEY REFERENCES pipeline_runs(id),
  status TEXT NOT NULL,
  reason TEXT,
  updated_at TEXT NOT NULL
);
"""

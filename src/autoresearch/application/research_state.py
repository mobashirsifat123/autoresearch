"""Consolidated, read-only research state for one project.

SQLite already stores every stage's truth. This module adds a single
deterministic snapshot that answers the operator's questions without ad-hoc
queries: what is the current question, what is the novelty/methodology state,
what did the experiment do, what is the latest scientific decision, which
recovery action is pending, and what blocks the manuscript.

It is a *view*, not a second source of truth: every field is read from existing
rows. The snapshot is written as a checksummed artifact at the end of each
workflow invocation and can be regenerated at any time.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from autoresearch.domain.models import ArtifactReference, content_checksum, utc_now
from autoresearch.application.recovery import latest_recovery_decision
from autoresearch.storage.repository import ResearchRepository

STATE_VERSION = "research-state-v1"


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list, tuple)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _row(repository: ResearchRepository, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    # Only database-level absence is tolerated; a programming error (renamed
    # column, bad SQL) must not silently look like an empty project.
    try:
        return repository.row(sql, params)
    except sqlite3.Error:
        return None


def _latest_review_audit(repository: ResearchRepository, project_id: str) -> dict[str, Any] | None:
    try:
        rows = repository.rows(
            "SELECT path FROM artifacts WHERE project_id=? AND artifact_type='review_audit' "
            "ORDER BY rowid DESC LIMIT 1",
            (project_id,),
        )
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    try:
        return json.loads(Path(str(rows[0]["path"])).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_research_state(repository: ResearchRepository, project_id: str) -> dict[str, Any] | None:
    project = repository.get_project(project_id)
    if project is None:
        return None

    gap = _row(
        repository,
        "SELECT id, run_id, statement, classification, confidence, "
        "supporting_paper_ids_json, supporting_claim_ids_json, counter_evidence_claim_ids_json "
        "FROM research_gaps WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
        (project_id,),
    )
    portfolio = _row(
        repository,
        "SELECT id, run_id, low_risk_candidate_id, balanced_candidate_id, high_risk_candidate_id "
        "FROM novelty_portfolios WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
        (project_id,),
    )
    freeze = _row(
        repository,
        "SELECT id, run_id, candidate_id, version, artifact_path, checksum "
        "FROM methodology_freezes WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
        (project_id,),
    )
    spec = _row(
        repository,
        "SELECT id, run_id, candidate_id, hypothesis, methodology_freeze_id, gap_id, "
        "monitor_contract_json FROM experiment_specs WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
        (project_id,),
    )
    job = None
    analysis = None
    report = None
    if spec is not None:
        job = _row(
            repository,
            "SELECT id, state, attempt, heartbeat_at, error_json, created_at, updated_at "
            "FROM worker_jobs WHERE spec_id=? ORDER BY rowid DESC LIMIT 1",
            (spec["id"],),
        )
        analysis = _row(
            repository,
            "SELECT id, hypothesis_support_assessment, evidence_status "
            "FROM statistical_analyses WHERE spec_id=? ORDER BY rowid DESC LIMIT 1",
            (spec["id"],),
        )
        if analysis is not None:
            report = _row(
                repository,
                "SELECT id, decision, decision_evidence, uncertainty "
                "FROM experiment_reports WHERE analysis_id=? ORDER BY rowid DESC LIMIT 1",
                (analysis["id"],),
            )
    manuscript = _row(
        repository,
        "SELECT id, status, decision, markdown_path FROM manuscripts "
        "WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
        (project_id,),
    )
    findings = repository.findings_for_project(project_id, limit=50)
    finding_total = int((_row(repository, "SELECT COUNT(*) AS n FROM research_findings WHERE project_id=?", (project_id,)) or {}).get("n") or 0)
    finding_by_status = {
        str(row["status"]): int(row["n"])
        for row in repository.rows(
            "SELECT status, COUNT(*) AS n FROM research_findings WHERE project_id=? GROUP BY status",
            (project_id,),
        )
    }
    recovery = latest_recovery_decision(
        repository,
        project_id=project_id,
        run_id=str(spec["run_id"]) if spec is not None else None,
    )
    audit = _latest_review_audit(repository, project_id)

    job_state = str(job.get("state") or "").upper() if job else ""
    report_decision = str(report.get("decision") or "").lower() if report else ""
    manuscript_status = str(manuscript.get("status") or "").lower() if manuscript else ""

    if manuscript is not None:
        state = "complete" if manuscript_status == "complete" else (
            "draft_gate_failed" if manuscript_status == "draft" else "manuscript_incomplete"
        )
    elif report is not None:
        state = "manuscript_pending"
    elif analysis is not None:
        state = "report_pending"
    elif job_state == "COMPLETED":
        # The process completed but no validated analysis exists yet. This is
        # not "approval pending"; it is an ingestion/analysis gap.
        state = "analysis_pending"
    elif job_state in {"RUNNING", "LEASED", "ANALYZING"}:
        state = "execution_running"
    elif job_state == "QUEUED":
        state = "execution_queued"
    elif job_state in {"FAILED", "STALE", "CANCELED"}:
        state = "operational_failure"
    elif spec is not None:
        state = "approval_pending"
    elif freeze is not None:
        state = "design_pending"
    elif portfolio is not None:
        state = "methodology_pending"
    elif gap is not None:
        state = "novelty_pending"
    else:
        state = "review_pending"

    conflicts = list((audit or {}).get("cross_paper_conflicts") or [])[:10]
    missing = list((audit or {}).get("missing_evidence") or [])[:20]
    open_questions: list[str] = []
    if gap is not None and not _loads(gap.get("counter_evidence_claim_ids_json"), []):
        open_questions.append("No explicit counter-evidence claim was identified for the selected gap.")
    if conflicts:
        open_questions.append(
            f"{len(conflicts)} candidate cross-paper conflict(s) require expert review; "
            "they are not verified contradictions."
        )
    if report_decision in {"refuted", "mechanism_contradicted"}:
        open_questions.append("The proposed mechanism was refuted; novelty must change before another run.")
    if recovery and recovery.get("action") == "revise_novelty" and not any("refuted" in item for item in open_questions):
        open_questions.append("A recovery decision follows a refuted or contradicted mechanism; novelty must change before another experiment.")
    if report_decision in {"no_effect", "insufficient_evidence"}:
        open_questions.append("The experiment did not discriminate the hypothesis; redesign before repeating.")
    if recovery and recovery.get("action") == "redesign_experiment" and not any("discriminate" in item for item in open_questions):
        open_questions.append("A recovery decision requires a more discriminating experiment design.")
    if missing:
        open_questions.append(f"{len(missing)} missing-evidence item(s) recorded by the review audit.")

    if (recovery and state != "complete"
            and recovery.get("next_stage") in {"experiment", "novelty", "review"}):
        state = "recovery_required"

    return {
        "kind": "research_state",
        "version": STATE_VERSION,
        "generated_at": utc_now(),
        "project": {"id": project.id, "title": project.title, "state_db_path": project.state_db_path},
        "state": state,
        "question": (
            {
                "gap_id": gap["id"],
                "run_id": gap["run_id"],
                "statement": gap["statement"],
                "classification": gap["classification"],
                "confidence": gap["confidence"],
                "supporting_papers": len(_loads(gap.get("supporting_paper_ids_json"), [])),
                "supporting_claims": len(_loads(gap.get("supporting_claim_ids_json"), [])),
                "counter_evidence_claims": len(_loads(gap.get("counter_evidence_claim_ids_json"), [])),
            }
            if gap else None
        ),
        "novelty": (
            {
                "portfolio_id": portfolio["id"],
                "candidate_ids": [
                    portfolio.get("low_risk_candidate_id"),
                    portfolio.get("balanced_candidate_id"),
                    portfolio.get("high_risk_candidate_id"),
                ],
                "methodology_freeze": (
                    {
                        "freeze_id": freeze["id"],
                        "candidate_id": freeze["candidate_id"],
                        "version": freeze["version"],
                        "checksum": freeze["checksum"],
                    }
                    if freeze else None
                ),
            }
            if portfolio else None
        ),
        "experiment": (
            {
                "spec_id": spec["id"],
                "run_id": spec["run_id"],
                "candidate_id": spec["candidate_id"],
                "hypothesis": spec["hypothesis"],
                "methodology_freeze_id": spec["methodology_freeze_id"],
                "monitor_signals": len((_loads(spec.get("monitor_contract_json"), {}) or {}).get("monitors") or []),
                "job": (
                    {
                        "job_id": job["id"],
                        "state": job["state"],
                        "attempt": job["attempt"],
                        "heartbeat_at": job.get("heartbeat_at"),
                        "error": _loads(job.get("error_json"), None),
                    }
                    if job else None
                ),
                "analysis_id": analysis["id"] if analysis else None,
                "report_id": report["id"] if report else None,
                "decision": report.get("decision") if report else None,
            }
            if spec else None
        ),
        "findings": {
            "count": finding_total,
            "by_status": dict(sorted(finding_by_status.items())),
            "latest": (
                {
                    "finding_id": findings[0].id,
                    "status": findings[0].status.value,
                    "lesson": findings[0].lesson[:300],
                }
                if findings else None
            ),
        },
        "recovery": (
            {
                "action": recovery.get("action"),
                "next_stage": recovery.get("next_stage"),
                "loop": recovery.get("loop"),
                "outcome": recovery.get("outcome"),
                "rationale": str(recovery.get("rationale") or "")[:400],
                "artifact_path": recovery.get("artifact_path"),
            }
            if recovery else None
        ),
        "literature": {
            "conflict_candidates": conflicts,
            "missing_evidence": missing,
        },
        "manuscript": (
            {
                "manuscript_id": manuscript["id"],
                "status": manuscript["status"],
                "decision": manuscript["decision"],
                "markdown": manuscript.get("markdown_path"),
            }
            if manuscript else None
        ),
        "open_questions": open_questions,
    }


def write_research_state(repository: ResearchRepository, project_id: str) -> dict[str, Any] | None:
    """Write the snapshot to disk and persist it as an artifact when possible."""
    state = build_research_state(repository, project_id)
    if state is None:
        return None
    root = repository.artifact_root / project_id / "state"
    path = root / "research_state.json"
    text = json.dumps(state, indent=2, sort_keys=True, default=str)
    try:
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        state["artifact_path"] = None
        return state
    try:
        run_rows = repository.rows(
            "SELECT id FROM pipeline_runs WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
            (project_id,),
        )
        run_id = str(run_rows[0]["id"]) if run_rows else ""
        if run_id:
            repository.save_artifact(ArtifactReference(
                uuid4().hex, project_id=project_id, run_id=run_id,
                artifact_type="research_state", path=str(path),
                source_evidence=(project_id,),
                provider="deterministic", model="research-state",
                prompt_version=STATE_VERSION, checksum=content_checksum(text),
            ))
    except Exception:  # noqa: BLE001 — snapshot persistence must not break the workflow
        pass
    state["artifact_path"] = str(path)
    return state

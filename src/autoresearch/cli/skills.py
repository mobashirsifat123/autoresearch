"""Stable Claude Code skill bridge and canonical skill management.

Architecture enforced here:

    Claude Skill -> `autoresearch skill ...` -> application service
        -> scientific pipeline -> structured artifact
        -> Claude Code interprets the --json result

The skills never drive an AgentRuntime conversational loop. Each command
calls a direct application service and returns a single JSON document.

Contract guarantees:
    * every command emits exactly one JSON document on stdout (no log noise)
      and structured errors on stderr
    * stable exit codes: 0 ok, 1 internal, 2 usage, 3 approval required,
      4 not found, 5 insufficient evidence, 6 worker unavailable
    * all output is passed through secret redaction
    * experiment planning never executes code; execution requires an exact
      specification approval that this module records only when --accept is
      passed explicitly
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import typer

from autoresearch.application.config import load_settings
from autoresearch.application.factory import ApplicationContainer
from autoresearch.domain.models import (
    EnvironmentManifest,
    ExperimentReport,
    ExperimentSpec,
    NoveltyCandidate,
    ResearchGap,
)
from autoresearch.providers.redaction import redact, redact_text

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_APPROVAL_REQUIRED = 3
EXIT_NOT_FOUND = 4
EXIT_INSUFFICIENT_EVIDENCE = 5
EXIT_WORKER_UNAVAILABLE = 6

# Exit codes for known machine-prefixed error messages ("code: detail").
_KNOWN_CODES = {
    "approval_required": EXIT_APPROVAL_REQUIRED,
    "approval_expired": EXIT_APPROVAL_REQUIRED,
    "checksum_mismatch": EXIT_USAGE,
    "insufficient_evidence": EXIT_INSUFFICIENT_EVIDENCE,
    "metric_schema_invalid": EXIT_INSUFFICIENT_EVIDENCE,
    "validation_failed": EXIT_INSUFFICIENT_EVIDENCE,
    "worker_unavailable": EXIT_WORKER_UNAVAILABLE,
}


@dataclass(frozen=True)
class SkillMeta:
    name: str
    version: str
    description: str
    entry: str


SKILLS = {
    "deep-research": SkillMeta(
        name="deep-research",
        version="1.0.0",
        description="Discover related papers, run an evidence-backed deep review, and surface a cited research gap.",
        entry='autoresearch skill deep-research --seed-paper <pdf|doi|url>',
    ),
    "novelty": SkillMeta(
        name="novelty",
        version="1.0.0",
        description="Generate, prior-art-check, and adversarially critique novelty candidates for a validated gap.",
        entry='autoresearch skill novelty --project <id>',
    ),
    "experiment": SkillMeta(
        name="experiment",
        version="1.0.0",
        description="Plan, approve, execute, inspect, and report a falsifiable experiment from a novelty candidate.",
        entry='autoresearch skill experiment plan --candidate-id <id>',
    ),
}

skill_app = typer.Typer(
    name="skill",
    help="Stable Claude Code skill bridge and canonical skill management.",
    no_args_is_help=True,
)


# --------------------------------------------------------------------------
# Skill management (canonical -> installed)
# --------------------------------------------------------------------------

def _canonical_root() -> Path:
    return Path(__file__).resolve().parents[3] / "claude-skills"


def _user_skills_dir() -> Path:
    configured = os.environ.get("CLAUDE_SKILLS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / "skills"


def _canonical_dir(name: str) -> Path:
    return _canonical_root() / name


def _installed_dir(name: str) -> Path:
    return _user_skills_dir() / name


def _skill_files(name: str) -> list[Path]:
    root = _canonical_dir(name)
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_checksum(name: str) -> str:
    parts = b"\0".join(_sha256(path).encode("ascii") for path in _skill_files(name))
    return hashlib.sha256(parts).hexdigest()


def _provenance_file(name: str) -> Path:
    return _installed_dir(name) / "provenance.json"


def _installed_provenance(name: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_provenance_file(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _install_skill(name: str) -> dict[str, Any]:
    meta = SKILLS.get(name)
    if not meta:
        raise ValueError(f"unknown skill '{name}'; available: {', '.join(sorted(SKILLS))}")
    source = _canonical_dir(name)
    files = _skill_files(name)
    if not files:
        raise ValueError(f"canonical skill '{name}' is missing SKILL.md (expected at {source / 'SKILL.md'})")
    target = _installed_dir(name)
    target.mkdir(parents=True, exist_ok=True)
    for file in files:
        relative = file.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(file.read_bytes())
    checksum = _source_checksum(name)
    _provenance_file(name).write_text(json.dumps({
        "name": name,
        "version": meta.version,
        "source": str(source.resolve()),
        "source_checksum": checksum,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "name": name,
        "version": meta.version,
        "installed": True,
        "path": str(target.resolve()),
        "source_checksum": checksum,
    }


@skill_app.command("list")
def skill_list() -> None:
    """List canonical skills and their install state as JSON."""
    rows = []
    for name in sorted(SKILLS):
        meta = SKILLS[name]
        installed_marker = (_installed_dir(name) / "SKILL.md").is_file()
        canonical_present = (_canonical_dir(name) / "SKILL.md").is_file()
        provenance = _installed_provenance(name)
        synced = bool(provenance and provenance.get("source_checksum") == _source_checksum(name))
        rows.append({
            "name": name,
            "version": meta.version,
            "description": meta.description,
            "entry": meta.entry,
            "canonical_present": canonical_present,
            "installed": installed_marker,
            "synced_with_canonical": synced,
            "canonical_path": str(_canonical_dir(name)),
            "installed_path": str(_installed_dir(name)),
        })
    typer.echo(json.dumps({"ok": True, "operation": "skill/list", "skills": rows}, indent=2))


@skill_app.command("install")
def skill_install(
    names: list[str] = typer.Argument(None, help="Skills to install (default: all)."),
) -> None:
    """Install canonical skills into ~/.claude/skills/ (or $CLAUDE_SKILLS_DIR)."""
    selected = sorted(names or SKILLS)
    results = []
    for name in selected:
        if name not in SKILLS:
            raise typer.BadParameter(f"unknown skill '{name}'; available: {', '.join(sorted(SKILLS))}")
        results.append(_install_skill(name))
    typer.echo(json.dumps({"ok": True, "operation": "skill/install", "skills": results}, indent=2))


@skill_app.command("update")
def skill_update(
    names: list[str] = typer.Argument(None, help="Skills to refresh (default: all)."),
) -> None:
    """Re-sync installed skills from the canonical repo copies."""
    selected = sorted(names or SKILLS)
    results = []
    for name in selected:
        if name not in SKILLS:
            raise typer.BadParameter(f"unknown skill '{name}'; available: {', '.join(sorted(SKILLS))}")
        prior = _installed_provenance(name)
        result = _install_skill(name)
        result["changed"] = bool(prior is None or prior.get("source_checksum") != result["source_checksum"])
        results.append(result)
    typer.echo(json.dumps({"ok": True, "operation": "skill/update", "skills": results}, indent=2))


@skill_app.command("doctor")
def skill_doctor() -> None:
    """Verify canonical skills, installs, repo hygiene, and backend/provider health."""
    checks: list[dict[str, Any]] = []
    for name in sorted(SKILLS):
        present = (_canonical_dir(name) / "SKILL.md").is_file()
        checks.append({
            "check": f"canonical/{name}",
            "ok": present,
            "detail": "SKILL.md present" if present else "missing canonical SKILL.md",
        })
    for name in sorted(SKILLS):
        provenance = _installed_provenance(name)
        if not provenance:
            checks.append({"check": f"installed/{name}", "ok": False, "detail": "not installed; run `autoresearch skill install`"})
        elif provenance.get("source_checksum") != _source_checksum(name):
            checks.append({"check": f"installed/{name}", "ok": False, "detail": "out of date; run `autoresearch skill update`"})
        else:
            checks.append({"check": f"installed/{name}", "ok": True, "detail": "synced with canonical"})
    repo_skills = _canonical_root().parent / ".claude" / "skills"
    if repo_skills.is_dir():
        for entry in sorted(path.name for path in repo_skills.iterdir() if path.is_dir()):
            checks.append({
                "check": f"repo-copy/{entry}",
                "ok": False,
                "detail": "divergent copy under .claude/skills; canonicalize with `autoresearch skill install`",
            })

    required_roles = ("literature_extraction", "research_reasoning", "scientific_critic")

    async def provider_check() -> dict[str, Any]:
        container = ApplicationContainer(load_settings())
        try:
            missing_roles = [role for role in required_roles if role not in container.router.assignments]
            if missing_roles:
                return {"check": "providers", "ok": False, "detail": f"no model assigned to roles: {', '.join(missing_roles)}"}
            return {"check": "providers", "ok": True, "detail": "all research roles assigned"}
        finally:
            await container.close()

    try:
        checks.append(asyncio.run(provider_check()))
    except Exception as exc:  # container construction can fail without valid config
        checks.append({"check": "backend", "ok": False, "detail": redact_text(str(exc))})

    overall = all(check["ok"] for check in checks)
    typer.echo(json.dumps({
        "ok": overall,
        "operation": "skill/doctor",
        "overall": "healthy" if overall else "degraded",
        "exit_code": EXIT_OK if overall else EXIT_ERROR,
        "checks": checks,
    }, indent=2))
    raise typer.Exit(EXIT_OK if overall else EXIT_ERROR)


# --------------------------------------------------------------------------
# Research bridge (direct application services)
# --------------------------------------------------------------------------

def _classify(exc: Exception) -> tuple[str, int]:
    text = str(exc).strip()
    candidate: str | None = None
    if ":" in text:
        head = text.split(":", 1)[0].strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9_]+", head):
            candidate = head
    if candidate in _KNOWN_CODES:
        return candidate, _KNOWN_CODES[candidate]
    if candidate:
        return candidate, EXIT_ERROR
    lowered = text.lower()
    if "not found" in lowered:
        return "not_found", EXIT_NOT_FOUND
    return "internal_error", EXIT_ERROR


def _fail(code: str, exc: Exception, exit_code: int) -> None:
    typer.echo(json.dumps({
        "ok": False,
        "operation": None,
        "error": {
            "code": code,
            "message": redact_text(str(exc)),
            "type": type(exc).__name__,
        },
        "exit_code": exit_code,
    }, indent=2), err=True)
    raise typer.Exit(exit_code)


def _invoke(operation: Callable[[ApplicationContainer], Awaitable[Any]], *, operation_name: str) -> None:
    async def execute() -> Any:
        container = ApplicationContainer(load_settings())
        try:
            return await operation(container)
        finally:
            await container.close()

    try:
        result = asyncio.run(execute())
    except PermissionError as exc:
        _fail("approval_required", exc, EXIT_APPROVAL_REQUIRED)
    except (ValueError, RuntimeError) as exc:
        code, exit_code = _classify(exc)
        _fail(code, exc, exit_code)
    except Exception as exc:
        _fail("internal_error", exc, EXIT_ERROR)

    payload: dict[str, Any] = {"ok": True, "operation": operation_name}
    if isinstance(result, dict):
        payload.update(result)
    else:
        payload["result"] = result
    typer.echo(json.dumps(redact(payload), indent=2, default=str))


def _gap_summary(gap: ResearchGap) -> dict[str, Any]:
    return {
        "id": gap.id,
        "statement": gap.statement,
        "classification": gap.classification.value,
        "confidence": gap.confidence,
        "supporting_paper_ids": list(gap.supporting_paper_ids),
        "supporting_claim_ids": list(gap.supporting_claim_ids),
        "counter_evidence_claim_ids": list(gap.counter_evidence_claim_ids),
        "why_unsolved": gap.why_unsolved,
        "missing_experiment": gap.missing_experiment,
        "falsification_test": gap.falsification_test,
    }


def _discovery_summary(outcome) -> dict[str, Any]:
    return {
        "project_id": outcome.project_id,
        "run_id": outcome.run_id,
        "requested": outcome.requested,
        "retrieved": outcome.retrieved,
        "downloaded": outcome.downloaded,
        "unavailable": outcome.unavailable,
        "rejected": outcome.rejected,
        "failed": outcome.failed,
        "artifact_path": outcome.artifact_path,
    }


def _spec_summary(spec: ExperimentSpec) -> dict[str, Any]:
    return {
        "id": spec.id,
        "candidate_id": spec.candidate_id,
        "run_id": spec.run_id,
        "hypothesis": spec.hypothesis,
        "proposed_change": spec.proposed_change,
        "falsification_condition": spec.falsification_condition,
        "metrics": list(spec.metrics),
        "random_seeds": list(spec.random_seeds),
        "required_seed_count": spec.required_seed_count,
        "statistical_plan": spec.statistical_plan,
        "network_policy": spec.network_policy,
        "target_worker": spec.target_worker,
    }


def _manifest_summary(manifest: EnvironmentManifest | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    return {
        "manifest_id": manifest.id,
        "spec_id": manifest.spec_id,
        "environment_manager": manifest.environment_manager,
        "python_version": manifest.python_version,
        "dependency_lock_checksum": manifest.dependency_lock_checksum,
        "dataset_paths": list(manifest.dataset_paths),
    }


@skill_app.command("status")
def skill_status(
    project: str | None = typer.Option(None, "--project", help="Project ID to summarize."),
) -> None:
    """Summarize the durable research state for a project (or all projects)."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        if project:
            return {"projects": [_project_status(c, project)]}
        rows = c.repository.rows("SELECT id,title,created_at FROM projects ORDER BY created_at DESC")
        return {"projects": [_project_status(c, str(row["id"])) for row in rows[:20]]}

    _invoke(operation, "skill/status")


def _project_status(c: ApplicationContainer, project_id: str) -> dict[str, Any]:
    project = c.repository.get_project(project_id)
    if not project:
        raise ValueError(f"project_not_found: project '{project_id}' not found")
    papers = c.repository.list_papers(project_id)
    latest_gap = c.repository.row(
        "SELECT id,statement,classification FROM research_gaps WHERE project_id=? ORDER BY confidence DESC LIMIT 1",
        (project_id,),
    )
    latest_portfolio = c.repository.row(
        "SELECT id,low_risk_candidate_id,balanced_candidate_id,high_risk_candidate_id FROM novelty_portfolios WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
        (project_id,),
    )
    latest_spec = c.repository.row(
        "SELECT id,candidate_id,hypothesis FROM experiment_specs WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
        (project_id,),
    )
    runs = c.repository.rows(
        "SELECT id,kind,status FROM pipeline_runs WHERE project_id=? ORDER BY started_at DESC",
        (project_id,),
    )
    return {
        "project_id": project.id,
        "title": project.title,
        "paper_count": len(papers),
        "runs": [{"id": row["id"], "kind": row["kind"], "status": row["status"]} for row in runs],
        "latest_gap": _db_gap_summary(latest_gap) if latest_gap else None,
        "latest_novelty_portfolio": dict(latest_portfolio) if latest_portfolio else None,
        "latest_experiment_spec": dict(latest_spec) if latest_spec else None,
    }


def _db_gap_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "statement": row["statement"], "classification": row["classification"]}


@skill_app.command("deep-research")
def deep_research(
    seed_paper: str = typer.Option("", "--seed-paper", help="Seed PDF path, DOI, or HTTPS URL."),
    from_catalog: Path | None = typer.Option(None, "--from-catalog", help="Local JSON paper catalog."),
    project: str | None = typer.Option(None, "--project", help="Existing project ID; review its corpus."),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    since_year: int = typer.Option(datetime.now(timezone.utc).year - 3, "--since-year", min=1900),
    top_k: int = typer.Option(5, "--top-k", min=1, max=15),
    hierarchical: bool = typer.Option(False, "--hierarchical/--flat", help="Chunk-level hierarchical review for long PDFs."),
) -> None:
    """Discover related papers, run a deep review, and surface one cited gap."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        if project:
            existing = c.repository.get_project(project)
            if not existing:
                raise ValueError(f"project_not_found: project '{project}' not found")
            discovery_summary: dict[str, Any] = {"project_id": project, "used_existing": True}
        elif from_catalog:
            outcome = await c.discovery.import_catalog(from_catalog)
            project = outcome.project_id
            discovery_summary = _discovery_summary(outcome)
        elif seed_paper:
            outcome = await c.discovery.discover(seed_paper, limit=limit, since_year=since_year)
            project = outcome.project_id
            discovery_summary = _discovery_summary(outcome)
        else:
            raise ValueError("usage: provide --seed-paper, --from-catalog, or --project")
        review = await c.review.review(project, top_k=top_k, hierarchical=hierarchical)
        return {
            "project_id": review.project_id,
            "discovery": discovery_summary,
            "review_run_id": review.run_id,
            "reviewed": review.reviewed,
            "failed_reviews": review.failed,
            "top_paper_ids": list(review.top_paper_ids),
            "gap": _gap_summary(review.gap),
            "json_report": review.json_report,
            "markdown_report": review.markdown_report,
        }

    _invoke(operation, "deep-research")


@skill_app.command("review")
def review(
    project: str = typer.Option(..., "--project", help="Project ID returned by discover."),
    top_k: int = typer.Option(5, "--top-k", min=1, max=15),
    hierarchical: bool = typer.Option(False, "--hierarchical/--flat", help="Chunk-level hierarchical review for long PDFs."),
) -> None:
    """Run the deep review over an existing project corpus."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        outcome = await c.review.review(project, top_k=top_k, hierarchical=hierarchical)
        return {
            "project_id": outcome.project_id,
            "review_run_id": outcome.run_id,
            "reviewed": outcome.reviewed,
            "failed_reviews": outcome.failed,
            "top_paper_ids": list(outcome.top_paper_ids),
            "gap": _gap_summary(outcome.gap),
            "json_report": outcome.json_report,
            "markdown_report": outcome.markdown_report,
        }

    _invoke(operation, "review")


@skill_app.command("novelty")
def novelty(
    project: str = typer.Option(..., "--project", help="Project ID with a validated research gap."),
    gap_id: str | None = typer.Option(None, "--gap-id", help="Specific gap; default is the highest-confidence gap."),
) -> None:
    """Generate, prior-art-check, and adversarially critique novelty candidates."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        existing = c.repository.get_project(project)
        if not existing:
            raise ValueError(f"project_not_found: project '{project}' not found")
        portfolio_id = await c.novelty.generate_portfolio(project, gap_id=gap_id)
        portfolio = c.repository.row("SELECT * FROM novelty_portfolios WHERE id=?", (portfolio_id,))
        if not portfolio:
            raise ValueError(f"portfolio_not_found: novelty portfolio '{portfolio_id}' not found")
        candidate_ids = tuple(
            value for value in (portfolio["low_risk_candidate_id"], portfolio["balanced_candidate_id"], portfolio["high_risk_candidate_id"])
            if value
        )
        return {
            "project_id": project,
            "portfolio_id": portfolio_id,
            "gap_id": gap_id,
            "candidate_ids": list(candidate_ids),
            "low_risk_candidate_id": portfolio["low_risk_candidate_id"],
            "balanced_candidate_id": portfolio["balanced_candidate_id"],
            "high_risk_candidate_id": portfolio["high_risk_candidate_id"],
        }

    _invoke(operation, "novelty")


# --------------------------------------------------------------------------
# Experiment lifecycle sub-command
# --------------------------------------------------------------------------

experiment_app = typer.Typer(
    name="experiment",
    help="Falsifiable experiment lifecycle from a novelty candidate. Planning never executes code.",
    no_args_is_help=True,
)


def _require_candidate(c: ApplicationContainer, candidate_id: str) -> NoveltyCandidate:
    candidate = c.repository.get_novelty_candidate(candidate_id)
    if not candidate:
        raise ValueError(f"novelty_candidate_not_found: candidate '{candidate_id}' not found")
    return candidate


@experiment_app.command("plan")
def experiment_plan(
    candidate_id: str = typer.Option(..., "--candidate-id", help="Novelty candidate to translate into a spec."),
) -> None:
    """Translate a novelty candidate into an approval-gated ExperimentSpec (no code runs)."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        _require_candidate(c, candidate_id)
        spec_id = await c.experiment.execute_candidate(candidate_id, mode="plan")
        spec = c.repository.get_experiment_spec_by_id(spec_id)
        preview = c.experiment.approval_preview_for_candidate(candidate_id)
        return {
            "spec_id": spec_id,
            "candidate_id": candidate_id,
            "run_id": spec.run_id,
            "status": "planned",
            "executed": False,
            "approval_required": True,
            "approval": {
                "worker": preview["worker"],
                "actions": preview["actions"],
                "checksum": preview["checksum"],
                "budget": preview["budget"],
            },
            "spec": _spec_summary(spec),
        }

    _invoke(operation, "experiment/plan")


@experiment_app.command("validate")
def experiment_validate(
    candidate_id: str = typer.Option(..., "--candidate-id", help="Novelty candidate to validate."),
) -> None:
    """Prepare and verify the execution environment for a spec (no code runs)."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        _require_candidate(c, candidate_id)
        spec_id = await c.experiment.execute_candidate(candidate_id, mode="validate")
        spec = c.repository.get_experiment_spec_by_id(spec_id)
        manifest = c.repository.get_environment_manifest(spec_id)
        return {
            "spec_id": spec_id,
            "candidate_id": candidate_id,
            "run_id": spec.run_id,
            "status": "validated",
            "executed": False,
            "environment": _manifest_summary(manifest),
        }

    _invoke(operation, "experiment/validate")


@experiment_app.command("approve")
def experiment_approve(
    candidate_id: str = typer.Option(..., "--candidate-id", help="Novelty candidate whose spec is being approved."),
    accept: bool = typer.Option(False, "--accept", help="Record the exact-spec approval. Without it nothing is written."),
) -> None:
    """Preview the exact-spec approval; with --accept record it to unblock execution."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        _require_candidate(c, candidate_id)
        preview = c.experiment.approval_preview_for_candidate(candidate_id)
        if not accept:
            return {
                "approved": False,
                "reason": "approval not recorded; review the exact spec preview and re-run with --accept",
                "preview": _approval_preview(preview),
            }
        run_id = str(preview["run_id"])
        approval = c.repository.save_exact_approval({
            "id": uuid4().hex,
            "run_id": run_id,
            "specification_checksum": str(preview["checksum"]),
            "approver": "claude-code-skill",
            "approved_scope": "local-experiment",
            "approved_target": str(preview["worker"]),
            "approved_budget": float(preview["budget"]),
            "expires_at": "9999-12-31T23:59:59+00:00",
            "status": "pending",
            "approved_actions": preview["actions"],
        })
        approved = c.repository.approve_exact(str(approval["id"]), approver="claude-code-skill") or approval
        return {
            "approved": True,
            "approval_id": approval["id"],
            "status": "approved",
            "checksum": preview["checksum"],
            "preview": _approval_preview(preview),
        }

    _invoke(operation, "experiment/approve")


def _approval_preview(preview: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": preview["run_id"],
        "spec_id": preview["spec_id"],
        "candidate_id": preview["candidate_id"],
        "checksum": preview["checksum"],
        "worker": preview["worker"],
        "actions": preview["actions"],
        "budget": preview["budget"],
    }


@experiment_app.command("execute")
def experiment_execute(
    candidate_id: str = typer.Option(..., "--candidate-id", help="Novelty candidate whose approved spec to execute."),
) -> None:
    """Execute an approved exact-spec action plan through the policy-controlled worker."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        _require_candidate(c, candidate_id)
        spec = c.repository.get_experiment_spec(candidate_id)
        if not spec:
            raise ValueError("approval_required: plan the experiment before execution")
        preview = c.experiment.approval_preview_for_candidate(candidate_id)
        existing = c.repository.approval_by_checksum(str(preview["checksum"]))
        if not existing or existing.get("status") != "approved":
            raise PermissionError(
                "approval_required: exact-spec approval not granted; run "
                "`autoresearch skill experiment approve --candidate-id <id> --accept` first"
            )
        spec_id = await c.experiment.execute_candidate(candidate_id, mode="execute")
        run = c.repository.get_run(spec.run_id)
        jobs = c.repository.worker_jobs_for_run(spec.run_id)
        return {
            "spec_id": spec_id,
            "candidate_id": candidate_id,
            "run_id": spec.run_id,
            "run_status": run.status.value if run else None,
            "jobs": jobs,
        }

    _invoke(operation, "experiment/execute")


@experiment_app.command("inspect")
def experiment_inspect(
    candidate_id: str = typer.Option(..., "--candidate-id", help="Novelty candidate whose spec to inspect."),
) -> None:
    """Inspect the durable environment manifest for a spec (no code runs)."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        _require_candidate(c, candidate_id)
        spec_id = await c.experiment.execute_candidate(candidate_id, mode="inspect")
        spec = c.repository.get_experiment_spec_by_id(spec_id)
        manifest = c.repository.get_environment_manifest(spec_id)
        return {
            "spec_id": spec_id,
            "candidate_id": candidate_id,
            "run_id": spec.run_id,
            "status": "inspected",
            "executed": False,
            "environment": _manifest_summary(manifest),
        }

    _invoke(operation, "experiment/inspect")


@experiment_app.command("report")
def experiment_report(
    candidate_id: str = typer.Option(..., "--candidate-id", help="Novelty candidate whose executed experiment to report."),
) -> None:
    """Write the scientific report for a completed, analyzed experiment."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        _require_candidate(c, candidate_id)
        spec = c.repository.get_experiment_spec(candidate_id)
        if not spec:
            raise ValueError("insufficient_evidence: no experiment spec exists for this candidate")
        analysis = c.repository.get_statistical_analysis(spec.id)
        if not analysis:
            raise ValueError(
                "insufficient_evidence: no completed statistical analysis exists for this candidate; "
                "execute the approved experiment first"
            )
        report: ExperimentReport = await c.experiment.pipeline.report_writer.write(spec, analysis)
        c.repository.save_experiment_report(report)
        return {
            "report_id": report.id,
            "analysis_id": analysis.id,
            "spec_id": spec.id,
            "candidate_id": candidate_id,
            "decision": report.decision.value,
            "decision_evidence": report.decision_evidence,
        }

    _invoke(operation, "experiment/report")


@experiment_app.command("cancel")
def experiment_cancel(
    run_id: str = typer.Option(..., "--run-id", help="Experiment run ID to cancel."),
) -> None:
    """Cancel an experiment run that has not reached a terminal state."""

    async def operation(c: ApplicationContainer) -> dict[str, Any]:
        run = c.repository.get_run(run_id)
        if not run:
            raise ValueError(f"run_not_found: run '{run_id}' not found")
        c.repository.cancel_run(run_id)
        return {"run_id": run_id, "status": "cancelled"}

    _invoke(operation, "experiment/cancel")


skill_app.add_typer(experiment_app)

"""Purpose: One durable coordinator for the literature-to-experiment workflow.

Advances a project through review → question → hypotheses → methodology →
design → (authorized) execution, reusing completed durable state instead of
re-running stages. Not a second orchestrator: every stage delegates to the
existing services (DiscoveryService, LiteratureReviewService, NoveltyService,
MethodologyFreezer, ExperimentService), which persist all
runs and artifacts. The stage dispositions returned here are a per-invocation
report over that durable state, not themselves persisted.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from autoresearch.application.services import (
    DiscoveryService,
    ExperimentService,
    LiteratureReviewService,
    NoveltyService,
)
from autoresearch.application.publication_contract import build_contract
from autoresearch.application.publication_readiness import assess_publication_readiness
from autoresearch.application.research_state import write_research_state
from autoresearch.application.venue_profiles import get_venue_profile
from autoresearch.experiments.publication_design import provenance_sources
from autoresearch.domain.models import PipelineKind, RunStatus
from autoresearch.novelty.methodology import MethodologyFreezer
from autoresearch.providers.router import ModelRouter
from autoresearch.storage.repository import ResearchRepository

logger = logging.getLogger(__name__)

STOP_STAGES = ("review", "question", "hypotheses", "methodology", "design", "experiment", "manuscript")
_STAGE_ORDER = {name: index for index, name in enumerate(STOP_STAGES)}

# Only these report decisions would justify a follow-up research cycle;
# retained as documentation of the loop policy (the automated revision cycle
# itself was removed with the minimal five-skill distribution).
_REVISION_WORTHY_DECISIONS = {
    "refuted", "insufficient_evidence", "no_effect",
    "implementation_failure", "optimization_failure", "data_limitation",
}


class WorkflowService:
    def __init__(
        self,
        repository: ResearchRepository,
        router: ModelRouter,
        *,
        discovery: DiscoveryService,
        review: LiteratureReviewService,
        novelty: NoveltyService,
        experiment: ExperimentService,
        manuscript: "ManuscriptService | None" = None,
    ) -> None:
        self.repository = repository
        self.router = router
        self.discovery = discovery
        self.review = review
        self.novelty = novelty
        self.experiment = experiment
        self.manuscript = manuscript
        self.freezer = MethodologyFreezer(repository, router)

    async def run(
        self,
        *,
        seed: str | None = None,
        project_id: str | None = None,
        until: str = "design",
        limit: int = 20,
        top_k: int = 5,
        max_cost_usd: float | None = None,
        max_total_tokens: int | None = None,
        publication_track: bool = False,
        venue_name: str | None = None,
    ) -> dict[str, Any]:
        if until not in _STAGE_ORDER:
            raise ValueError(f"until must be one of {STOP_STAGES}")
        if not seed and not project_id:
            raise ValueError("Provide a seed (PDF path, DOI, or HTTPS URL) or an existing project_id.")
        if seed and project_id:
            raise ValueError("Provide either a seed or a project_id, not both.")

        dispositions: list[dict[str, Any]] = []
        self._workflow_id = uuid4().hex[:12]
        self._publication_gaps: list[dict[str, Any]] = []
        self._freeze_gaps: list[dict[str, Any]] = []
        self._design_gaps: list[dict[str, Any]] = []
        self._publication_paths: dict[str, str] = {}
        self._review_corpus: dict[str, Any] = {}
        self._stage_metrics: list[dict[str, Any]] = []
        # Preserve an explicitly configured manuscript profile unless the
        # caller asks for a different one for this run.
        self._venue_name = venue_name or (
            self.manuscript.venue.name if self.manuscript is not None else "generic_journal"
        )
        if self.manuscript is not None and venue_name:
            self.manuscript.venue = get_venue_profile(venue_name)
        venue_name = self._venue_name
        _last_stage_time = time.monotonic()

        def record(stage: str, status: str, **ids: Any) -> None:
            nonlocal _last_stage_time
            now = time.monotonic()
            elapsed = round(now - _last_stage_time, 3)
            _last_stage_time = now
            dispositions.append({"stage": stage, "status": status, "elapsed_seconds": elapsed, **ids})
            self._stage_metrics.append({
                "stage": stage, "status": status, "elapsed_seconds": elapsed,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            self._emit_progress(stage, status, until)

        async def stage_invoke(stage: str, operation, *, retry_transient: bool = True):
            """Run one stage call with resilience semantics:
            - transient provider errors (throttle/timeout/outage) retry once;
            - a hard failure is annotated with the stage name and re-raised
              (the CLI classifies the ORIGINAL error, so exit codes stay
              meaningful) — completed earlier stages remain reusable."""
            from autoresearch.application.services import _with_transient_retry

            try:
                if retry_transient:
                    return await _with_transient_retry(operation, operation_name=f"workflow stage {stage}")
                return await operation()
            except BaseException as exc:
                try:
                    exc.add_note(f"workflow stage: {stage}")
                except AttributeError:  # pragma: no cover — non-Exception bases
                    pass
                raise

        # ---- Stage 0/1: discovery + review (produces the question/gap) ----
        project = project_id
        if project is None:
            discovery = await stage_invoke("discovery", lambda: self.discovery.discover(seed, limit=limit, max_cost_usd=max_cost_usd))
            project = discovery.project_id
            record("discovery", "completed", project_id=project, run_id=discovery.run_id, papers=discovery.retrieved)
        self._publication_project = project

        # The provider budget is project-scoped durable state: set it once
        # here so it binds resume invocations and every later stage, not only
        # the discovery/review services that also accept it. None preserves
        # the existing setting for that dimension.
        if max_cost_usd is not None or max_total_tokens is not None:
            self.repository.set_project_budget(
                project, hard_stop=max_cost_usd, token_ceiling=max_total_tokens,
            )

        gap = self._completed_gap(project)
        if gap is not None:
            record("review", "reused", project_id=project, run_id=gap["run_id"], gap_id=gap["id"])
            record("question", "reused", gap_id=gap["id"], classification=gap["classification"])
        else:
            review = await stage_invoke("review", lambda: self.review.review(project, top_k=top_k, max_cost_usd=max_cost_usd))
            gap = {"id": review.gap.id, "run_id": review.run_id, "classification": review.gap.classification.value}
            record("review", "completed", project_id=project, run_id=review.run_id,
                   gap_id=review.gap.id, report=review.json_report,
                   review_corpus=review.review_corpus_path,
                   citation_contexts=review.citation_contexts_path,
                   publication_gaps=list(review.publication_gaps))
            record("question", "completed", gap_id=review.gap.id, classification=review.gap.classification.value)
        self._review_corpus = self._load_review_corpus(str(gap["run_id"]))
        contract = build_contract(
            project_id=project,
            venue=get_venue_profile(venue_name),
            review_corpus=self._review_corpus,
        )
        self._publish_contract(project, contract)
        if publication_track and self._fatal_for_stage("review"):
            record("review_gate", "publication_gap", gaps=self._fatal_for_stage("review"))
            return self._result(
                project, dispositions, stopped_at="review",
                note="Publication track stopped before novelty: review corpus gaps. "
                     "Run review with --limit 40 --top-k 20 (or fix the listed gaps), then resume.",
            )
        if _STAGE_ORDER[until] <= _STAGE_ORDER["question"]:
            return self._result(project, dispositions, stopped_at=until)

        # ---- Stage 2: hypotheses (novelty portfolio for THIS question) ----
        portfolio_row = self.repository.latest_novelty_portfolio(project)
        if portfolio_row is not None:
            # Coherence: a portfolio generated for a different gap must not be
            # reused for the question this run reports.
            portfolio_gap = self.repository.row(
                "SELECT gap_id FROM novelty_candidates WHERE id=?",
                (portfolio_row.low_risk_candidate_id or portfolio_row.balanced_candidate_id or portfolio_row.high_risk_candidate_id,),
            )
            if portfolio_gap and str(portfolio_gap["gap_id"]) != str(gap["id"]):
                record("hypotheses", "regenerated", reason="existing portfolio targets a different question",
                       previous_portfolio_id=portfolio_row.id)
                portfolio_row = None
        if portfolio_row is not None:
            record("hypotheses", "reused", portfolio_id=portfolio_row.id,
                   candidates=[portfolio_row.low_risk_candidate_id, portfolio_row.balanced_candidate_id, portfolio_row.high_risk_candidate_id])
        else:
            # A dead novelty process leaves an orphaned 'running' run that
            # blocks clean status forever; candidates and critiques are
            # durable per run, and generation is cheap to restart.
            self._retire_stale_running(project, PipelineKind.NOVELTY)

            async def _generate_once():
                return await self.novelty.generate_portfolio(project, gap_id=str(gap["id"]))

            try:
                portfolio_id = await stage_invoke("hypotheses", _generate_once)
            except RuntimeError as exc:
                # Zero candidates passing the gates is an HONEST outcome but
                # also model-sample dependent: one bounded second sample is
                # justified before reporting it as a dead end.
                if "insufficient_evidence" in str(exc):
                    record("hypotheses", "retrying", reason=str(exc)[:200])
                    portfolio_id = await stage_invoke("hypotheses", _generate_once)
                else:
                    raise
            portfolio_row = self.repository.get_novelty_portfolio_by_id(str(portfolio_id))
            if portfolio_row is None:
                raise RuntimeError(f"portfolio_missing_after_generation: {portfolio_id}")
            record("hypotheses", "completed", portfolio_id=portfolio_row.id,
                   candidates=[portfolio_row.low_risk_candidate_id, portfolio_row.balanced_candidate_id, portfolio_row.high_risk_candidate_id])
        if _STAGE_ORDER[until] <= _STAGE_ORDER["hypotheses"]:
            return self._result(project, dispositions, stopped_at=until)

        # ---- Stage 3: methodology freeze (versioned) ----
        freeze = self.repository.latest_methodology_freeze(project)
        if freeze is not None:
            record("methodology", "reused", methodology_freeze_id=freeze.id, version=freeze.version,
                   candidate_id=freeze.candidate_id, artifact=freeze.artifact_path)
        else:
            candidate_id = portfolio_row.low_risk_candidate_id or portfolio_row.balanced_candidate_id or portfolio_row.high_risk_candidate_id
            candidate = self.repository.get_novelty_candidate(str(candidate_id))
            if candidate is None:
                raise RuntimeError(f"portfolio_candidate_missing: {candidate_id}")
            rationale = (
                "Workflow default: the portfolio's low-risk candidate (highest feasibility and "
                "compute efficiency under the diversity-aware selector). Override by freezing a "
                "different candidate explicitly."
            )
            # The freeze attaches to the candidate's own novelty run via its
            # provenance; no extra mislabeled run is created.
            freeze = await stage_invoke("methodology", lambda: self.freezer.freeze(
                candidate, selection_rationale=rationale, portfolio_id=str(portfolio_row.id),
                venue_profile=get_venue_profile(venue_name),
            ))
            record("methodology", "completed", methodology_freeze_id=freeze.id, version=freeze.version,
                   candidate_id=freeze.candidate_id, artifact=freeze.artifact_path)
        from autoresearch.novelty.methodology import publication_design_gaps as methodology_design_gaps

        methodology_payload: dict[str, Any] = {}
        try:
            methodology_payload = json.loads(
                Path(freeze.artifact_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            methodology_payload = {}
        candidate_obj = self.repository.get_novelty_candidate(freeze.candidate_id)
        contract = build_contract(
            project_id=project,
            venue=get_venue_profile(venue_name),
            review_corpus=self._review_corpus,
            candidate=candidate_obj,
            methodology=methodology_payload,
        )
        self._freeze_gaps = (
            methodology_design_gaps(
                methodology_payload, profile=get_venue_profile(venue_name)
            )
            if methodology_payload
            else [{
                "code": "methodology_artifact_unreadable",
                "stage": "novelty",
                "message": "frozen methodology artifact is unreadable",
                "fatal": True,
            }]
        )
        self._design_gaps = []
        self._publish_contract(project, contract)
        if publication_track and self._fatal_for_stage("novelty"):
            record("methodology_gate", "publication_gap", gaps=self._fatal_for_stage("novelty"))
            return self._result(
                project, dispositions, stopped_at="methodology",
                note="Publication track stopped before design: the frozen methodology lacks "
                     "a publication-grade design (baselines, ablation, power, datasets, or delta). "
                     "Fix the freeze/design and resume.",
            )
        if _STAGE_ORDER[until] <= _STAGE_ORDER["methodology"]:
            return self._result(project, dispositions, stopped_at=until)

        # ---- Stage 4: experimental design (consumes the freeze) ----
        candidate_id = freeze.candidate_id
        spec = self.repository.get_experiment_spec(candidate_id)
        if spec is not None and spec.methodology_freeze_id == freeze.id:
            record("design", "reused", spec_id=spec.id, gap_id=spec.gap_id,
                   methodology_freeze_id=spec.methodology_freeze_id)
        elif spec is not None:
            # Re-planning would REPLACE the spec row; specs with dependent
            # rows (manifests, jobs, analyses) cannot be replaced without
            # breaking foreign keys. Surface the conflict instead of crashing.
            dependents = self.repository.row(
                "SELECT (SELECT COUNT(*) FROM environment_manifests WHERE spec_id=?1)"
                " + (SELECT COUNT(*) FROM worker_jobs WHERE spec_id=?1)"
                " + (SELECT COUNT(*) FROM statistical_analyses WHERE spec_id=?1) AS n",
                (spec.id,),
            )
            if int(dependents["n"]) > 0:
                record("design", "spec_conflict",
                       reason="a spec with executed/validated history exists for a different freeze version",
                       spec_id=spec.id, spec_freeze_id=spec.methodology_freeze_id,
                       expected_freeze_id=freeze.id)
                return self._result(project, dispositions, stopped_at="design",
                                    note="Resolve the spec conflict manually (new candidate or explicit spec removal) before re-planning.")
            previous_spec_id = spec.id
            spec_id = await stage_invoke("design", lambda: self.experiment.execute_candidate(candidate_id, mode="plan"))
            spec = self.repository.get_experiment_spec(candidate_id)
            record("design", "replanned", spec_id=spec_id, gap_id=spec.gap_id,
                   methodology_freeze_id=spec.methodology_freeze_id,
                   previous_spec_id=previous_spec_id)
        else:
            spec_id = await stage_invoke("design", lambda: self.experiment.execute_candidate(candidate_id, mode="plan"))
            spec = self.repository.get_experiment_spec(candidate_id)
            record("design", "completed", spec_id=spec_id, gap_id=spec.gap_id,
                   methodology_freeze_id=spec.methodology_freeze_id)

        from autoresearch.experiments.publication_design import publication_design_gaps

        design_gaps = publication_design_gaps(
            spec, profile=get_venue_profile(venue_name)
        )
        contract = build_contract(
            project_id=project,
            venue=get_venue_profile(venue_name),
            review_corpus=self._review_corpus,
            candidate=candidate_obj,
            methodology=methodology_payload,
            spec=spec,
        )
        self._design_gaps = design_gaps
        self._freeze_gaps = []
        self._publish_contract(project, contract)
        design_fatal = [
            gap for gap in design_gaps if gap.get("fatal", True)
        ]
        if publication_track and design_fatal:
            record("design_gate", "publication_gap", gaps=design_fatal)
            return self._result(
                project, dispositions, stopped_at="design",
                note="Publication track stopped before implementation/execution: "
                     "the experiment design does not meet the publication contract. "
                     "Fix the listed design gaps and re-plan.",
            )

        # ---- Stage 4b: implementation (research code generation, verified) ----
        # The methodology→code gap: if no experiment script exists in the spec's
        # repository, generate one through the router and statically verify it
        # (mechanism coverage + measurement contract). Failures are recorded,
        # never masked — an unverified implementation blocks honest execution.
        script_path = Path(str(spec.repository)) / "scripts" / "run_experiment.py"
        implementation_status = "reused"
        implementation_verified = False
        try:
            # Verify an existing user/generated script as well; a present file
            # is not evidence that it meets the static contract.
            implementation = await self.experiment.implement_candidate(candidate_id)
            implementation_status = str(implementation.get("status", "unknown"))
            implementation_verified = str(implementation.get("fidelity") or "") == "STATIC_VERIFIED"
            record("implementation", implementation_status,
                   spec_id=spec.id, script=implementation.get("script_path"),
                   fidelity=implementation.get("fidelity"),
                   mechanism_coverage=implementation.get("verification", {}).get("mechanism_coverage"))
        except Exception as exc:  # generation failure is reported, never masked
            implementation_status = "failed"
            implementation_verified = False
            record("implementation", "failed", error=f"{type(exc).__name__}: {exc}"[:300])

        if _STAGE_ORDER[until] <= _STAGE_ORDER["design"]:
            return self._result(project, dispositions, stopped_at=until)

        # A generated or reused implementation that failed deterministic
        # verification must never reach approval/execution.
        if implementation_status == "failed" or not implementation_verified:
            return self._result(
                project, dispositions, stopped_at="implementation",
                note="Implementation failed static verification; repair or regenerate it before "
                     "approval. No execution was attempted.",
            )

        # ---- Stage 5: execution — only under genuine existing approval ----
        package = self.experiment.approval_preview_for_candidate(candidate_id)
        approval = self.repository.approval_by_checksum(str(package["checksum"]))
        if not approval or approval.get("status") != "approved":
            record("experiment", "awaiting_approval", spec_id=spec.id,
                   checksum=package["checksum"], actions=package["actions"],
                   implementation_checksum=package.get("implementation_checksum"),
                   implementation_verified=package.get("implementation_verified"),
                   monitor_contract=package.get("monitor_contract"),
                   missing_requirement="Exact-specification approval for the checksum above.")
            return self._result(project, dispositions, stopped_at="experiment",
                                note="Execution package prepared; approval required before any run.")

        try:
            await stage_invoke("experiment", lambda: self.experiment.execute_candidate(candidate_id, mode="execute"))
        except RuntimeError as exc:
            message = str(exc)
            recovery = self.experiment.recovery_for_candidate(candidate_id) or {}
            record(
                "experiment",
                "cancelled" if "execution_cancelled" in message else (
                    "worker_unavailable" if "worker_unavailable" in message else "execution_failed"
                ),
                spec_id=spec.id,
                error=message,
                recovery_action=recovery.get("action"),
                recovery_next_stage=recovery.get("next_stage"),
                recovery_loop=recovery.get("loop"),
                recovery_artifact=recovery.get("artifact_path"),
            )
            return self._result(project, dispositions, stopped_at="experiment")
        # Execution completing proves only that the approved action ended; a
        # scientific report exists only after artifacts are ingested and
        # analyzed. Never fabricate a decision from a bare exit code.
        report_row = self._latest_report_for_candidate(candidate_id)
        if report_row is None:
            recovery = self.experiment.recovery_for_candidate(candidate_id) or {}
            record("experiment", "executed_analysis_pending", spec_id=spec.id,
                   recovery_action=recovery.get("action"),
                   recovery_next_stage=recovery.get("next_stage"),
                   recovery_loop=recovery.get("loop"),
                   recovery_artifact=recovery.get("artifact_path"),
                   note="Approved action completed; awaiting artifact ingestion and statistical analysis before any interpretation.")
        else:
            recovery = self.experiment.recovery_for_candidate(candidate_id) or {}
            record("experiment", "completed", spec_id=spec.id, report_id=report_row["id"],
                   decision=report_row["decision"],
                   recovery_action=recovery.get("action"),
                   recovery_next_stage=recovery.get("next_stage"),
                   recovery_loop=recovery.get("loop"),
                   recovery_artifact=recovery.get("artifact_path"))
            self._rebuild_post_execution_contract(project, candidate_id)

        # ---- Stage 7: manuscript (consumes the exact validated lineage) ----
        if until == "manuscript":
            if self.manuscript is None:
                record("manuscript", "unavailable", reason="manuscript service not configured in this coordinator")
            else:
                report_row = self._latest_report_for_candidate(candidate_id)
                if report_row is None:
                    record("manuscript", "blocked",
                           reason="no analyzed experiment report for the selected candidate; "
                                  "advance execution (with measurement ingestion and analysis) first",
                           candidate_id=candidate_id, spec_id=spec.id)
                else:
                    self._retire_stale_running(project, PipelineKind.MANUSCRIPT)
                    try:
                        outcome = await stage_invoke(
                            "manuscript",
                            lambda: self.manuscript.generate(
                                project,
                                publication_contract_path=self._publication_paths.get(
                                    "publication_contract"
                                ),
                            ),
                        )
                        record("manuscript", str(outcome.get("status", "unknown")),
                               manuscript_id=outcome.get("manuscript_id"),
                               decision=outcome.get("decision"),
                               submission_ready=outcome.get("submission_ready"),
                               readiness=(outcome.get("readiness") or {}).get("readiness"),
                               review=(outcome.get("review") or {}).get("meta_decision"),
                               evidence_gaps=outcome.get("evidence_gaps", []),
                               markdown=outcome.get("artifacts", {}).get("markdown"))
                    except Exception as exc:  # generation failure is reported, never masked
                        record("manuscript", "failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return self._result(project, dispositions, stopped_at=until)

    # ------------------------------------------------------------------

    def _retire_stale_running(self, project_id: str, kind: PipelineKind) -> None:
        """Cancel orphaned 'running' runs of a kind before starting new work.

        A killed process leaves its run marked running forever, which blocks
        clean status and confuses reuse. Durable artifacts (candidates,
        checkpoints, analyses) are reused across runs, so retiring an orphan
        loses nothing."""
        for row in self.repository.rows(
            "SELECT id FROM pipeline_runs WHERE project_id=? AND kind=? AND status=?",
            (project_id, kind.value, RunStatus.RUNNING.value),
        ):
            self.repository.cancel_run(row["id"])

    def _latest_report_for_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        """Latest experiment report keyed through the candidate's spec run
        (reports are stamped with spec.run_id, never spec.id)."""
        return self.repository.row(
            "SELECT id, decision FROM experiment_reports WHERE run_id IN"
            " (SELECT run_id FROM experiment_specs WHERE candidate_id=?)"
            " ORDER BY rowid DESC LIMIT 1",
            (candidate_id,),
        )

    def _completed_gap(self, project_id: str) -> dict[str, Any] | None:
        """Latest gap of a completed review run for the project."""
        row = self.repository.row(
            "SELECT g.id, g.run_id, g.classification, r.status AS run_status"
            " FROM research_gaps g JOIN pipeline_runs r ON r.id = g.run_id"
            " WHERE g.project_id=? ORDER BY g.rowid DESC LIMIT 1",
            (project_id,),
        )
        if row and row["run_status"] == RunStatus.COMPLETED.value:
            return row
        return None

    def _emit_progress(self, stage: str, status: str, until: str) -> None:
        """Step-level progress for the requested loop (never time estimates)."""
        from autoresearch.application.progress import report

        alias = {"implementation": "design"}
        current = alias.get(stage, stage)
        if current not in STOP_STAGES or until not in STOP_STAGES:
            return
        index = STOP_STAGES.index(current)
        target = STOP_STAGES.index(until)
        total = target + 1
        completed = min(index + 1, total)
        remaining = list(STOP_STAGES[index + 1: total])
        report(f"workflow.{current}", completed, total, detail=f"{current} {status}", remaining=remaining)

    def _load_review_corpus(self, run_id: str) -> dict[str, Any]:
        checkpoint = self.repository.checkpoint(run_id, "review_publication")
        payload = dict((checkpoint or {}).get("payload") or {})
        path = payload.get("review_corpus")
        if not path:
            return {}
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _publish_contract(self, project: str, contract) -> None:
        """Persist the shared contract and cumulative publication gaps."""
        from autoresearch.application.publication_contract import contract_slices

        root = self.repository.artifact_root / "workflows" / self._workflow_id
        root.mkdir(parents=True, exist_ok=True)
        contract_path = root / "publication_contract.json"
        gaps_path = root / "publication_gaps.json"
        contract_path.write_text(
            json.dumps(contract.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        # Rebuild from the current contract plus the current stage slices.
        # Never keep a satisfied code from an older stage.
        merged = {gap.code: gap.to_dict() for gap in contract.gaps}
        for gap in [*self._freeze_gaps, *self._design_gaps]:
            if isinstance(gap, dict) and gap.get("code"):
                merged[str(gap["code"])] = gap
        self._publication_gaps = list(merged.values())
        gaps_path.write_text(
            json.dumps(
                {
                    "workflow_id": self._workflow_id,
                    "project_id": project,
                    "gaps": self._publication_gaps,
                    "slices": [item.to_dict() for item in contract_slices(contract)],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._publication_paths = {
            "publication_contract": str(contract_path),
            "publication_gaps": str(gaps_path),
        }

    def _rebuild_post_execution_contract(self, project: str, candidate_id: str):
        """Rebuild the contract with the executed analysis/report, not the plan."""
        spec = self.repository.get_experiment_spec(candidate_id)
        if spec is None:
            return None
        analysis = self.repository.get_statistical_analysis(spec.id)
        if analysis is None:
            return None
        report = self.repository.get_experiment_report(analysis.id)
        if report is None:
            return None
        reviewed_ids = set(self._review_corpus.get("reviewed_paper_ids") or [])
        rows = self.repository.rows(
            "SELECT * FROM papers WHERE project_id=?", (project,)
        )
        corpus = [row for row in rows if str(row.get("id")) in reviewed_ids] or rows
        venue = get_venue_profile(self._venue_name)
        readiness = assess_publication_readiness(
            project_id=project,
            spec=spec,
            analysis=analysis,
            report=report,
            papers=corpus,
            corpus_papers=corpus,
            venue=venue,
            execution_verified=True,
        )
        contract = build_contract(
            project_id=project,
            venue=venue,
            review_corpus=self._review_corpus,
            candidate=self.repository.get_novelty_candidate(candidate_id),
            spec=spec,
            analysis=analysis,
            report=report,
            execution_verified=True,
            study_type=readiness.study_type,
            provenance_sources=provenance_sources(self.repository, spec),
        )
        self._freeze_gaps = []
        self._design_gaps = []
        self._publish_contract(project, contract)
        return contract

    def _fatal_for_stage(self, stage: str) -> list[dict[str, Any]]:
        return [
            gap for gap in self._publication_gaps
            if gap.get("stage") == stage and gap.get("fatal", True)
        ]

    def _result(self, project: str, dispositions: list[dict[str, Any]], *, stopped_at: str, note: str | None = None) -> dict[str, Any]:
        root = self.repository.artifact_root / "workflows" / getattr(
            self, "_workflow_id", uuid4().hex[:12]
        )
        root.mkdir(parents=True, exist_ok=True)
        metrics_path = root / "stage_metrics.json"
        totals = self.repository.row(
            "SELECT COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
            "COALESCE(SUM(estimated_cost),0) AS cost FROM provider_usage WHERE project_id=?",
            (project,),
        ) or {}
        metrics_path.write_text(
            json.dumps(
                {
                    "workflow_id": getattr(self, "_workflow_id", ""),
                    "project_id": project,
                    "stages": getattr(self, "_stage_metrics", []),
                    "prompt_tokens": int(totals.get("prompt_tokens") or 0),
                    "completion_tokens": int(totals.get("completion_tokens") or 0),
                    "estimated_cost": float(totals.get("cost") or 0.0),
                    "publication_gaps": getattr(self, "_publication_gaps", []),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        state_snapshot = write_research_state(self.repository, project)
        result: dict[str, Any] = {
            "workflow_id": getattr(self, "_workflow_id", uuid4().hex[:12]),
            "project_id": project,
            "stopped_at": stopped_at,
            "stages": dispositions,
            "research_state": (state_snapshot or {}).get("state"),
            "pending_recovery": (state_snapshot or {}).get("recovery"),
            "publication_gaps": getattr(self, "_publication_gaps", []),
            "artifacts": {
                "stage_metrics": str(metrics_path),
                **getattr(self, "_publication_paths", {}),
                **({"research_state": state_snapshot["artifact_path"]} if state_snapshot and state_snapshot.get("artifact_path") else {}),
            },
        }
        if note:
            result["note"] = note
        return result

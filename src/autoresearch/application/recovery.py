"""Deterministic recovery policy: fast operational loop vs slow scientific loop.

An executed command is not a scientific result, and a failed command is not
automatically a failed hypothesis. This module classifies the two:

- **fast loop** — the run was operationally invalid (crash, timeout, stall,
  numerical blow-up, integrity mismatch). The next action is a bounded retry,
  an implementation repair, or a parameter adjustment. The hypothesis is not
  revised.
- **slow loop** — the run completed and the deterministic analysis produced a
  scientific decision. The next action may advance to the manuscript, redesign
  the experiment, return to novelty (refuted/contradicted mechanism), return to
  review (data/domain limitation), or stop.

The policy returns a **recommendation** with its rationale and evidence ids.
Expensive actions are never auto-executed: execution always needs a fresh
exact-spec approval, and re-entering novelty/review needs an explicit user
resume. The decision is persisted as a checksummed artifact and an event so the
next stage and the operator see the same record.

This is a deterministic adaptation of EmbodiedAct's hot-fix / local-adjust /
replan budgets. No LLM is called here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from autoresearch.domain.models import ArtifactReference, content_checksum, utc_now
from autoresearch.storage.repository import ResearchRepository

POLICY_VERSION = "recovery-policy-v1"

FAST_ACTIONS = {"retry_same_spec", "repair_implementation", "adjust_parameters"}
SLOW_ACTIONS = {"redesign_experiment", "revise_novelty", "request_more_evidence", "stop", "advance_manuscript"}


@dataclass(frozen=True)
class RecoveryDecision:
    project_id: str
    run_id: str
    spec_id: str
    source: str                 # execution | report
    outcome: str
    loop: str                   # fast | slow | none
    action: str
    next_stage: str             # experiment | novelty | review | manuscript | none
    rationale: str
    evidence_ids: tuple[str, ...] = ()
    requires_approval: bool = True
    requires_user_action: bool = True
    policy_version: str = POLICY_VERSION
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "recovery_decision",
            "id": self.id,
            "created_at": self.created_at,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "spec_id": self.spec_id,
            "source": self.source,
            "outcome": self.outcome,
            "loop": self.loop,
            "action": self.action,
            "next_stage": self.next_stage,
            "rationale": self.rationale,
            "evidence_ids": list(self.evidence_ids),
            "requires_approval": self.requires_approval,
            "requires_user_action": self.requires_user_action,
            "policy_version": self.policy_version,
        }


def _monitor_reason(monitor: Mapping[str, Any] | None) -> str:
    if not monitor:
        return ""
    return str(monitor.get("abort_reason") or monitor.get("reason") or "")[:300]


def classify_execution_failure(
    *,
    project_id: str,
    run_id: str,
    spec_id: str,
    state: str,
    error_category: str = "",
    error_reason: str = "",
    attempt: int = 0,
    monitor: Mapping[str, Any] | None = None,
) -> RecoveryDecision:
    """Map an operational execution failure to a bounded next action."""
    category = (error_category or "UNKNOWN").upper()
    reason = (error_reason or _monitor_reason(monitor) or "execution did not complete")[:500]
    evidence = tuple(item for item in (spec_id, str((monitor or {}).get("monitor_report") or "")) if item)

    if state.upper() == "CANCELED":
        return RecoveryDecision(
            project_id, run_id, spec_id, "execution", state, "none", "stop", "none",
            "Execution was cancelled by the operator; do not retry without a new instruction.",
            evidence,
        )

    if any(token in reason.lower() for token in ("implementation_integrity", "result_integrity")):
        return RecoveryDecision(
            project_id, run_id, spec_id, "execution", category, "fast", "repair_implementation", "experiment",
            "The executed script or the stored measurement result failed an integrity check. "
            "Regenerate and re-verify the implementation (or discard the tampered result), "
            "then approve the new exact action. Do not ingest edited bytes.",
            evidence,
        )

    if category == "PERCEPTION_ABORT":
        return RecoveryDecision(
            project_id, run_id, spec_id, "execution", category, "fast", "repair_implementation", "experiment",
            f"Runtime perception stopped the run before completion: {reason}. "
            "Treat this as an implementation/operational defect first (numerical stability, bounds, "
            "or a broken probe), not as evidence against the scientific hypothesis. "
            "A parameter change is justified only after the cause is identified.",
            evidence,
        )

    if category in {"STALL", "TIMEOUT"}:
        if attempt < 2:
            action = "retry_same_spec"
            rationale = (
                f"{category.lower()} occurred on attempt {attempt}: {reason}. "
                "One bounded retry of the SAME approved specification is permitted because the "
                "scientific design did not change. A second failure requires parameter or design review."
            )
        else:
            action = "adjust_parameters"
            rationale = (
                f"{category.lower()} repeated on attempt {attempt}: {reason}. "
                "Do not retry unchanged; inspect the compute budget, checkpoint cadence, and runtime "
                "parameters before a new approval."
            )
        return RecoveryDecision(project_id, run_id, spec_id, "execution", category, "fast", action, "experiment", rationale, evidence)

    if category in {"WORKER_UNAVAILABLE", "ENVIRONMENT"}:
        return RecoveryDecision(
            project_id, run_id, spec_id, "execution", category, "fast", "retry_same_spec", "experiment",
            f"The execution worker or environment was unavailable: {reason}. "
            "This is operational; retry the same approved action after the worker is restored.",
            evidence,
        )

    # Non-zero exit, worker exception, or an unclassified process failure:
    # the implementation is the first suspect. Never silently rerun it.
    return RecoveryDecision(
        project_id, run_id, spec_id, "execution", category, "fast", "repair_implementation", "experiment",
        f"The approved process did not complete cleanly ({category}: {reason}). "
        "Repair the implementation or environment, re-run static verification, and re-approve. "
        "Do not present this as a scientific result.",
        evidence,
    )


def classify_report_decision(
    *,
    project_id: str,
    run_id: str,
    spec_id: str,
    decision: str,
    report_id: str = "",
    analysis_id: str = "",
    uncertainty: str = "",
) -> RecoveryDecision:
    """Map a completed, deterministic scientific decision to the next stage."""
    value = (decision or "insufficient_evidence").lower()
    evidence = tuple(item for item in (spec_id, analysis_id, report_id) if item)
    suffix = f" Reported uncertainty: {uncertainty[:240]}" if uncertainty else ""

    if value in {"supported", "partially_supported"}:
        return RecoveryDecision(
            project_id, run_id, spec_id, "report", value, "none", "advance_manuscript", "manuscript",
            f"The deterministic analysis reported '{value}' against the preregistered gates; "
            "the evidence chain may proceed to the manuscript, which applies its own readiness gate. "
            "A partial result must carry its limitations into the manuscript." + suffix,
            evidence, requires_approval=False, requires_user_action=False,
        )

    if value in {"refuted", "mechanism_contradicted"}:
        return RecoveryDecision(
            project_id, run_id, spec_id, "report", value, "slow", "revise_novelty", "novelty",
            "The preregistered gates rejected the proposed mechanism. A renamed rerun is not a new "
            "method: return to novelty with the recorded refutation and require an explicit mechanism "
            "change before another experiment." + suffix,
            evidence,
        )

    if value in {"no_effect", "insufficient_evidence"}:
        return RecoveryDecision(
            project_id, run_id, spec_id, "report", value, "slow", "redesign_experiment", "experiment",
            "The study did not discriminate the hypothesis (null or underpowered/inconclusive). "
            "Redesign for discriminability (stronger manipulation, lower-variance metric, more seeds) "
            "rather than patching code; if no design can discriminate it, stop." + suffix,
            evidence,
        )

    if value in {"implementation_failure", "evaluation_artifact"}:
        return RecoveryDecision(
            project_id, run_id, spec_id, "report", value, "fast", "repair_implementation", "experiment",
            "The measurements or evaluation artifacts failed integrity/provenance checks. "
            "This is an implementation/measurement defect, not a scientific outcome." + suffix,
            evidence,
        )

    if value == "optimization_failure":
        return RecoveryDecision(
            project_id, run_id, spec_id, "report", value, "fast", "adjust_parameters", "experiment",
            "Optimization failed while the method remained testable; tune the optimization parameters "
            "within the same preregistered design, then re-approve. This is not a hypothesis revision." + suffix,
            evidence,
        )

    if value in {"data_limitation", "domain_specific"}:
        return RecoveryDecision(
            project_id, run_id, spec_id, "report", value, "slow", "request_more_evidence", "review",
            "The result is bounded by data/domain coverage. Return to the literature and evidence "
            "corpus to determine whether the question is answerable with the available data before "
            "spending more compute." + suffix,
            evidence,
        )

    if value == "cost_prohibitive":
        return RecoveryDecision(
            project_id, run_id, spec_id, "report", value, "slow", "stop", "none",
            "The approved budget cannot answer the question. Stop rather than silently shrinking the "
            "design below its preregistered power/validity requirements." + suffix,
            evidence,
        )

    return RecoveryDecision(
        project_id, run_id, spec_id, "report", value, "slow", "stop", "none",
        f"Unrecognized decision '{value}'; stop and require human review rather than inventing a next action." + suffix,
        evidence,
    )


def write_recovery_decision(
    repository: ResearchRepository,
    decision: RecoveryDecision,
) -> dict[str, Any]:
    """Persist the decision as a checksummed artifact and an API event."""
    payload = decision.to_dict()
    root = repository.artifact_root / decision.project_id / decision.run_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"recovery_decision_{decision.source}_{decision.id[:8]}.json"
    payload["artifact_path"] = str(path)
    text = json.dumps(payload, indent=2, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    repository.save_artifact(ArtifactReference(
        uuid4().hex,
        project_id=decision.project_id,
        run_id=decision.run_id,
        artifact_type="recovery_decision",
        path=str(path),
        source_evidence=decision.evidence_ids,
        provider="deterministic",
        model="recovery-policy",
        prompt_version=POLICY_VERSION,
        checksum=content_checksum(text),
    ))
    repository.append_api_event(decision.run_id, "research.recovery_decision", {
        "action": decision.action,
        "next_stage": decision.next_stage,
        "loop": decision.loop,
        "outcome": decision.outcome,
        "rationale": decision.rationale[:400],
        "artifact": str(path),
    })
    return payload


def latest_recovery_decision(
    repository: ResearchRepository,
    *,
    project_id: str,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    """Read the newest persisted recovery decision (project-scoped)."""
    if run_id:
        rows = repository.rows(
            "SELECT path FROM artifacts WHERE project_id=? AND run_id=? "
            "AND artifact_type='recovery_decision' ORDER BY rowid DESC LIMIT 1",
            (project_id, run_id),
        )
    else:
        rows = repository.rows(
            "SELECT path FROM artifacts WHERE project_id=? AND artifact_type='recovery_decision' "
            "ORDER BY rowid DESC LIMIT 1",
            (project_id,),
        )
    if not rows:
        return None
    try:
        return json.loads(Path(str(rows[0]["path"])).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

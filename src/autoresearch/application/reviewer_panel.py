"""Deterministic reviewer panel and meta-review.

This is a hostile *checklist*, not a substitute for peer review. It scores the
draft on the dimensions reviewers actually use (novelty, rigor, clarity,
reproducibility, significance), records concrete required fixes, and refuses
to pass a paper whose readiness gate, body hygiene, or venue lint failed.

The panel is deliberately deterministic so it can run offline and in CI. A
model-based reviewer ensemble can be added behind the same interface later;
the gate must never treat a model's opinion as ground truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from autoresearch.application.body_hygiene import HygieneReport
from autoresearch.application.contribution_contract import ContributionContract
from autoresearch.application.literature_synthesis import LiteratureSynthesis
from autoresearch.application.publication_readiness import ReadinessReport
from autoresearch.application.venue_profiles import VenueLint, VenueProfile


def _sections(markdown: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = ""
    for line in markdown.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = re.sub(r"^\d+\.\s*", "", match.group(1)).strip()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
    return sections


def _title(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    return ""


@dataclass(frozen=True)
class ReviewerScore:
    reviewer: str
    novelty: float
    rigor: float
    clarity: float
    reproducibility: float
    significance: float
    strengths: tuple[str, ...]
    weaknesses: tuple[str, ...]
    required_fixes: tuple[str, ...]
    decision: str

    @property
    def mean(self) -> float:
        return round(
            (self.novelty + self.rigor + self.clarity + self.reproducibility + self.significance) / 5.0,
            2,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer": self.reviewer,
            "scores": {
                "novelty": self.novelty,
                "rigor": self.rigor,
                "clarity": self.clarity,
                "reproducibility": self.reproducibility,
                "significance": self.significance,
            },
            "mean": self.mean,
            "strengths": list(self.strengths),
            "weaknesses": list(self.weaknesses),
            "required_fixes": list(self.required_fixes),
            "decision": self.decision,
        }


@dataclass
class ReviewerReport:
    reviews: tuple[ReviewerScore, ...]
    meta_decision: str
    meta_rationale: str
    scores: dict[str, float]
    required_fixes: list[str] = field(default_factory=list)
    blocking_issues: list[str] = field(default_factory=list)
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "reviewer_panel",
            "reviews": [review.to_dict() for review in self.reviews],
            "meta_decision": self.meta_decision,
            "meta_rationale": self.meta_rationale,
            "scores": dict(self.scores),
            "required_fixes": list(self.required_fixes),
            "blocking_issues": list(self.blocking_issues),
            "passed": self.passed,
        }


def _clamp(value: float) -> float:
    return float(max(1, min(5, round(value, 1))))


def deterministic_review(
    markdown: str,
    *,
    readiness: ReadinessReport,
    contract: ContributionContract | None,
    synthesis: LiteratureSynthesis | None,
    hygiene: HygieneReport,
    venue_lint_result: VenueLint,
    profile: VenueProfile,
    has_exact_commands: bool = False,
) -> ReviewerReport:
    """Score the draft and produce the meta-review that gates submission."""
    sections = _sections(markdown)
    title = _title(markdown)
    abstract = sections.get("Abstract", "")
    discussion = sections.get("Discussion", "")
    results = sections.get("Results", "")
    limitations_block = ""
    match = re.search(r"### Study Limitations[^\n]*\n(.*?)(?=\n### |\Z)", discussion, re.DOTALL)
    if match:
        limitations_block = match.group(1)
    limitation_count = len(re.findall(r"^\d+\.\s", limitations_block, re.MULTILINE))

    required_fixes: list[str] = []
    required_fixes.extend(readiness.blocking_issues)
    required_fixes.extend(f"body hygiene: {issue.code} at line {issue.line}" for issue in hygiene.errors)
    required_fixes.extend(f"venue lint: {violation}" for violation in venue_lint_result.violations)
    if not abstract.strip():
        required_fixes.append("missing abstract")
    if limitation_count < 3:
        required_fixes.append(f"only {limitation_count} numbered study limitations (need >= 3)")
    if not results.strip():
        required_fixes.append("missing Results section")
    required_fixes = list(dict.fromkeys(required_fixes))

    novelty = 2.0
    if contract is not None:
        if contract.closest_prior_work:
            novelty += 1.0
        if contract.contributions:
            novelty += 0.5
        if contract.contributions and contract.contributions[0].novelty_delta:
            novelty += 0.5
    if synthesis is not None and synthesis.papers:
        novelty += 0.5
    if readiness.study_type == "simulation":
        novelty = min(novelty, 3.0)

    rigor = 1.0
    if readiness.provenance.real_execution:
        rigor += 1.0
    if readiness.evidence.power_established:
        rigor += 1.0
    if readiness.evidence.external_baselines >= profile.min_baselines:
        rigor += 1.0
    if readiness.evidence.ablations >= profile.min_ablations:
        rigor += 1.0
    if readiness.evidence.n_seeds >= 5:
        rigor += 0.5
    if readiness.study_type == "simulation":
        rigor = min(rigor, 2.0)

    clarity = 3.0
    if title and "…" not in title and len(title.split()) <= 20:
        clarity += 0.5
    if abstract.strip():
        clarity += 0.5
    if limitation_count >= 3:
        clarity += 0.5
    if hygiene.errors:
        clarity -= min(2.0, 0.5 * len(hygiene.errors))
    if not synthesis or not synthesis.papers:
        clarity -= 0.5
    if "Statistical tests (deterministic" in results:
        clarity -= 1.0

    reproducibility = 1.0
    if readiness.provenance.dataset_checksum:
        reproducibility += 0.5
    if readiness.provenance.repository_commit:
        reproducibility += 0.5
    if readiness.evidence.n_seeds >= 3:
        reproducibility += 1.0
    if has_exact_commands:
        reproducibility += 1.0
    if readiness.provenance.real_execution:
        reproducibility += 1.0
    if readiness.study_type == "simulation":
        reproducibility = min(reproducibility, 2.5)

    significance = 3.0
    if contract is not None and contract.takeaway:
        significance += 0.5
    if readiness.evidence.decision in ("supported", "partially_supported"):
        significance += 0.5
    if readiness.study_type == "simulation":
        significance -= 1.0
    if not readiness.submission_ready:
        significance -= 0.5

    scores = {
        "novelty": _clamp(novelty),
        "rigor": _clamp(rigor),
        "clarity": _clamp(clarity),
        "reproducibility": _clamp(reproducibility),
        "significance": _clamp(significance),
    }

    strengths: list[str] = []
    weaknesses: list[str] = []
    if readiness.provenance.real_execution:
        strengths.append("measurements are backed by a verified execution identity")
    else:
        weaknesses.append("measurement provenance is unverified or simulated")
    if synthesis and synthesis.papers:
        strengths.append(f"related work synthesizes {len(synthesis.papers)} reviewed papers")
    else:
        weaknesses.append("no multi-paper literature synthesis")
    if limitation_count >= 3:
        strengths.append("study limitations are enumerated")
    else:
        weaknesses.append("fewer than three study limitations")
    if readiness.evidence.power_established:
        strengths.append("power is discussed")
    else:
        weaknesses.append("power is not established")
    if readiness.evidence.external_baselines < profile.min_baselines:
        weaknesses.append("external baselines are missing")
    if readiness.evidence.ablations < profile.min_ablations:
        weaknesses.append("ablations are missing")
    if hygiene.errors:
        weaknesses.append(f"{len(hygiene.errors)} body-hygiene errors")
    if not venue_lint_result.passed:
        weaknesses.append("venue lint has violations")

    mean_score = round(sum(scores.values()) / len(scores), 2)
    if not readiness.submission_ready or readiness.readiness == "not_ready":
        decision = "reject"
    elif mean_score < profile.reviewer_threshold or any(score < 3.0 for score in scores.values()):
        decision = "major_revision"
    elif required_fixes:
        decision = "major_revision" if len(required_fixes) > 3 else "minor_revision"
    elif all(score >= 4.0 for score in scores.values()):
        decision = "accept"
    else:
        decision = "minor_revision"

    review = ReviewerScore(
        reviewer="deterministic panel (novelty, rigor, clarity, reproducibility, significance)",
        novelty=scores["novelty"],
        rigor=scores["rigor"],
        clarity=scores["clarity"],
        reproducibility=scores["reproducibility"],
        significance=scores["significance"],
        strengths=tuple(strengths),
        weaknesses=tuple(weaknesses),
        required_fixes=tuple(required_fixes),
        decision=decision,
    )

    blocking = list(readiness.blocking_issues)
    blocking.extend(f"body hygiene: {issue.code}" for issue in hygiene.errors)
    blocking.extend(f"venue lint: {violation}" for violation in venue_lint_result.violations)
    blocking = list(dict.fromkeys(blocking))
    passed = (
        decision == "accept"
        and mean_score >= profile.reviewer_threshold
        and not required_fixes
        and not blocking
        and venue_lint_result.passed
        and hygiene.passed
    )
    rationale = (
        f"Meta-review: {decision.upper()} (mean score {mean_score}/5, threshold "
        f"{profile.reviewer_threshold}). {len(required_fixes)} required fix(es); "
        f"{len(blocking)} blocking issue(s). This is a deterministic checklist, not peer review."
    )
    return ReviewerReport(
        reviews=(review,),
        meta_decision=decision,
        meta_rationale=rationale,
        scores={**scores, "mean": mean_score},
        required_fixes=required_fixes,
        blocking_issues=blocking,
        passed=passed,
    )

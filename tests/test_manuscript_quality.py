"""Adversarial manuscript-quality tests.

These tests encode the failures found in the AutoFID manuscript:

* a template-perfect paper that no reviewer would accept;
* the model writer rejecting its own results table and cited literature;
* a simulation presented as measurement;
* a three-paper "literature review" instead of a corpus synthesis.

They are regression tests for the publication gate, not for the FastTrack
checklist. A paper can pass the checklist and still fail every test here.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoresearch.application.body_hygiene import audit_body
from autoresearch.application.claim_ledger import build_claim_ledger
from autoresearch.application.contribution_contract import pick_title
from autoresearch.application.literature_synthesis import build_literature_synthesis
from autoresearch.application.manuscript import ManuscriptService
from autoresearch.application.publication_readiness import (
    EvidenceSufficiency,
    ProvenanceReport,
    ReadinessReport,
    assess_publication_readiness,
)
from autoresearch.application.reviewer_panel import deterministic_review
from autoresearch.application.venue_profiles import VenueLint, get_venue_profile, venue_lint
from autoresearch.storage.repository import ResearchRepository


def _analysis(seeds: int = 5) -> SimpleNamespace:
    seed_rows = tuple(
        {
            "seed": index,
            "baseline": {"accuracy": 0.80},
            "candidate": {"accuracy": 0.81 + index * 0.001},
        }
        for index in range(seeds)
    )
    return SimpleNamespace(
        id="analysis1",
        baseline_metrics={"accuracy": 0.80},
        proposed_metrics={"accuracy": 0.81},
        effect_size={"accuracy": 0.01},
        confidence_intervals={"accuracy": (0.79, 0.82)},
        seed_level_results=seed_rows,
        ablation_contributions={},
        parameter_count_diff=0,
        flops_latency_diff=None,
        training_inference_cost=0.0,
        failure_case_analysis="",
        hypothesis_support_assessment="supported",
        decision_gates={"accuracy_gate": True},
        statistical_tests={
            "accuracy": {
                "test": "wilcoxon_signed_rank",
                "alternative": "two-sided",
                "exact": True,
                "p_value": 0.0625,
                "status": "VALID",
                "raw_observations": [
                    {"baseline": 0.80, "candidate": 0.81 + index * 0.001}
                    for index in range(seeds)
                ],
            }
        },
        evidence_status="supported",
    )


def _paper() -> dict:
    return {
        "id": "p1",
        "title": "Benchmarking Autonomous Agents",
        "abstract": (
            "The top model reaches a 40% success rate. FIRE-Bench reports a "
            "46.7 F1 with a 95% confidence interval of 0.81-0.83. The benchmark "
            "covers 100 tasks."
        ),
        "year": 2026,
        "venue": "Test Venue",
    }


def _ledger():
    analysis = _analysis()
    table = (
        "| Metric | Baseline (mean±std) | Candidate (mean±std) | Δ mean | Cohen's d | exact p | n | Gate |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| accuracy | 0.8000 ±0.0000 | 0.8100 ±0.0063 | +0.0100 | +3.010 | 0.0625 | 5 | PASS |"
    )
    return build_claim_ledger(
        gap={"statement": "Scores are not comparable across benchmarks.", "supporting_paper_ids_json": '["p1"]'},
        candidate=SimpleNamespace(id="c1", method="Calibration protocol", difference_from_existing="A shared anchor set."),
        spec=None,
        analysis=analysis,
        report=None,
        papers=[_paper()],
        keys={"p1": "paper1"},
        tables_md=table,
        decision="supported",
    )


def test_numeric_audit_accepts_results_table_and_cited_literature():
    """Regression: the old audit rejected p, d, std, n, and cited values."""
    ledger = _ledger()
    draft = (
        "| Metric | Baseline (mean±std) | Candidate (mean±std) | Δ mean | Cohen's d | exact p | n | Gate |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| accuracy | 0.8000 ±0.0000 | 0.8100 ±0.0063 | +0.0100 | +3.010 | 0.0625 | 5 | PASS |\n\n"
        "Prior work reported a 40% success rate and a 46.7 F1 [@paper1], with a "
        "confidence interval of 0.81-0.83 over 100 tasks."
    )
    violations = ledger.audit_text(draft)
    assert violations == [], violations


def test_numeric_audit_rejects_a_genuinely_unsupported_number():
    ledger = _ledger()
    violations = ledger.audit_text("We additionally evaluated 12345 held-out samples.")
    assert violations
    assert violations[0]["token"] == "12345"


def test_cited_value_requires_the_matching_citation_key():
    ledger = build_claim_ledger(
        gap={"statement": "A question."},
        candidate=None,
        spec=None,
        analysis=_analysis(),
        report=None,
        papers=[
            {"id": "p1", "title": "Paper One", "abstract": "Reported 40% success.", "year": 2026},
            {"id": "p2", "title": "Paper Two", "abstract": "Reported 46.7 F1.", "year": 2026},
        ],
        keys={"p1": "paper1", "p2": "paper2"},
        tables_md="",
        decision="supported",
    )
    wrong = ledger.audit_text("Prior work reported 46.7 F1 [@paper1].")
    assert wrong and "matching citation" in wrong[0]["reason"], wrong
    right = ledger.audit_text("Prior work reported 46.7 F1 [@paper2].")
    assert right == [], right
    uncited = ledger.audit_text("Our method reaches 46.7 F1.")
    assert uncited, uncited


def test_results_cannot_claim_a_gap_or_literature_number():
    ledger = build_claim_ledger(
        gap={"statement": "Prior work reported 46.7 F1."},
        candidate=None,
        spec=None,
        analysis=_analysis(),
        report=None,
        papers=[{"id": "p1", "title": "Paper One", "abstract": "Reported 46.7 F1.", "year": 2026}],
        keys={"p1": "paper1"},
        tables_md="",
        decision="supported",
    )
    violations = ledger.audit_text("## 4. Results\n\nOur method reaches 46.7 F1.")
    assert violations, "a literature value was accepted as a result"
    assert any(item["token"] == "46.7" for item in violations)
    cited = ledger.audit_text("## 4. Results\n\nOur method reaches 46.7 F1 [@paper1].")
    assert cited, "a cited literature value was accepted as our result"


def test_abstract_results_cannot_claim_a_gap_number():
    ledger = build_claim_ledger(
        gap={"statement": "Prior work reported 46.7 F1."},
        candidate=None,
        spec=None,
        analysis=_analysis(),
        report=None,
        papers=[{"id": "p1", "title": "Paper One", "abstract": "Reported 46.7 F1.", "year": 2026}],
        keys={"p1": "paper1"},
        tables_md="",
        decision="supported",
    )
    markdown = (
        "## Abstract\n\n"
        "**Background:** Prior work reported 46.7 F1.\n\n"
        "**Results:** Our method reaches 46.7 F1.\n"
    )
    violations = ledger.audit_text(markdown)
    assert violations
    assert any(item["token"] == "46.7" for item in violations)


def test_numeric_audit_does_not_use_substring_matches():
    ledger = _ledger()
    # The ledger contains a 2026 paper year; a fabricated "20" must not be
    # accepted just because "2026" contains it.
    violations = ledger.audit_text("We evaluated 20 additional samples.")
    assert any(item["token"] == "20" for item in violations), violations


def test_numbered_list_marker_is_exempt_but_the_body_is_not():
    ledger = _ledger()
    violations = ledger.audit_text("1. We evaluated 12345 additional samples.")
    tokens = {item["token"] for item in violations}
    assert "1" not in tokens
    assert "12345" in tokens
    paren = ledger.audit_text("1) We evaluated 12345 additional samples.")
    paren_tokens = {item["token"] for item in paren}
    assert "1" not in paren_tokens
    assert "12345" in paren_tokens


def test_numeric_audit_exempts_claim_ids_and_years():
    ledger = _ledger()
    draft = (
        "The claim 8e936bada4094d22b2f5185777861493 was recorded in 2026, "
        "and the citation key is [@paper1]."
    )
    assert ledger.audit_text(draft) == []


def test_readiness_detects_simulation_and_blocks_empirical_submission(tmp_path):
    script = tmp_path / "scripts" / "run_experiment.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "def run():\n"
        "    # Simulate GPT-5.2 reference agent and judge on anchor tasks\n"
        "    return 0\n",
        encoding="utf-8",
    )
    spec = SimpleNamespace(
        repository=str(tmp_path),
        dataset_checksum="not_computed_no_checksum_supplied",
        repository_commit="not_applicable_no_commit_supplied",
        statistical_plan="Power is not established.",
        random_seeds=(1, 2, 3, 4, 5),
        required_seed_count=5,
    )
    report = SimpleNamespace(
        decision="partially_supported",
        baseline_results={},
        ablations="ABLATION_NOT_RUN",
        robustness_tests="not claimed",
        limitations=("Simulated data.",),
        uncertainty="Power is not established.",
        decision_evidence="Two gates passed.",
    )
    readiness = assess_publication_readiness(
        project_id="p",
        spec=spec,
        analysis=_analysis(),
        report=report,
        papers=[_paper()],
        corpus_papers=[_paper()],
        venue=get_venue_profile("generic_journal"),
        execution_verified=True,
    )
    assert readiness.provenance.simulated is True
    assert readiness.submission_ready is False
    assert "simulated_evidence" in readiness.blocking_issues
    assert "dataset_checksum_missing" in readiness.blocking_issues
    assert readiness.readiness == "technical_report"


def test_readiness_accepts_a_real_powered_study(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_experiment.py").write_text(
        "from pathlib import Path\nprint(Path('data.csv').read_text())\n",
        encoding="utf-8",
    )
    spec = SimpleNamespace(
        repository=str(tmp_path),
        dataset_identity="dataset A; dataset B",
        dataset_checksum="9f2c4e7a1b3d5f608c9e0a4b7d2f6e8c1a3b5d7f9e0c2a4b6d8f0e1c3a5b7d9f",
        baseline_commit="3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
        baseline_configuration="baseline A; baseline B",
        statistical_plan="Power analysis establishes 0.8 power for the planned effect.",
        random_seeds=(1, 2, 3, 4, 5),
        required_seed_count=5,
    )
    report = SimpleNamespace(
        decision="supported",
        baseline_results={"accuracy": 0.1, "f1": 0.2},
        ablations="measured",
        robustness_tests="measured",
        limitations=("Single benchmark.",),
        uncertainty="Power analysis establishes adequate power.",
        decision_evidence="The primary gate passed.",
    )
    corpus = [
        {"id": f"p{index}", "title": f"Paper {index}"} for index in range(20)
    ]
    readiness = assess_publication_readiness(
        project_id="p",
        spec=spec,
        analysis=_analysis(),
        report=report,
        papers=corpus,
        corpus_papers=corpus,
        venue=get_venue_profile("generic_journal"),
        execution_verified=True,
    )
    assert readiness.provenance.repository_commit == spec.baseline_commit
    assert readiness.provenance.real_execution is True
    assert readiness.submission_ready is True, readiness.blocking_issues
    assert readiness.readiness == "submission_ready"


def test_readiness_reads_the_production_dict_report_shape(tmp_path):
    """Regression: production supplies a sqlite dict, not a dataclass."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_experiment.py").write_text(
        "from pathlib import Path\nprint(Path('data.csv').read_text())\n",
        encoding="utf-8",
    )
    spec = SimpleNamespace(
        repository=str(tmp_path),
        dataset_identity="dataset A; dataset B",
        dataset_checksum="9f2c4e7a1b3d5f608c9e0a4b7d2f6e8c1a3b5d7f9e0c2a4b6d8f0e1c3a5b7d9f",
        baseline_commit="3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
        baseline_configuration="baseline A; baseline B",
        statistical_plan="Power analysis establishes 0.8 power.",
        random_seeds=(1, 2, 3, 4, 5),
        required_seed_count=5,
    )
    report = {
        "id": "report1",
        "decision": "supported",
        "baseline_results": {"accuracy": 0.1, "f1": 0.2},
        "ablations": "measured",
        "robustness_tests": "measured",
        "limitations": ["Single benchmark."],
        "uncertainty": "Power analysis establishes adequate power.",
        "decision_evidence": "The primary gate passed.",
    }
    corpus = [{"id": f"p{index}", "title": f"Paper {index}"} for index in range(20)]
    readiness = assess_publication_readiness(
        project_id="p",
        spec=spec,
        analysis=_analysis(),
        report=report,
        papers=corpus,
        corpus_papers=corpus,
        venue=get_venue_profile("generic_journal"),
        execution_verified=True,
    )
    assert readiness.evidence.decision == "supported"
    assert readiness.evidence.external_baselines == 2
    assert readiness.evidence.power_established is True
    assert readiness.evidence.ablations == 1
    assert readiness.provenance.real_execution is True
    assert readiness.submission_ready is True, readiness.blocking_issues


def test_weak_rng_simulation_cannot_be_submission_ready(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_experiment.py").write_text(
        "import random\nfrom pathlib import Path\n"
        "Path('data.csv').read_text()\nvalue = random.gauss(0, 1)\n",
        encoding="utf-8",
    )
    spec = SimpleNamespace(
        repository=str(tmp_path),
        dataset_identity="dataset A; dataset B",
        dataset_checksum="9f2c4e7a1b3d5f608c9e0a4b7d2f6e8c1a3b5d7f9e0c2a4b6d8f0e1c3a5b7d9f",
        baseline_commit="3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
        baseline_configuration="baseline A; baseline B",
        statistical_plan="Power analysis establishes 0.8 power.",
        random_seeds=(1, 2, 3, 4, 5),
        required_seed_count=5,
    )
    report = {
        "decision": "supported",
        "baseline_results": {"accuracy": 0.1, "f1": 0.2},
        "ablations": "measured",
        "robustness_tests": "measured",
        "uncertainty": "Power analysis establishes adequate power.",
        "decision_evidence": "The primary gate passed.",
    }
    corpus = [{"id": f"p{index}", "title": f"Paper {index}"} for index in range(20)]
    readiness = assess_publication_readiness(
        project_id="p",
        spec=spec,
        analysis=_analysis(),
        report=report,
        papers=corpus,
        corpus_papers=corpus,
        venue=get_venue_profile("generic_journal"),
        execution_verified=True,
    )
    assert readiness.provenance.simulation_suspected is True
    assert readiness.submission_ready is False
    assert "provenance_unverified" in readiness.blocking_issues


def test_single_baseline_with_two_metrics_counts_once(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_experiment.py").write_text(
        "from pathlib import Path\nPath('data.csv').read_text()\n", encoding="utf-8"
    )
    spec = SimpleNamespace(
        repository=str(tmp_path),
        dataset_checksum="9f2c4e7a1b3d5f608c9e0a4b7d2f6e8c1a3b5d7f9e0c2a4b6d8f0e1c3a5b7d9f",
        baseline_commit="3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
        baseline_configuration="baseline A",
        statistical_plan="Power analysis establishes 0.8 power.",
        random_seeds=(1, 2, 3, 4, 5),
        required_seed_count=5,
    )
    report = {
        "decision": "supported",
        "baseline_results": {"accuracy": 0.8, "f1": 0.7},
        "ablations": "measured",
        "robustness_tests": "measured",
        "uncertainty": "Power analysis establishes adequate power.",
        "decision_evidence": "The primary gate passed.",
    }
    corpus = [{"id": f"p{index}", "title": f"Paper {index}"} for index in range(20)]
    readiness = assess_publication_readiness(
        project_id="p",
        spec=spec,
        analysis=_analysis(),
        report=report,
        papers=corpus,
        corpus_papers=corpus,
        venue=get_venue_profile("generic_journal"),
        execution_verified=True,
    )
    assert readiness.evidence.external_baselines == 1
    assert any("external_baselines_missing" in issue for issue in readiness.blocking_issues)


def test_dummy_source_without_execution_artifact_is_blocked(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_experiment.py").write_text(
        "print('measured')\n", encoding="utf-8"
    )
    spec = SimpleNamespace(
        repository=str(tmp_path),
        dataset_identity="dataset A; dataset B",
        dataset_checksum="9f2c4e7a1b3d5f608c9e0a4b7d2f6e8c1a3b5d7f9e0c2a4b6d8f0e1c3a5b7d9f",
        baseline_commit="3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
        baseline_configuration="baseline A; baseline B",
        statistical_plan="Power analysis establishes 0.8 power.",
        random_seeds=(1, 2, 3, 4, 5),
        required_seed_count=5,
    )
    report = {
        "decision": "supported",
        "baseline_results": {"accuracy": 0.8, "f1": 0.7},
        "ablations": "measured",
        "robustness_tests": "measured",
        "uncertainty": "Power analysis establishes adequate power.",
        "decision_evidence": "The primary gate passed.",
    }
    corpus = [{"id": f"p{index}", "title": f"Paper {index}"} for index in range(20)]
    readiness = assess_publication_readiness(
        project_id="p",
        spec=spec,
        analysis=_analysis(),
        report=report,
        papers=corpus,
        corpus_papers=corpus,
        venue=get_venue_profile("generic_journal"),
        execution_verified=False,
    )
    assert readiness.submission_ready is False
    assert "execution_artifact_missing" in readiness.blocking_issues
    assert "provenance_unverified" in readiness.blocking_issues


def test_power_analysis_not_performed_is_not_established(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_experiment.py").write_text(
        "from pathlib import Path\nPath('data.csv').read_text()\n", encoding="utf-8"
    )
    spec = SimpleNamespace(
        repository=str(tmp_path),
        dataset_checksum="9f2c4e7a1b3d5f608c9e0a4b7d2f6e8c1a3b5d7f9e0c2a4b6d8f0e1c3a5b7d9f",
        baseline_commit="3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
        baseline_configuration="baseline A; baseline B",
        statistical_plan="Power analysis was not performed.",
        random_seeds=(1, 2, 3, 4, 5),
        required_seed_count=5,
    )
    report = {
        "decision": "supported",
        "baseline_results": {"accuracy": 0.8},
        "ablations": "measured",
        "robustness_tests": "measured",
        "uncertainty": "No power analysis was performed.",
        "decision_evidence": "The primary gate passed.",
    }
    corpus = [{"id": f"p{index}", "title": f"Paper {index}"} for index in range(20)]
    readiness = assess_publication_readiness(
        project_id="p",
        spec=spec,
        analysis=_analysis(),
        report=report,
        papers=corpus,
        corpus_papers=corpus,
        venue=get_venue_profile("generic_journal"),
        execution_verified=True,
    )
    assert readiness.evidence.power_established is False
    assert "power_not_established" in readiness.blocking_issues


def test_body_hygiene_rejects_internal_ids_and_raw_json_but_allows_appendices():
    markdown = (
        "# A title…\n\n"
        "## 1. Introduction\n\n"
        "The research question (ID 50c0d44af7194382a4b6f7d05e175406) is recorded.\n\n"
        "## 4. Results\n\n"
        '{"p_value": 0.0625}\n\n'
        "## Appendices\n\n"
        "- Gap 50c0d44af7194382a4b6f7d05e175406\n"
    )
    report = audit_body(markdown)
    codes = {issue.code for issue in report.errors}
    assert "title_ellipsis" in codes
    assert "internal_research_id" in codes
    assert "raw_json_in_results" in codes
    # The Introduction id is an error; the identical id in Appendices is not.
    hex_errors = [issue for issue in report.errors if issue.code == "internal_hex_id"]
    assert len(hex_errors) == 1
    assert hex_errors[0].line == 5


def test_venue_lint_rejects_truncated_title_and_missing_references():
    markdown = (
        "# A title with a truncation…\n\n"
        "## Abstract\n\nShort.\n\n"
        "## Introduction\n\nText.\n\n"
        "## Literature Review\n\nText.\n\n"
        "## Methodology\n\nText.\n\n"
        "## Results\n\nText.\n\n"
        "## Discussion\n\nText.\n\n"
        "## References\n\n- [@paper1]: One.\n"
    )
    lint = venue_lint(markdown, get_venue_profile("generic_journal"))
    assert lint.passed is False
    assert any("title_no_ellipsis" in violation for violation in lint.violations)
    assert any("reference_coverage" in violation for violation in lint.violations)


def _ready_readiness() -> ReadinessReport:
    return ReadinessReport(
        project_id="p",
        venue="fasttrack",
        study_type="empirical",
        readiness="submission_ready",
        submission_ready=True,
        provenance=ProvenanceReport(
            simulated=False,
            simulation_suspected=False,
            real_execution=True,
            dataset_checksum="9f2c" * 16,
            repository_commit="3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
            source_path="run_experiment.py",
            markers=(),
            warnings=(),
        ),
        evidence=EvidenceSufficiency(
            n_seeds=5,
            power_established=True,
            external_baselines=2,
            ablations=1,
            robustness_tests=1,
            related_work_coverage=20,
            reference_count=20,
            decision="supported",
        ),
    )


def test_venue_lint_requires_reference_entries_not_just_intext_keys():
    citations = " ".join(f"Prior work [@paper{index}]." for index in range(20))
    markdown = (
        "# A title\n\n## Abstract\n\nShort abstract.\n\n## Introduction\n\n"
        + citations
        + "\n\n## References\n\n"
    )
    lint = venue_lint(markdown, get_venue_profile("generic_journal"))
    assert any("reference_coverage" in violation for violation in lint.violations)


def test_venue_lint_does_not_accept_statements_from_reference_titles():
    markdown = (
        "# A title\n\n## Abstract\n\nShort abstract.\n\n## Introduction\n\nText.\n\n"
        "## References\n\n- [@paper1]: Data and code availability in practice.\n"
    )
    lint = venue_lint(markdown, get_venue_profile("generic_journal"))
    assert any("required_statements" in violation for violation in lint.violations)


def test_reviewer_threshold_blocks_a_low_mean_score():
    from dataclasses import replace

    from test_manuscript_template import _complete_manuscript

    markdown = _complete_manuscript()
    profile = replace(get_venue_profile("fasttrack"), reviewer_threshold=5.0)
    review = deterministic_review(
        markdown,
        readiness=_ready_readiness(),
        contract=None,
        synthesis=None,
        hygiene=audit_body(markdown),
        venue_lint_result=VenueLint(venue="fasttrack", passed=True),
        profile=profile,
    )
    assert review.passed is False
    assert review.meta_decision in ("major_revision", "reject")


def test_reviewer_required_fixes_block_a_pass():
    from test_manuscript_template import _complete_manuscript

    markdown = _complete_manuscript().replace(
        "2. Second limitation.\n3. Third limitation.\n", ""
    )
    review = deterministic_review(
        markdown,
        readiness=_ready_readiness(),
        contract=None,
        synthesis=None,
        hygiene=audit_body(markdown),
        venue_lint_result=VenueLint(venue="fasttrack", passed=True),
        profile=get_venue_profile("fasttrack"),
    )
    assert review.required_fixes
    assert review.passed is False
    assert review.meta_decision in ("minor_revision", "major_revision")


def test_template_perfect_but_unready_is_rejected_by_reviewer_panel():
    readiness = ReadinessReport(
        project_id="p",
        venue="generic_journal",
        study_type="simulation",
        readiness="technical_report",
        submission_ready=False,
        provenance=ProvenanceReport(
            simulated=True,
            simulation_suspected=True,
            real_execution=False,
            dataset_checksum="not_computed_no_checksum_supplied",
            repository_commit="not_applicable_no_commit_supplied",
            source_path=None,
            markers=("simulate",),
            warnings=(),
        ),
        evidence=EvidenceSufficiency(
            n_seeds=5,
            power_established=False,
            external_baselines=0,
            ablations=0,
            robustness_tests=0,
            related_work_coverage=3,
            reference_count=3,
            decision="partially_supported",
        ),
        blocking_issues=["simulated_evidence", "dataset_checksum_missing"],
    )
    markdown = (
        "# A template-perfect title\n\n"
        "## Abstract\n\n**Background:** b. **Gap in Knowledge:** g. "
        "**Contribution:** c. **Population & Methods:** m. **Results:** r. "
        "**Conclusion:** x.\n\n"
        "## Keywords\n\nbenchmark, calibration, agents\n\n"
        "## 1. Introduction\n\nText.\n\n"
        "## 2. Literature Review\n\nText.\n\n"
        "## 3. Methodology\n\nText.\n\n"
        "## 4. Results\n\n| Metric | Value |\n|---|---|\n| accuracy | 0.8 |\n\n"
        "## 5. Discussion\n\n### Recap of what we found\n\nText.\n\n"
        "### Study Limitations (at least 3)\n1. One limitation here.\n"
        "2. Two limitation here.\n3. Three limitation here.\n\n"
        "### Strengths of this study\n\n- s\n\n"
        "### Implications for future research\n\n- f\n\n"
        "### Possible interpretations and future work (speculative)\n\n- p\n\n"
        "### Conclusion\n\nText.\n\n"
        "## References\n\n- [@paper1]: One.\n- [@paper2]: Two.\n- [@paper3]: Three.\n\n"
        "## Appendices\n\n- Appendix A.\n"
    )
    hygiene = audit_body(markdown)
    lint = venue_lint(markdown, get_venue_profile("generic_journal"))
    review = deterministic_review(
        markdown,
        readiness=readiness,
        contract=None,
        synthesis=None,
        hygiene=hygiene,
        venue_lint_result=lint,
        profile=get_venue_profile("generic_journal"),
    )
    assert review.passed is False
    assert review.meta_decision in ("reject", "major_revision")
    assert any("simulated_evidence" in issue for issue in review.blocking_issues)


def test_tikz_negative_effect_bars_start_left_of_zero(tmp_path):
    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    service = ManuscriptService(repository)
    analysis = SimpleNamespace(effect_size={"accuracy": -0.5})
    _, tikz = service._figures(analysis, tmp_path)
    assert "(1.500" in tikz, tikz


def test_delivery_state_enforces_complete_iff_submission_ready():
    assert ManuscriptService._delivery_state(
        {"status": "complete", "submission_ready": True}, "complete"
    ) == ("complete", True)
    assert ManuscriptService._delivery_state(
        {"status": "complete", "submission_ready": False}, "complete"
    ) == ("draft", False)
    assert ManuscriptService._delivery_state(
        {"status": "draft", "submission_ready": True}, "draft"
    ) == ("draft", False)
    assert ManuscriptService._delivery_state(None, "complete") == ("draft", False)


def test_title_never_contains_ellipsis(tmp_path):
    candidate = SimpleNamespace(
        method="A very long method name " + "with many components " * 12,
        difference_from_existing="A difference.",
    )
    title = ManuscriptService._title(candidate, complete=True, keywords=["calibration"])
    assert "…" not in title
    assert "..." not in title
    assert len(title.split()) <= 20


def test_contract_title_stays_within_twenty_words():
    from autoresearch.application.contribution_contract import (
        ContributionContract,
        pick_title,
    )

    contract = ContributionContract(
        working_title="x",
        title_candidates=(" ".join(["long"] * 30),),
        takeaway="",
        contributions=(),
        target_venue="generic_journal",
        audience="",
        closest_prior_work=(),
        threats=(),
        figure_table_plan=(),
        keywords=("calibration",),
    )
    title = pick_title(contract, ["calibration"])
    assert len(title.split()) <= 20
    assert "calibration" in title.lower()


def test_keywords_filter_method_fragments(tmp_path):
    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    service = ManuscriptService(repository)
    papers = [{
        "id": "p1",
        "title": "Benchmarking Autonomous Agents for Scientific Discovery",
        "abstract": "",
        "year": 2026,
    }]
    keywords = service._keywords(
        "project",
        papers,
        gap={"statement": "Cross-benchmark calibration of autonomous research agents."},
        candidate=SimpleNamespace(method="A fixed reference agent with standardized scaffold"),
        spec=SimpleNamespace(dataset_identity="BioDataLab and FIRE-Bench"),
    )
    assert 3 <= len(keywords) <= 6
    assert "add" not in keywords
    assert "fixed" not in keywords
    assert "standardized" not in keywords
    assert any(term in keywords for term in ("benchmark", "calibration", "agent", "agents"))


@pytest.mark.anyio
async def test_literature_synthesis_uses_the_corpus_not_just_the_gap(tmp_path):
    from test_smoke_e2e import _build_loop

    repository, workflow, seed = _build_loop(tmp_path)
    first = await workflow.run(seed=str(seed), until="design")
    project_id = first["project_id"]
    candidate_id = next(
        stage["candidate_id"] for stage in first["stages"] if stage["stage"] == "methodology"
    )
    gap = repository.row(
        "SELECT * FROM research_gaps WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
        (project_id,),
    )
    candidate = repository.get_novelty_candidate(candidate_id)
    spec = repository.get_experiment_spec(candidate_id)
    papers = repository.rows("SELECT * FROM papers WHERE project_id=?", (project_id,))
    keys = {str(paper["id"]): f"paper{index}" for index, paper in enumerate(papers, start=1)}
    synthesis = build_literature_synthesis(
        repository,
        project_id,
        gap=gap,
        candidate=candidate,
        spec=spec,
        keys=keys,
        limit=24,
    )
    assert len(synthesis.papers) >= 5, len(synthesis.papers)
    # The offline fixture is intentionally homogeneous; theme count is not the
    # assertion. Corpus coverage and a real comparison table are.
    assert len(synthesis.themes) >= 1, synthesis.themes
    markdown = "\n".join(synthesis.to_markdown_lines())
    assert "Comparison of reviewed studies" in markdown
    assert "[@paper" in markdown

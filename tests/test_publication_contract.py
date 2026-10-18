"""P0 publication-contract and early-gap tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoresearch.application.publication_contract import (
    PublicationContract,
    PublicationGap,
    assess_contract,
    build_contract,
)
from autoresearch.application.venue_profiles import get_venue_profile
from autoresearch.experiments.publication_design import publication_design_gaps
from autoresearch.novelty.methodology import publication_design_gaps as novelty_gaps


def _spec(**overrides):
    values = {
        "dataset_identity": "dataset A; dataset B",
        "dataset_checksum": "9f2c4e7a1b3d5f608c9e0a4b7d2f6e8c1a3b5d7f9e0c2a4b6d8f0e1c3a5b7d9f",
        "baseline_commit": "3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
        "baseline_configuration": "Baseline A; Baseline B",
        "ablations": ("remove component X",),
        "robustness_tests": ("seed variance",),
        "statistical_plan": "Power analysis establishes 0.8 power for d=0.4.",
        "required_seed_count": 5,
        "metrics": ("accuracy",),
        "run_id": "run1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _contract(**overrides):
    values = {
        "project_id": "p",
        "target_venue": "generic_journal",
        "contribution_statement": "A protocol for comparable benchmarks.",
        "novelty_delta": "Unlike prior work, it calibrates across benchmarks.",
        "closest_prior_work": ("paper1",),
        "datasets": ("dataset A", "dataset B"),
        "baselines": ("Baseline A", "Baseline B"),
        "metrics": ("accuracy",),
        "ablations": ("remove X",),
        "robustness_tests": ("seed variance",),
        "power_analysis": "Power analysis establishes 0.8 power.",
        "power_established": True,
        "required_seeds": 5,
        "related_work_coverage": 20,
        "full_text_ratio": 1.0,
        "execution_provenance": {
            "execution_verified": True,
            "analysis_id": "analysis1",
            "report_id": "report1",
            "dataset_checksum": "9f2c" * 16,
            "dataset_checksum_source": "computed",
            "baseline_commit": "3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
            "baseline_commit_source": "computed",
            "study_type": "empirical",
        },
        "reviewer_threshold": 3.5,
    }
    values.update(overrides)
    return PublicationContract(**values)


def test_contract_round_trip():
    contract = _contract()
    contract.gaps = assess_contract(contract, get_venue_profile("generic_journal"))
    restored = PublicationContract.from_dict(contract.to_dict())
    assert restored.to_dict() == contract.to_dict()


def test_contract_missing_fields_produce_exact_gaps():
    contract = PublicationContract(project_id="p", target_venue="generic_journal")
    gaps = assess_contract(contract, get_venue_profile("generic_journal"))
    codes = {gap.code for gap in gaps}
    assert {
        "contribution_missing", "novelty_delta_missing",
        "closest_prior_work_missing", "reviewed_papers_below_minimum",
        "baselines_below_minimum", "ablations_missing", "robustness_missing",
        "power_not_established", "datasets_below_minimum",
        "dataset_checksum_missing", "baseline_commit_missing",
        "execution_artifact_missing",
    } <= codes


def test_contract_accepts_a_complete_empirical_slice():
    contract = _contract()
    assert assess_contract(contract, get_venue_profile("generic_journal")) == []


def test_contract_rejects_format_valid_but_unverified_provenance():
    contract = _contract()
    contract.execution_provenance = {
        **contract.execution_provenance,
        "dataset_checksum": "deadbeef" * 8,
        "dataset_checksum_source": "model",
        "baseline_commit": "cafebabe" * 5,
        "baseline_commit_source": "model",
    }
    codes = {gap.code for gap in assess_contract(contract, get_venue_profile("generic_journal"))}
    assert "dataset_checksum_unverified" in codes
    assert "baseline_commit_unverified" in codes


def test_contract_rejects_missing_contribution_and_unestablished_power():
    contract = _contract(
        contribution_statement="",
        novelty_delta="",
        power_analysis="Power is not established.",
        power_established=False,
    )
    codes = {gap.code for gap in assess_contract(contract, get_venue_profile("generic_journal"))}
    assert "contribution_missing" in codes
    assert "novelty_delta_missing" in codes
    assert "power_not_established" in codes


def test_contract_rejects_zero_full_text_and_too_few_seeds():
    contract = _contract(full_text_ratio=0.0, required_seeds=1)
    codes = {gap.code for gap in assess_contract(contract, get_venue_profile("generic_journal"))}
    assert "full_text_ratio_below_threshold" in codes
    assert "seeds_below_minimum" in codes


def test_contract_does_not_resurrect_freeze_values_when_spec_is_empty():
    spec = SimpleNamespace(
        dataset_identity="dataset A",
        dataset_checksum="9f2c4e7a1b3d5f608c9e0a4b7d2f6e8c1a3b5d7f9e0c2a4b6d8f0e1c3a5b7d9f",
        baseline_commit="3b7cccd9a1f2e4d6c8b0a2f4e6d8c0b2a4f6e8d0",
        baseline_configuration="",
        ablations=(),
        robustness_tests=(),
        statistical_plan="",
        required_seed_count=1,
        metrics=(),
    )
    methodology = {
        "methodology": {
            "contribution_statement": "A contribution.",
            "novelty_delta": "A delta.",
            "datasets": ["a", "b"],
            "baselines": ["A", "B"],
            "ablations": ["remove X"],
            "robustness_tests": ["seed variance"],
            "power_analysis": "Power analysis establishes 0.8 power.",
        }
    }
    contract = build_contract(
        project_id="p",
        venue=get_venue_profile("generic_journal"),
        review_corpus={"reviewed_papers": 20, "full_text_ratio": 1.0, "closest_prior_work": ["p1"]},
        methodology=methodology,
        spec=spec,
    )
    codes = {gap.code for gap in contract.gaps}
    assert "baselines_below_minimum" in codes
    assert "ablations_missing" in codes
    assert "robustness_missing" in codes
    assert "power_not_established" in codes


def test_workshop_profile_allows_a_workshop_design():
    spec = _spec(
        dataset_identity="Dataset A",
        baseline_configuration="Baseline A",
        ablations=(),
        robustness_tests=(),
        statistical_plan="Power is not established.",
        required_seed_count=3,
    )
    assert publication_design_gaps(
        spec, profile=get_venue_profile("workshop")
    ) == []
    assert publication_design_gaps(
        spec, profile=get_venue_profile("generic_journal")
    )


def test_workshop_contract_accepts_a_workshop_slice():
    contract = _contract(
        target_venue="workshop",
        datasets=("Dataset A",),
        baselines=("Baseline A",),
        ablations=(),
        robustness_tests=(),
        power_analysis="",
        power_established=False,
        required_seeds=3,
        related_work_coverage=8,
        full_text_ratio=1.0,
        execution_provenance={
            "execution_verified": False,
            "study_type": "empirical",
        },
    )
    assert assess_contract(contract, get_venue_profile("workshop")) == []


def test_build_contract_reads_dict_and_object_slices():
    spec = _spec()
    contract = build_contract(
        project_id="p",
        venue=get_venue_profile("generic_journal"),
        review_corpus={"reviewed_papers": 20, "full_text_ratio": 1.0, "closest_prior_work": ["p1"]},
        spec=spec,
        execution_verified=True,
    )
    assert contract.related_work_coverage == 20
    assert len(contract.baselines) == 2
    assert len(contract.datasets) == 2
    assert contract.power_established is True


def test_experiment_publication_design_gaps():
    assert publication_design_gaps(_spec()) == []
    one_baseline = publication_design_gaps(_spec(baseline_configuration="Baseline A"))
    assert any(gap["code"] == "baselines_below_minimum" for gap in one_baseline)
    no_ablation = publication_design_gaps(_spec(ablations=()))
    assert any(gap["code"] == "ablations_missing" for gap in no_ablation)
    no_power = publication_design_gaps(_spec(statistical_plan="Power is not established."))
    assert any(gap["code"] == "power_not_established" for gap in no_power)
    placeholder = publication_design_gaps(_spec(
        dataset_checksum="0" * 64, baseline_commit="a" * 40,
    ))
    assert any(gap["code"] == "dataset_checksum_missing" for gap in placeholder)
    assert any(gap["code"] == "baseline_commit_missing" for gap in placeholder)


def test_novelty_publication_design_gaps():
    complete = {
        "contribution_statement": "A contribution.",
        "novelty_delta": "Unlike prior work.",
        "datasets": ["a", "b"],
        "baselines": ["baseline A", "baseline B"],
        "ablations": ["remove X"],
        "robustness_tests": ["seed variance"],
        "power_analysis": "Power analysis establishes 0.8 power for d=0.4.",
        "expected_figures_tables": ["main table"],
        "failure_modes": ["implementation bug"],
        "stop_conditions": ["stop if data missing"],
    }
    assert novelty_gaps(complete) == []
    incomplete = novelty_gaps({"contribution_statement": "A contribution."})
    codes = {gap["code"] for gap in incomplete}
    assert {"baselines_below_minimum", "ablations_missing", "power_not_established"} <= codes


@pytest.mark.anyio
async def test_review_publication_corpus_and_20_paper_gate(tmp_path):
    from test_smoke_e2e import _build_loop

    repository, workflow, seed = _build_loop(tmp_path)
    result = await workflow.run(seed=str(seed), until="review", limit=20, top_k=20)
    stage = next(item for item in result["stages"] if item["stage"] == "review")
    assert stage["status"] == "completed"
    corpus = json.loads(Path(stage["review_corpus"]).read_text(encoding="utf-8"))
    assert corpus["reviewed_papers"] == 20
    assert len(corpus["themes"]) >= 3, corpus["themes"]
    assert Path(stage["citation_contexts"]).is_file()
    assert not any(
        gap["code"] == "reviewed_papers_below_minimum"
        for gap in corpus["publication_gaps"]
    )
    metrics_path = Path(result["artifacts"]["stage_metrics"])
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["stages"]
    assert all("elapsed_seconds" in item for item in metrics["stages"])
    assert "prompt_tokens" in metrics and "estimated_cost" in metrics


@pytest.mark.anyio
async def test_review_publication_reports_five_paper_gap(tmp_path):
    from test_smoke_e2e import _build_loop

    repository, workflow, seed = _build_loop(tmp_path)
    result = await workflow.run(seed=str(seed), until="review", limit=10, top_k=5)
    stage = next(item for item in result["stages"] if item["stage"] == "review")
    corpus = json.loads(Path(stage["review_corpus"]).read_text(encoding="utf-8"))
    assert corpus["reviewed_papers"] < 20
    assert any(
        gap["code"] == "reviewed_papers_below_minimum"
        for gap in corpus["publication_gaps"]
    )


@pytest.mark.anyio
async def test_publication_track_stops_before_novelty_on_small_review(tmp_path):
    from test_smoke_e2e import _build_loop

    repository, workflow, seed = _build_loop(tmp_path)
    result = await workflow.run(
        seed=str(seed), until="design", limit=10, top_k=5,
        publication_track=True, venue_name="generic_journal",
    )
    assert result["stopped_at"] == "review"
    assert any(item["stage"] == "review_gate" for item in result["stages"])
    assert result["publication_gaps"]
    assert Path(result["artifacts"]["publication_gaps"]).is_file()


@pytest.mark.anyio
async def test_publication_track_stops_at_methodology_without_publication_design(tmp_path):
    from test_smoke_e2e import _build_loop

    repository, workflow, seed = _build_loop(tmp_path)
    result = await workflow.run(
        seed=str(seed), until="design", limit=20, top_k=20, publication_track=True,
    )
    assert result["stopped_at"] == "methodology"
    assert any(item["stage"] == "methodology_gate" for item in result["stages"])
    assert any(gap["stage"] == "novelty" for gap in result["publication_gaps"])

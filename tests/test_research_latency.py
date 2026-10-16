"""Bound request amplification, preserve evidence, and cancel stalled research."""

import asyncio
from time import perf_counter

import httpx
import pytest

import test_workflow_coordination as fixtures
from autoresearch.experiments.planner import ExperimentSpecGenerator
from autoresearch.providers.capabilities import Capability
from autoresearch.providers.config import ProviderConfig
from autoresearch.providers.errors import ErrorKind, ProviderError
from autoresearch.providers.llm.openai_compatible import OpenAICompatibleProvider
from autoresearch.review.extraction import PaperReviewExtractor


@pytest.mark.anyio
async def test_research_defaults_have_no_stage_deadlines(tmp_path, monkeypatch):
    import inspect
    from autoresearch.application.services import LiteratureReviewService, NoveltyService, ExperimentService

    for method in (LiteratureReviewService.review, NoveltyService.generate_portfolio, ExperimentService.execute_candidate):
        assert inspect.signature(method).parameters["max_runtime_seconds"].default is None
    deadlines = []
    original = asyncio.timeout

    def record_deadline(seconds):
        deadlines.append(seconds)
        return original(seconds)

    monkeypatch.setattr(asyncio, "timeout", record_deadline)
    _, workflow, _, seed, _ = fixtures._harness(tmp_path)
    result = await workflow.run(seed=str(seed), until="design")
    assert result["stopped_at"] == "design"
    assert len(deadlines) >= 3 and all(limit is None for limit in deadlines)


def test_project_unlimited_tokens_overrides_inherited_ceiling(tmp_path, monkeypatch):
    from autoresearch.application.config import load_settings

    config = tmp_path / "config.toml"
    config.write_text("[research]\nmax_tokens = 0\n")
    monkeypatch.setenv("AUTORESEARCH_PROVIDER_CONFIG", str(config))
    monkeypatch.setenv("AUTORESEARCH_DEFAULT_MAX_TOKENS", "2000000")
    assert load_settings().default_max_tokens is None


@pytest.mark.anyio
async def test_default_provider_allows_slow_uncapped_completion(monkeypatch):
    monkeypatch.setenv("K", "fixture-key")
    seen = []

    async def slow(request):
        import json

        seen.append(json.loads(request.content))
        await asyncio.sleep(0.04)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"finished": true}'}, "finish_reason": "stop"}], "usage": {}})

    config = ProviderConfig("p", "openai_compatible", "https://example.test", "K", frozenset(Capability))
    provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(slow))
    try:
        assert provider.client.timeout.read is None and provider.client.timeout.pool is None
        assert config.total_timeout == 0
        result = await provider.complete(model="m", messages=[{"role": "user", "content": "Finish the research"}])
        assert result.text == '{"finished": true}'
        assert "max_tokens" not in seen[0] and "thinking" not in seen[0]
    finally:
        await provider.close()


@pytest.mark.anyio
async def test_review_uses_one_call_per_paper_and_reuses_interrupted_work(tmp_path):
    repo, workflow, _, seed, _ = fixtures._harness(tmp_path)
    result = await workflow.run(seed=str(seed), until="review")
    project = result["project_id"]
    reviews = repo.rows("SELECT * FROM paper_reviews WHERE project_id=?", (project,))
    billed = repo.row("SELECT COUNT(*) n FROM provider_usage WHERE operation LIKE 'paper_review%'")["n"]
    assert billed == len(reviews)
    # Losing the later gap stage must not force already verified paper work to repeat.
    repo.finish_run(reviews[0]["run_id"], error="interrupted after paper checkpoints")
    outcome = await workflow.review.review(project, top_k=3)
    assert outcome.reused == len(reviews)
    assert repo.row("SELECT COUNT(*) n FROM provider_usage WHERE operation LIKE 'paper_review%'")["n"] == billed


def test_evidence_rejects_changed_numbers_and_keeps_whitespace_normalization():
    from autoresearch.domain.models import PaperRecord

    paper = PaperRecord("p", "project", "r", "Title", "", (), 2025, None, None, None, None, ("test",))
    source = "Accuracy improved by 10 percent.\nThe evaluation\nuses five seeds."
    spans = [
        {"statement": "fabricated", "excerpt": "Accuracy improved by 11 percent."},
        {"statement": "measured", "excerpt": "The evaluation uses five seeds."},
    ]
    claims = PaperReviewExtractor._claims(spans, paper, "r", source, "p", "m")
    assert [c.statement for c in claims] == ["measured"]


@pytest.mark.anyio
async def test_cached_approximate_quotes_are_reextracted(tmp_path):
    repo, workflow, _, seed, _ = fixtures._harness(tmp_path)
    result = await workflow.run(seed=str(seed), until="review")
    claim = repo.row("SELECT id,paper_id FROM evidence_claims WHERE project_id=? LIMIT 1", (result["project_id"],))
    repo.execute("UPDATE evidence_claims SET excerpt=? WHERE id=?", ("Invented numerical improvement of 99 percent.", claim["id"]))
    before = repo.row("SELECT COUNT(*) n FROM provider_usage WHERE operation='paper_review'")["n"]
    outcome = await workflow.review.review(result["project_id"], top_k=3)
    assert outcome.reused == outcome.reviewed - 1
    assert repo.row("SELECT COUNT(*) n FROM provider_usage WHERE operation='paper_review'")["n"] == before + 1


@pytest.mark.anyio
async def test_planning_uses_two_calls_and_inspection_does_not_replan(tmp_path):
    repo, workflow, _, seed, _ = fixtures._harness(tmp_path)
    result = await workflow.run(seed=str(seed), until="design")
    design = next(s for s in result["stages"] if s["stage"] == "design")
    spec = repo.get_experiment_spec_by_id(design["spec_id"])
    planning = repo.rows("SELECT operation FROM provider_usage WHERE operation IN ('experiment_planning','adversarial_experiment_analysis','experiment_design_selection')")
    assert sorted(r["operation"] for r in planning) == ["adversarial_experiment_analysis", "experiment_planning"]
    assert "Adversarial check:" in spec.falsification_condition
    usage_before = repo.row("SELECT COUNT(*) n FROM provider_usage")["n"]
    preview_before = workflow.experiment.approval_preview_for_candidate(spec.candidate_id)
    for mode in ("validate", "inspect"):
        assert await workflow.experiment.execute_candidate(spec.candidate_id, mode=mode) == spec.id
    assert repo.row("SELECT COUNT(*) n FROM provider_usage")["n"] == usage_before
    assert workflow.experiment.approval_preview_for_candidate(spec.candidate_id)["checksum"] == preview_before["checksum"]


def test_power_uses_standardized_effect_not_raw_metric_threshold():
    planner = ExperimentSpecGenerator(None)
    design = {"minimum_meaningful_effect": {"map": 0.001}, "random_seeds": [1, 2, 3, 4, 5]}
    assert planner._power_analysis(design) == 5
    # Do not claim 80% power with a silent 30-seed cap: d=.5 needs 63 here.
    assert planner._power_analysis({**design, "expected_effect_size": 0.5, "power_target": 0.8}) == 63


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["review", "novelty", "experiment"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_deadlines_and_cancellation_close_runs(tmp_path, monkeypatch, stage, cancel):
    repo, workflow, _, seed, _ = fixtures._harness(tmp_path)
    result = await workflow.run(seed=str(seed), until="design")
    spec_id = next(s for s in result["stages"] if s["stage"] == "design")["spec_id"]
    spec = repo.get_experiment_spec_by_id(spec_id)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def stalled(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    if stage == "review":
        monkeypatch.setattr(workflow.review.extractor, "extract", stalled)
        operation = workflow.review.review(result["project_id"], fresh=True, max_runtime_seconds=1)
    elif stage == "novelty":
        monkeypatch.setattr(workflow.novelty.pipeline, "generate_portfolio", stalled)
        operation = workflow.novelty.generate_portfolio(result["project_id"], max_runtime_seconds=0.03)
    else:
        monkeypatch.setattr(workflow.experiment.pipeline, "plan_for_candidate", stalled)
        operation = workflow.experiment.execute_candidate(spec.candidate_id, max_runtime_seconds=0.03)
    task = asyncio.create_task(operation)
    await asyncio.wait_for(started.wait(), timeout=3)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert stopped.is_set()
    latest = repo.row("SELECT * FROM pipeline_runs ORDER BY rowid DESC LIMIT 1")
    assert latest["status"] == ("cancelled" if cancel else "failed") and latest["completed_at"]
    if not cancel:
        assert "runtime_limit" in latest["error"]
    if stage == "review":
        assert not any(c["status"] == "running" for c in repo.checkpoints(latest["id"]))


@pytest.mark.anyio
async def test_critics_overlap_and_receive_prior_art(tmp_path, monkeypatch):
    repo, workflow, _, seed, _ = fixtures._harness(tmp_path)
    result = await workflow.run(seed=str(seed), until="hypotheses")
    candidate_row = repo.row("SELECT id FROM novelty_candidates WHERE project_id=? LIMIT 1", (result["project_id"],))
    candidate = repo.get_novelty_candidate(candidate_row["id"])
    assessment = repo.get_prior_art_assessment(candidate.id)
    critic = workflow.novelty.pipeline.critic
    original = critic._single_evaluate
    entered = 0
    all_entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        nonlocal entered
        assert kwargs["prior_art"] == assessment
        entered += 1
        if entered == 3:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), timeout=1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(critic, "_single_evaluate", blocked)
    critique = await critic.evaluate(candidate, prior_art=assessment)
    assert assessment.id in critique.provenance.source_evidence


@pytest.mark.anyio
async def test_provider_deadline_includes_retry_backoff():
    calls = 0

    async def unavailable(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "unavailable"})

    config = ProviderConfig("p", "openai_compatible", "https://example.test", "K", frozenset(Capability), total_timeout=0.03, max_retries=4)
    provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(unavailable))
    started = perf_counter()
    try:
        with pytest.raises(ProviderError, match="total request deadline") as error:
            await provider.request_json("POST", "/chat/completions", headers={})
        assert error.value.kind == ErrorKind.TIMEOUT
        assert calls == 1 and perf_counter() - started < 1
        assert provider._gate._inflight == 0
    finally:
        await provider.close()

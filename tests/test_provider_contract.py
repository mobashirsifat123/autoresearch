from __future__ import annotations

import json

import httpx
import pytest

from autoresearch.providers.budget import ProviderBudgetGuard
from autoresearch.providers.capabilities import Capability, ModelRole
from autoresearch.providers.config import ModelAssignment, ProviderConfig, reject_secret_fields
from autoresearch.providers.errors import ErrorKind, ProviderError
from autoresearch.providers.llm import AnthropicProvider, GeminiProvider, OpenAICompatibleProvider
from autoresearch.providers.registry import ProviderRegistry
from autoresearch.providers.redaction import redact_text
from autoresearch.providers.router import ModelRouter
from autoresearch.providers.testing import FakeEmbeddingProvider, FakeLLMProvider
from autoresearch.storage import ResearchRepository


def _router(tmp_path, *, usage=True):
    repository = ResearchRepository(tmp_path / "research.sqlite3", tmp_path / "artifacts")
    project = repository.create_project("Provider test")
    llm = FakeLLMProvider(responses=[{"value": "ok"}])
    embeddings = FakeEmbeddingProvider()
    registry = ProviderRegistry()
    registry.register_llm("fake", llm)
    registry.register_embeddings("vectors", embeddings)
    assignments = {
        ModelRole.LITERATURE_EXTRACTION: ModelAssignment(ModelRole.LITERATURE_EXTRACTION, "fake", "fake-model"),
        ModelRole.EMBEDDINGS: ModelAssignment(ModelRole.EMBEDDINGS, "vectors", "fake-embedding-v1"),
    }
    return ModelRouter(registry, assignments, usage_sink=repository.record_provider_usage if usage else None, budget_guard=ProviderBudgetGuard(repository)), repository, project


@pytest.mark.asyncio
async def test_router_records_structured_and_embedding_usage(tmp_path):
    router, repository, project = _router(tmp_path)
    result = await router.structured_output(ModelRole.LITERATURE_EXTRACTION, messages=[{"role": "user", "content": "test"}], schema={"name": "test"}, project_id=project.id, run_id=None)
    vectors = await router.embed(["alpha", "beta"], project_id=project.id)
    assert result == {"value": "ok"}
    assert len(vectors.vectors) == 2
    rows = repository.rows("SELECT role,provider,estimated_cost FROM provider_usage ORDER BY created_at")
    assert {row["role"] for row in rows} == {"literature_extraction", "embeddings"}
    assert all(row["provider"] in {"fake", "vectors"} for row in rows)


def test_registry_rejects_missing_capabilities():
    provider = FakeLLMProvider()
    provider.capabilities = frozenset({Capability.TEXT})
    registry = ProviderRegistry()
    registry.register_llm("weak", provider)
    with pytest.raises(ProviderError, match="structured_output"):
        ModelRouter(registry, {ModelRole.LITERATURE_EXTRACTION: ModelAssignment(ModelRole.LITERATURE_EXTRACTION, "weak", "model")})


def test_research_reasoning_assignment_requires_structured_output():
    provider = FakeLLMProvider()
    provider.capabilities = frozenset({Capability.TEXT, Capability.REASONING})
    registry = ProviderRegistry()
    registry.register_llm("reasoner", provider)
    with pytest.raises(ProviderError, match="structured_output"):
        ModelRouter(
            registry,
            {ModelRole.RESEARCH_REASONING: ModelAssignment(ModelRole.RESEARCH_REASONING, "reasoner", "model")},
        )


@pytest.mark.asyncio
async def test_fallback_is_transient_only():
    class Failure(FakeLLMProvider):
        def __init__(self, kind):
            super().__init__()
            self.kind = kind
        async def complete(self, **kwargs):
            raise ProviderError("failure", self.kind, self.name)

    fallback = FakeLLMProvider(responses=[{"ok": True}])
    registry = ProviderRegistry()
    registry.register_llm("primary", Failure(ErrorKind.RATE_LIMIT))
    registry.register_llm("fallback", fallback)
    assignment = ModelAssignment(ModelRole.LITERATURE_EXTRACTION, "primary", "a", (("fallback", "b"),))
    router = ModelRouter(registry, {ModelRole.LITERATURE_EXTRACTION: assignment})
    assert await router.structured_output(ModelRole.LITERATURE_EXTRACTION, messages=[], schema={}) == {"ok": True}

    registry2 = ProviderRegistry()
    registry2.register_llm("primary", Failure(ErrorKind.INVALID_REQUEST))
    registry2.register_llm("fallback", FakeLLMProvider(responses=[{"wrong": True}]))
    router2 = ModelRouter(registry2, {ModelRole.LITERATURE_EXTRACTION: assignment})
    with pytest.raises(ProviderError) as error:
        await router2.structured_output(ModelRole.LITERATURE_EXTRACTION, messages=[], schema={})
    assert error.value.kind is ErrorKind.INVALID_REQUEST


@pytest.mark.asyncio
async def test_budget_hard_stop_blocks_provider_call(tmp_path):
    router, repository, project = _router(tmp_path)
    repository.set_project_budget(project.id, hard_stop=0)
    with pytest.raises(ProviderError) as error:
        await router.embed(["blocked"], project_id=project.id)
    assert error.value.kind is ErrorKind.BUDGET


@pytest.mark.asyncio
async def test_openai_compatible_adapter_contract(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "not-a-real-secret")
    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer not-a-real-secret"
        body = json.loads(request.content)
        assert body["model"] == "external-model"
        assert body["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(200, json={"id": "req-1", "model": "external-model", "choices": [{"message": {"content": '{"ok":true}'}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    config = ProviderConfig("external", "openai_compatible", "https://provider.example/v1", "TEST_PROVIDER_KEY", frozenset({Capability.TEXT, Capability.STRUCTURED_OUTPUT}), pricing={"input_per_million": 1, "output_per_million": 2, "currency": "USD"})
    provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))
    result = await provider.complete(model="external-model", messages=[{"role": "user", "content": "hello"}], response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object"}}})
    assert result.text == '{"ok":true}'
    assert result.estimated_cost == pytest.approx(0.000014)
    await provider.close()


@pytest.mark.asyncio
async def test_openai_compatible_adapter_retries_rate_limit(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "not-a-real-secret")
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"id": "req-2", "model": "external-model", "choices": [{"message": {"content": '{"ok":true}'}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    config = ProviderConfig("external", "openai_compatible", "https://provider.example/v1", "TEST_PROVIDER_KEY", frozenset({Capability.TEXT, Capability.STRUCTURED_OUTPUT}), max_retries=1)
    provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))
    result = await provider.complete(model="external-model", messages=[{"role": "user", "content": "hello"}], response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object"}}})
    assert calls == 2 and json.loads(result.text) == {"ok": True}
    await provider.close()


@pytest.mark.asyncio
async def test_anthropic_adapter_forces_strict_structured_tool(monkeypatch):
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "not-a-real-secret")

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        assert body["tools"][0]["name"] == "answer"
        assert body["tools"][0]["strict"] is True
        assert body["tools"][0]["input_schema"]["required"] == ["ok"]
        assert body["tool_choice"] == {"type": "tool", "name": "answer", "disable_parallel_tool_use": True}
        return httpx.Response(200, json={"id": "msg-1", "model": "claude-test", "content": [{"type": "tool_use", "name": "answer", "input": {"ok": True}}], "usage": {"input_tokens": 8, "output_tokens": 3}})

    config = ProviderConfig("anthropic", "anthropic", "https://api.anthropic.com", "TEST_ANTHROPIC_KEY", frozenset({Capability.TEXT, Capability.STRUCTURED_OUTPUT}))
    provider = AnthropicProvider(config, transport=httpx.MockTransport(handler))
    result = await provider.complete(model="claude-test", messages=[{"role": "user", "content": "hello"}], response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}})
    assert json.loads(result.text) == {"ok": True}
    await provider.close()


@pytest.mark.asyncio
async def test_gemini_adapter_sends_json_schema(monkeypatch):
    monkeypatch.setenv("TEST_GEMINI_KEY", "not-a-real-secret")

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        config = body["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["responseJsonSchema"]["required"] == ["ok"]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}], "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2}})

    config = ProviderConfig("gemini", "gemini", "https://generativelanguage.googleapis.com", "TEST_GEMINI_KEY", frozenset({Capability.TEXT, Capability.STRUCTURED_OUTPUT}))
    provider = GeminiProvider(config, transport=httpx.MockTransport(handler))
    result = await provider.complete(model="gemini-test", messages=[{"role": "user", "content": "hello"}], response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}})
    assert json.loads(result.text) == {"ok": True}
    await provider.close()


def test_configuration_rejects_inline_secrets():
    with pytest.raises(ValueError, match="Secret values"):
        reject_secret_fields({"api_key": "never-store-this"}, location="providers.test")


def test_redaction_removes_url_credentials_and_sensitive_query_values():
    value = "https://alice:password@example.test/v1?token=secret-value&safe=yes"
    redacted = redact_text(value)
    assert "password" not in redacted
    assert "secret-value" not in redacted
    assert redacted == "https://[REDACTED]example.test/v1?token=[REDACTED]&safe=yes"

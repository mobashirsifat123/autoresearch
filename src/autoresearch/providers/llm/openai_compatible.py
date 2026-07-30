"""OpenAI-compatible chat-completions adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..config import ProviderConfig
from ..errors import ErrorKind, ProviderError
from .base import CompletionResult, ModelInfo
from .http_base import HTTPProviderBase


class OpenAICompatibleProvider(HTTPProviderBase):
    def __init__(self, config: ProviderConfig, *, transport: Any | None = None) -> None:
        super().__init__(config, transport=transport)

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key()}", "Content-Type": "application/json"}

    async def complete(self, *, model: str, messages: list[dict[str, Any]], temperature: float = 0.2, max_tokens: int = 2048, response_format: dict[str, Any] | None = None, tools: list[dict[str, Any]] | None = None) -> CompletionResult:
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if response_format:
            normalized_format = dict(response_format)
            if normalized_format.get("type") == "json_schema":
                schema_document = dict(normalized_format.get("json_schema") or {})
                schema_document["strict"] = True
                normalized_format["json_schema"] = schema_document
            body["response_format"] = normalized_format
        if tools:
            body["tools"] = tools
        payload, headers = await self.request_json("POST", "/chat/completions", headers=self.headers(), json_body=body)
        try:
            choice = payload["choices"][0]["message"]
            text = choice.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Provider response omitted choices[0].message.", ErrorKind.SCHEMA, self.name) from exc
        raw = {"tool_calls": choice.get("tool_calls") or []}
        return self.completion_result(text=str(text), model=str(payload.get("model") or model), usage=payload.get("usage") or {}, request_id=headers.get("x-request-id") or payload.get("id"), raw=raw)

    def stream(self, *, model: str, messages: list[dict[str, Any]], temperature: float = 0.2, max_tokens: int = 2048) -> AsyncIterator[str]:
        async def iterator() -> AsyncIterator[str]:
            await self._limiter.acquire()
            async with self._semaphore:
                try:
                    async with self.client.stream("POST", "/chat/completions", headers=self.headers(), json={"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "stream": True}) as response:
                        if response.is_error:
                            raise ProviderError(f"Provider returned HTTP {response.status_code}.", ErrorKind.SERVER, self.name, response.status_code)
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                return
                            payload = json.loads(data)
                            token = payload.get("choices", [{}])[0].get("delta", {}).get("content")
                            if token:
                                yield str(token)
                except ProviderError:
                    raise
                except Exception as exc:
                    raise ProviderError("Provider streaming failed.", ErrorKind.UNAVAILABLE, self.name) from exc
        return iterator()

    async def model_info(self, model: str) -> ModelInfo:
        if model:
            return await super().model_info(model)
        await self.request_json("GET", "/models", headers=self.headers())
        return await super().model_info(model)

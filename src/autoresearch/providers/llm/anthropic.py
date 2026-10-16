"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..errors import ErrorKind, ProviderError
from .base import CompletionResult
from .http_base import HTTPProviderBase


class AnthropicProvider(HTTPProviderBase):
    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key(), "anthropic-version": "2023-06-01", "Content-Type": "application/json"}

    async def complete(self, *, model: str, messages: list[dict[str, Any]], temperature: float = 0.2, max_tokens: int | None = None, response_format: dict[str, Any] | None = None, tools: list[dict[str, Any]] | None = None) -> CompletionResult:
        system = "\n".join(str(item.get("content", "")) for item in messages if item.get("role") == "system")
        # Recent Claude models reject explicit sampling parameters; omit
        # temperature so the same adapter remains model-version neutral.
        # Anthropic REQUIRES an explicit output cap, so "unlimited" maps to a
        # high model-safe limit rather than an omitted parameter.
        effective_cap = max_tokens if max_tokens is not None else 8192
        body: dict[str, Any] = {"model": model, "messages": [item for item in messages if item.get("role") != "system"], "max_tokens": effective_cap}
        if system:
            body["system"] = system
        structured_tool_name: str | None = None
        if response_format:
            if tools:
                raise ProviderError("Structured output and free-form tools cannot be combined in one Anthropic request.", ErrorKind.INVALID_REQUEST, self.name)
            schema_document = response_format.get("json_schema") or {}
            structured_tool_name = str(schema_document.get("name") or "structured_output")
            body["tools"] = [{
                "name": structured_tool_name,
                "description": "Return the requested structured result.",
                "input_schema": schema_document.get("schema") or {"type": "object"},
                "strict": True,
            }]
            body["tool_choice"] = {"type": "tool", "name": structured_tool_name, "disable_parallel_tool_use": True}
        elif tools:
            body["tools"] = [_anthropic_tool(tool) for tool in tools]
        payload, headers = await self.request_json("POST", "/v1/messages", headers=self.headers(), json_body=body)
        blocks = payload.get("content") or []
        stop_reason = payload.get("stop_reason")
        if structured_tool_name:
            structured = next((block for block in blocks if block.get("type") == "tool_use" and block.get("name") == structured_tool_name), None)
            if not structured or not isinstance(structured.get("input"), dict):
                if stop_reason == "max_tokens":
                    # The output cap cut the forced tool call mid-flight — a
                    # reasoning model spends hidden thinking against the same
                    # budget. Report truncation (not a schema error) so the
                    # router's self-heal retries with the cap doubled.
                    return self.completion_result(
                        text="",
                        model=str(payload.get("model") or model),
                        usage=payload.get("usage") or {},
                        request_id=headers.get("request-id") or payload.get("id"),
                        raw={"tool_calls": [], "finish_reason": "length", "effective_max_tokens": effective_cap},
                    )
                raise ProviderError(
                    "Anthropic response omitted the required structured tool output.",
                    ErrorKind.SCHEMA,
                    self.name,
                    details={"stop_reason": stop_reason},
                )
            text = json.dumps(structured["input"], ensure_ascii=False)
        else:
            text = "".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")
        if not text and not any(block.get("type") == "tool_use" for block in blocks):
            raise ProviderError("Anthropic response contained no text or tool call.", ErrorKind.SCHEMA, self.name)
        # Normalize Anthropic's "max_tokens" to the provider-neutral "length"
        # the router's truncation self-heal listens for, and report the cap
        # actually applied so the router can double it even when the caller
        # configured none (this adapter always sends an explicit max_tokens).
        raw = {
            "tool_calls": [block for block in blocks if block.get("type") == "tool_use"],
            "finish_reason": "length" if stop_reason == "max_tokens" else stop_reason,
            "effective_max_tokens": effective_cap,
        }
        return self.completion_result(text=text, model=str(payload.get("model") or model), usage=payload.get("usage") or {}, request_id=headers.get("request-id") or payload.get("id"), raw=raw)

    def stream(self, *, model: str, messages: list[dict[str, Any]], temperature: float = 0.2, max_tokens: int | None = None) -> AsyncIterator[str]:
        async def iterator() -> AsyncIterator[str]:
            result = await self.complete(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
            yield result.text
        return iterator()


def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    return {"name": function.get("name"), "description": function.get("description") or "", "input_schema": function.get("parameters") or {"type": "object"}}

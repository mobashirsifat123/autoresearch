"""Durable bounded conversational orchestration over the backend ModelRouter."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from autoresearch.domain.models import PipelineKind, RunStatus, dataclass_payload
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.redaction import redact_text

from .capabilities import CapabilityRegistry
from .models import AgentSession, AgentTurn, CapabilityState, RoutingDecision, SessionMode, ToolObservation, TurnStatus
from .permissions import AgentPermissionService
from .routing import AgentIntentRouter
from .store import AgentStore
from .tools import AgentToolRegistry


PROMPT_VERSION = "agent-runtime-v1"
SYSTEM_PROMPT = """You are AutoResearch, a general-purpose coding and research assistant.
Default to normal repository and coding assistance. Use the supplied generic tools only when observation is needed.
Never invoke or claim that a scientific pipeline ran unless the backend explicitly activated one.
Never request provider credentials. Keep responses concise, evidence-based, and distinguish observations from inferences.
Scientific novelty is never proven; use cautious statuses such as potentially novel or expert review required."""


class AgentRuntime:
    def __init__(self, container: Any, *, allowed_roots: tuple[Path, ...]) -> None:
        self.container = container
        self.store = AgentStore(container.repository)
        self.capabilities = CapabilityRegistry(container)
        self.tools = AgentToolRegistry()
        self.permissions = AgentPermissionService(self.store)
        self.intent_router = AgentIntentRouter()
        roots = tuple(path.expanduser().resolve() for path in allowed_roots)
        self.allowed_roots = roots or (Path.cwd().resolve(),)

    def create_session(self, project_id: str, *, active_repository: str | None = None, working_directory: str | None = None, token_budget: int = 32_000, cost_budget: float | None = None) -> AgentSession:
        if not self.container.repository.get_project(project_id):
            raise ValueError(f"Project '{project_id}' was not found.")
        repository = self._location(active_repository or str(Path.cwd()))
        working = self._location(working_directory or str(repository), within=repository)
        return self.store.create_session(project_id, str(repository), str(working), token_budget=token_budget, cost_budget=cost_budget)

    def session_payload(self, session_id: str) -> dict[str, Any]:
        session = self.store.require_session(session_id)
        turns = self.container.repository.rows("SELECT id FROM agent_turns WHERE session_id=? ORDER BY created_at", (session_id,))
        permissions = self.container.repository.rows("SELECT id,turn_id,tool_id,scope,repository,action,decision,expires_at,created_at,updated_at FROM agent_permissions WHERE session_id=? ORDER BY created_at", (session_id,))
        suggestions = self.container.repository.rows("SELECT id,turn_id,capability_id,status,created_at,decided_at FROM agent_capability_suggestions WHERE session_id=? ORDER BY created_at", (session_id,))
        return {
            "session": asdict(session),
            "messages": self.store.messages(session_id),
            "turns": [asdict(value) for row in turns if (value := self.store.turn(row["id"]))],
            "permissions": permissions,
            "suggestions": suggestions,
            "artifacts": self.store.artifacts(session_id),
        }

    def start_turn(self, session_id: str, message: str, *, capability_override: str | None = None, token_budget: int | None = None, cost_budget: float | None = None, time_budget_seconds: float = 120.0) -> AgentTurn:
        session = self.store.require_session(session_id)
        cleaned = " ".join(message.split())
        if not cleaned or len(message) > 100_000:
            raise ValueError("Agent message must contain between 1 and 100000 characters.")
        if capability_override:
            SessionMode(capability_override)
        return self.store.create_turn(session_id, message, override_mode=capability_override, max_tool_iterations=3, max_model_calls=4, time_budget_seconds=min(time_budget_seconds, 600.0), token_budget=min(token_budget or session.token_budget, session.token_budget), cost_budget=session.cost_budget if cost_budget is None else min(cost_budget, session.cost_budget) if session.cost_budget is not None else cost_budget)

    async def execute_turn(self, turn_id: str) -> None:
        turn = self.store.require_turn(turn_id)
        session = self.store.require_session(turn.session_id)
        started = time.monotonic()
        self.store.update_session(session.id, cancellation_requested=False)
        self.store.update_turn(turn.id, status=TurnStatus.RUNNING)
        self._event(session.id, "agent.turn_started", {"turn_id": turn.id, "message_id": self._input_message_id(turn.id)})
        try:
            routing = self.intent_router.route(turn.message, mode=session.selected_mode, auto_routing=session.auto_routing, override=turn.override_mode)
            turn = self.store.update_turn(turn.id, routing=routing)
            self._event(session.id, "agent.routing_decided", {"turn_id": turn.id, "routing": asdict(routing)})
            if routing.recommended_capability and not routing.explicit and routing.confirmation_required:
                await self._suggest(turn, session, routing)
                return
            if routing.recommended_capability:
                await self._scientific_turn(turn, session, routing, started)
                return
            await self._default_turn(turn, session, started)
        except asyncio.CancelledError:
            self.store.update_turn(turn.id, status=TurnStatus.CANCELLED, error="Cancelled by user.", completed=True)
            self._event(session.id, "agent.turn_canceled", {"turn_id": turn.id})
            raise
        except Exception as exc:
            message = redact_text(f"{type(exc).__name__}: {exc}")
            self.store.update_turn(turn.id, status=TurnStatus.FAILED, error=message, completed=True)
            self._event(session.id, "agent.tool_failed" if "tool" in message.lower() else "agent.turn_failed", {"turn_id": turn.id, "error": {"code": _error_code(exc), "message": message}})

    async def approve_suggestion(self, suggestion_id: str, *, approved: bool) -> AgentTurn | None:
        suggestion = self.store.decide_suggestion(suggestion_id, approved)
        original = self.store.require_turn(str(suggestion["turn_id"]))
        if not approved:
            response = "I’ll continue without activating that scientific capability. Default agent mode remains available."
            self.store.append_message(original.session_id, "assistant", response, turn_id=original.id, metadata={"suggestion_id": suggestion_id, "declined": True})
            self._response(original.session_id, original.id, response)
            self.store.update_turn(original.id, status=TurnStatus.COMPLETED, completed=True)
            self._event(original.session_id, "agent.turn_completed", {"turn_id": original.id, "status": "completed"})
            return None
        mode = _mode_for_capability(str(suggestion["capability_id"]))
        self.store.update_turn(original.id, status=TurnStatus.COMPLETED, completed=True)
        self._event(original.session_id, "capability.selected", {"turn_id": original.id, "capability_id": suggestion["capability_id"], "scope": "one_turn"})
        return self.start_turn(original.session_id, str(suggestion["message"]), capability_override=mode.value)

    async def decide_permission(self, permission_id: str, *, approved: bool, expires_at: str | None = None) -> AgentTurn | None:
        permission = self.store.decide_permission(permission_id, approved=approved, expires_at=expires_at)
        turn = self.store.require_turn(str(permission["turn_id"]))
        call = next((item for item in self.store.pending_tool_calls(turn.id) if item.get("permission_id") == permission_id), None)
        if not call:
            return turn
        if self.store.require_session(turn.session_id).cancellation_requested:
            self.store.update_tool_call(call["id"], status="cancelled", error="Tool execution was cancelled before approval.")
            self.store.update_turn(turn.id, status=TurnStatus.CANCELLED, error="Cancelled before permission was resolved.", completed=True)
            self._event(turn.session_id, "agent.turn_canceled", {"turn_id": turn.id, "permission_id": permission_id})
            return None
        if not approved:
            self.store.update_tool_call(call["id"], status="denied", error="Permission denied by user.")
            response = f"I did not run `{call['tool_id']}` because permission was denied."
            self.store.append_message(turn.session_id, "assistant", response, turn_id=turn.id)
            self._response(turn.session_id, turn.id, response)
            self.store.update_turn(turn.id, status=TurnStatus.COMPLETED, completed=True)
            self._event(turn.session_id, "agent.turn_completed", {"turn_id": turn.id, "status": "completed"})
            return None
        session = self.store.require_session(turn.session_id)
        await self._execute_tool_call(turn, session, call)
        self.store.update_turn(turn.id, status=TurnStatus.RUNNING)
        return turn

    def cancel(self, session_id: str) -> None:
        session = self.store.update_session(session_id, cancellation_requested=True)
        if session.active_run_id:
            run = self.container.repository.get_run(session.active_run_id)
            if run and run.status in {RunStatus.RUNNING, RunStatus.PENDING}:
                self.container.repository.cancel_run(run.id)

    async def continue_after_permission(self, turn_id: str) -> None:
        turn = self.store.require_turn(turn_id)
        await self._default_turn(turn, self.store.require_session(turn.session_id), time.monotonic())

    async def _suggest(self, turn: AgentTurn, session: AgentSession, routing: RoutingDecision) -> None:
        capability = self.capabilities.get(str(routing.recommended_capability))
        if capability.state is CapabilityState.DISABLED:
            await self._unavailable(turn, session, capability.id, capability.reason or "Capability unavailable.")
            return
        suggestion = self.store.create_suggestion(session.id, turn.id, capability.id, self.intent_router.strip_override(turn.message), routing)
        self.store.update_turn(turn.id, status=TurnStatus.AWAITING_CONFIRMATION)
        self._event(session.id, "capability.suggested", {"turn_id": turn.id, "suggestion_id": suggestion["id"], "capability": asdict(capability), "reason": routing.reason, "estimated_cost": "Depends on configured provider usage; no exact estimate is available.", "confirmation_required": True})

    async def _default_turn(self, turn: AgentTurn, session: AgentSession, started: float) -> None:
        capability = self.capabilities.get("general_agent_chat")
        if capability.state is CapabilityState.DISABLED:
            await self._unavailable(turn, session, capability.id, capability.reason or "No conversational model is configured.")
            return
        messages = self._context(session)
        while True:
            turn = self.store.require_turn(turn.id)
            self._require_budget(turn, started)
            if turn.model_calls >= turn.max_model_calls:
                raise RuntimeError("agent_limit_reached: maximum model calls reached for this turn")
            self._event(session.id, "agent.reasoning_started", {"turn_id": turn.id, "model_call": turn.model_calls + 1})
            role = self._agent_role()
            result = await self.container.router.tool_call(role, messages=messages, tools=self.tools.provider_tools(), project_id=session.project_id, run_id=None, session_id=session.id, operation="agent_turn")
            turn = self._account(turn, result.completion)
            calls = [_normalize_tool_call(item) for item in result.calls]
            calls = [item for item in calls if item]
            if not calls:
                text = result.completion.text.strip() or "I completed the turn, but the configured model returned no text."
                self.store.append_message(session.id, "assistant", text, turn_id=turn.id, metadata={"prompt_version": PROMPT_VERSION})
                self._response(session.id, turn.id, text)
                self.store.update_turn(turn.id, status=TurnStatus.COMPLETED, completed=True)
                self._event(session.id, "agent.turn_completed", {"turn_id": turn.id, "status": "completed", "model_calls": turn.model_calls, "tool_iterations": turn.tool_iterations})
                return
            if turn.tool_iterations + len(calls) > turn.max_tool_iterations:
                raise RuntimeError("agent_limit_reached: maximum tool iterations reached for this turn")
            for item in calls:
                call = self.store.create_tool_call(session.id, turn.id, str(item["name"]), dict(item["arguments"]))
                self._event(session.id, "agent.tool_proposed", {"turn_id": turn.id, "tool_call_id": call["id"], "tool_id": call["tool_id"], "arguments": call["arguments"]})
                definition = self.tools.get(call["tool_id"])
                decision = self.permissions.check(session, turn.id, definition, call["arguments"])
                if not decision.allowed:
                    self.store.update_tool_call(call["id"], status="awaiting_permission", permission_id=decision.permission_id)
                    self.store.update_turn(turn.id, status=TurnStatus.AWAITING_PERMISSION)
                    self._event(session.id, "agent.permission_required", {"turn_id": turn.id, "tool_call_id": call["id"], "permission_id": decision.permission_id, "tool_id": call["tool_id"], "scope": definition.permission, "repository": session.active_repository, "arguments": call["arguments"]})
                    return
                observation = await self._execute_tool_call(turn, session, call)
                messages.append({"role": "user", "content": f"Tool observation ({observation.tool_id}):\n{observation.content[:100000]}"})

    async def _execute_tool_call(self, turn: AgentTurn, session: AgentSession, call: dict[str, Any]) -> ToolObservation:
        self.store.update_tool_call(call["id"], status="running")
        self._event(session.id, "agent.tool_started", {"turn_id": turn.id, "tool_call_id": call["id"], "tool_id": call["tool_id"]})
        try:
            observation = await self.tools.execute(session, str(call["tool_id"]), dict(call["arguments"]))
        except Exception as exc:
            message = redact_text(f"{type(exc).__name__}: {exc}")
            self.store.update_tool_call(call["id"], status="failed", error=message)
            self._event(session.id, "agent.tool_failed", {"turn_id": turn.id, "tool_call_id": call["id"], "tool_id": call["tool_id"], "error": {"code": _error_code(exc), "message": message}})
            raise
        self.store.update_tool_call(call["id"], status="completed", observation=asdict(observation))
        current = self.store.require_turn(turn.id)
        self.store.update_turn(turn.id, tool_iterations=current.tool_iterations + 1)
        self.store.append_message(session.id, "tool", observation.content, turn_id=turn.id, metadata={"tool_id": observation.tool_id, **observation.metadata})
        self._event(session.id, "agent.tool_completed", {"turn_id": turn.id, "tool_call_id": call["id"], "tool_id": call["tool_id"], "ok": observation.ok, "metadata": observation.metadata, "observation_excerpt": observation.content[:4000]})
        return observation

    async def _scientific_turn(self, turn: AgentTurn, session: AgentSession, routing: RoutingDecision, started: float) -> None:
        capability = self.capabilities.get(str(routing.recommended_capability))
        if capability.state is CapabilityState.DISABLED:
            await self._unavailable(turn, session, capability.id, capability.reason or "Capability unavailable.")
            return
        self._require_budget(turn, started)
        message = self.intent_router.strip_override(turn.message)
        self._event(session.id, "agent.tool_started", {"turn_id": turn.id, "tool_id": capability.id, "capability": asdict(capability)})
        run_id, summary = await self._run_scientific(capability.id, session, message)
        self.store.update_session(session.id, active_run_id=run_id)
        artifacts = self.container.repository.artifacts_for_run(run_id)
        for artifact in artifacts:
            self.store.link_artifact(session.id, artifact.id, turn.id)
            self._event(session.id, "agent.artifact_linked", {"turn_id": turn.id, "run_id": run_id, "artifact": dataclass_payload(artifact)})
        self._event(session.id, "agent.tool_completed", {"turn_id": turn.id, "tool_id": capability.id, "run_id": run_id, "summary": summary, "artifact_ids": [item.id for item in artifacts]})
        response = await self._scientific_response(turn, session, capability.display_name, summary, artifacts)
        self.store.append_message(session.id, "assistant", response, turn_id=turn.id, metadata={"run_id": run_id, "capability_id": capability.id, "artifact_ids": [item.id for item in artifacts]})
        self._response(session.id, turn.id, response)
        self.store.update_turn(turn.id, status=TurnStatus.COMPLETED, completed=True)
        self._event(session.id, "agent.turn_completed", {"turn_id": turn.id, "status": "completed", "run_id": run_id, "capability_id": capability.id})

    async def _run_scientific(self, capability_id: str, session: AgentSession, message: str) -> tuple[str, dict[str, Any]]:
        repository = self.container.repository
        if capability_id == "study_search":
            seed = message.strip()
            if not seed:
                raise ValueError("required_input_missing: provide a research topic, PDF path, DOI, or HTTPS URL")
            limit = _requested_count(message, default=20, maximum=100)
            run = repository.create_run(session.project_id, PipelineKind.DISCOVERY, {"seed_input": seed, "limit": limit, "since_year": None, "max_cost_usd": None, "max_runtime_seconds": None})
            self.store.update_session(session.id, active_run_id=run.id)
            self._event(session.id, "agent.tool_progress", {"run_id": run.id, "stage": "study_search", "status": "running"})
            outcome = await self.container.discovery._execute(session.project_id, run.id, self.container.discovery.parser.parse_seed(seed), limit=limit, since_year=None, max_runtime_seconds=None)
            return run.id, asdict(outcome)
        if capability_id == "literature_review":
            if not repository.list_papers(session.project_id):
                raise ValueError("required_input_missing: no paper set exists; run Study Search first")
            top_k = _requested_count(message, default=5, maximum=15)
            run = repository.create_run(session.project_id, PipelineKind.REVIEW, {"top_k": top_k, "hierarchical": True, "max_cost_usd": None, "max_runtime_seconds": None})
            self.store.update_session(session.id, active_run_id=run.id)
            outcome = await self.container.review._execute(run.id, session.project_id, top_k=top_k, hierarchical=True, max_runtime_seconds=None)
            return run.id, {**asdict(outcome), "gap": dataclass_payload(outcome.gap)}
        if capability_id == "novelty_generation":
            gaps = repository.rows("SELECT id,confidence,statement FROM research_gaps WHERE project_id=? ORDER BY confidence DESC", (session.project_id,))
            gap_id = _mentioned_id(message, gaps) or _resolve_artifact(gaps, message, "research gap")
            run = repository.create_run(session.project_id, PipelineKind.NOVELTY, {"gap_id": gap_id})
            self.store.update_session(session.id, active_run_id=run.id)
            portfolio_id = await self.container.novelty.generate_portfolio(session.project_id, gap_id=gap_id, run_id=run.id)
            repository.finish_run(run.id)
            portfolio = repository.get_novelty_portfolio(run.id)
            return run.id, {"portfolio_id": portfolio_id, "portfolio": dataclass_payload(portfolio) if portfolio else None, "novelty_status": "potentially novel; expert review required"}
        if capability_id in {"experiment_design", "experiment_execution_local"}:
            candidates = repository.rows("SELECT id,method,status FROM novelty_candidates WHERE project_id=? ORDER BY rowid DESC", (session.project_id,))
            candidate_id = _mentioned_id(message, candidates) or _resolve_artifact(candidates, message, "novelty candidate")
            mode = "execute" if capability_id == "experiment_execution_local" else "plan"
            run = repository.create_run(session.project_id, PipelineKind.EXPERIMENT, {"candidate_id": candidate_id, "mode": mode})
            self.store.update_session(session.id, active_run_id=run.id)
            spec_id = await self.container.experiment.execute_candidate(candidate_id, run_id=run.id, mode=mode)
            repository.finish_run(run.id)
            preview = self.container.experiment.approval_preview_for_candidate(candidate_id)
            return run.id, {"spec_id": spec_id, "candidate_id": candidate_id, "mode": mode, "approval": preview if mode == "plan" else {"checksum": preview["checksum"], "executed": True}}
        if capability_id == "result_conditioned_reinvention":
            raise ValueError("capability_unavailable: Reinvention pipeline is not implemented in the Python backend")
        raise ValueError(f"capability_unavailable: unsupported scientific capability '{capability_id}'")

    async def _scientific_response(self, turn: AgentTurn, session: AgentSession, name: str, summary: dict[str, Any], artifacts: list[Any]) -> str:
        current = self.store.require_turn(turn.id)
        if current.model_calls >= current.max_model_calls:
            return f"{name} completed. Durable artifacts: {', '.join(item.id for item in artifacts) or 'none'}."
        role = self._agent_role()
        result = await self.container.router.complete(role, messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"A backend scientific capability named {name} completed. Summarize it conversationally without inventing facts. Durable result:\n{json.dumps(summary, ensure_ascii=False, default=str)[:40000]}\nArtifact IDs: {[item.id for item in artifacts]}"}], project_id=session.project_id, session_id=session.id, operation="agent_scientific_summary")
        self._account(current, result)
        return result.text.strip() or f"{name} completed with durable evidence artifacts."

    async def _unavailable(self, turn: AgentTurn, session: AgentSession, capability_id: str, reason: str) -> None:
        response = f"{capability_id} is unavailable: {reason} Default agent assistance remains available."
        self.store.append_message(session.id, "assistant", response, turn_id=turn.id, metadata={"capability_unavailable": capability_id})
        self._event(session.id, "capability.unavailable", {"turn_id": turn.id, "capability_id": capability_id, "reason": reason})
        self._response(session.id, turn.id, response)
        self.store.update_turn(turn.id, status=TurnStatus.COMPLETED, completed=True)
        self._event(session.id, "agent.turn_completed", {"turn_id": turn.id, "status": "completed"})

    def _context(self, session: AgentSession) -> list[dict[str, Any]]:
        artifacts = self.store.artifacts(session.id)
        artifact_context = [{"id": item["id"], "type": item["artifact_type"], "run_id": item["run_id"]} for item in artifacts[-20:]]
        messages: list[dict[str, Any]] = [{"role": "system", "content": f"{SYSTEM_PROMPT}\nActive repository: {session.active_repository}\nWorking directory: {session.working_directory}\nSession mode: {session.selected_mode.value}\nReferenced artifacts: {json.dumps(artifact_context)}\nPrompt version: {PROMPT_VERSION}"}]
        for item in self.store.messages(session.id, limit=50):
            role = str(item["role"])
            content = str(item["content"])
            messages.append({"role": "user" if role == "tool" else role, "content": f"Tool observation: {content}" if role == "tool" else content})
        return messages

    def _agent_role(self) -> ModelRole:
        for role in (ModelRole.CHAT, ModelRole.CODING, ModelRole.RESEARCH_REASONING):
            if role in self.container.router.assignments:
                return role
        raise ValueError("capability_unavailable: no conversational model role is configured")

    def _account(self, turn: AgentTurn, completion: Any) -> AgentTurn:
        updated = self.store.update_turn(turn.id, model_calls=turn.model_calls + 1, input_tokens=turn.input_tokens + int(completion.prompt_tokens or 0), output_tokens=turn.output_tokens + int(completion.completion_tokens or 0), estimated_cost=turn.estimated_cost + float(completion.estimated_cost or 0))
        if updated.input_tokens + updated.output_tokens > updated.token_budget:
            raise RuntimeError("agent_budget_exceeded: per-turn token budget exceeded")
        if updated.cost_budget is not None and updated.estimated_cost > updated.cost_budget:
            raise RuntimeError("agent_budget_exceeded: per-turn cost budget exceeded")
        return updated

    def _require_budget(self, turn: AgentTurn, started: float) -> None:
        if time.monotonic() - started > turn.time_budget_seconds:
            raise TimeoutError("agent_timeout: turn time budget exceeded")
        if self.store.require_session(turn.session_id).cancellation_requested:
            raise asyncio.CancelledError

    def _response(self, session_id: str, turn_id: str, text: str) -> None:
        for offset in range(0, len(text), 160):
            self._event(session_id, "agent.response_delta", {"turn_id": turn_id, "delta": text[offset:offset + 160]})

    def _event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> int:
        return self.store.append_event(session_id, event_type, payload)

    def _location(self, raw: str, *, within: Path | None = None) -> Path:
        path = Path(raw).expanduser().resolve(strict=True)
        roots = (within,) if within else self.allowed_roots
        if not any(path == root or root in path.parents for root in roots):
            raise ValueError("agent_path_forbidden: repository or working directory is outside configured agent roots")
        if not path.is_dir():
            raise ValueError("agent_path_invalid: repository and working directory must be directories")
        return path

    def _input_message_id(self, turn_id: str) -> str:
        row = self.container.repository.row("SELECT input_message_id FROM agent_turns WHERE id=?", (turn_id,))
        return str(row["input_message_id"]) if row else ""


def _normalize_tool_call(raw: dict[str, Any]) -> dict[str, Any] | None:
    function = raw.get("function") if isinstance(raw.get("function"), dict) else None
    name = function.get("name") if function else raw.get("name")
    arguments: Any = function.get("arguments") if function else raw.get("input") or raw.get("args") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("tool_invalid_input: provider returned invalid tool arguments") from exc
    if not name or not isinstance(arguments, dict):
        return None
    return {"name": str(name), "arguments": arguments}


def _mode_for_capability(capability_id: str) -> SessionMode:
    mapping = {"study_search": SessionMode.STUDY_SEARCH, "literature_review": SessionMode.LITERATURE_REVIEW, "novelty_generation": SessionMode.NOVELTY, "experiment_design": SessionMode.EXPERIMENT, "experiment_execution_local": SessionMode.EXPERIMENT}
    if capability_id not in mapping:
        raise ValueError(f"capability_unavailable: capability '{capability_id}' cannot be selected")
    return mapping[capability_id]


def _requested_count(message: str, *, default: int, maximum: int) -> int:
    matches = [int(value) for value in re.findall(r"\b(\d{1,3})\b", message)]
    value = next((item for item in matches if 1 <= item <= maximum), default)
    return min(maximum, max(1, value))


def _mentioned_id(message: str, rows: list[dict[str, Any]]) -> str | None:
    return next((str(row["id"]) for row in rows if str(row["id"]) in message), None)


def _resolve_artifact(rows: list[dict[str, Any]], message: str, label: str) -> str:
    if not rows:
        raise ValueError(f"required_input_missing: no {label} exists in the active project")
    if len(rows) == 1 or re.search(r"\b(latest|strongest|top|best)\b", message, re.I):
        return str(rows[0]["id"])
    choices = [{key: value for key, value in row.items() if key in {"id", "statement", "method", "confidence", "status"}} for row in rows[:10]]
    raise ValueError(f"ambiguous_artifact: choose one {label}: {json.dumps(choices, ensure_ascii=False)}")


def _error_code(exc: Exception) -> str:
    text = str(exc)
    prefix = text.split(":", 1)[0]
    return prefix if re.fullmatch(r"[a-z][a-z0-9_]*", prefix) else "agent_error"

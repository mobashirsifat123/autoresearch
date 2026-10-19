"""Deterministic runtime perception for experiment subprocesses.

The executor must not wait for a long run to finish before noticing that the
run is already invalid. This module implements the transferable part of
EmbodiedAct's runtime perception stack (monitor contract, probe line protocol,
severity ladder, bounded telemetry history) without any LLM calls:

- generated experiment code emits ``AUTORESEARCH_PROBE:{"name": ..., "value": ...}``
  (the legacy ``EA_PROBE:`` prefix is accepted for interoperability);
- a small, optional contract declares at most three signals with bounds,
  trends, or a safe ``abort_if`` predicate;
- every sample is graded ``info`` / ``suspect`` / ``fatal`` by deterministic
  rules. ``fatal`` means the worker terminates the process group; ``suspect``
  is retained as evidence for the scientific report and recovery policy, never
  as an automatic abort on its own.

Safety model for ``abort_if``: expression-only AST allowlist (no attributes,
subscripts, lambdas, comprehensions, power operator, or keywords), an explicit
function whitelist, and empty builtins. Invalid predicates are ignored rather
than executed. This is adapted at the mechanism level from EmbodiedAct's
perceiver compiler; no upstream code is copied.
"""

from __future__ import annotations

import ast
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

PROBE_PREFIXES = ("AUTORESEARCH_PROBE:", "EA_PROBE:")
EVENT_PREFIXES = ("AUTORESEARCH_EVENT:", "EA_EMIT:")
MAX_MONITORS = 3
MAX_HISTORY = 64
MAX_TRACE = 200
MAX_PREDICATE_CHARS = 400

_ALLOWED_FUNCS = {
    "abs": abs,
    "min": min,
    "max": max,
    "isnan": math.isnan,
    "isinf": math.isinf,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
}
_ALLOWED_NAMES = set(_ALLOWED_FUNCS) | {"value", "prev", "step"}
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Call, ast.Name, ast.Load, ast.Constant, ast.IfExp,
)


def _as_float(value: Any) -> float | None:
    try:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            return float(value)
    except (TypeError, ValueError):
        return None
    return None


def _clean_source(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = text.replace("&&", " and ").replace("||", " or ")
    text = text.replace("~=", "!=")
    return text.strip()


@dataclass(frozen=True)
class MonitorSignal:
    """One declared signal and how to grade its samples."""

    name: str
    constraint_id: str = ""
    units: str = ""
    cadence: str = "every_step"
    minimum: float | None = None
    maximum: float | None = None
    trend: str = ""
    abort_if: str = ""
    why: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "MonitorSignal | None":
        if not isinstance(raw, Mapping):
            return None
        name = str(raw.get("name") or "").strip()
        if not name:
            return None
        healthy = raw.get("healthy") if isinstance(raw.get("healthy"), Mapping) else {}
        return cls(
            name=name[:80],
            constraint_id=str(raw.get("constraint_id") or "")[:80],
            units=str(raw.get("units") or "")[:40],
            cadence=str(raw.get("cadence") or "every_step")[:40],
            minimum=_as_float(healthy.get("min")),
            maximum=_as_float(healthy.get("max")),
            trend=str(healthy.get("trend") or "").strip().lower()[:20],
            abort_if=_clean_source(raw.get("abort_if"))[:MAX_PREDICATE_CHARS],
            why=str(raw.get("why") or "")[:300],
        )

    def to_dict(self) -> dict[str, Any]:
        healthy: dict[str, Any] = {}
        if self.minimum is not None:
            healthy["min"] = self.minimum
        if self.maximum is not None:
            healthy["max"] = self.maximum
        if self.trend:
            healthy["trend"] = self.trend
        return {
            "name": self.name,
            "constraint_id": self.constraint_id,
            "units": self.units,
            "cadence": self.cadence,
            "healthy": healthy,
            "abort_if": self.abort_if,
            "why": self.why,
        }


@dataclass(frozen=True)
class MonitorContract:
    """Parsed probe contract. Absence is valid: process-level checks remain."""

    monitors: tuple[MonitorSignal, ...] = ()
    stall_seconds: float = 0.0
    warnings: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: Any) -> "MonitorContract":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, Mapping):
            return cls()
        warnings: list[str] = []
        monitors: list[MonitorSignal] = []
        items = raw.get("monitors") or []
        if not isinstance(items, (list, tuple)):
            warnings.append("monitor_contract.monitors was not an array; ignored")
            items = []
        seen_names: set[str] = set()
        for item in items:
            signal = MonitorSignal.from_raw(item)
            if signal is None:
                warnings.append("monitor_contract contained an entry without a name; ignored")
                continue
            if signal.name in seen_names:
                warnings.append(f"duplicate monitor name '{signal.name}' ignored; the first declaration wins")
                continue
            if signal.minimum is not None and signal.maximum is not None and signal.minimum > signal.maximum:
                warnings.append(f"monitor '{signal.name}' has min > max; bounds disabled")
                signal = MonitorSignal(
                    name=signal.name, constraint_id=signal.constraint_id, units=signal.units,
                    cadence=signal.cadence, trend=signal.trend, abort_if=signal.abort_if, why=signal.why,
                )
            seen_names.add(signal.name)
            monitors.append(signal)
        if len(monitors) > MAX_MONITORS:
            warnings.append(
                f"monitor_contract declared {len(monitors)} signals; only the first "
                f"{MAX_MONITORS} are enforced"
            )
            monitors = monitors[:MAX_MONITORS]
        stall = _as_float(raw.get("stall_seconds")) or 0.0
        if stall < 0:
            warnings.append("monitor_contract.stall_seconds was negative; disabled")
            stall = 0.0
        return cls(monitors=tuple(monitors), stall_seconds=stall, warnings=tuple(warnings))

    def signal(self, name: str) -> MonitorSignal | None:
        return next((item for item in self.monitors if item.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "monitors": [item.to_dict() for item in self.monitors],
            "stall_seconds": self.stall_seconds,
            "warnings": list(self.warnings),
        }


@dataclass
class CompiledPredicate:
    source: str
    _fn: Any
    failures: int = 0
    disabled: bool = False

    def evaluate(self, value: float, prev: float | None, step: int | None) -> bool:
        if self.disabled:
            return False
        try:
            return bool(self._fn(value, prev, step))
        except Exception:  # noqa: BLE001 — a bad predicate must not kill the run
            self.failures += 1
            if self.failures >= 3:
                self.disabled = True
            return False


def compile_predicate(source: str | None) -> CompiledPredicate | None:
    """Compile a bounded boolean expression, or return ``None`` if unsafe."""
    text = _clean_source(source)
    if not text or len(text) > MAX_PREDICATE_CHARS:
        return None
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return None
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                return None
            if node.keywords:
                return None
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_NAMES:
            return None
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
            return None
    code = compile(tree, "<monitor-predicate>", "eval")

    def _fn(value: float, prev: float | None, step: int | None) -> bool:
        scope = dict(_ALLOWED_FUNCS)
        scope.update({"value": value, "prev": prev, "step": step})
        return bool(eval(code, {"__builtins__": {}}, scope))  # noqa: S307 — AST allowlist above

    return CompiledPredicate(source=text, _fn=_fn)


@dataclass(frozen=True)
class StreamVerdict:
    severity: str  # info | suspect | fatal
    reason: str = ""
    signal: str = ""
    value: float | None = None
    step: int | None = None
    ts: float = 0.0

    @property
    def abort(self) -> bool:
        return self.severity == "fatal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "reason": self.reason,
            "signal": self.signal,
            "value": self.value,
            "step": self.step,
            "ts": self.ts,
        }


def parse_probe_line(line: str) -> tuple[str, dict[str, Any]] | None:
    """Parse one stdout line into ``("probe"|"event", payload)``.

    Unknown or malformed lines return ``None``; they remain ordinary stdout.
    """
    stripped = line.strip()
    for prefix in PROBE_PREFIXES:
        if stripped.startswith(prefix):
            payload = _loads(stripped[len(prefix):])
            if isinstance(payload, dict) and str(payload.get("name") or "").strip():
                return "probe", dict(payload)
            return None
    for prefix in EVENT_PREFIXES:
        if stripped.startswith(prefix):
            payload = _loads(stripped[len(prefix):])
            if isinstance(payload, dict) and str(payload.get("event") or "").strip():
                return "event", dict(payload)
            return None
    return None


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


class StreamingPerception:
    """Incremental, deterministic grading of a live probe stream."""

    def __init__(
        self,
        contract: MonitorContract | Mapping[str, Any] | None = None,
        *,
        increase_tol: float = 1e-9,
        suspect_after: int = 3,
        fatal_after: int = 6,
    ) -> None:
        self.contract = contract if isinstance(contract, MonitorContract) else MonitorContract.parse(contract)
        self.increase_tol = max(0.0, float(increase_tol))
        self.suspect_after = max(1, int(suspect_after))
        self.fatal_after = max(self.suspect_after, int(fatal_after))
        self._history: dict[str, list[float]] = {}
        self._violations: dict[str, int] = {}
        self._predicates: dict[str, CompiledPredicate] = {}
        for signal in self.contract.monitors:
            compiled = compile_predicate(signal.abort_if)
            if compiled is not None:
                self._predicates[signal.name] = compiled
        self._trace: list[dict[str, Any]] = []
        self._sample_counts: dict[str, int] = {}
        self._suspect_count = 0
        self._fatal_count = 0
        self._malformed_probe_lines = 0
        self.first_fatal: StreamVerdict | None = None
        self._predicate_status: dict[str, str] = {
            signal.name: ("compiled" if signal.name in self._predicates else "not_compiled")
            for signal in self.contract.monitors if signal.abort_if
        }

    # -- observation -----------------------------------------------------
    def observe(self, payload: Mapping[str, Any], ts: float | None = None) -> StreamVerdict:
        now = time.time() if ts is None else float(ts)
        name = str(payload.get("name") or "").strip()[:80]
        step = _as_float(payload.get("step"))
        step_int = int(step) if step is not None and math.isfinite(step) else None
        value = _as_float(payload.get("value"))
        if not name:
            return StreamVerdict("info", "probe without a name ignored", ts=now)
        if value is None:
            return self._record(StreamVerdict("suspect", f"{name}: non-numeric probe value", name, None, step_int, now))
        if math.isnan(value) or math.isinf(value):
            return self._record(self._fatal(f"{name}={value}", name, value, step_int, now))

        previous = self._history.get(name, [None])[-1]  # type: ignore[list-item]
        self._append(name, value)
        self._sample_counts[name] = self._sample_counts.get(name, 0) + 1
        signal = self.contract.signal(name)

        predicate = self._predicates.get(name)
        if predicate is not None and predicate.evaluate(value, previous, step_int):
            return self._record(self._fatal(
                f"{name}: abort_if fired ({predicate.source[:120]})", name, value, step_int, now,
            ))

        if signal is not None:
            if signal.maximum is not None and value > signal.maximum:
                return self._record(self._fatal(
                    f"{name}={value:g} > max {signal.maximum:g}", name, value, step_int, now,
                ))
            if signal.minimum is not None and value < signal.minimum:
                return self._record(self._fatal(
                    f"{name}={value:g} < min {signal.minimum:g}", name, value, step_int, now,
                ))
            trend = signal.trend
            if trend in {"increasing", "decreasing"} and previous is not None:
                if trend == "decreasing":
                    violated = value > previous and (value - previous) > self.increase_tol * max(abs(previous), 1.0)
                else:
                    violated = value < previous and (previous - value) > self.increase_tol * max(abs(previous), 1.0)
                if violated:
                    self._violations[name] = self._violations.get(name, 0) + 1
                else:
                    self._violations[name] = 0
                count = self._violations.get(name, 0)
                if count >= self.fatal_after:
                    return self._record(self._fatal(
                        f"{name} trend violation persisted ({count} consecutive samples; expected {trend})",
                        name, value, step_int, now,
                    ))
                if count >= self.suspect_after:
                    return self._record(StreamVerdict(
                        "suspect",
                        f"{name} not {trend} ({count} consecutive samples)",
                        name, value, step_int, now,
                    ))
        return StreamVerdict("info", signal=name, value=value, step=step_int, ts=now)

    def on_event(self, payload: Mapping[str, Any], ts: float | None = None) -> StreamVerdict:
        now = time.time() if ts is None else float(ts)
        event = str(payload.get("event") or "").strip()[:120]
        declared = str(payload.get("severity") or "").lower()
        value = _as_float(payload.get("value"))
        if declared == "info":
            return StreamVerdict("info", f"guard info:{event}", event, value, None, now)
        if declared in {"warning", "suspect"}:
            return self._record(StreamVerdict("suspect", f"guard warning:{event}", event, value, None, now))
        return self._record(self._fatal(f"guard:{event}", event, value, None, now))

    def observe_line(self, line: str, ts: float | None = None) -> StreamVerdict | None:
        parsed = parse_probe_line(line)
        if parsed is None:
            stripped = line.strip()
            if any(stripped.startswith(prefix) for prefix in (*PROBE_PREFIXES, *EVENT_PREFIXES)):
                self._malformed_probe_lines += 1
            return None
        kind, payload = parsed
        return self.observe(payload, ts=ts) if kind == "probe" else self.on_event(payload, ts=ts)

    # -- views -----------------------------------------------------------
    def recent_window(self, n: int = 10) -> dict[str, list[float]]:
        return {name: list(values[-n:]) for name, values in self._history.items() if values}

    def trend_features(self) -> dict[str, dict[str, float]]:
        features: dict[str, dict[str, float]] = {}
        for name, values in self._history.items():
            if not values:
                continue
            features[name] = {
                "last": values[-1],
                "min": min(values),
                "max": max(values),
                "n": float(len(values)),
                "slope_est": (values[-1] - values[0]) / max(len(values) - 1, 1),
            }
        return features

    def render_digest(self, max_chars: int = 1200) -> str:
        parts = [
            f"{name}: n={len(values)} last={values[-1]:g} min={min(values):g} max={max(values):g}"
            for name, values in self._history.items() if values
        ]
        return "\n".join(parts)[:max_chars]

    @property
    def verdicts(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._trace)

    @property
    def suspect_count(self) -> int:
        return self._suspect_count

    @property
    def fatal_count(self) -> int:
        return self._fatal_count

    def summary(self) -> dict[str, Any]:
        return {
            "contract": self.contract.to_dict(),
            "signals": self.trend_features(),
            "samples": dict(self._sample_counts),
            "unobserved_monitors": [
                signal.name for signal in self.contract.monitors
                if not self._sample_counts.get(signal.name)
            ],
            "predicate_status": dict(self._predicate_status),
            "malformed_probe_lines": self._malformed_probe_lines,
            "suspect_verdicts": self._suspect_count,
            "fatal_verdicts": self._fatal_count,
            "first_fatal": self.first_fatal.to_dict() if self.first_fatal else None,
            "digest": self.render_digest(),
        }

    # -- internals -------------------------------------------------------
    def _fatal(self, reason: str, signal: str, value: float | None, step: int | None, ts: float) -> StreamVerdict:
        return StreamVerdict("fatal", reason, signal, value, step, ts)

    def _record(self, verdict: StreamVerdict) -> StreamVerdict:
        if verdict.severity == "suspect":
            self._suspect_count += 1
        elif verdict.severity == "fatal":
            self._fatal_count += 1
            if self.first_fatal is None:
                self.first_fatal = verdict
        if verdict.severity != "info" and len(self._trace) < MAX_TRACE:
            self._trace.append(verdict.to_dict())
        return verdict

    def _append(self, name: str, value: float) -> None:
        history = self._history.setdefault(name, [])
        history.append(value)
        if len(history) > MAX_HISTORY:
            del history[0]


def build_monitor_report(
    perception: StreamingPerception,
    *,
    exit_code: int | None,
    elapsed_seconds: float,
    abort_reason: str = "",
    abort_category: str = "",
    timed_out: bool = False,
    stalled: bool = False,
) -> dict[str, Any]:
    """Serialize the perception state for durable evidence.

    This is a bounded summary plus non-info verdicts, not a full replay of
    every streamed sample.
    """
    payload = perception.summary()
    payload.update({
        "kind": "execution_monitor",
        "exit_code": exit_code,
        "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
        "abort_reason": abort_reason,
        "abort_category": abort_category,
        "timed_out": bool(timed_out),
        "stalled": bool(stalled),
        "aborted": bool(abort_reason),
        "verdicts": list(perception.verdicts),
    })
    return payload

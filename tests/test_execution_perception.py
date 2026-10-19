"""Deterministic runtime perception: contract parsing, safety, severity ladder."""

from __future__ import annotations

import json

from autoresearch.experiments.perception import (
    MonitorContract,
    StreamingPerception,
    build_monitor_report,
    compile_predicate,
    parse_probe_line,
)


def test_probe_line_protocol_accepts_current_and_legacy_prefixes():
    current = parse_probe_line('AUTORESEARCH_PROBE:{"name": "loss", "value": 1.5, "step": 2}')
    legacy = parse_probe_line('EA_PROBE:{"name": "loss", "value": 1.5}')
    event = parse_probe_line('AUTORESEARCH_EVENT:{"event": "diverged", "value": 9}')
    assert current == ("probe", {"name": "loss", "value": 1.5, "step": 2})
    assert legacy == ("probe", {"name": "loss", "value": 1.5})
    assert event == ("event", {"event": "diverged", "value": 9})
    assert parse_probe_line("ordinary stdout") is None
    assert parse_probe_line('AUTORESEARCH_PROBE:{"value": 1.0}') is None


def test_contract_parses_bounds_and_caps_to_three_signals():
    contract = MonitorContract.parse({
        "stall_seconds": 120,
        "monitors": [
            {"name": "loss", "healthy": {"max": 10, "trend": "decreasing"}, "why": "divergence"},
            {"name": "grad", "healthy": {"min": 0}, "why": "negative norm"},
            {"name": "acc", "healthy": {"min": 0, "max": 1}, "why": "range"},
            {"name": "extra", "why": "must be ignored"},
        ],
    })
    assert [signal.name for signal in contract.monitors] == ["loss", "grad", "acc"]
    assert contract.monitors[0].maximum == 10
    assert contract.monitors[0].trend == "decreasing"
    assert contract.stall_seconds == 120
    assert any("only the first 3" in warning for warning in contract.warnings)


def test_predicate_compiler_allows_math_but_rejects_code_execution():
    allowed = compile_predicate("value > 5 and abs(prev - value) < 2")
    assert allowed is not None
    assert allowed.evaluate(6.0, 5.0, 1) is True
    for unsafe in (
        "__import__('os').system('echo pwn')",
        "open('/etc/passwd').read()",
        "[x for x in range(3)]",
        "value ** 2 > 4",
    ):
        assert compile_predicate(unsafe) is None


def test_severity_ladder_nan_bound_trend_and_guard_events():
    engine = StreamingPerception({
        "monitors": [
            {"name": "loss", "healthy": {"max": 100, "trend": "decreasing"}, "why": "divergence"},
            {"name": "score", "healthy": {"min": 0}, "why": "range"},
        ]
    }, suspect_after=2, fatal_after=4)

    assert engine.observe({"name": "loss", "value": 10.0}).severity == "info"
    assert engine.observe({"name": "loss", "value": float("nan")}).severity == "fatal"
    assert engine.observe({"name": "loss", "value": 101.0}).severity == "fatal"
    assert engine.observe({"name": "score", "value": -1.0}).severity == "fatal"

    # A trend violation is suspect first, then fatal after persistence.
    engine = StreamingPerception(
        {"monitors": [{"name": "loss", "healthy": {"trend": "decreasing"}, "why": "divergence"}]},
        suspect_after=2, fatal_after=4,
    )
    assert engine.observe({"name": "loss", "value": 1.0}).severity == "info"
    assert engine.observe({"name": "loss", "value": 1.2}).severity == "info"
    # One ordinary uptick is not a fatal trend; suspicion needs persistence.
    assert engine.observe({"name": "loss", "value": 1.1}).severity == "info"
    assert engine.observe({"name": "loss", "value": 1.4}).severity == "info"
    assert engine.observe({"name": "loss", "value": 1.6}).severity == "suspect"
    assert engine.observe({"name": "loss", "value": 1.8}).severity == "suspect"
    verdict = engine.observe({"name": "loss", "value": 2.0})
    assert verdict.severity == "fatal"
    assert engine.first_fatal is not None

    guard = engine.on_event({"event": "diverged", "value": 9})
    assert guard.severity == "fatal"
    warning = engine.on_event({"event": "noise", "severity": "warning"})
    assert warning.severity == "suspect"


def test_monitor_report_is_replayable_evidence():
    engine = StreamingPerception({"monitors": [{"name": "loss", "healthy": {"max": 5}, "why": "bound"}]})
    engine.observe({"name": "loss", "value": 1.0, "step": 0})
    engine.observe({"name": "loss", "value": 9.0, "step": 1})
    report = build_monitor_report(
        engine, exit_code=-15, elapsed_seconds=0.2,
        abort_reason="loss=9 > max 5", abort_category="PERCEPTION_ABORT",
    )
    assert report["kind"] == "execution_monitor"
    assert report["aborted"] is True
    assert report["abort_category"] == "PERCEPTION_ABORT"
    assert report["fatal_verdicts"] == 1
    assert report["first_fatal"]["signal"] == "loss"
    assert report["verdicts"][-1]["severity"] == "fatal"
    assert json.loads(json.dumps(report))["exit_code"] == -15


def test_unobserved_and_malformed_probes_are_reported():
    engine = StreamingPerception({"monitors": [
        {"name": "loss", "healthy": {"max": 5}, "why": "bound"},
        {"name": "grad", "why": "unused"},
    ]})
    engine.observe_line("AUTORESEARCH_PROBE:{not json}")
    engine.observe({"name": "loss", "value": 1.0})
    summary = engine.summary()
    assert summary["unobserved_monitors"] == ["grad"]
    assert summary["malformed_probe_lines"] == 1


def test_invalid_predicate_is_marked_not_compiled():
    engine = StreamingPerception({"monitors": [
        {"name": "loss", "abort_if": "__import__('os').system('echo x')", "why": "bad"},
    ]})
    assert engine.summary()["predicate_status"]["loss"] == "not_compiled"

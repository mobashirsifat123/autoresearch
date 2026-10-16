"""CLI-level functional tests for the five-skill distribution.

Every command of every skill is exercised offline through Typer's CliRunner:
help surfaces, usage validation, clean JSON error paths (no tracebacks), and
the honest not-found / credential-refusal behavior students will hit first.

No network, no paid API: provider-backed commands are expected to fail with
their documented machine-readable error, never a crash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoresearch.cli.app import app

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _payload(result):
    return json.loads(result.output)


ALL_COMMANDS = [
    ("skill",),
    ("skill", "list"),
    ("skill", "install"),
    ("skill", "update"),
    ("skill", "uninstall"),
    ("skill", "doctor"),
    ("skill", "review"),
    ("skill", "novelty"),
    ("skill", "workflow"),
    ("skill", "manuscript"),
    ("skill", "experiment",),
    ("skill", "experiment", "plan"),
    ("skill", "experiment", "validate"),
    ("skill", "experiment", "implement"),
    ("skill", "experiment", "approve"),
    ("skill", "experiment", "execute"),
    ("skill", "experiment", "inspect"),
    ("skill", "experiment", "report"),
    ("skill", "experiment", "cancel"),
]


@pytest.mark.parametrize("command", ALL_COMMANDS, ids=[" ".join(c) for c in ALL_COMMANDS])
def test_help_works_for_every_command(command):
    result = _invoke(*command, "--help")
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


def test_root_help_lists_the_five_skills():
    result = _invoke("--help")
    assert result.exit_code == 0
    for name in ("review", "novelty", "experiment", "manuscript", "workflow"):
        assert name in result.output


def test_skill_list_reports_exactly_five_skills():
    result = _invoke("skill", "list")
    assert result.exit_code == 0
    payload = _payload(result)
    names = {skill["name"] for skill in payload["skills"]}
    assert names == {"review", "novelty", "experiment", "manuscript", "workflow"}


def test_unknown_experiment_subcommand_is_clean_usage_error():
    result = _invoke("skill", "experiment", "does-not-exist")
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_missing_required_option_is_usage_error_not_crash():
    # manuscript requires --project
    result = _invoke("skill", "manuscript")
    assert result.exit_code == 2
    assert "Traceback" not in result.output


@pytest.mark.parametrize("args", [
    ("skill", "experiment", "plan", "--candidate-id", "missing"),
    ("skill", "experiment", "report", "--candidate-id", "missing"),
    ("skill", "novelty", "--project", "missing"),
    ("skill", "manuscript", "--project", "missing"),
])
def test_unknown_ids_produce_clean_json_errors(tmp_path, monkeypatch, args):
    """Student-facing data errors must be machine-readable JSON with the
    documented exit codes — never a Python traceback."""
    monkeypatch.setenv("AUTORESEARCH_DATA_ROOT", str(tmp_path / "data"))
    result = _invoke(*args)
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    try:
        payload = _payload(result)
        assert payload.get("ok") is False
        assert payload.get("error", {}).get("code")
    except json.JSONDecodeError:
        pytest.fail(f"non-JSON error output:\n{result.output[:400]}")


def test_workflow_requires_seed_or_project(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORESEARCH_DATA_ROOT", str(tmp_path / "data"))
    result = _invoke("skill", "workflow", "--until", "review")
    assert result.exit_code == 2  # usage: one of --seed-paper/--from-catalog/--project required
    assert "Traceback" not in result.output


def test_workflow_rejects_unknown_stop_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORESEARCH_DATA_ROOT", str(tmp_path / "data"))
    result = _invoke("skill", "workflow", "--seed-paper", "x.pdf", "--until", "pubish")
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_skill_doctor_reports_without_config(tmp_path, monkeypatch):
    """A fresh machine with no provider config gets a clean diagnosis:
    machine-readable JSON naming exactly what is missing, degraded exit 1,
    never a traceback."""
    monkeypatch.setenv("AUTORESEARCH_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AUTORESEARCH_PROVIDER_CONFIG", str(tmp_path / "none.toml"))
    result = _invoke("skill", "doctor")
    assert result.exit_code == 1  # degraded state is a signal, not a crash
    assert "Traceback" not in result.output
    payload = _payload(result)
    assert payload["overall"] == "degraded"
    checks = {check["check"]: check for check in payload["checks"]}
    assert "providers" in checks
    assert checks["providers"]["ok"] is False
    assert checks["providers"]["detail"]


def test_install_targets_are_validated():
    result = _invoke("skill", "install", "--target", "not-a-target")
    assert result.exit_code != 0
    assert "Traceback" not in result.output

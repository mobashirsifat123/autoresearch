from typer.testing import CliRunner

from autoresearch.cli.app import app


def test_cli_exposes_required_commands():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "discover" in result.stdout
    assert "review" in result.stdout
    assert "resume" in result.stdout
    discover_help = runner.invoke(app, ["discover", "--help"]).stdout
    review_help = runner.invoke(app, ["review", "--help"]).stdout
    assert "--seed-paper" in discover_help and "--max-runtime-seconds" in discover_help
    assert "--top-k" in review_help and "--hierarchical" in review_help and "--max-cost-usd" in review_help
    assert "--run" in runner.invoke(app, ["resume", "--help"]).stdout


def test_product_commands_are_exposed_without_raw_secret_input():
    runner = CliRunner()
    help_text = runner.invoke(app, ["--help"]).stdout
    for command in ("version", "setup", "login", "doctor", "serve", "status", "diagnostics", "update", "rollback", "uninstall", "run"):
        assert command in help_text
    assert "--auth-token-env" in runner.invoke(app, ["login", "--help"]).stdout
    assert "api-key" not in runner.invoke(app, ["setup", "--help"]).stdout.lower()

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

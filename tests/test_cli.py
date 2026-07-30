from typer.testing import CliRunner

from autoresearch.cli.app import app


def test_cli_exposes_required_commands():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "discover" in result.stdout
    assert "review" in result.stdout
    assert "--seed-paper" in runner.invoke(app, ["discover", "--help"]).stdout
    assert "--top-k" in runner.invoke(app, ["review", "--help"]).stdout

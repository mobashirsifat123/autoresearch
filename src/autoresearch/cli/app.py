"""AutoResearch milestone CLI."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from autoresearch.application.config import load_settings
from autoresearch.application.factory import ApplicationContainer
from autoresearch.providers.redaction import redact_text

app = typer.Typer(name="autoresearch", help="Evidence-first research discovery and review.", no_args_is_help=True)


@app.command()
def discover(
    seed_paper: str = typer.Option(..., "--seed-paper", help="Seed PDF path, DOI, or HTTPS URL."),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    since_year: int = typer.Option(datetime.now(timezone.utc).year - 3, "--since-year", min=1900),
) -> None:
    """Retrieve recent related papers and legally accessible PDFs."""
    _run(lambda container: container.discovery.discover(seed_paper, limit=limit, since_year=since_year))


@app.command()
def review(
    project: str = typer.Option(..., "--project", help="Project ID returned by discover."),
    top_k: int = typer.Option(5, "--top-k", min=1, max=15),
) -> None:
    """Review the corpus, rank top papers, and write one cited gap report."""
    _run(lambda container: container.review.review(project, top_k=top_k))


def _run(operation: Any) -> None:
    async def execute() -> Any:
        container = ApplicationContainer(load_settings())
        try:
            return await operation(container)
        finally:
            await container.close()
    try:
        result = asyncio.run(execute())
    except Exception as exc:
        typer.echo(json.dumps({"error": redact_text(str(exc)), "type": type(exc).__name__}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(asdict(result), indent=2, default=str))


def main() -> None:
    app()


if __name__ == "__main__":
    main()

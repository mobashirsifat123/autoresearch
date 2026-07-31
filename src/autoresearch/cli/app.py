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
from autoresearch.application.audit import write_audit
from autoresearch.domain.models import PipelineKind
from autoresearch.providers.redaction import redact_text

app = typer.Typer(name="autoresearch", help="Evidence-first research discovery and review.", no_args_is_help=True)


@app.command()
def discover(
    seed_paper: str = typer.Option(..., "--seed-paper", help="Seed PDF path, DOI, or HTTPS URL."),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    since_year: int = typer.Option(datetime.now(timezone.utc).year - 3, "--since-year", min=1900),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd", min=0.0, help="Cumulative project provider-cost hard stop."),
    max_runtime_seconds: float | None = typer.Option(None, "--max-runtime-seconds", min=1.0, help="Runtime limit for this invocation."),
) -> None:
    """Retrieve recent related papers and legally accessible PDFs."""
    _run(lambda container: container.discovery.discover(seed_paper, limit=limit, since_year=since_year, max_cost_usd=max_cost_usd, max_runtime_seconds=max_runtime_seconds))


@app.command()
def review(
    project: str = typer.Option(..., "--project", help="Project ID returned by discover."),
    top_k: int = typer.Option(5, "--top-k", min=1, max=15),
    hierarchical: bool = typer.Option(False, "--hierarchical/--flat", help="Use chunk-level hierarchical review for long legal PDFs."),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd", min=0.0, help="Cumulative project provider-cost hard stop."),
    max_runtime_seconds: float | None = typer.Option(None, "--max-runtime-seconds", min=1.0, help="Runtime limit for this invocation."),
) -> None:
    """Review the corpus, rank top papers, and write one cited gap report."""
    _run(lambda container: container.review.review(project, top_k=top_k, hierarchical=hierarchical, max_cost_usd=max_cost_usd, max_runtime_seconds=max_runtime_seconds))


@app.command()
def resume(
    run: str = typer.Option(..., "--run", help="Interrupted or partially failed run ID."),
) -> None:
    """Resume a discovery or review run from durable checkpoints."""
    async def operation(container: ApplicationContainer):
        stored = container.repository.get_run(run)
        if not stored:
            raise ValueError(f"Pipeline run '{run}' was not found.")
        if stored.kind is PipelineKind.DISCOVERY:
            return await container.discovery.resume(run)
        if stored.kind is PipelineKind.REVIEW:
            return await container.review.resume(run)
        raise ValueError(f"Pipeline kind '{stored.kind.value}' is not resumable in this milestone.")
    _run(operation)


@app.command()
def audit(
    project: str = typer.Option(..., "--project", help="Project ID to validate."),
    run: str | None = typer.Option(None, "--run", help="Optional run ID to associate with the validation artifact."),
) -> None:
    """Validate provenance, evidence, usage, recovery, and secret redaction."""
    _run(lambda container: {"project": project, "validation_path": str(write_audit(container.repository, project, run_id=run))})


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
    typer.echo(json.dumps(asdict(result) if hasattr(result, "__dataclass_fields__") else result, indent=2, default=str))


def main() -> None:
    app()


if __name__ == "__main__":
    main()

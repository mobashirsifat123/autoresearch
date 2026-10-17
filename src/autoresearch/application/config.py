"""Filesystem and provider configuration derived from environment variables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_root: Path
    database_path: Path
    artifact_root: Path
    download_root: Path
    provider_config: Path
    search_sources: tuple[str, ...]
    agent_roots: tuple[Path, ...] = ()
    # Project-budget defaults applied by the guard when a project has no
    # explicit --max-tokens / --max-cost-usd. None leaves that dimension uncapped.
    default_max_tokens: int | None = None
    default_max_cost_usd: float | None = None
    # "deterministic" (default): manuscripts are field-assembled, zero model
    # calls. "model": model-assisted scientific argument gated by the
    # deterministic numeric/citation/overclaim audit (falls back on any
    # surviving violation).
    manuscript_writer: str = "deterministic"
    # Quality-width knobs: parallel expansion adds tokens (width), not
    # wall-clock (depth). All defaults are ON; every one degrades gracefully.
    discovery_query_perspectives: int = 3
    discovery_citation_expansion: bool = True
    discovery_citation_anchors: int = 5
    discovery_citation_neighbors: int = 10
    review_ensemble: bool = True


def _positive_int(value: str | int | None) -> int | None:
    try:
        parsed = int(str(value or "").strip())
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _positive_float(value: str | None) -> float | None:
    try:
        parsed = float(str(value or "").strip())
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _boolean(value: object) -> bool | None:
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def load_settings() -> Settings:
    data_root = Path(os.getenv("AUTORESEARCH_DATA_ROOT", ".autoresearch/data")).expanduser().resolve()
    config = Path(os.getenv("AUTORESEARCH_PROVIDER_CONFIG", ".autoresearch/config.toml")).expanduser().resolve()
    research = {}
    if config.is_file():
        with config.open("rb") as handle:
            research = tomllib.load(handle).get("research", {})
    sources = tuple(item.strip().lower() for item in os.getenv("AUTORESEARCH_SEARCH_SOURCES", "semantic_scholar,openalex,crossref,arxiv,openreview").split(",") if item.strip())
    citation_expansion = _boolean(research.get("discovery_citation_expansion", os.getenv("AUTORESEARCH_DISCOVERY_CITATION_EXPANSION")))
    review_ensemble = _boolean(research.get("review_ensemble", os.getenv("AUTORESEARCH_REVIEW_ENSEMBLE")))
    return Settings(
        data_root=data_root,
        database_path=Path(os.getenv("AUTORESEARCH_DATABASE_PATH", str(data_root / "autoresearch.sqlite3"))).expanduser().resolve(),
        artifact_root=Path(os.getenv("AUTORESEARCH_ARTIFACT_ROOT", str(data_root / "artifacts"))).expanduser().resolve(),
        download_root=Path(os.getenv("AUTORESEARCH_DOWNLOAD_ROOT", str(data_root / "downloads"))).expanduser().resolve(),
        provider_config=config,
        search_sources=sources,
        agent_roots=tuple(Path(item).expanduser().resolve() for item in os.getenv("AUTORESEARCH_AGENT_ROOTS", str(Path.cwd())).split(os.pathsep) if item.strip()),
        # A project config can explicitly request no token ceiling (0), even
        # when the launching shell inherited a finite global default.
        default_max_tokens=_positive_int(research.get("max_tokens", os.getenv("AUTORESEARCH_DEFAULT_MAX_TOKENS"))),
        default_max_cost_usd=_positive_float(os.getenv("AUTORESEARCH_DEFAULT_MAX_COST_USD")),
        manuscript_writer=str(research.get("manuscript_writer", os.getenv("AUTORESEARCH_MANUSCRIPT_WRITER", "deterministic"))).strip().lower() or "deterministic",
        discovery_query_perspectives=_positive_int(research.get("discovery_query_perspectives", os.getenv("AUTORESEARCH_DISCOVERY_QUERY_PERSPECTIVES"))) or 3,
        discovery_citation_expansion=True if citation_expansion is None else citation_expansion,
        discovery_citation_anchors=_positive_int(research.get("discovery_citation_anchors")) or 5,
        discovery_citation_neighbors=_positive_int(research.get("discovery_citation_neighbors")) or 10,
        review_ensemble=True if review_ensemble is None else review_ensemble,
    )

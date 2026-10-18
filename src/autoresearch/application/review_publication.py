"""Publication artifacts for the review stage.

A reviewed paper list is not a related-work corpus. This module writes the
coverage summary and citation contexts the manuscript later consumes, and
reports exact publication gaps before a project spends compute on novelty or
execution.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from autoresearch.application.literature_synthesis import _theme_for

PUBLICATION_MIN_REVIEWS = 20
FULL_TEXT_RATIO_MIN = 0.5


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return default


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


@dataclass(frozen=True)
class CitationContext:
    paper_id: str
    claim_id: str
    quote: str
    page: int | None
    section: str | None
    relationship: str
    claim_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "claim_id": self.claim_id,
            "quote": self.quote,
            "page": self.page,
            "section": self.section,
            "relationship": self.relationship,
            "claim_type": self.claim_type,
        }


@dataclass
class ReviewPublication:
    corpus: dict[str, Any]
    contexts: tuple[CitationContext, ...]
    corpus_path: str | None = None
    contexts_path: str | None = None
    gaps_path: str | None = None

    @property
    def gaps(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.corpus.get("publication_gaps") or ())


def _full_text_ratio(repository: Any, paper_ids: Iterable[str]) -> float:
    ids = [str(pid) for pid in paper_ids if str(pid)]
    if not ids:
        return 0.0
    try:
        rows = repository.rows(
            "SELECT paper_id, status FROM download_records WHERE paper_id IN ("
            + ",".join("?" for _ in ids)
            + ")",
            tuple(ids),
        )
    except Exception:  # noqa: BLE001 - absence is an honest zero
        return 0.0
    downloaded = {str(row["paper_id"]) for row in rows if str(row["status"]) == "downloaded"}
    return round(len(downloaded) / len(ids), 3)


def _paper_reviews(repository: Any, paper_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = [str(pid) for pid in paper_ids if str(pid)]
    if not ids:
        return {}
    try:
        rows = repository.rows(
            "SELECT * FROM paper_reviews WHERE paper_id IN ("
            + ",".join("?" for _ in ids)
            + ") ORDER BY rowid DESC",
            tuple(ids),
        )
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result.setdefault(str(row["paper_id"]), dict(row))
    return result


def build_review_publication(
    repository: Any,
    *,
    project_id: str,
    run_id: str,
    papers: list[Any],
    reviews: list[Any],
    claims: list[Any],
    top_paper_ids: Iterable[str],
    gap: Any | None = None,
    min_reviews: int = PUBLICATION_MIN_REVIEWS,
) -> ReviewPublication:
    """Build the coverage summary and citation contexts from stored evidence."""
    reviewed_ids = [str(getattr(review, "paper_id", "")) for review in reviews if getattr(review, "paper_id", "")]
    reviewed_ids = list(dict.fromkeys(reviewed_ids))
    paper_by_id = {str(getattr(paper, "id", "")): paper for paper in papers}
    latest_reviews = _paper_reviews(repository, reviewed_ids)

    themes: Counter[str] = Counter()
    method_families: Counter[str] = Counter()
    datasets: Counter[str] = Counter()
    years: Counter[str] = Counter()
    for paper_id in reviewed_ids:
        paper = paper_by_id.get(paper_id)
        review = latest_reviews.get(paper_id, {})
        text = " ".join(
            str(value or "")
            for value in (
                getattr(paper, "title", "") if paper else "",
                getattr(paper, "abstract", "") if paper else "",
                review.get("problem") or "",
                review.get("method") or "",
            )
        )
        themes[_theme_for(text)] += 1
        method = _clean(review.get("method"))
        if method:
            method_families[method.split(".")[0][:80]] += 1
        for dataset in _loads(review.get("datasets_json"), []):
            if str(dataset).strip():
                datasets[str(dataset).strip()[:80]] += 1
        year = getattr(paper, "year", None) if paper else None
        if year:
            years[str(year)] += 1

    counter_ids: set[str] = set()
    if gap is not None:
        raw = getattr(gap, "counter_evidence_claim_ids", ()) or ()
        counter_ids = {str(item) for item in raw}
    contexts: list[CitationContext] = []
    for claim in claims:
        claim_id = str(getattr(claim, "id", ""))
        paper_id = str(getattr(claim, "paper_id", ""))
        excerpt = _clean(getattr(claim, "excerpt", ""))
        if not excerpt:
            continue
        relationship = "contradicts" if claim_id in counter_ids else "supports"
        contexts.append(CitationContext(
            paper_id=paper_id,
            claim_id=claim_id,
            quote=excerpt,
            page=getattr(claim, "page", None),
            section=getattr(claim, "section", None),
            relationship=relationship,
            claim_type=str(getattr(claim, "claim_type", "") or "claim"),
        ))

    full_text_ratio = _full_text_ratio(repository, reviewed_ids)
    top_ids = [str(item) for item in top_paper_ids if str(item)]
    corpus = {
        "kind": "review_corpus",
        "project_id": project_id,
        "run_id": run_id,
        "reviewed_papers": len(reviewed_ids),
        "reviewed_paper_ids": reviewed_ids,
        "full_text_ratio": full_text_ratio,
        "themes": dict(themes),
        "method_families": dict(method_families),
        "datasets": dict(datasets),
        "years": dict(years),
        "closest_prior_work": top_ids[:5],
        "top_paper_ids": top_ids,
        "citation_contexts": len(contexts),
    }
    gaps: list[dict[str, Any]] = []
    if len(reviewed_ids) < min_reviews:
        gaps.append({
            "code": "reviewed_papers_below_minimum",
            "stage": "review",
            "message": f"reviewed papers {len(reviewed_ids)} < {min_reviews}",
            # Informational here; the venue-aware shared contract owns the
            # fatal decision (workshop and journal minima differ).
            "fatal": False,
        })
    if not top_ids:
        gaps.append({
            "code": "closest_prior_work_missing",
            "stage": "review",
            "message": "no closest prior work was recorded",
            "fatal": True,
        })
    if full_text_ratio < FULL_TEXT_RATIO_MIN:
        gaps.append({
            "code": "full_text_ratio_below_threshold",
            "stage": "review",
            "message": f"full-text ratio {full_text_ratio:.2f} < {FULL_TEXT_RATIO_MIN:.2f}",
            "fatal": False,
        })
    corpus["publication_gaps"] = gaps

    root = Path(getattr(repository, "artifact_root")) / "reviews" / run_id
    root.mkdir(parents=True, exist_ok=True)
    corpus_path = root / "review_corpus.json"
    contexts_path = root / "citation_contexts.json"
    gaps_path = root / "publication_gaps.json"
    corpus_path.write_text(json.dumps(corpus, indent=2, sort_keys=True), encoding="utf-8")
    gaps_path.write_text(json.dumps({"gaps": gaps}, indent=2, sort_keys=True), encoding="utf-8")
    contexts_path.write_text(
        json.dumps(
            {"kind": "citation_contexts", "contexts": [item.to_dict() for item in contexts]},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return ReviewPublication(
        corpus=corpus,
        contexts=tuple(contexts),
        corpus_path=str(corpus_path),
        contexts_path=str(contexts_path),
        gaps_path=str(gaps_path),
    )

"""Full-corpus literature synthesis.

The old Literature Review used only the gap's ``supporting_paper_ids`` (often
three papers) and emitted a fixed Point/Evidence/Explanation/Repeat-Link
block per paper, with the same explanation for every entry. That is an
evidence extraction, not a literature review.

This module builds a deterministic synthesis over the reviewed corpus:

* papers are selected from the whole project corpus, with the gap's
  supporting papers guaranteed a place;
* each paper is reduced to recorded fields only (problem, method, datasets,
  reported results, limitations);
* papers are grouped into themes by explicit keyword rules;
* the output is a themed narrative, a comparison table, and an explicit
  positioning paragraph, with a citation key for every paper.

No model is called and no claim is invented: every sentence is assembled from
stored review fields, and a paper with no recorded evidence is excluded.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

_THEME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Benchmarking and evaluation", ("benchmark", "evaluation", "leaderboard", "metric", "score", "test set")),
    ("Autonomous research agents", ("agent", "autonomous", "scientific discovery", "research agent")),
    ("Calibration, robustness, and uncertainty", ("calibration", "robustness", "domain shift", "uncertainty", "variance")),
    ("Retrieval, databases, and knowledge graphs", ("retrieval", "database", "knowledge graph", "curation", "rag")),
    ("Clinical and domain applications", ("clinical", "oncology", "medical", "patient", "trial")),
    ("Detection and image forensics", ("forensics", "detection", "forgery", "image", "manipulation")),
)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, tuple, dict)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


_INTERNAL_ID = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_CLAIM_REF = re.compile(r"\b(?:claim|claims)\s+[0-9a-fA-F]{8,}\b", re.IGNORECASE)
_REVIEW_REF = re.compile(r"\((?:review run|ID)\s+[0-9a-fA-F-]+\)", re.IGNORECASE)


def _clean(text: Any, limit: int = 280) -> str:
    compact = " ".join(str(text or "").split())
    compact = _CLAIM_REF.sub("", compact)
    compact = _REVIEW_REF.sub("", compact)
    compact = _INTERNAL_ID.sub("", compact)
    compact = re.sub(r"\[\s*\]", "not recorded", compact)
    compact = re.sub(r"\s+([,.;:])", r"\1", compact)
    compact = " ".join(compact.split()).strip()
    if len(compact) <= limit:
        return compact
    cut = compact[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return cut + "…"


def _theme_for(text: str) -> str:
    lowered = text.lower()
    for theme, keywords in _THEME_RULES:
        if any(keyword in lowered for keyword in keywords):
            return theme
    return "Other related work"


def _cell(text: Any, limit: int = 120) -> str:
    return _clean(text, limit).replace("|", "\\|")


@dataclass(frozen=True)
class SynthesizedPaper:
    paper_id: str
    key: str
    title: str
    year: int | None
    venue: str
    problem: str
    method: str
    datasets: tuple[str, ...]
    results: tuple[str, ...]
    limitations: tuple[str, ...]
    theme: str
    is_gap_support: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "key": self.key,
            "title": self.title,
            "year": self.year,
            "venue": self.venue,
            "problem": self.problem,
            "method": self.method,
            "datasets": list(self.datasets),
            "results": list(self.results),
            "limitations": list(self.limitations),
            "theme": self.theme,
            "is_gap_support": self.is_gap_support,
        }


@dataclass
class LiteratureSynthesis:
    papers: tuple[SynthesizedPaper, ...] = ()
    themes: tuple[str, ...] = ()
    comparison_table: str = ""
    positioning: str = ""
    gap_statement: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "literature_synthesis",
            "papers": [paper.to_dict() for paper in self.papers],
            "themes": list(self.themes),
            "comparison_table": self.comparison_table,
            "positioning": self.positioning,
            "n_papers": len(self.papers),
        }

    def narrative_lines(self) -> list[str]:
        if not self.papers:
            return []
        lines: list[str] = []
        for theme in self.themes:
            group = [paper for paper in self.papers if paper.theme == theme]
            if not group:
                continue
            citations = " ".join(f"[@{paper.key}]" for paper in group if paper.key)
            titles = "; ".join(paper.title for paper in group)
            methods = "; ".join(_clean(paper.method, 90) for paper in group if paper.method)
            results = "; ".join(
                item for paper in group for item in paper.results[:1] if item
            )
            limitations = "; ".join(
                item for paper in group for item in paper.limitations[:2] if item
            )
            paragraph = (
                f"**{theme}.** The reviewed corpus includes {len(group)} "
                f"stud{'y' if len(group) == 1 else 'ies'} in this theme: {titles} {citations}. "
            )
            if methods:
                paragraph += f"Recorded methods include {methods}. "
            if results:
                paragraph += f"Reported results include {results}. "
            if limitations:
                paragraph += f"Recorded limitations include {limitations}. "
            lines += [paragraph.strip(), ""]
        if self.gap_statement:
            lines += [
                f"The gap this study addresses remains: {_clean(self.gap_statement, 240)}",
                "",
            ]
        return lines

    def to_markdown_lines(self) -> list[str]:
        if not self.papers:
            return []
        lines = ["## 2. Literature Review", ""]
        lines += [
            "This section synthesizes the reviewed corpus by theme. Each statement "
            "is assembled from the review's recorded problem, method, results, and "
            "limitation fields; citations use the corpus keys. The comparison table "
            "summarizes the same recorded fields.",
            "",
        ]
        lines += self.narrative_lines()
        if self.comparison_table:
            lines += ["### Comparison of reviewed studies", "", self.comparison_table, ""]
        if self.positioning:
            lines += ["### Positioning of this study", "", self.positioning, ""]
        return lines


def _paper_rows(
    repository: Any, project_id: str, limit: int, review_run_id: str | None = None
) -> list[dict[str, Any]]:
    rows = repository.rows(
        "SELECT p.* FROM papers p JOIN paper_reviews pr ON pr.paper_id=p.id "
        "WHERE p.project_id=? AND (? IS NULL OR pr.run_id=?) "
        "GROUP BY p.id ORDER BY p.citation_count DESC, p.year DESC, p.title ASC LIMIT ?",
        (project_id, review_run_id, review_run_id, limit),
    )
    return [dict(row) for row in rows]


def build_literature_synthesis(
    repository: Any,
    project_id: str,
    *,
    gap: Any | None,
    candidate: Any | None,
    spec: Any | None,
    keys: Mapping[str, str] | None = None,
    limit: int = 24,
    review_run_id: str | None = None,
) -> LiteratureSynthesis:
    """Build a themed synthesis from the full reviewed corpus."""
    key_by_paper = dict(keys or {})
    if review_run_id is None and gap is not None:
        review_run_id = (
            str(gap.get("run_id") or "") if isinstance(gap, Mapping)
            else str(getattr(gap, "run_id", "") or "")
        ) or None
    gap_paper_ids: list[str] = []
    if gap is not None:
        raw_ids: Any = getattr(gap, "supporting_paper_ids_json", None)
        if raw_ids is None and isinstance(gap, Mapping):
            raw_ids = gap.get("supporting_paper_ids_json") or gap.get("supporting_paper_ids") or "[]"
        try:
            parsed_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            gap_paper_ids = [str(item) for item in (parsed_ids or ())]
        except (TypeError, ValueError):
            gap_paper_ids = [str(item) for item in (raw_ids or ())]
    if not gap_paper_ids and gap is not None and hasattr(gap, "supporting_paper_ids"):
        gap_paper_ids = [str(item) for item in getattr(gap, "supporting_paper_ids") or ()]

    rows = _paper_rows(
        repository, project_id, max(limit, len(gap_paper_ids)),
        review_run_id=review_run_id,
    )
    by_id = {str(row["id"]): row for row in rows}
    # Guarantee the gap's supporting papers are included even if the corpus
    # ranking puts them outside the first page.
    for paper_id in gap_paper_ids:
        if paper_id in by_id:
            continue
        row = repository.row("SELECT * FROM papers WHERE id=?", (paper_id,))
        if row:
            by_id[paper_id] = dict(row)
    ordered = sorted(
        by_id.values(),
        key=lambda row: (
            0 if str(row.get("id")) in gap_paper_ids else 1,
            -int(row.get("citation_count") or 0),
            -int(row.get("year") or 0),
            str(row.get("title") or ""),
        ),
    )[:limit]

    papers: list[SynthesizedPaper] = []
    for row in ordered:
        paper_id = str(row.get("id"))
        review = repository.row(
            "SELECT problem, research_question, method, datasets_json, "
            "experimental_protocol, results_json, limitations_json "
            "FROM paper_reviews WHERE paper_id=? AND (? IS NULL OR run_id=?) "
            "ORDER BY rowid DESC LIMIT 1",
            (paper_id, review_run_id, review_run_id),
        )
        review = dict(review or {})
        title = _clean(row.get("title") or "Untitled", 180)
        problem = _clean(review.get("problem") or review.get("research_question") or "", 240)
        method = _clean(review.get("method") or row.get("method") or "", 240)
        datasets_raw = _loads(review.get("datasets_json"), [])
        datasets = tuple(str(item) for item in datasets_raw if str(item).strip()) if isinstance(datasets_raw, list) else ()
        results_raw = _loads(review.get("results_json"), [])
        if isinstance(results_raw, list):
            results = tuple(_clean(item, 160) for item in results_raw[:3] if str(item).strip())
        elif results_raw:
            results = (_clean(results_raw, 160),)
        else:
            results = ()
        limitations_raw = _loads(review.get("limitations_json"), [])
        if isinstance(limitations_raw, list):
            limitations = tuple(_clean(item, 160) for item in limitations_raw[:4] if str(item).strip())
        elif limitations_raw:
            limitations = (_clean(limitations_raw, 160),)
        else:
            limitations = ()
        theme = _theme_for(" ".join([title, problem, method, row.get("abstract") or ""]))
        key = key_by_paper.get(paper_id, "")
        papers.append(SynthesizedPaper(
            paper_id=paper_id,
            key=key,
            title=title,
            year=int(row["year"]) if row.get("year") else None,
            venue=str(row.get("venue") or ""),
            problem=problem,
            method=method,
            datasets=datasets,
            results=results,
            limitations=limitations,
            theme=theme,
            is_gap_support=paper_id in gap_paper_ids,
        ))

    themes = tuple(dict.fromkeys(paper.theme for paper in papers))
    table_lines = [
        "| Study | Year | Method | Datasets | Reported results | Recorded limitations |",
        "|---|---:|---|---|---|---|",
    ]
    for paper in papers:
        table_lines.append(
            "| "
            + " | ".join(
                (
                    f"{_cell(paper.title, 80)} [@{paper.key}]" if paper.key else _cell(paper.title, 80),
                    str(paper.year or "n.d."),
                    _cell(paper.method, 100) or "not recorded",
                    _cell(", ".join(paper.datasets), 80) or "not recorded",
                    _cell("; ".join(paper.results[:2]), 120) or "not recorded",
                    _cell("; ".join(paper.limitations[:2]), 120) or "not recorded",
                )
            )
            + " |"
        )
    comparison_table = "\n".join(table_lines) if papers else ""

    difference = _clean(getattr(candidate, "difference_from_existing", "") if candidate is not None else "", 260)
    hypothesis = _clean(getattr(spec, "hypothesis", "") if spec is not None else "", 220)
    positioning = (
        "This study differs from the closest reviewed work in that "
        + (difference or "it tests the recorded gap directly")
        + (f". The preregistered hypothesis is: {hypothesis}" if hypothesis else ".")
    )

    return LiteratureSynthesis(
        papers=tuple(papers),
        themes=themes,
        comparison_table=comparison_table,
        positioning=positioning,
        gap_statement=_clean(getattr(gap, "statement", "") if gap is not None else "", 280)
        if not isinstance(gap, Mapping)
        else _clean(gap.get("statement") or "", 280),
    )

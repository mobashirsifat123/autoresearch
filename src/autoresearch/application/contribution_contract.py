"""Contribution contract: the paper's argument before its prose.

A template says which sections exist. A contract says what the paper claims,
why it is new, which evidence supports each claim, and which artifact proves
it. The contract is deterministic (assembled from the frozen lineage) and is
used by the title/abstract builders and the model writer. A human PI can edit
the JSON before drafting; the manuscript stage never certifies novelty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from autoresearch.application.evidence_access import field as _field

_VAGUE_TERMS = ("an analysis", "a study of", "an investigation", "new insights")
_TRAILING_STOPWORDS = {
    "of", "for", "with", "and", "or", "the", "a", "an", "to", "in", "on",
    "by", "that", "which", "from", "as", "at", "into", "over", "under",
}


def _clean(text: Any, limit: int | None = None) -> str:
    compact = " ".join(str(text or "").split())
    compact = compact.replace("…", "").replace("...", "").strip(" .,:;-")
    compact = re.sub(r"\[\s*\]", "not recorded", compact)
    if limit is not None and len(compact) > limit:
        compact = compact[:limit].rsplit(" ", 1)[0].rstrip(" .,:;-")
    return compact


def _bounded_words(text: Any, max_words: int) -> str:
    words = _clean(text).split()[:max_words]
    while words and words[-1].lower().strip(".,:;-") in _TRAILING_STOPWORDS:
        words.pop()
    return " ".join(words).rstrip(" .,:;-")


def _head_phrase(text: Any, max_words: int = 8) -> str:
    """The noun phrase before an explanatory clause.

    'A cross-benchmark calibration protocol that replaces X' -> 'A
    cross-benchmark calibration protocol'. This keeps titles specific instead
    of truncating a sentence mid-clause.
    """
    cleaned = _clean(text)
    for separator in (" that ", " which ", " where ", ":", " — ", " - "):
        if separator in cleaned:
            cleaned = cleaned.split(separator, 1)[0]
            break
    return _bounded_words(cleaned, max_words)


def _first_sentence(text: Any, max_words: int = 40) -> str:
    compact = _clean(text)
    match = re.split(r"(?<=[.!?])\s+", compact, maxsplit=1)
    return _bounded_words(match[0] if match else compact, max_words)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _seed_topic(seed: Any) -> str:
    if seed is None:
        return ""
    if isinstance(seed, Mapping):
        return str(seed.get("topic") or seed.get("research_question") or "")
    return str(getattr(seed, "topic", "") or getattr(seed, "research_question", "") or "")


@dataclass(frozen=True)
class Contribution:
    id: str
    claim: str
    novelty_delta: str
    evidence_ids: tuple[str, ...]
    artifact: str
    status: str = "recorded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "claim": self.claim,
            "novelty_delta": self.novelty_delta,
            "evidence_ids": list(self.evidence_ids),
            "artifact": self.artifact,
            "status": self.status,
        }


@dataclass
class ContributionContract:
    working_title: str
    title_candidates: tuple[str, ...]
    takeaway: str
    contributions: tuple[Contribution, ...]
    target_venue: str
    audience: str
    closest_prior_work: tuple[str, ...]
    threats: tuple[str, ...]
    figure_table_plan: tuple[str, ...]
    keywords: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "contribution_contract",
            "working_title": self.working_title,
            "title_candidates": list(self.title_candidates),
            "takeaway": self.takeaway,
            "contributions": [item.to_dict() for item in self.contributions],
            "target_venue": self.target_venue,
            "audience": self.audience,
            "closest_prior_work": list(self.closest_prior_work),
            "threats": list(self.threats),
            "figure_table_plan": list(self.figure_table_plan),
            "keywords": list(self.keywords),
        }

    def to_markdown(self) -> str:
        lines = [
            "# Contribution contract",
            "",
            f"**Working title:** {self.working_title}",
            "",
            f"**Takeaway:** {self.takeaway}",
            "",
            f"**Target venue:** {self.target_venue}",
            f"**Audience:** {self.audience}",
            "",
            "## Contributions",
            "",
        ]
        for item in self.contributions:
            lines += [
                f"### {item.id}: {item.claim}",
                f"- Novelty delta: {item.novelty_delta}",
                f"- Evidence: {', '.join(item.evidence_ids) or 'not recorded'}",
                f"- Artifact: {item.artifact}",
                "",
            ]
        lines += ["## Closest prior work", ""]
        lines += [f"- {item}" for item in self.closest_prior_work] or ["- not recorded"]
        lines += ["", "## Threats to validity", ""]
        lines += [f"- {item}" for item in self.threats] or ["- not recorded"]
        lines += ["", "## Figure / table plan", ""]
        lines += [f"- {item}" for item in self.figure_table_plan] or ["- not recorded"]
        return "\n".join(lines) + "\n"


def _ensure_keyword(title: str, keywords: Iterable[str]) -> str:
    terms = [term for term in keywords if term and len(term) > 2]
    if not terms:
        return title
    lowered = title.lower()
    if any(re.search(rf"\b{re.escape(term.lower())}\b", lowered) for term in terms):
        return title
    keyword = terms[0]
    candidate = f"{title}: {keyword}"
    if len(candidate.split()) <= 20:
        return candidate
    words = title.split()
    return " ".join((words[:19] + [keyword])[:20])


def _title_score(title: str, keywords: Iterable[str]) -> int:
    words = title.split()
    score = 0
    if 8 <= len(words) <= 16:
        score += 2
    if len(words) > 20:
        score -= 10
    if "…" in title or "..." in title:
        score -= 10
    lowered = title.lower()
    if any(term.lower() in lowered for term in keywords):
        score += 3
    if any(vague in lowered for vague in _VAGUE_TERMS):
        score -= 10
    if words and words[-1].lower().strip(".,:;-") in _TRAILING_STOPWORDS:
        score -= 4
    if "of:" in lowered or "for:" in lowered or "with:" in lowered:
        score -= 4
    if any(term in lowered for term in ("preregistered", "evidence", "evaluation", "benchmark", "study", "protocol")):
        score += 1
    return score


def pick_title(contract: ContributionContract, keywords: Iterable[str]) -> str:
    terms = [term for term in keywords if term]
    candidates = [_clean(title) for title in contract.title_candidates if _clean(title)]
    if not candidates:
        candidates = [_clean(contract.working_title) or "An evidence-backed study"]
    adjusted = [_ensure_keyword(title, terms) for title in candidates]
    # Keep the contract honest: never return a title with an ellipsis or a
    # vague blocklist phrase. If every candidate is bad, use a specific
    # fallback built from the contract.
    adjusted = [
        title for title in adjusted
        if "…" not in title
        and len(title.split()) <= 20
        and not any(v in title.lower() for v in _VAGUE_TERMS)
    ]
    if not adjusted:
        adjusted = [_ensure_keyword("Evidence from a preregistered evaluation", terms)]
    return max(adjusted, key=lambda title: _title_score(title, terms))


def build_contribution_contract(
    *,
    gap: Any | None,
    candidate: Any | None,
    spec: Any | None,
    analysis: Any | None,
    report: Any | None,
    synthesis: Any | None,
    readiness: Any | None,
    keywords: Iterable[str],
    seed: Any | None = None,
    venue: Any | None = None,
) -> ContributionContract:
    """Assemble one page of scientific argument from the frozen lineage."""
    gap_map = _as_mapping(gap)
    gap_statement = str(
        getattr(gap, "statement", "") if gap is not None and not isinstance(gap, Mapping)
        else gap_map.get("statement") or ""
    )
    method = _clean(_field(candidate, "method", "")) or "the proposed method"
    difference = _clean(_field(candidate, "difference_from_existing", ""))
    decision = str(_field(report, "decision", "") or "INCOMPLETE")
    decision_evidence = _clean(_field(report, "decision_evidence", ""))
    n_seeds = len(_field(analysis, "seed_level_results", ()) or ())
    metrics = list(_field(analysis, "proposed_metrics", {}) or {})
    effects = dict(_field(analysis, "effect_size", {}) or {})
    primary_metric = metrics[0] if metrics else "the primary metric"
    primary_effect = float(effects.get(primary_metric, 0.0)) if effects else 0.0
    domain = _clean(_seed_topic(seed) or gap_statement)
    method_short = _bounded_words(method, 10)
    method_head = _head_phrase(method, 8)
    domain_short = _bounded_words(domain, 7)
    takeaway = _first_sentence(decision_evidence or f"{method_short} was evaluated over {n_seeds} seeds; the preregistered gates classified the outcome as {decision}.")
    result_phrase = f"a preregistered {n_seeds}-seed evaluation" if n_seeds else "a preregistered evaluation"

    title_candidates = [
        f"{method_head}: {result_phrase}",
        f"{method_head} for {domain_short}" if domain_short else f"{method_head}: evidence and limits",
        f"Evidence on {method_head} from {n_seeds} seeds" if n_seeds else f"Evidence on {method_head}",
        f"Rethinking {domain_short}: {method_head}" if domain_short else f"Rethinking the evaluation of {method_head}",
        f"{method_head} and the limits of current practice",
    ]
    keywords_tuple = tuple(dict.fromkeys(str(term).strip() for term in keywords if str(term).strip()))
    title_candidates = tuple(_ensure_keyword(_clean(title), keywords_tuple) for title in title_candidates)

    prior_work: list[str] = []
    if synthesis is not None:
        for paper in list(getattr(synthesis, "papers", ()))[:6]:
            if getattr(paper, "is_gap_support", False) or len(prior_work) < 3:
                citation = f" [@{paper.key}]" if getattr(paper, "key", "") else ""
                prior_work.append(f"{getattr(paper, 'title', 'Untitled')}{citation}")

    contributions = (
        Contribution(
            id="C1",
            claim=f"We introduce and evaluate {method_short}.",
            novelty_delta=difference or "The recorded difference from existing work is not yet specified.",
            evidence_ids=(str(_field(candidate, "id", "") or ""),),
            artifact="Methodology and experiment specification",
        ),
        Contribution(
            id="C2",
            claim=(
                f"Across {n_seeds} seed(s), {primary_metric} changed by {primary_effect:+.4f}; "
                f"the preregistered decision is {decision}."
            ),
            novelty_delta="The empirical result is reported with exact tests and per-seed observations.",
            evidence_ids=(str(_field(analysis, "id", "") or ""), str(_field(report, "id", "") or "")),
            artifact="Results table and statistical analysis",
        ),
        Contribution(
            id="C3",
            claim="The study is preregistered and its evidence chain is checksummed.",
            novelty_delta="The contribution includes a reproducible lineage, not only a headline number.",
            evidence_ids=(
                str(_field(spec, "id", "") or ""),
                str(_field(analysis, "id", "") or ""),
            ),
            artifact="Evidence bundle, manifest, and appendices",
        ),
    )

    threats: list[str] = []
    if report is not None:
        threats.extend(str(item) for item in (_field(report, "limitations", ()) or ()) if str(item).strip())
    if readiness is not None:
        threats.extend(f"Readiness blocker: {item}" for item in (getattr(readiness, "blocking_issues", ()) or ()))
    threats = list(dict.fromkeys(threats))

    plan = [
        "Main results table: baseline vs candidate per metric, with effect sizes, exact p, and n.",
        "Effect-size figure generated deterministically from the validated seed observations.",
        "Literature comparison table assembled from the reviewed corpus.",
    ]
    if analysis is not None and dict(_field(analysis, "ablation_contributions", {}) or {}):
        plan.append("Ablation table: measured descendant runs only.")
    else:
        plan.append("Ablation table: not available in this lineage (report as a limitation).")

    return ContributionContract(
        working_title=pick_title(
            ContributionContract(
                working_title=title_candidates[0],
                title_candidates=title_candidates,
                takeaway=takeaway,
                contributions=contributions,
                target_venue=str(_field(venue, "name", "") or "generic_journal"),
                audience=domain_short or "the target research community",
                closest_prior_work=tuple(prior_work),
                threats=tuple(threats),
                figure_table_plan=tuple(plan),
                keywords=keywords_tuple,
            ),
            keywords_tuple,
        ),
        title_candidates=title_candidates,
        takeaway=takeaway,
        contributions=contributions,
        target_venue=str(_field(venue, "name", "") or "generic_journal"),
        audience=domain_short or "the target research community",
        closest_prior_work=tuple(prior_work),
        threats=tuple(threats),
        figure_table_plan=tuple(plan),
        keywords=keywords_tuple,
    )

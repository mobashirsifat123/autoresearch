"""Claim ledger: sentence-level numeric traceability.

The old manuscript audit scanned every digit in the draft and rejected the
whole paper when a token was not in a narrow float set. That rejected cited
literature values (``40%``, ``46.7``, ``0.81-0.83``) and the results table's
own ``p``, Cohen's ``d``, ``std``, and ``n`` — the exact values the prompt
required the writer to embed verbatim.

This module replaces that with an evidence ledger:

* every fact the study may assert is a :class:`Claim` with evidence ids,
  numbers, and citations;
* the allowed-number set includes every numeric leaf of the validated
  analysis, the rendered tables, the spec, the report, and the reviewed
  corpus;
* the audit checks a *sentence* against the ledger and exempts years, section
  numbers, citation keys, DOIs, URLs, hex ids, code spans, and verbatim table
  rows;
* a violation is a sentence to repair, never a reason to discard the paper.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from autoresearch.application.evidence_access import field as _field

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_HEX_RE = re.compile(r"[0-9a-fA-F]{16,}")
_URLISH_RE = re.compile(r"(https?://|www\.|doi:|arxiv:|10\.\d{4,}/)", re.IGNORECASE)


def _is_dataclass_instance(value: Any) -> bool:
    return dataclasses.is_dataclass(value) and not isinstance(value, type)


def _walk_numbers(value: Any) -> Iterable[float]:
    """Yield every numeric leaf, including numbers embedded in strings."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield float(value)
        return
    if isinstance(value, str):
        # Do not turn hex lineage ids/checksums into "allowed numbers".
        cleaned = _HEX_RE.sub(" ", value)
        for match in _NUMBER_RE.finditer(cleaned):
            try:
                yield float(match.group(0))
            except ValueError:
                continue
        return
    if _is_dataclass_instance(value):
        for item in dataclasses.asdict(value).values():
            yield from _walk_numbers(item)
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_numbers(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _walk_numbers(item)


def _rounded_variants(value: float) -> Iterable[float]:
    yield value
    for precision in (2, 3, 4, 5, 6):
        yield round(value, precision)


def _add_numbers(store: set[float], value: Any) -> None:
    for number in _walk_numbers(value):
        for variant in _rounded_variants(number):
            store.add(variant)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _split_sentences(text: str) -> list[str]:
    # Table rows and headings are handled by the caller; this is prose.
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
    return [part.strip() for part in parts if part.strip()]


def _line_is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def _inside_span(line: str, start: int, end: int, pattern: re.Pattern[str]) -> bool:
    for match in pattern.finditer(line):
        if match.start() <= start and end <= match.end():
            return True
    return False


def _token_is_wordlike(line: str, start: int, end: int) -> bool:
    if start > 0 and (line[start - 1].isalpha() or line[start - 1] == "_"):
        return True
    if end < len(line) and (line[end].isalpha() or line[end] == "_"):
        return True
    # gpt-5, paper-3, v2: a hyphen between a letter and a digit is part of a
    # name/version, not a measured number.
    if start > 1 and line[start - 1] == "-" and line[start - 2].isalpha():
        return True
    return False


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    type: str
    evidence_ids: tuple[str, ...]
    numbers: tuple[float, ...]
    citations: tuple[str, ...]
    section: str
    confidence: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "type": self.type,
            "evidence_ids": list(self.evidence_ids),
            "numbers": [round(number, 8) for number in self.numbers],
            "citations": list(self.citations),
            "section": self.section,
            "confidence": self.confidence,
        }


@dataclass
class ClaimLedger:
    claims: tuple[Claim, ...] = ()
    allowed_numbers: tuple[float, ...] = ()
    evidence_text: str = ""
    tables_md: str = ""
    study_numbers: tuple[float, ...] = ()
    literature_numbers: dict[str, tuple[float, ...]] = field(default_factory=dict)
    results_numbers: tuple[float, ...] = ()
    _study_set: set[float] = field(default_factory=set, repr=False)
    _literature_sets: dict[str, set[float]] = field(default_factory=dict, repr=False)
    _results_set: set[float] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self._study_set = set(self.study_numbers)
        self._literature_sets = {
            str(key): set(values) for key, values in self.literature_numbers.items()
        }
        self._results_set = set(self.results_numbers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "claim_ledger",
            "claims": [claim.to_dict() for claim in self.claims],
            "allowed_numbers": sorted(set(self.allowed_numbers)),
            "study_number_count": len(self._study_set),
            "literature_keys": sorted(self._literature_sets),
            "evidence_chars": len(self.evidence_text),
        }

    @staticmethod
    def _number_allowed_in(token: str, allowed: set[float]) -> bool:
        try:
            value = float(token)
        except ValueError:
            return False
        candidates = (value, abs(value))
        return any(
            abs(candidate - allowed_value) < 1e-9
            for candidate in candidates
            for allowed_value in allowed
        )

    def _token_exempt(self, sentence: str, match: re.Match[str]) -> bool:
        token = match.group(0)
        start, end = match.start(), match.end()
        if not token or token in ("-", "+"):
            return True
        if _YEAR_RE.fullmatch(token):
            return True
        if _token_is_wordlike(sentence, start, end):
            return True
        if _inside_span(sentence, start, end, _HEX_RE):
            return True
        # URL/DOI/arXiv identifiers anywhere in the whitespace token.
        left = sentence.rfind(" ", 0, start) + 1
        right = sentence.find(" ", end)
        word = sentence[left : right if right != -1 else len(sentence)]
        if _URLISH_RE.search(word):
            return True
        # Citation keys like [@paper1] or [@paper-1].
        bracket_left = sentence.rfind("[", 0, start)
        bracket_right = sentence.find("]", end)
        if bracket_left != -1 and bracket_right != -1 and "@" in sentence[bracket_left:bracket_right]:
            return True
        # Numbered list/section marker at the start of the sentence. The
        # sentence splitter may leave just "2." as its own sentence.
        leading = re.match(r"^\s*(\d+)[.)](?:\s|$)", sentence)
        if leading and token == leading.group(1):
            return True
        return False

    def audit_text(self, text: str) -> list[dict[str, Any]]:
        """Return one violation per untraceable number, with its sentence.

        Numbers are scoped to the sentence: study numbers are always allowed;
        a paper's reported numbers are allowed only when that paper's citation
        key appears in the same sentence. This prevents a cited 46.7 F1 from
        paper B from being asserted as this study's result.
        """
        violations: list[dict[str, Any]] = []
        citation_re = re.compile(r"\[@([A-Za-z0-9_-]+)\]")
        in_code = False
        section = ""
        abstract_results = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            if stripped.startswith("## ") and not stripped.startswith("### "):
                section = re.sub(r"^\d+\.\s*", "", stripped[3:].strip())
            if section == "Abstract":
                abstract_results = stripped.startswith("**Results:**") or stripped.startswith("**Conclusion:**")
            else:
                abstract_results = False
            is_heading = stripped.startswith("#")
            if is_heading:
                line = stripped.lstrip("#").strip()
                stripped = line
            if not stripped:
                continue
            if _line_is_table_row(line) and self.tables_md and stripped in self.tables_md:
                continue
            for sentence in _split_sentences(line):
                cited = set(citation_re.findall(sentence))
                # Results and the Abstract's Results/Conclusion paragraphs may
                # only use result numbers. A gap-statement number such as a
                # paper's reported 46.7 F1 cannot become "our result" there.
                results_scope = section == "Results" or abstract_results
                allowed = set(self._results_set if results_scope else self._study_set)
                if not results_scope:
                    for key in cited:
                        allowed.update(self._literature_sets.get(key, set()))
                for match in _NUMBER_RE.finditer(sentence):
                    token = match.group(0)
                    if self._token_exempt(sentence, match):
                        continue
                    # Headings carry structural small integers (e.g. "Study
                    # Limitations (at least 3)"); measured/large numbers are
                    # still audited.
                    if (
                        is_heading
                        and re.fullmatch(r"\d{1,2}", token)
                        and 0 <= int(token) <= 12
                    ):
                        continue
                    if self._number_allowed_in(token, allowed):
                        continue
                    if any(
                        self._number_allowed_in(token, values)
                        for values in self._literature_sets.values()
                    ):
                        reason = (
                            "cited value appears without the matching citation key "
                            "in this sentence"
                        )
                    elif results_scope:
                        reason = "number is not a result value for this study"
                    else:
                        reason = "number not traceable to the evidence ledger"
                    violations.append({
                        "token": token,
                        "sentence": sentence.strip()[:400],
                        "reason": reason,
                    })
        return violations


def _paper_review_fields(notes: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(notes or {})


def build_claim_ledger(
    *,
    gap: Any | None,
    candidate: Any | None,
    spec: Any | None,
    analysis: Any | None,
    report: Any | None,
    papers: list[dict[str, Any]],
    review_notes: Mapping[str, Mapping[str, Any]] | None = None,
    keys: Mapping[str, str] | None = None,
    tables_md: str = "",
    decision: str = "INCOMPLETE",
) -> ClaimLedger:
    """Build the deterministic evidence ledger used by the writer and audit."""
    key_by_paper = dict(keys or {})
    notes_by_paper = {str(key): dict(value) for key, value in (review_notes or {}).items()}
    claims: list[Claim] = []
    allowed: set[float] = set()
    study_allowed: set[float] = set()
    literature_allowed: dict[str, set[float]] = {}
    results_allowed: set[float] = set()

    def add_claim(
        claim_id: str,
        text: str,
        claim_type: str,
        evidence_ids: Iterable[str],
        numbers: Iterable[float],
        citations: Iterable[str] = (),
        section: str = "results",
    ) -> None:
        number_tuple = tuple(sorted({round(float(value), 8) for value in numbers}))
        citation_tuple = tuple(citations)
        claims.append(Claim(
            id=claim_id,
            text=text,
            type=claim_type,
            evidence_ids=tuple(str(item) for item in evidence_ids if str(item)),
            numbers=number_tuple,
            citations=citation_tuple,
            section=section,
        ))
        for number in number_tuple:
            for variant in _rounded_variants(number):
                allowed.add(variant)
                if citation_tuple:
                    for key in citation_tuple:
                        literature_allowed.setdefault(str(key), set()).add(variant)
                else:
                    study_allowed.add(variant)

    method = str(_field(candidate, "method", "") or "").strip()
    difference = str(_field(candidate, "difference_from_existing", "") or "").strip()
    if method:
        add_claim(
            "C-METHOD",
            "The study evaluates " + method + (f". Its recorded difference from existing work: {difference}." if difference else "."),
            "method",
            [_field(candidate, "id", "")],
            list(_walk_numbers(method + " " + difference)),
            section="methodology",
        )

    if analysis is not None:
        baseline = dict(_field(analysis, "baseline_metrics", {}) or {})
        proposed = dict(_field(analysis, "proposed_metrics", {}) or {})
        effects = dict(_field(analysis, "effect_size", {}) or {})
        tests = dict(_field(analysis, "statistical_tests", {}) or {})
        seed_rows = list(_field(analysis, "seed_level_results", ()) or ())
        n_seeds = len(seed_rows)
        for metric in sorted(set(baseline) | set(proposed) | set(effects)):
            test = tests.get(metric) if isinstance(tests.get(metric), Mapping) else {}
            p_value = test.get("p_value") if isinstance(test, Mapping) else None
            text = (
                f"{metric}: baseline mean {float(baseline.get(metric, 0.0)):.4f}, "
                f"candidate mean {float(proposed.get(metric, 0.0)):.4f}, "
                f"mean difference {float(effects.get(metric, 0.0)):+.4f}"
            )
            if p_value is not None:
                text += f"; exact p={float(p_value):.4f}"
            text += f"; n={n_seeds}."
            add_claim(
                f"C-RESULT-{metric}",
                text,
                "observation",
                [_field(analysis, "id", "")],
                [
                    float(baseline.get(metric, 0.0)),
                    float(proposed.get(metric, 0.0)),
                    float(effects.get(metric, 0.0)),
                    *([float(p_value)] if p_value is not None else []),
                    float(n_seeds),
                ],
                section="results",
            )

    if report is not None:
        decision_evidence = _field(report, "decision_evidence", "") or f"Decision: {decision}"
        report_id = _field(report, "id", "")
        add_claim(
            "C-DECISION",
            str(decision_evidence),
            "observation",
            [report_id],
            list(_walk_numbers(str(decision_evidence))),
            section="discussion",
        )
        for index, limitation in enumerate(_field(report, "limitations", ()) or (), start=1):
            add_claim(
                f"C-LIMITATION-{index}",
                str(limitation),
                "limitation",
                [report_id],
                list(_walk_numbers(str(limitation))),
                section="discussion",
            )

    for paper in papers:
        paper_id = str(paper.get("id", ""))
        key = key_by_paper.get(paper_id, "")
        notes = _paper_review_fields(notes_by_paper.get(paper_id))
        title = str(paper.get("title") or "Untitled")
        problem = str(notes.get("problem") or notes.get("research_question") or "").strip()
        method_text = str(notes.get("method") or paper.get("method") or "").strip()
        results = notes.get("results_json") or paper.get("abstract") or ""
        if isinstance(results, str):
            try:
                parsed_results = json.loads(results)
                if isinstance(parsed_results, list):
                    results = "; ".join(str(item) for item in parsed_results[:2])
                else:
                    results = str(parsed_results)
            except (TypeError, ValueError):
                results = results[:400]
        elif isinstance(results, list):
            results = "; ".join(str(item) for item in results[:2])
        limitations = notes.get("limitations_json") or []
        if isinstance(limitations, str):
            try:
                parsed_limits = json.loads(limitations)
                limitations = parsed_limits if isinstance(parsed_limits, list) else [limitations]
            except (TypeError, ValueError):
                limitations = [limitations]
        text = f"{title} [{key or 'uncited'}]"
        if problem:
            text += f". Problem: {problem}"
        if method_text:
            text += f". Method: {method_text}"
        if results:
            text += f". Reported results: {results}"
        if limitations:
            text += ". Recorded limitations: " + "; ".join(str(item) for item in limitations[:3])
        add_claim(
            f"C-PAPER-{key or paper_id[:8]}",
            text,
            "literature",
            [paper_id],
            list(_walk_numbers(text)),
            citations=[key] if key else [],
            section="literature",
        )

    evidence_parts = [
        _as_text(gap),
        _as_text(candidate),
        _as_text(spec),
        _as_text(analysis),
        _as_text(report),
        _as_text(papers),
        _as_text(notes_by_paper),
        tables_md,
    ]
    evidence_text = "\n".join(part for part in evidence_parts if part)

    # Study numbers come from the validated lineage and the rendered table.
    # Paper metadata numbers are scoped to that paper's citation key by the
    # C-PAPER claims above; they are not globally allowed.
    for value in (gap, candidate, spec, analysis, report, tables_md):
        _add_numbers(study_allowed, value)
    for value in (analysis, tables_md):
        _add_numbers(results_allowed, value)
    # The rendered table may contain derived std/d/p values not stored as
    # individual analysis fields; parse its numeric cells too.
    for match in _NUMBER_RE.finditer(tables_md or ""):
        try:
            for variant in _rounded_variants(float(match.group(0))):
                study_allowed.add(variant)
                results_allowed.add(variant)
        except ValueError:
            continue
    for values in literature_allowed.values():
        allowed.update(values)
    allowed.update(study_allowed)

    return ClaimLedger(
        claims=tuple(claims),
        allowed_numbers=tuple(sorted(allowed)),
        evidence_text=evidence_text,
        tables_md=tables_md,
        study_numbers=tuple(sorted(study_allowed)),
        literature_numbers={
            key: tuple(sorted(values)) for key, values in literature_allowed.items()
        },
        results_numbers=tuple(sorted(results_allowed)),
    )

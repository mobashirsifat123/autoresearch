"""Body-hygiene lint for generated manuscripts.

Template compliance does not make a paper readable. This lint catches the
internal scaffolding that leaked into the AutoFID manuscript body: lineage
ids, workspace checksums, claim ids, review-run references, raw JSON in
Results, truncation ellipses in the title, and boilerplate review phrases.

The lint is deliberately narrow: it only rejects text that should live in the
appendices/evidence bundle (or nowhere), never legitimate scientific prose.
Honest "not specified at freeze time" markers are allowed in Methodology and
Appendices, where they belong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_HEX_ID = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_REVIEW_RUN = re.compile(r"\breview run\b", re.IGNORECASE)
_CHECKSUM_PREFIX = re.compile(r"\bchecksum prefix\b", re.IGNORECASE)
_CLAIM_ID = re.compile(r"\bclaim\s+[0-9a-fA-F]{8,}\b", re.IGNORECASE)
_RESEARCH_ID = re.compile(r"\bresearch question \(ID\b", re.IGNORECASE)
_NOT_SPECIFIED = re.compile(r"\bnot specified at freeze time\b", re.IGNORECASE)
_BOILERPLATE = (
    "Next study in the review ranking follows",
    "Additional recorded evidence",
    "no supporting papers recorded",
    "Cited as evidence for the research gap",
)
_PLACEHOLDERS = ("TODO", "TBD", "FIXME")


@dataclass(frozen=True)
class HygieneIssue:
    code: str
    detail: str
    line: int
    severity: str = "error"  # error | warning

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "line": self.line,
            "severity": self.severity,
        }


@dataclass
class HygieneReport:
    issues: list[HygieneIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[HygieneIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[HygieneIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "body_hygiene",
            "passed": self.passed,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }


def _sections(markdown: str) -> list[tuple[int, str]]:
    """Return (line_number, section_name) for each line."""
    result: list[tuple[int, str]] = []
    current = ""
    for index, line in enumerate(markdown.splitlines(), start=1):
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = re.sub(r"^\d+\.\s*", "", match.group(1)).strip()
        result.append((index, current))
    return result


def audit_body(markdown: str) -> HygieneReport:
    report = HygieneReport()
    section_by_line = dict(_sections(markdown))
    in_results = False
    in_code = False

    for index, line in enumerate(markdown.splitlines(), start=1):
        section = section_by_line.get(index, "")
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if index == 1 and line.startswith("# "):
            title = line[2:].strip()
            if "…" in title or "..." in title:
                report.issues.append(HygieneIssue(
                    "title_ellipsis", "title contains a truncation marker", index,
                ))
        if section == "Results":
            in_results = True
        elif section:
            in_results = False

        allowed_appendices = section in ("References", "Appendices")
        # "not specified at freeze time" is an honest marker in Methods and in
        # the Study Limitations part of the Discussion; it is not body filler.
        allowed_method = section in ("Methodology", "Methods", "Discussion")

        for match in _HEX_ID.finditer(line):
            if allowed_appendices:
                continue
            left = line.rfind(" ", 0, match.start()) + 1
            right = line.find(" ", match.end())
            token = line[left : right if right != -1 else len(line)]
            if any(marker in token.lower() for marker in ("http", "doi", "arxiv", "www.")):
                continue
            report.issues.append(HygieneIssue(
                "internal_hex_id", f"lineage/checksum id '{match.group(0)[:16]}…' in body", index,
            ))
        for pattern, code, detail in (
            (_RESEARCH_ID, "internal_research_id", "internal research-question id in body"),
            (_REVIEW_RUN, "internal_review_run", "review-run reference in body"),
            (_CHECKSUM_PREFIX, "internal_checksum", "checksum prefix in body"),
            (_CLAIM_ID, "internal_claim_id", "claim id in body"),
        ):
            if pattern.search(line) and not allowed_appendices:
                report.issues.append(HygieneIssue(code, detail, index))
        if _NOT_SPECIFIED.search(line) and not (allowed_appendices or allowed_method):
            report.issues.append(HygieneIssue(
                "freeze_placeholder_outside_methods",
                "'not specified at freeze time' outside the Methodology/appendices",
                index,
            ))
        for phrase in _BOILERPLATE:
            if phrase.lower() in line.lower():
                report.issues.append(HygieneIssue(
                    "review_boilerplate", f"boilerplate phrase '{phrase}'", index,
                ))
        for placeholder in _PLACEHOLDERS:
            if placeholder.lower() in line.lower() and not allowed_appendices:
                report.issues.append(HygieneIssue(
                    "placeholder_text", f"placeholder '{placeholder}' in body", index,
                ))
        if in_results and (stripped.startswith("{") or stripped.startswith("[{")):
            report.issues.append(HygieneIssue(
                "raw_json_in_results", "raw JSON block in Results", index,
            ))
    return report

"""Venue profiles and submission lint.

The FastTrack template is one profile, not the universal objective. A venue
profile records the constraints a real submission must satisfy (abstract
style, reference coverage, anonymity, required statements, reviewer
threshold). The manuscript stage uses the profile for its readiness gate and
for a venue lint; the FastTrack audit remains available for the generic
teaching template.

This module is deliberately deterministic and dependency-free: it parses the
generated Markdown, never a PDF, and reports only checks it can actually
perform. Page limits that require a compiled PDF are recorded as "not
checked", never silently passed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VenueProfile:
    name: str
    kind: str
    page_limit: int | None
    abstract_style: str  # "structured" | "unstructured"
    anonymized: bool
    min_references: int
    min_seeds: int
    min_baselines: int
    min_ablations: int
    require_power: bool
    require_real_execution: bool
    required_sections: tuple[str, ...]
    required_statements: tuple[str, ...]
    template: str  # "fasttrack" | "generic"
    reviewer_threshold: float
    min_datasets: int = 1
    min_robustness: int = 0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "page_limit": self.page_limit,
            "abstract_style": self.abstract_style,
            "anonymized": self.anonymized,
            "min_references": self.min_references,
            "min_seeds": self.min_seeds,
            "min_baselines": self.min_baselines,
            "min_ablations": self.min_ablations,
            "require_power": self.require_power,
            "require_real_execution": self.require_real_execution,
            "required_sections": list(self.required_sections),
            "required_statements": list(self.required_statements),
            "template": self.template,
            "reviewer_threshold": self.reviewer_threshold,
            "min_datasets": self.min_datasets,
            "min_robustness": self.min_robustness,
            "notes": list(self.notes),
        }


_PROFILES: dict[str, VenueProfile] = {
    # The bundled teaching template. Kept for backward compatibility and for
    # users who explicitly want its six-part structured abstract.
    "fasttrack": VenueProfile(
        name="fasttrack",
        kind="generic",
        page_limit=5,
        abstract_style="structured",
        anonymized=False,
        min_references=3,
        min_seeds=3,
        min_baselines=0,
        min_ablations=0,
        require_power=False,
        require_real_execution=False,
        required_sections=(
            "Introduction", "Literature Review", "Methodology", "Results",
            "Discussion", "References", "Appendices",
        ),
        required_statements=(),
        template="fasttrack",
        reviewer_threshold=2.5,
        notes=("Pedagogical template; not a venue submission format.",),
    ),
    "generic_journal": VenueProfile(
        name="generic_journal",
        kind="journal",
        page_limit=None,
        abstract_style="structured",
        anonymized=False,
        min_references=20,
        min_seeds=3,
        min_baselines=2,
        min_ablations=1,
        require_power=True,
        require_real_execution=True,
        required_sections=(
            "Introduction", "Related Work", "Methods", "Results",
            "Discussion", "Conclusion", "References",
        ),
        required_statements=(
            "Data and code availability", "Limitations",
        ),
        template="generic",
        reviewer_threshold=3.5,
        min_datasets=2,
        min_robustness=1,
        notes=("Generic journal profile; adapt to the target journal's author guidelines.",),
    ),
    "neurips": VenueProfile(
        name="neurips",
        kind="conference",
        page_limit=9,
        abstract_style="unstructured",
        anonymized=True,
        min_references=20,
        min_seeds=3,
        min_baselines=2,
        min_ablations=1,
        require_power=True,
        require_real_execution=True,
        required_sections=(
            "Introduction", "Related Work", "Method", "Results",
            "Discussion", "Conclusion", "References",
        ),
        required_statements=(
            "Limitations", "Broader Impact", "Data and code availability",
        ),
        template="generic",
        reviewer_threshold=4.0,
        min_datasets=2,
        min_robustness=1,
        notes=("Adapt to the current NeurIPS author kit; page limit excludes references.",),
    ),
    "acl": VenueProfile(
        name="acl",
        kind="conference",
        page_limit=8,
        abstract_style="unstructured",
        anonymized=True,
        min_references=20,
        min_seeds=3,
        min_baselines=2,
        min_ablations=1,
        require_power=True,
        require_real_execution=True,
        required_sections=(
            "Introduction", "Related Work", "Method", "Results",
            "Discussion", "Conclusion", "References",
        ),
        required_statements=(
            "Limitations", "Ethics", "Data and code availability",
        ),
        template="generic",
        reviewer_threshold=4.0,
        min_datasets=2,
        min_robustness=1,
        notes=("Adapt to the current ACL author kit; includes limitations and ethics statements.",),
    ),
    "cvpr": VenueProfile(
        name="cvpr",
        kind="conference",
        page_limit=8,
        abstract_style="unstructured",
        anonymized=True,
        min_references=20,
        min_seeds=3,
        min_baselines=2,
        min_ablations=1,
        require_power=True,
        require_real_execution=True,
        required_sections=(
            "Introduction", "Related Work", "Method", "Results",
            "Discussion", "Conclusion", "References",
        ),
        required_statements=(
            "Limitations", "Data and code availability",
        ),
        template="generic",
        reviewer_threshold=4.0,
        min_datasets=2,
        min_robustness=1,
        notes=("Adapt to the current CVPR author kit; page limit excludes references.",),
    ),
    "workshop": VenueProfile(
        name="workshop",
        kind="workshop",
        page_limit=4,
        abstract_style="unstructured",
        anonymized=True,
        min_references=8,
        min_seeds=3,
        min_baselines=1,
        min_ablations=0,
        require_power=False,
        require_real_execution=False,
        required_sections=(
            "Introduction", "Related Work", "Method", "Results",
            "Discussion", "Conclusion", "References",
        ),
        required_statements=("Limitations",),
        template="generic",
        reviewer_threshold=3.0,
        notes=("Workshop profile: exploratory and simulation studies are acceptable when labeled.",),
    ),
    "technical_report": VenueProfile(
        name="technical_report",
        kind="report",
        page_limit=None,
        abstract_style="unstructured",
        anonymized=False,
        min_references=3,
        min_seeds=1,
        min_baselines=0,
        min_ablations=0,
        require_power=False,
        require_real_execution=False,
        required_sections=(
            "Introduction", "Method", "Results", "Discussion", "References",
        ),
        required_statements=(),
        template="generic",
        reviewer_threshold=2.0,
        notes=("Technical report profile: honest scaffold for incomplete or simulation evidence.",),
    ),
}

_ALIASES = {
    # True synonyms only. Results is not Experiments; Discussion is not
    # Conclusion. A venue that requires a distinct section must see it.
    "Related Work": ("Related Work", "Literature Review"),
    "Literature Review": ("Literature Review", "Related Work"),
    "Methods": ("Methods", "Methodology", "Method"),
    "Method": ("Method", "Methods", "Methodology"),
    "Methodology": ("Methodology", "Methods", "Method"),
    "Experiments": ("Experiments",),
    "Results": ("Results",),
    "Conclusion": ("Conclusion",),
    "Discussion": ("Discussion",),
}


def get_venue_profile(name: str | None) -> VenueProfile:
    """Return a known profile; unknown names fall back honestly to the
    generic journal profile (never silently to the teaching template)."""
    key = (name or "generic_journal").strip().lower()
    return _PROFILES.get(key, _PROFILES["generic_journal"])


def available_venue_profiles() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))


def _sections(markdown: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = ""
    for line in markdown.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = re.sub(r"^\d+\.\s*", "", match.group(1)).strip()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
    return sections


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", text))


def _has_section(sections: dict[str, str], name: str) -> bool:
    return any(alias in sections for alias in _ALIASES.get(name, (name,)))


def _citation_keys(markdown: str) -> list[str]:
    return sorted(set(re.findall(r"\[@([A-Za-z0-9_-]+)\]", markdown)))


@dataclass
class VenueLint:
    venue: str
    passed: bool
    violations: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "passed": self.passed,
            "violations": list(self.violations),
            "checks": list(self.checks),
            "notes": list(self.notes),
        }


def venue_lint(markdown: str, profile: VenueProfile) -> VenueLint:
    """Deterministic submission lint for one venue profile.

    Only checks that can be performed on Markdown are enforced. Page limits
    and anonymity of the compiled PDF are recorded as not checked unless a PDF
    is supplied to a later stage.
    """
    sections = _sections(markdown)
    names = list(sections)
    title = ""
    for line in markdown.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            break
    abstract = sections.get("Abstract", "")
    citations = _citation_keys(markdown)
    checks: list[dict[str, Any]] = []
    violations: list[str] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            violations.append(f"{name}: {detail}")

    record("title_present", bool(title), "title present" if title else "missing H1 title")
    record(
        "title_no_ellipsis",
        "…" not in title and "..." not in title,
        "title has no truncation marker" if "…" not in title and "..." not in title
        else "title contains a truncation marker",
    )
    record(
        "title_word_limit",
        _word_count(title) <= 20,
        f"{_word_count(title)} words (max 20)",
    )
    record("abstract_present", bool(abstract.strip()), "abstract present" if abstract.strip() else "missing abstract")
    if abstract.strip():
        words = _word_count(abstract)
        if profile.abstract_style == "structured":
            missing = [
                part for part in (
                    "Background", "Gap in Knowledge", "Contribution",
                    "Population & Methods", "Results", "Conclusion",
                )
                if part not in abstract
            ]
            record("abstract_structured", not missing, f"missing parts: {missing}" if missing else "six labeled parts present")
            record("abstract_word_count", 150 <= words <= 250, f"{words} words (required 150-250)")
        else:
            record("abstract_word_count", 100 <= words <= 300, f"{words} words (required 100-300 for unstructured abstracts)")
    missing_sections = [name for name in profile.required_sections if not _has_section(sections, name)]
    record(
        "required_sections",
        not missing_sections,
        f"missing sections: {missing_sections}" if missing_sections else "all required sections present",
    )
    reference_section = sections.get("References", "")
    reference_keys = sorted(set(re.findall(r"\[@([A-Za-z0-9_-]+)\]", reference_section)))
    record(
        "reference_coverage",
        len(reference_keys) >= profile.min_references,
        f"{len(reference_keys)} reference entries (minimum {profile.min_references}); "
        f"{len(citations)} in-text keys",
    )
    # Statements must appear in the body, not in a reference title or the
    # appendices.
    body_text = "\n".join(
        text for name, text in sections.items()
        if name not in ("References", "Appendices")
    )
    missing_statements = [
        statement for statement in profile.required_statements
        if statement.lower() not in body_text.lower()
    ]
    record(
        "required_statements",
        not missing_statements,
        f"missing statements: {missing_statements}" if missing_statements else "required statements present",
    )
    if profile.page_limit is not None:
        checks.append({
            "check": "page_limit",
            "ok": True,
            "detail": f"not checked on Markdown (venue limit {profile.page_limit} pages; verify the compiled PDF)",
        })
    if profile.anonymized:
        checks.append({
            "check": "anonymity",
            "ok": True,
            "detail": "not checked on Markdown; verify author block and PDF metadata before submission",
        })
    return VenueLint(
        venue=profile.name,
        passed=not violations,
        violations=violations,
        checks=checks,
        notes=list(profile.notes),
    )

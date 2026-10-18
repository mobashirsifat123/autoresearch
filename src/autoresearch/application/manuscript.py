"""Purpose: Stage-4 manuscript generation over one exact research lineage.

Consumes the durable outputs of review (gap + verified claims), novelty
(frozen methodology), and experiment (spec → analysis → report) and produces
an editable, cited manuscript (Markdown + LaTeX + BibTeX + tables + manifest,
optionally a compiled PDF).

Design rules, enforced structurally:

* **Deterministic generation** — every sentence is assembled from stored
  evidence fields; no model call invents prose, numbers, citations, authors,
  or results. Numerical values are formatted straight from the statistical
  analysis, which itself computed them from raw seed observations.
* **Exact lineage** — the gap, portfolio, freeze, spec, analysis, and report
  must form one coherent chain (each link references the previous). A broken
  chain yields an explicitly incomplete draft plus a precise evidence-gap
  report, never a fabricated completion.
* **Honest outcomes** — negative, null, and refuted decisions are reported
  as first-class results with their gate evidence.
* **No publication** — this stage writes files locally; it never submits or
  publicly posts anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from autoresearch.application.body_hygiene import HygieneReport, audit_body
from autoresearch.application.claim_ledger import ClaimLedger, build_claim_ledger
from autoresearch.application.contribution_contract import (
    ContributionContract,
    build_contribution_contract,
    pick_title,
)
from autoresearch.application.keywords import keyword_tokens, pick_keywords
from autoresearch.application.literature_synthesis import (
    LiteratureSynthesis,
    build_literature_synthesis,
)
from autoresearch.application.publication_readiness import (
    ReadinessReport,
    assess_publication_readiness,
)
from autoresearch.application.reviewer_panel import ReviewerReport, deterministic_review
from autoresearch.application.venue_profiles import (
    VenueLint,
    VenueProfile,
    get_venue_profile,
    venue_lint,
)
from autoresearch.domain.models import (
    ArtifactProvenance,
    ManuscriptRecord,
    PipelineKind,
    RunStatus,
)
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.redaction import redact_text
from autoresearch.storage.repository import ResearchRepository

MANUSCRIPT_PROMPT_VERSION = "manuscript-v3-readiness-gated"
MANUSCRIPT_WRITER_PROMPT_VERSION = "manuscript-writer-v4-evidence-ledger"

# FastTrack Academic Research Paper Template: every instruction line is
# transcribed into template-spec.json next to this package's manuscript
# skill; deterministic checks load their parameters from it so the spec is
# the single source of truth (fail-closed: unreadable spec aborts generation).
TEMPLATE_SPEC_PATH = (
    Path(__file__).resolve().parents[1] / "skills" / "manuscript" / "template-spec.json"
)

# Claims that require evidence this pipeline cannot produce; a deterministic
# audit rejects them rather than letting a draft overstate.
_OVERCLAIM_PATTERNS = (
    "state-of-the-art",
    "sota",
    "first ever",
    "first-ever",
    "groundbreaking",
    "unprecedented",
    "outperforms all",
    "best model",
    "novel state",
)

# Small structural integers (section numbers, enumeration) tolerated by the
# numeric audit; every other number must trace to the allowed evidence set.
_STRUCTURAL_INT_MAX = 12

# Template body sections in the required order (template-spec.json
# parameters.section_order). Literature Review is conditional on evidence.
_BODY_SECTION_NAMES = (
    "Introduction",
    "Literature Review",
    "Methodology",
    "Results",
    "Discussion",
    "References",
    "Appendices",
)

# Methodology fields the template names (template-spec.json T061-T069).
_TEMPLATE_METHODOLOGY_FIELDS = (
    "study_design",
    "data_collection",
    "sample_and_eligibility",
    "data_analysis",
    "ethical_considerations",
)

_NOT_SPECIFIED = "not specified at freeze time"

# Generic words that make poor indexing terms. Domain terms (benchmark,
# calibration, agent, forensics, ...) are deliberately not filtered.
_KEYWORD_STOPWORDS = {
    "add", "added", "fixed", "reference", "standardized", "standardised",
    "using", "based", "method", "methods", "study", "studies", "paper",
    "papers", "result", "results", "approach", "new", "proposed", "analysis",
    "data", "model", "models", "system", "systems", "framework", "framework",
    "our", "their", "this", "that", "from", "with", "into", "over", "under",
}


@lru_cache(maxsize=1)
def _template_parameters() -> dict[str, Any]:
    try:
        spec = json.loads(TEMPLATE_SPEC_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"manuscript_template_spec_error: cannot load {TEMPLATE_SPEC_PATH}: {exc}"
        ) from exc
    params = spec.get("parameters")
    if not isinstance(params, dict):
        raise ValueError(
            "manuscript_template_spec_error: template-spec.json has no parameters block"
        )
    return params


@dataclass
class TemplateAudit:
    checks: list[dict[str, Any]]
    violations: list[str]
    inside_out_order: tuple[str, ...] | None = None

    @property
    def passed(self) -> bool:
        return not self.violations


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", text))


def _h1_title(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return ""


def _section_texts(markdown: str) -> dict[str, str]:
    """H2 section name -> body text. Section names lose their numeric prefix."""
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


def _manuscript_keyword_terms(markdown: str) -> list[str]:
    text = _section_texts(markdown).get("Keywords", "")
    terms: list[str] = []
    for part in re.split(r"[,\n]", text):
        term = part.strip().strip(";").lstrip("-").strip()
        if term and "not available" not in term:
            terms.append(term)
    return terms


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _next_action(issue: str) -> str:
    """Turn a machine-readable blocking issue into a concrete next action."""
    if issue.startswith("simulated_evidence"):
        return ("Reframe the study explicitly as a simulation study (workshop/technical report), "
                "or run the claimed protocol on real benchmarks.")
    if issue.startswith("dataset_checksum_missing"):
        return "Record the dataset content checksum in the experiment spec and re-run the analysis."
    if issue.startswith("implementation_commit_missing"):
        return "Record the implementation commit hash and re-run the analysis."
    if issue.startswith("execution_artifact_missing"):
        return "Bind a completed execution result/worker job to the analyzed spec run before claiming empirical evidence."
    if issue.startswith("real_execution_not_verified"):
        return "Provide a verified execution identity (execution artifact + checksums), or label the study as a simulation."
    if issue.startswith("power_not_established"):
        return "Add a power analysis with a justified effect size and seed count."
    if issue.startswith("external_baselines_missing"):
        return "Add at least the venue-required number of external baselines and re-run."
    if issue.startswith("ablations_missing"):
        return "Run at least the venue-required number of ablations."
    if issue.startswith("related_work_coverage_below_minimum"):
        return "Expand the reviewed corpus and re-run the literature review before drafting."
    if issue.startswith("venue lint:"):
        return f"Fix or explicitly waive the venue lint violation: {issue.removeprefix('venue lint: ')}"
    if issue.startswith("body hygiene:"):
        return f"Remove the internal scaffolding from the body: {issue.removeprefix('body hygiene: ')}"
    if "quality" in issue.lower():
        return "Supply exemplar PDFs and revise the prose until the quality gate passes."
    return issue


def _parse_report_limitations(value: Any) -> list[str]:
    """Experiment-report limitations: JSON list for new reports, plain text
    for legacy rows. Never returns an invented entry."""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return [value] if value.strip() else []
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
        return [value] if value.strip() else []
    return []


def run_template_audit(
    markdown: str,
    *,
    complete: bool,
    literature_evidence: bool,
    figure_paths: tuple[str, ...] = (),
    inside_out_order: tuple[str, ...] | None = None,
) -> TemplateAudit:
    """Deterministic FastTrack template audit (fail-closed at the call site).

    ``complete`` manuscripts must satisfy every check; incomplete drafts are
    relaxed on the checks a template paper cannot honor without fabricated
    content (abstract length, keyword count, section order, title length) but
    never on the vague-title blocklist or Results objectivity."""
    params = _template_parameters()
    checks: list[dict[str, Any]] = []
    violations: list[str] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            violations.append(f"{name}: {detail}")

    sections = _section_texts(markdown)
    names = list(sections)
    title = _h1_title(markdown)
    lowered_title = title.lower()
    abstract_text = sections.get("Abstract", "")
    keywords = _manuscript_keyword_terms(markdown)
    results_text = sections.get("Results", "").lower()

    # Vague-title blocklist applies to every manuscript, complete or not.
    vague_hits = [
        term
        for term in params["title"]["vague_terms_blocklist"]
        if term in lowered_title
    ]
    record(
        "title_vague_blocklist",
        not vague_hits,
        f"vague terms found: {vague_hits}" if vague_hits else "no vague terms",
    )

    if complete:
        parts = list(params["abstract"]["parts"])
        missing_parts = [part for part in parts if part not in abstract_text]
        record(
            "abstract_six_parts",
            not missing_parts,
            f"missing parts: {missing_parts}"
            if missing_parts
            else f"all {len(parts)} labeled parts present",
        )
        words = _word_count(abstract_text)
        low, high = params["abstract"]["word_min"], params["abstract"]["word_max"]
        record(
            "abstract_word_count",
            low <= words <= high,
            f"{words} words (required {low}-{high})",
        )
        kw_min, kw_max = params["keywords"]["min"], params["keywords"]["max"]
        record(
            "keywords_count",
            kw_min <= len(keywords) <= kw_max,
            f"{len(keywords)} terms ({keywords}) (required {kw_min}-{kw_max})",
        )
        expected = ["Introduction"] + (
            ["Literature Review"] if literature_evidence else []
        ) + ["Methodology", "Results", "Discussion", "References", "Appendices"]
        body = [name for name in names if name in _BODY_SECTION_NAMES]
        record(
            "section_order",
            body == expected,
            f"sections in template order: {body} (expected {expected})",
        )
        missing_front = [
            name for name in ("Abstract", "Keywords", "Introduction") if name not in names
        ]
        if missing_front:
            record(
                "abstract_keywords_before_introduction",
                False,
                f"missing required section(s): {missing_front}",
            )
        else:
            record(
                "abstract_keywords_before_introduction",
                names.index("Abstract") < names.index("Introduction")
                and names.index("Keywords") < names.index("Introduction"),
                "Abstract and Keywords precede Introduction",
            )
        title_words = _word_count(title)
        record(
            "title_word_count",
            title_words <= params["title"]["max_words"],
            f"{title_words} words (max {params['title']['max_words']})",
        )
        title_keyword = [
            term
            for term in keywords
            if term
            and re.search(rf"\b{re.escape(term.lower())}\b", lowered_title)
        ]
        record(
            "title_contains_keyword",
            bool(title_keyword),
            f"title contains keyword(s): {title_keyword or 'none'}",
        )
        interpretation_hits = [
            term
            for term in params["results"]["interpretation_terms_blocklist"]
            if term in results_text
        ]
        record(
            "results_objective_only",
            not interpretation_hits,
            f"interpretation words in Results: {interpretation_hits}"
            if interpretation_hits
            else "no interpretation words in Results",
        )
        results_body = sections.get("Results", "")
        has_table = bool(
            re.search(r"^\|.+\|\s*$", results_body, re.MULTILINE)
            and re.search(r"^\|[\s:|-]+\|\s*$", results_body, re.MULTILINE)
        )
        record(
            "results_has_table",
            has_table,
            "Results contains a data table",
        )
        if figure_paths:
            record(
                "results_has_figure",
                any(
                    path in sections.get("Results", "")
                    or path.rsplit("/", 1)[-1] in sections.get("Results", "")
                    for path in figure_paths
                ),
                f"Results references figure(s): {list(figure_paths)}",
            )
        discussion_text = sections.get("Discussion", "")
        limitations_block = ""
        limitation_match = re.search(
            r"### Study Limitations[^\n]*\n(.*?)(?=\n### |\Z)",
            discussion_text,
            re.DOTALL,
        )
        if limitation_match:
            limitations_block = limitation_match.group(1)
        limitation_count = len(
            re.findall(r"^\d+\.\s", limitations_block, re.MULTILINE)
        )
        record(
            "discussion_limitations_at_least_3",
            limitation_count >= params["limitations"]["min"],
            f"{limitation_count} numbered limitations (required >= {params['limitations']['min']})",
        )
        missing_discussion_parts = [
            part
            for part in (
                "### Recap of what we found",
                "### Strengths of this study",
                "### Implications for future research",
            )
            if part not in discussion_text
        ]
        record(
            "discussion_structure",
            not missing_discussion_parts,
            f"missing Discussion parts: {missing_discussion_parts}"
            if missing_discussion_parts
            else "recap, strengths, implications present",
        )
        speculative_label = re.search(
            r"^### [^\n]*[Pp]ossible [Ii]nterpretations[^\n]*[Ff]uture[^\n]*$",
            discussion_text,
            re.MULTILINE,
        )
        record(
            "discussion_speculation_labeled",
            bool(speculative_label),
            "speculative content confined to the labeled 'Possible interpretations and future work' part",
        )
    else:
        record(
            "incomplete_draft_relaxed",
            True,
            "incomplete draft: template checks that require fabricated content are skipped; "
            "the manuscript stays visibly incomplete",
        )

    if inside_out_order is not None:
        constrained = ["Methodology", "Results", "Discussion", "Introduction", "Abstract", "Keywords", "Title"]
        positions = {name: inside_out_order.index(name) for name in constrained if name in inside_out_order}
        ordered = all(
            positions[a] < positions[b]
            for a, b in zip(constrained, constrained[1:])
            if a in positions and b in positions
        )
        record(
            "inside_out_order",
            ordered,
            f"emission order {list(inside_out_order)} places methodology→results→discussion "
            "before introduction→abstract→keywords→title",
        )

    return TemplateAudit(checks=checks, violations=violations, inside_out_order=inside_out_order)


class ManuscriptService:
    def __init__(
        self,
        repository: ResearchRepository,
        router=None,
        writer_mode: str = "deterministic",
        venue_profile: str = "generic_journal",
        download_root: Path | None = None,
    ) -> None:
        self.repository = repository
        self.router = router
        # "deterministic" (default): field-assembled scaffold, zero model
        # calls. "model": model-assisted scientific argument through the
        # router, gated by the evidence ledger and the publication readiness
        # gate. A deterministic draft is never labelled submission-ready.
        self.writer_mode = (
            writer_mode
            if writer_mode in ("deterministic", "model")
            else "deterministic"
        )
        self.venue: VenueProfile = get_venue_profile(venue_profile)
        self.download_root = Path(download_root).expanduser().resolve() if download_root else None

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    @staticmethod
    def _delivery_state(
        checklist: dict[str, Any] | None, manuscript_status: str
    ) -> tuple[str, bool]:
        """One invariant for DB status, checklist, and returned JSON.

        ``complete`` requires ``submission_ready``; a stale/edited checklist
        that says otherwise is downgraded to draft and reported as not ready.
        """
        status = str((checklist or {}).get("status") or manuscript_status)
        ready = bool((checklist or {}).get("submission_ready"))
        if status == "complete" and not ready:
            status = "draft"
        if status != "complete":
            ready = False
        return status, ready

    async def generate(self, project_id: str) -> dict[str, Any]:
        project = self.repository.get_project(project_id)
        if not project:
            raise ValueError(f"project_not_found: project '{project_id}' not found")
        lineage, gaps = self._resolve_lineage(project_id)
        from autoresearch.application.progress import report as _progress

        _progress(
            "manuscript.steps", 1, 5, detail="lineage resolved",
            remaining=["evidence bundle", "draft", "audit", "manifest"],
        )
        run = self.repository.create_run(
            project_id, PipelineKind.MANUSCRIPT, {"lineage": lineage}
        )
        try:
            manuscript = await self._build(project, run.id, lineage, gaps)
            self.repository.save_manuscript(manuscript)
            self.repository.finish_run(run.id)
            root = Path(manuscript.markdown_path).parent

            def _sidecar(name: str) -> tuple[dict[str, Any] | None, str | None]:
                path = root / name
                if not path.is_file():
                    return None, None
                try:
                    return json.loads(path.read_text(encoding="utf-8")), str(path)
                except (OSError, ValueError):
                    return None, str(path)

            readiness_payload, readiness_path = _sidecar("readiness_report.json")
            contract_payload, contract_path = _sidecar("contribution_contract.json")
            synthesis_payload, synthesis_path = _sidecar("literature_synthesis.json")
            review_payload, review_path = _sidecar("review_report.json")
            venue_lint_payload, venue_lint_path = _sidecar("venue_lint.json")
            hygiene_payload, hygiene_path = _sidecar("body_hygiene.json")
            ledger_payload, ledger_path = _sidecar("claim_ledger.json")
            publication_gaps_payload, publication_gaps_path = _sidecar("publication_gaps.json")
            checklist_payload, checklist_path = _sidecar("submission_checklist.json")
            quality, quality_path = _sidecar("quality_report.json")
            if quality is None:
                quality = {
                    "kind": "quality_report",
                    "gate": {
                        "passed": False,
                        "verdict": "MISSING",
                        "reason": "quality_report.json is missing; failing closed",
                    },
                    "path": None,
                }
            elif quality_path:
                quality["path"] = quality_path

            # One authoritative decision: the persisted checklist computed by
            # _build from every gate. generate() must never invent a different
            # boolean from a subset of the gates.
            effective_status, submission_ready = self._delivery_state(
                checklist_payload, manuscript.status
            )
            if checklist_payload is not None and (
                checklist_payload.get("status") != effective_status
                or bool(checklist_payload.get("submission_ready")) != submission_ready
            ):
                checklist_payload["status"] = effective_status
                checklist_payload["submission_ready"] = submission_ready
                if checklist_path:
                    Path(checklist_path).write_text(
                        json.dumps(checklist_payload, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
            if effective_status != manuscript.status:
                self.repository.execute(
                    "UPDATE manuscripts SET status=? WHERE id=?",
                    (effective_status, manuscript.id),
                )
            template_audit: dict[str, Any] = {
                "passed": None,
                "violations": [],
                "path": None,
            }
            markdown_text = Path(manuscript.markdown_path).read_text(encoding="utf-8")
            audit_file = Path(manuscript.markdown_path).with_name("template_audit.json")
            if audit_file.is_file():
                try:
                    audit_payload = json.loads(audit_file.read_text(encoding="utf-8"))
                    template_audit = {
                        "passed": bool(audit_payload.get("passed")),
                        "violations": list(audit_payload.get("violations") or []),
                        "checks": list(audit_payload.get("checks") or []),
                        "inside_out_order": list(audit_payload.get("inside_out_order") or []),
                        "path": str(audit_file),
                    }
                except (ValueError, OSError):
                    pass
            return {
                "manuscript_id": manuscript.id,
                "project_id": project_id,
                "run_id": run.id,
                "status": effective_status,
                "title": manuscript.title,
                "decision": manuscript.decision,
                "keywords": _manuscript_keyword_terms(markdown_text),
                "template_audit": template_audit,
                "lineage": lineage,
                "evidence_gaps": json.loads(manuscript.gaps_json),
                "submission_ready": submission_ready,
                "readiness": readiness_payload,
                "contribution_contract": contract_payload,
                "literature_synthesis": synthesis_payload,
                "review": review_payload,
                "venue_lint": venue_lint_payload,
                "body_hygiene": hygiene_payload,
                "claim_ledger": ledger_payload,
                "publication_gaps": publication_gaps_payload,
                "submission_checklist": checklist_payload,
                "quality": quality,
                "artifacts": {
                    "markdown": manuscript.markdown_path,
                    "latex": manuscript.latex_path,
                    "bibtex": manuscript.bibtex_path,
                    "evidence_bundle": manuscript.evidence_bundle_path,
                    "manifest": manuscript.manifest_path,
                    "pdf": manuscript.pdf_path,
                    **(
                        {"quality_report": quality["path"]}
                        if quality.get("path")
                        else {}
                    ),
                    **(
                        {"template_audit": str(audit_file)}
                        if audit_file.is_file()
                        else {}
                    ),
                    **({"readiness_report": readiness_path} if readiness_path else {}),
                    **({"contribution_contract": contract_path} if contract_path else {}),
                    **({"literature_synthesis": synthesis_path} if synthesis_path else {}),
                    **({"review_report": review_path} if review_path else {}),
                    **({"venue_lint": venue_lint_path} if venue_lint_path else {}),
                    **({"body_hygiene": hygiene_path} if hygiene_path else {}),
                    **({"claim_ledger": ledger_path} if ledger_path else {}),
                    **({"publication_gaps": publication_gaps_path} if publication_gaps_path else {}),
                    **({"submission_checklist": checklist_path} if checklist_path else {}),
                },
            }
        except BaseException as exc:
            self.repository.finish_run(
                run.id, error=redact_text(f"{type(exc).__name__}: {exc}")
            )
            raise

    # ------------------------------------------------------------------
    # Lineage resolution (exact chain, no arbitrary "latest" mixing)
    # ------------------------------------------------------------------

    def _resolve_lineage(self, project_id: str) -> tuple[dict[str, Any], list[str]]:
        gaps: list[str] = []
        gap_row = self.repository.row(
            "SELECT g.* FROM research_gaps g JOIN pipeline_runs r ON r.id = g.run_id "
            "WHERE g.project_id=? AND r.status=? ORDER BY g.rowid DESC LIMIT 1",
            (project_id, RunStatus.COMPLETED.value),
        )
        if gap_row is None:
            gaps.append("review: no research gap from a completed review run")
            return {"project_id": project_id}, gaps

        portfolio = None
        for row in self.repository.novelty_portfolios_for_project(project_id):
            candidate_id = (
                row.low_risk_candidate_id
                or row.balanced_candidate_id
                or row.high_risk_candidate_id
            )
            candidate = (
                self.repository.get_novelty_candidate(str(candidate_id))
                if candidate_id
                else None
            )
            if candidate and str(candidate.gap_id) == str(gap_row["id"]):
                portfolio = row
                break
        if portfolio is None:
            gaps.append("novelty: no portfolio for the current research question")

        freeze = self.repository.latest_methodology_freeze(project_id)
        if freeze is not None:
            frozen_candidate = self.repository.get_novelty_candidate(
                freeze.candidate_id
            )
            if frozen_candidate is None or str(frozen_candidate.gap_id) != str(
                gap_row["id"]
            ):
                gaps.append(
                    "methodology: frozen methodology targets a different research question"
                )
                freeze = None
        elif portfolio is not None:
            gaps.append(
                "methodology: no frozen methodology for the portfolio's candidate"
            )

        spec = None
        analysis = None
        report = None
        if freeze is not None:
            spec = self.repository.get_experiment_spec(freeze.candidate_id)
            if spec is None:
                gaps.append("experiment: no specification for the frozen methodology")
            elif str(spec.methodology_freeze_id) != str(freeze.id):
                gaps.append(
                    "experiment: specification was planned against a different methodology version"
                )
                spec = None
        if spec is not None:
            analysis = self.repository.get_statistical_analysis(spec.id)
            if analysis is None:
                gaps.append(
                    "experiment: no validated statistical analysis (measurement ingestion/analysis pending)"
                )
        if spec is not None:
            report = (
                self.repository.row(
                    "SELECT * FROM experiment_reports WHERE analysis_id=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (analysis.id,),
                )
                if analysis is not None
                else None
            )
            if report is None:
                gaps.append("experiment: no report for the analyzed specification")

        return {
            "project_id": project_id,
            "gap_id": str(gap_row["id"]),
            "review_run_id": str(gap_row["run_id"]),
            "portfolio_id": str(portfolio.id) if portfolio else None,
            "candidate_id": freeze.candidate_id if freeze else None,
            "methodology_freeze_id": freeze.id if freeze else None,
            "methodology_version": freeze.version if freeze else None,
            "spec_id": spec.id if spec else None,
            "analysis_id": analysis.id if analysis else None,
            "report_id": str(report["id"]) if report else None,
        }, gaps

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def _build(
        self, project, run_id: str, lineage: dict[str, Any], gaps: list[str]
    ) -> ManuscriptRecord:
        manuscript_id = uuid4().hex
        root = self.repository.artifact_root / "manuscripts" / manuscript_id
        root.mkdir(parents=True, exist_ok=True)

        gap = (
            self.repository.row(
                "SELECT * FROM research_gaps WHERE id=?", (lineage["gap_id"],)
            )
            if lineage.get("gap_id")
            else None
        )
        lineage_papers = self._lineage_papers(gap)
        # The literature review must synthesize the reviewed corpus, not only
        # the gap's supporting set. Lineage papers stay first so the gap is
        # never detached from its own evidence.
        review_run_id = str(lineage.get("review_run_id") or "") or None
        corpus_papers = self._corpus_papers(
            project.id, lineage_papers, review_run_id=review_run_id
        )
        papers = corpus_papers or lineage_papers
        claims = self._lineage_claims(gap)
        counter_claims = self._counter_claims(gap)
        review_notes = self._paper_review_notes(papers, review_run_id=review_run_id)
        candidate = (
            self.repository.get_novelty_candidate(lineage["candidate_id"])
            if lineage.get("candidate_id")
            else None
        )
        methodology = self._methodology_record(lineage)
        if methodology is None and lineage.get("methodology_freeze_id"):
            gaps.append("methodology: frozen artifact unreadable")
        spec = (
            self.repository.get_experiment_spec(lineage["candidate_id"])
            if lineage.get("candidate_id")
            else None
        )
        analysis = self.repository.get_statistical_analysis(spec.id) if spec else None
        report = (
            self.repository.row(
                "SELECT * FROM experiment_reports WHERE id=?", (lineage["report_id"],)
            )
            if lineage.get("report_id")
            else None
        )
        seed = self.repository.row(
            "SELECT topic, research_question FROM seed_papers WHERE project_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (project.id,),
        )

        complete = not gaps and analysis is not None and report is not None
        decision = str(report["decision"]) if report else "INCOMPLETE"

        keys = {
            paper["id"]: f"paper{index}" for index, paper in enumerate(papers, start=1)
        }
        bib = self._bibtex(papers, keys)
        tables_md, tables_tex = self._tables(analysis)
        keywords = self._keywords(project.id, papers, gap=gap, candidate=candidate, spec=spec)

        # Publication readiness comes BEFORE prose. It decides whether this is
        # an empirical submission, a workshop paper, a simulation report, or
        # not ready at all. The gate never turns a simulation into evidence.
        readiness = assess_publication_readiness(
            project_id=project.id,
            spec=spec,
            analysis=analysis,
            report=report,
            papers=papers,
            corpus_papers=corpus_papers or papers,
            venue=self.venue,
            execution_verified=self._execution_verified(spec),
        )
        # Freeze-time publication gaps are a shared-contract slice; a missing
        # baseline/power/delta at freeze must still block a submission.
        freeze_gaps = [
            gap for gap in (methodology or {}).get("publication_gaps", [])
            if isinstance(gap, dict)
        ]
        shared_ready = not any(gap.get("fatal", True) for gap in freeze_gaps)
        synthesis = build_literature_synthesis(
            self.repository,
            project.id,
            gap=gap,
            candidate=candidate,
            spec=spec,
            keys=keys,
            limit=24,
            review_run_id=review_run_id,
        )
        contract = build_contribution_contract(
            gap=gap,
            candidate=candidate,
            spec=spec,
            analysis=analysis,
            report=report,
            synthesis=synthesis,
            readiness=readiness,
            keywords=keywords,
            seed=seed,
            venue=self.venue,
        )
        ledger = build_claim_ledger(
            gap=gap,
            candidate=candidate,
            spec=spec,
            analysis=analysis,
            report=report,
            papers=papers,
            review_notes=review_notes,
            keys=keys,
            tables_md=tables_md,
            decision=decision,
        )

        # Inside-Out Writing Method (template-spec.json T13-T18, T130-T131):
        # methods → results → discussion → introduction → abstract → title.
        # The emission order is recorded in template_audit.json and checked.
        inside_out: list[str] = []
        figure_svg, figure_tikz = self._figures(analysis, root)
        methodology_lines = self._methodology_markdown(
            methodology, candidate, spec, lineage, complete, gaps
        )
        inside_out.append("Methodology")
        results_lines = self._results_markdown(
            analysis, report, tables_md, figure_svg, complete
        )
        inside_out.append("Results")
        limitations = self._study_limitations(
            report, methodology, spec, analysis, readiness=readiness
        )
        discussion_lines = self._discussion_markdown(
            decision, report, limitations, gap, methodology, spec, analysis,
            review_notes, papers, complete, gaps,
            synthesis=synthesis, contract=contract, readiness=readiness,
        )
        conclusion_lines: list[str] = []
        if self.venue.template != "fasttrack":
            conclusion_lines += [
                "## Conclusion",
                "",
                self._sanitize(contract.takeaway) or f"The preregistered decision is {decision}.",
                "",
            ]
            for statement in self.venue.required_statements:
                if statement.lower() == "limitations":
                    continue
                conclusion_lines += [f"### {statement}", ""]
                if statement.lower() == "data and code availability":
                    conclusion_lines += [
                        "The evidence bundle, manifest, exact commands, and tables are "
                        "stored with this manuscript; restricted data are not redistributed.",
                        "",
                    ]
                elif statement.lower() == "broader impact":
                    conclusion_lines += [
                        "The study evaluates a computational method; its broader impact "
                        "is bounded by the limitations and scope stated in the Discussion.",
                        "",
                    ]
                elif statement.lower() == "ethics":
                    conclusion_lines += [
                        "Ethical considerations are recorded in the Methodology; fields "
                        "not specified at freeze time are reported as limitations rather "
                        "than invented.",
                        "",
                    ]
                else:
                    conclusion_lines += [
                        f"{statement} is reported in the evidence bundle and the Discussion.",
                        "",
                    ]
        inside_out.append("Discussion")
        introduction_lines = self._introduction_markdown(
            gap, counter_claims, review_notes, candidate, spec, seed, complete, gaps,
            synthesis=synthesis,
        )
        inside_out.append("Introduction")
        literature_lines = synthesis.to_markdown_lines() if synthesis.papers else []
        inside_out.append("Literature Review")
        abstract_lines = self._abstract_markdown(
            gap, counter_claims, candidate, methodology, spec, analysis, report,
            complete, gaps, seed, contract=contract, synthesis=synthesis,
        )
        inside_out.append("Abstract")
        inside_out.append("Keywords")
        title = self._title(candidate, complete, keywords, contract=contract)
        inside_out.append("Title")
        references_lines = self._references_markdown(papers, keys)
        appendices_lines = self._appendices_markdown(
            project, lineage, papers, claims, analysis, report, complete, gaps
        )

        markdown = "\n".join(
            [
                f"# {title}",
                "",
                *abstract_lines,
                *self._keywords_lines(keywords, complete),
                *introduction_lines,
                *literature_lines,
                *methodology_lines,
                *results_lines,
                *discussion_lines,
                *conclusion_lines,
                *references_lines,
                *appendices_lines,
            ]
        )
        latex = self._latex(
            title=title,
            papers=papers,
            keys=keys,
            tables_tex=tables_tex,
            figure_tikz=figure_tikz,
            analysis=analysis,
            report=report,
            gap=gap,
            counter_claims=counter_claims,
            candidate=candidate,
            methodology=methodology,
            spec=spec,
            limitations=limitations,
            keywords=keywords,
            gaps=gaps,
            complete=complete,
            claims=claims,
            review_notes=review_notes,
            seed=seed,
            lineage=lineage,
        )

        bundle = self._evidence_bundle(
            lineage, papers, claims, analysis, report, keywords=keywords,
            limitations=limitations, exact_commands=self._exact_commands(report),
            figure_paths=([figure_svg] if figure_svg else []),
        )
        bundle_path = root / "evidence_bundle.json"
        bundle_path.write_text(
            json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
        )
        markdown_path = root / "manuscript.md"
        markdown_path.write_text(markdown, encoding="utf-8")
        latex_path = root / "manuscript.tex"
        latex_path.write_text(latex, encoding="utf-8")
        bibtex_path = root / "references.bib"
        bibtex_path.write_text(bib, encoding="utf-8")
        (root / "tables").mkdir(exist_ok=True)
        (root / "tables" / "results.md").write_text(tables_md, encoding="utf-8")
        (root / "tables" / "results.tex").write_text(tables_tex, encoding="utf-8")

        # Gate artifacts are written before prose is judged so every later
        # stage (writer, reviewer, human) sees the same exact evidence.
        (root / "readiness_report.json").write_text(
            json.dumps(readiness.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        (root / "contribution_contract.json").write_text(
            json.dumps(contract.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        (root / "literature_synthesis.json").write_text(
            json.dumps(synthesis.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        (root / "claim_ledger.json").write_text(
            json.dumps(ledger.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        (root / "publication_gaps.json").write_text(
            json.dumps({"gaps": freeze_gaps}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        from autoresearch.application.progress import report as _progress

        _progress(
            "manuscript.steps", 2, 5, detail="evidence bundle and draft files written",
            remaining=["template audit", "manifest", "compile"],
        )

        self._verify_citations(markdown, latex, bib, keys)
        if complete:
            self._verify_consistency(markdown, decision)
        if self.venue.template == "fasttrack":
            template_audit = run_template_audit(
                markdown,
                complete=complete,
                literature_evidence=bool(papers or claims),
                figure_paths=tuple([figure_svg] if figure_svg else []),
                inside_out_order=tuple(inside_out),
            )
            if complete and not template_audit.passed:
                # Fail-closed: a template violation in the deterministic draft
                # is a generation bug or a sparse evidence chain — never
                # silently shipped as a complete manuscript.
                raise ValueError(
                    "manuscript_template_error: deterministic draft violates the FastTrack "
                    f"template: {'; '.join(template_audit.violations)}"
                )
        else:
            # Venue profiles replace the FastTrack checklist as the format
            # gate; the venue lint is run after the shipped text is final.
            template_audit = TemplateAudit(
                checks=[{
                    "check": "venue_profile",
                    "ok": True,
                    "detail": f"FastTrack audit skipped; venue profile '{self.venue.name}' is the format gate",
                }],
                violations=[],
            )
        audit_path = root / "template_audit.json"
        audit_path.write_text(
            json.dumps(
                {
                    "kind": "template_audit",
                    "template": (
                        "FastTrack Academic Research Paper Template"
                        if self.venue.template == "fasttrack"
                        else self.venue.name
                    ),
                    "spec": (
                        str(TEMPLATE_SPEC_PATH)
                        if self.venue.template == "fasttrack"
                        else None
                    ),
                    "passed": template_audit.passed,
                    "violations": template_audit.violations,
                    "checks": template_audit.checks,
                    "inside_out_order": inside_out,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # Model-assisted scientific writing (optional): a model drafts the
        # argument; a deterministic audit keeps numerical/citation authority
        # and template compliance. Surviving violations fall back to the
        # deterministic draft.
        writer_report: dict[str, Any] = {
            "mode": "deterministic",
            "used_model_draft": False,
            "audit": None,
            "template_audit": {
                "passed": template_audit.passed,
                "violations": template_audit.violations,
            },
        }
        if complete and self.writer_mode == "model" and self.router is not None:
            writer_report = await self._model_draft(
                root=root,
                project=project,
                markdown=markdown,
                analysis=analysis,
                spec=spec,
                papers=papers,
                keys=keys,
                gap=gap,
                counter_claims=counter_claims,
                methodology=methodology,
                report=report,
                gaps=gaps,
                tables_md=tables_md,
                decision=decision,
                limitations=limitations,
                keywords=keywords,
                figure_svg=figure_svg,
                review_notes=review_notes,
                seed=seed,
                contract=contract,
                synthesis=synthesis,
                ledger=ledger,
            )
            if writer_report.get("used_model_draft") and writer_report.get(
                "markdown_path"
            ):
                candidate_path = Path(str(writer_report["markdown_path"]))
                if self.venue.template == "fasttrack":
                    shipped_audit = run_template_audit(
                        candidate_path.read_text(encoding="utf-8"),
                        complete=True,
                        literature_evidence=bool(papers or claims),
                        figure_paths=tuple([figure_svg] if figure_svg else []),
                    )
                else:
                    shipped_audit = TemplateAudit(
                        checks=[{
                            "check": "venue_profile",
                            "ok": True,
                            "detail": f"venue profile '{self.venue.name}' is the format gate",
                        }],
                        violations=[],
                    )
                if not shipped_audit.passed:
                    # The sidecar audit must describe the SHIPPED text; a model
                    # draft that fails the re-audit is discarded and the
                    # deterministic draft restored — never shipped silently.
                    writer_report["used_model_draft"] = False
                    writer_report["fallback"] = "deterministic"
                    writer_report["reason"] = (
                        "template audit failed on the final model draft; "
                        "deterministic draft restored"
                    )
                    markdown_path.write_text(markdown, encoding="utf-8")
                    writer_report["markdown_path"] = None
                else:
                    markdown = candidate_path.read_text(encoding="utf-8")
                    self._verify_citations(markdown, latex, bib, keys)
                    self._verify_consistency(markdown, decision)
                    template_audit = shipped_audit
                    writer_report["template_audit"] = {
                        "audited": "shipped manuscript.md (model draft)",
                        "passed": shipped_audit.passed,
                        "violations": shipped_audit.violations,
                        "checks": shipped_audit.checks,
                    }
                    # template_audit.json always describes the shipped text.
                    audit_path.write_text(
                        json.dumps(
                            {
                                "kind": "template_audit",
                                "template": (
                                    "FastTrack Academic Research Paper Template"
                                    if self.venue.template == "fasttrack"
                                    else self.venue.name
                                ),
                                "spec": (
                                    str(TEMPLATE_SPEC_PATH)
                                    if self.venue.template == "fasttrack"
                                    else None
                                ),
                                "audited": "shipped manuscript.md (model draft, writer mode)",
                                "passed": shipped_audit.passed,
                                "violations": shipped_audit.violations,
                                "checks": shipped_audit.checks,
                                "inside_out_order": inside_out,
                            },
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
            audit_path = root / "writing_audit.json"
            audit_path.write_text(
                json.dumps(writer_report, indent=2, default=str), encoding="utf-8"
            )

        # The shipped text is re-audited after an optional model draft. Body
        # hygiene, venue lint, the deterministic reviewer panel, and the prose
        # quality report all describe the same text the user receives.
        hygiene = audit_body(markdown)
        venue_lint_result = venue_lint(markdown, self.venue)
        review = deterministic_review(
            markdown,
            readiness=readiness,
            contract=contract,
            synthesis=synthesis,
            hygiene=hygiene,
            venue_lint_result=venue_lint_result,
            profile=self.venue,
            has_exact_commands=bool(self._exact_commands(report)),
        )
        quality = self._quality_report(
            SimpleNamespace(markdown_path=str(markdown_path)), project.id
        )
        quality_passed = bool((quality.get("gate") or {}).get("passed"))
        submission_ready = bool(
            complete
            and readiness.submission_ready
            and shared_ready
            and quality_passed
            and review.passed
            and venue_lint_result.passed
            and hygiene.passed
            and template_audit.passed
        )
        status = "complete" if submission_ready else ("draft" if complete else "incomplete")
        (root / "venue_lint.json").write_text(
            json.dumps(venue_lint_result.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        (root / "body_hygiene.json").write_text(
            json.dumps(hygiene.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        (root / "review_report.json").write_text(
            json.dumps(review.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        next_actions = list(dict.fromkeys(
            _next_action(issue)
            for issue in [*readiness.blocking_issues, *review.required_fixes]
        ))
        if not next_actions:
            next_actions = ["Author review, then venue submission checks."]
        (root / "submission_checklist.json").write_text(
            json.dumps(
                {
                    "kind": "submission_checklist",
                    "venue": self.venue.to_dict(),
                    "status": status,
                    "submission_ready": submission_ready,
                    "readiness": readiness.readiness,
                    "blocking_issues": list(readiness.blocking_issues),
                    "publication_gaps": list(freeze_gaps),
                    "required_fixes": list(review.required_fixes),
                    "quality_gate": quality.get("gate"),
                    "venue_lint_passed": venue_lint_result.passed,
                    "body_hygiene_passed": hygiene.passed,
                    "next_actions": next_actions,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        from autoresearch.application.progress import report as _progress

        _progress(
            "manuscript.steps", 4, 5, detail="template, venue, hygiene, and reviewer gates complete",
            remaining=["manifest", "compile"],
        )

        # Compile before hashing so the PDF (and any compiler sidecars) are
        # covered by the manifest, not written after it.
        pdf_path = self._compile(latex_path) if complete else None
        manifest = self._manifest(root, bundle)
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )

        from autoresearch.application.progress import report as _progress

        _progress(
            "manuscript.steps", 5, 5,
            detail=f"manifest written; pdf {'compiled' if pdf_path else 'not compiled (no verified compiler)'}",
            remaining=[],
        )

        provenance = ArtifactProvenance.create(
            project_id=project.id,
            run_id=run_id,
            source_evidence=[
                value
                for value in (
                    lineage.get("report_id"),
                    lineage.get("analysis_id"),
                    lineage.get("spec_id"),
                    lineage.get("methodology_freeze_id"),
                    lineage.get("gap_id"),
                )
                if value
            ],
            provider="deterministic",
            model="manuscript-writer",
            prompt_version=MANUSCRIPT_PROMPT_VERSION,
            content={
                "status": status,
                "submission_ready": submission_ready,
                "readiness": readiness.readiness,
                "decision": decision,
                "gaps": gaps,
            },
        )
        return ManuscriptRecord(
            id=manuscript_id,
            project_id=project.id,
            run_id=run_id,
            title=title,
            status=status,
            decision=decision,
            lineage_json=json.dumps(lineage, sort_keys=True),
            evidence_bundle_path=str(bundle_path),
            markdown_path=str(markdown_path),
            latex_path=str(latex_path),
            bibtex_path=str(bibtex_path),
            manifest_path=str(manifest_path),
            pdf_path=str(pdf_path) if pdf_path else None,
            gaps_json=json.dumps(gaps),
            provenance=provenance,
        )

    # ------------------------------------------------------------------
    # Quality report (prose audit + exemplar parity, 2026-10-16)
    # ------------------------------------------------------------------

    def _exemplar_texts(self, project_id: str, *, limit: int = 5) -> list[str]:
        """Texts of the top-reviewed papers whose PDFs resolve locally.

        Ranking: paper_reviews.confidence desc (deterministic tie-break by
        paper id). PDFs resolve from the content-addressed downloads tree
        (``.autoresearch/data/downloads/**``) by checksum prefix; papers
        without a local PDF are skipped honestly (they contribute nothing).
        """
        try:
            rows = self.repository.rows(
                "SELECT pr.paper_id AS paper_id, p.checksum AS checksum, "
                "d.path AS download_path, d.checksum AS download_checksum "
                "FROM paper_reviews pr "
                "JOIN papers p ON p.id = pr.paper_id "
                "LEFT JOIN download_records d ON d.paper_id = p.id AND d.status = 'downloaded' "
                "WHERE pr.project_id=? "
                "ORDER BY pr.confidence DESC, pr.paper_id ASC",
                (project_id,),
            )
        except Exception:  # noqa: BLE001 - table/column absence is an honest empty set
            return []
        if not rows:
            return []
        roots: list[Path] = []
        if self.download_root is not None:
            roots.append(self.download_root)
        roots.extend([
            Path(self.repository.artifact_root).parent / "downloads",
            Path(self.repository.artifact_root) / "downloads",
            Path(".autoresearch/data/downloads").resolve(),
        ])
        database_path = getattr(self.repository, "path", None)
        if database_path is not None:
            roots.append(Path(database_path).parent / "downloads")
        downloads_roots = list(dict.fromkeys(root for root in roots if root.is_dir()))
        if not downloads_roots:
            return []
        from autoresearch.ingestion.document_parser import DocumentParser

        parser = DocumentParser()
        texts: list[str] = []
        for row in rows:
            if len(texts) >= limit:
                break
            candidates: list[Path] = []
            download_path = str(row.get("download_path") or "")
            if download_path and Path(download_path).is_file():
                candidates = [Path(download_path)]
            if not candidates:
                checksum = str(row.get("download_checksum") or row["checksum"] or "")
                if len(checksum) < 12:
                    continue
                for downloads in downloads_roots:
                    candidates = list(downloads.glob(f"**/{checksum[:12]}*.pdf")) or list(
                        downloads.glob(f"{checksum[:32]}/*.pdf")
                    )
                    if candidates:
                        break
            if not candidates:
                continue
            try:
                parsed = parser.parse_seed(str(candidates[0]))
            except Exception:  # noqa: BLE001 - unparseable exemplar PDF is skipped, never fatal
                continue
            pages = [page.text for page in parsed.pages[:40] if page.text]
            if pages:
                texts.append("\n".join(pages))
        return texts

    def _quality_report(self, manuscript, project_id: str) -> dict[str, Any]:
        """Deterministic prose audit + exemplar parity for the generated
        manuscript, persisted as quality_report.json beside the artifacts.
        INCONCLUSIVE verdicts (no exemplars / no text) are recorded honestly
        and never treated as passes."""
        from autoresearch.application.exemplars import ExemplarExtractor, parity_check
        from autoresearch.application.prose_style import prose_audit

        report: dict[str, Any] = {
            "kind": "quality_report",
            "heuristic": "deterministic",
        }
        try:
            markdown = Path(manuscript.markdown_path).read_text(encoding="utf-8")
        except OSError as exc:
            return {
                **report,
                "prose_audit": {
                    "verdict": "INCONCLUSIVE",
                    "reason": f"unreadable_markdown:{type(exc).__name__}",
                },
                "gate": {
                    "passed": False,
                    "verdict": "INCONCLUSIVE",
                    "reason": "unreadable manuscript",
                },
            }

        exemplar_texts = self._exemplar_texts(project_id)
        audit = prose_audit(markdown, exemplar_texts)
        guidance = (
            list(audit.rewrite_guidance)
            if not isinstance(audit.rewrite_guidance, str)
            else [audit.rewrite_guidance]
        )
        report["prose_audit"] = {
            "verdict": audit.verdict,
            "heuristic": audit.heuristic,
            "tell_density": audit.metrics.tell_density if audit.metrics else None,
            "tells": [dict(t) for t in audit.tells],
            "band_deviations": [dict(d) for d in audit.band_deviations],
            "rewrite_guidance": guidance,
            "notes": list(audit.notes),
        }
        if exemplar_texts:
            extractor = ExemplarExtractor()
            profile = extractor.profile(
                [
                    extractor.features(f"paper_{index}", text)
                    for index, text in enumerate(exemplar_texts)
                ]
            )
            parity = parity_check(markdown, profile)
            report["exemplar_parity"] = {
                "parity": parity.parity,
                "n_exemplars": len(exemplar_texts),
                "gaps": [dict(g) for g in parity.gaps],
                "guidance": list(parity.guidance),
                "notes": list(parity.notes),
            }
        else:
            report["exemplar_parity"] = {
                "parity": False,
                "n_exemplars": 0,
                "notes": ["no_exemplar_pdfs_resolved: parity not assessed"],
            }
        parity_passed = bool(
            report["exemplar_parity"].get("parity")
        ) if exemplar_texts else False
        gate_passed = audit.verdict == "PASS" and parity_passed
        if gate_passed:
            gate_reason = "prose audit and exemplar parity passed"
        elif not exemplar_texts:
            gate_reason = "no exemplar PDFs resolved; style evidence is absent"
        elif audit.verdict != "PASS":
            gate_reason = "prose audit did not pass"
        else:
            gate_reason = "exemplar parity gaps remain"
        report["gate"] = {
            "passed": gate_passed,
            "verdict": audit.verdict,
            "parity": parity_passed,
            "reason": gate_reason,
        }
        try:
            out_path = Path(manuscript.markdown_path).with_name("quality_report.json")
            out_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            report["path"] = str(out_path)
        except OSError:
            report["path"] = None  # reported in-memory; write failure disclosed
        return report

    # ------------------------------------------------------------------
    # Evidence accessors
    # ------------------------------------------------------------------

    def _lineage_papers(self, gap) -> list[dict[str, Any]]:
        if gap is None:
            return []
        ids = [
            str(value)
            for value in json.loads(gap.get("supporting_paper_ids_json") or "[]")
        ]
        papers: list[dict[str, Any]] = []
        for paper_id in ids:
            row = self.repository.row("SELECT * FROM papers WHERE id=?", (paper_id,))
            if row:
                papers.append(row)
        return papers

    def _execution_verified(self, spec) -> bool:
        """True only when a durable execution artifact is bound to the spec run."""
        if spec is None:
            return False
        try:
            row = self.repository.row(
                "SELECT COUNT(*) AS n FROM execution_results WHERE run_id=?",
                (spec.run_id,),
            )
            return bool(row and int(row["n"]) > 0)
        except Exception:  # noqa: BLE001 - absent table/column is honestly unverified
            return False

    def _corpus_papers(
        self,
        project_id: str,
        lineage_papers: list[dict[str, Any]],
        limit: int = 24,
        review_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """The *reviewed* corpus for the literature synthesis.

        Downloaded-but-unreviewed papers are not counted as related-work
        coverage. Lineage papers come first; the rest are ordered by citation
        count and recency.
        """
        limit = max(limit, len(lineage_papers))
        rows = self.repository.rows(
            "SELECT p.* FROM papers p JOIN paper_reviews pr ON pr.paper_id=p.id "
            "WHERE p.project_id=? AND (? IS NULL OR pr.run_id=?) "
            "GROUP BY p.id ORDER BY p.citation_count DESC, p.year DESC, p.title ASC LIMIT ?",
            (project_id, review_run_id, review_run_id, limit),
        )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for paper in lineage_papers:
            paper_id = str(paper.get("id"))
            if paper_id not in seen:
                seen.add(paper_id)
                result.append(paper)
        for row in rows:
            paper_id = str(row.get("id"))
            if paper_id not in seen:
                seen.add(paper_id)
                result.append(dict(row))
        return result[:limit]

    def _lineage_claims(self, gap) -> list[dict[str, Any]]:
        if gap is None:
            return []
        ids = [
            str(value)
            for value in json.loads(gap.get("supporting_claim_ids_json") or "[]")
        ]
        claims: list[dict[str, Any]] = []
        for claim_id in ids:
            row = self.repository.row(
                "SELECT * FROM evidence_claims WHERE id=?", (claim_id,)
            )
            if row:
                claims.append(row)
        return claims

    def _methodology_record(self, lineage: dict[str, Any]) -> dict[str, Any] | None:
        freeze_id = lineage.get("methodology_freeze_id")
        if not freeze_id:
            return None
        freeze = self.repository.latest_methodology_freeze(lineage["project_id"])
        if freeze is None or freeze.id != freeze_id:
            return None
        try:
            return json.loads(Path(freeze.artifact_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------------
    # Deterministic content builders (FastTrack template, inside-out)
    # ------------------------------------------------------------------

    @staticmethod
    def _title(
        candidate, complete: bool, keywords: list[str], contract=None
    ) -> str:
        if contract is not None:
            base = pick_title(contract, keywords)
            suffix = "" if complete else " (incomplete draft)"
            return base + suffix
        method = (
            " ".join(str(candidate.method).split())
            if candidate
            else "Unspecified method"
        )
        method_words = method.split()
        if len(method_words) > 12:
            # Drop words; never append a truncation ellipsis to a title.
            method = " ".join(method_words[:12]).rstrip(".,;:")
        method = method.rstrip(".,;:")
        keyword = next(
            (term for term in keywords if term.lower() not in method.lower()),
            keywords[0] if keywords else "",
        )
        base = (
            f"{method}: a preregistered paired-seed evaluation of {keyword}"
            if keyword
            else f"{method}: an evidence-backed evaluation"
        )
        suffix = "" if complete else " (incomplete draft)"
        return base + suffix

    def _keywords(
        self,
        project_id: str,
        papers: list[dict[str, Any]],
        *,
        gap=None,
        candidate=None,
        spec=None,
    ) -> list[str]:
        """Keywords block: Findings Memory + paper/seed metadata only.

        Method fragments ("add", "fixed", "standardized", model names) are
        filtered; domain terms from the gap, candidate, and dataset identity
        rank first. If the durable record honestly provides fewer than three
        useful terms, the unfiltered extracted terms are retained rather than
        inventing new ones.
        """
        sources: list[list[str]] = []
        try:
            findings = self.repository.findings_for_project(project_id, limit=50)
        except Exception:  # noqa: S110 - findings are supplementary; absence is honest
            findings = []
        for finding in findings:
            try:
                stored = json.loads(finding.keywords_json or "[]")
            except (ValueError, TypeError):
                stored = []
            if isinstance(stored, list):
                sources.append([str(item) for item in stored])
        sources.append(keyword_tokens(
            str((gap or {}).get("statement") or "") if isinstance(gap, dict) else "",
            str(getattr(candidate, "method", "") or ""),
            str(getattr(spec, "dataset_identity", "") or ""),
            *[str(p.get("title") or "") for p in papers],
        ))
        merged = pick_keywords(sources, maximum=16)
        filtered: list[str] = []
        for term in merged:
            lowered = term.lower().strip()
            if not lowered or lowered in _KEYWORD_STOPWORDS:
                continue
            if len(lowered) < 3 or re.search(r"\d", lowered):
                continue
            if lowered not in filtered:
                filtered.append(lowered)
        if len(filtered) < 3:
            for term in merged:
                lowered = term.lower().strip()
                if lowered and lowered not in filtered:
                    filtered.append(lowered)
                if len(filtered) >= 3:
                    break
        return filtered[:6]

    @staticmethod
    def _keywords_lines(keywords: list[str], complete: bool) -> list[str]:
        text = (
            ", ".join(keywords)
            if keywords
            else "not available (insufficient durable evidence)"
        )
        return ["## Keywords", "", text, ""]

    def _counter_claims(self, gap) -> list[dict[str, Any]]:
        if gap is None:
            return []
        ids = [
            str(value)
            for value in json.loads(gap.get("counter_evidence_claim_ids_json") or "[]")
        ]
        claims: list[dict[str, Any]] = []
        for claim_id in ids:
            row = self.repository.row(
                "SELECT * FROM evidence_claims WHERE id=?", (claim_id,)
            )
            if row:
                claims.append(row)
        return claims

    def _paper_review_notes(
        self, papers, review_run_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Per-paper review fields from the exact review run."""
        notes: dict[str, dict[str, Any]] = {}
        for paper in papers:
            row = self.repository.row(
                "SELECT problem, research_question, method, experimental_protocol, "
                "results_json, limitations_json FROM paper_reviews "
                "WHERE paper_id=? AND (? IS NULL OR run_id=?) "
                "ORDER BY rowid DESC LIMIT 1",
                (paper["id"], review_run_id, review_run_id),
            )
            if row:
                notes[str(paper["id"])] = dict(row)
        return notes

    @staticmethod
    def _methodology_field(methodology, field: str) -> str:
        if not methodology:
            return _NOT_SPECIFIED
        block = methodology.get("methodology", methodology)
        value = str(block.get(field) or "").strip() if isinstance(block, dict) else ""
        return value or _NOT_SPECIFIED

    @staticmethod
    def _sanitize(text: Any) -> str:
        """Remove internal lineage identifiers from prose.

        The evidence bundle and appendices keep the exact ids; the body must
        read like a paper, not like a database row. This is a presentation
        sanitizer, not a provenance rewrite: the stored evidence is unchanged.
        """
        value = str(text or "")
        value = re.sub(r"\b(?:claim|claims)\s+[0-9a-fA-F]{8,}\b", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\((?:review run|ID)\s+[0-9a-fA-F-]+\)", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\(checksum prefix[^)]*\)", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\b[0-9a-fA-F]{16,}\b", "", value)
        value = re.sub(r"\bpaper\s*\)", ")", value, flags=re.IGNORECASE)
        value = re.sub(r"\bpaper\s*(?=[,;])", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s+([,.;:])", r"\1", value)
        value = re.sub(r"\(\s*,\s*\)", "", value)
        value = re.sub(r",\s*\)", ")", value)
        value = re.sub(r"\(\s*\)", "", value)
        return " ".join(value.split()).strip()

    @staticmethod
    def _period_join(*parts: str) -> str:
        """Join sentence parts with '. ', normalizing trailing punctuation so
        stored evidence that already ends with '.' never yields '..'."""
        cleaned = []
        for part in parts:
            text = " ".join(str(part).split()).rstrip(" .;,")
            if text:
                cleaned.append(text)
        return ". ".join(cleaned) + "."

    def _abstract_markdown(
        self,
        gap,
        counter_claims,
        candidate,
        methodology,
        spec,
        analysis,
        report,
        complete,
        gaps,
        seed,
        *,
        contract=None,
        synthesis=None,
    ) -> list[str]:
        lines = ["## Abstract", ""]
        if not (complete and analysis is not None and report is not None and gap is not None):
            lines += [
                "This manuscript is explicitly INCOMPLETE: the experimental evidence chain required for "
                "scientific conclusions is not present in the durable record. Missing: "
                + "; ".join(gaps)
                + ". "
                "No result is fabricated to fill these gaps.",
                "",
            ]
            return lines
        block = (methodology or {}).get("methodology", methodology or {})

        def _snip(text: str, words: int) -> str:
            """Deterministic bounded snippet (first N words) so a single long
            recorded field can never blow the abstract past the template's
            150-250 word window."""
            joined = " ".join(self._sanitize(text).split())
            tokens = joined.split()
            if len(tokens) <= words:
                return joined
            return " ".join(tokens[:words]).rstrip(".,;:") + "."

        # The six labeled parts are rebuilt with progressively tighter caps
        # until the whole abstract lands inside the template's word window —
        # deterministic, and never drops measured numbers (they live in the
        # Results part, which is already compact).

        def _short_metric(name: str) -> str:
            """Long metric names (e.g. 'loto_top1_selection_accuracy
            (PRIMARY)') repeat across the abstract; abbreviate
            deterministically to the first token."""
            return str(name).split()[0].rstrip(",")

        template = _template_parameters()["abstract"]
        min_words, max_words = template["word_min"], template["word_max"]
        for factor in (1.0, 0.6, 0.4, 0.25, 0.15, 0.1):
            def cap(words: int) -> int:
                return max(1, int(words * factor))

            lines = ["## Abstract", ""]
            method = _snip(
                " ".join(str(candidate.method).split()) if candidate else "the selected candidate method",
                cap(30),
            )
            difference = _snip(
                str(candidate.difference_from_existing) if candidate and candidate.difference_from_existing else "not recorded",
                cap(25),
            )
            if contract is not None and contract.contributions:
                method = _snip(contract.contributions[0].claim, cap(30))
                difference = _snip(contract.contributions[0].novelty_delta, cap(25))
            metric_names = ", ".join(sorted({_short_metric(m) for m in analysis.proposed_metrics}))
            effects = "; ".join(
                f"{_short_metric(name)}: mean difference {value:+.4f}"
                for name, value in sorted(analysis.effect_size.items())
            ) or "none recorded"
            gates = ", ".join(
                f"{_short_metric(name)}: {'PASS' if passed else 'FAIL'}"
                for name, passed in dict(analysis.decision_gates).items()
            )
            counter_text = (
                " Counter-evidence recorded in the review: "
                + "; ".join(_snip(str(item["statement"]), cap(20)) for item in counter_claims[:3])
                if counter_claims
                else ""
            )
            seed_question = _snip(str(seed["research_question"]), cap(25)) if seed else ""
            procedure = "; ".join(
                str(step) for step in _as_list(block.get("procedure")) if str(step).strip()
            ) or "not recorded"
            procedure = _snip(procedure, cap(25))
            gap_part = self._period_join(
                f"Existing work does not close this question: {_snip(gap['why_unsolved'] or 'not recorded', cap(35))}{counter_text}",
                f"The recorded falsification test is: {_snip(gap['falsification_test'] or 'not recorded', cap(35))}",
            )
            if contract is not None and len(contract.contributions) >= 2:
                contribution_part = self._period_join(
                    _snip(contract.contributions[0].claim, cap(18)),
                    _snip(contract.contributions[1].claim, cap(15)),
                    _snip(contract.contributions[2].claim, cap(12)) if len(contract.contributions) >= 3 else "",
                )
            else:
                contribution_part = self._period_join(
                    f"To fill this gap we froze a methodology around {method} (difference: {difference}) and tested the preregistered hypothesis: {_snip(spec.hypothesis, cap(30)) if spec else 'not recorded'}",
                    _snip(str(block.get("hypothesis_statement") or ""), cap(30)),
                    f"Mechanism: {_snip(str(block.get('proposed_mechanism') or 'not recorded'), cap(25))}",
                )
            methods_part = self._period_join(
                f"Study design: {_snip(self._methodology_field(methodology, 'study_design'), cap(25))}",
                f"Data: {_snip(spec.dataset_identity, cap(15))}",
                f"split: {_snip(spec.split_definition, cap(15))}",
                f"Sample: {len(spec.random_seeds)} seed(s)",
                f"inclusion criteria: {spec.required_seed_count} seed(s) required",
                f"Analysis: {_snip(spec.statistical_plan, cap(30))}",
                f"Frozen procedure: {procedure}",
            )
            if contract is not None and contract.takeaway:
                conclusion_part = self._period_join(
                    _snip(contract.takeaway, cap(20)),
                    "Limitations and threats to validity are reported in the Discussion.",
                )
            elif factor <= 0.25:
                conclusion_part = self._period_join(
                    "Future research should address the limitations listed in the Discussion.",
                    f"Uncertainty recorded: {_snip(str(report['uncertainty']), cap(15))}",
                )
            else:
                conclusion_part = self._period_join(
                    "Future research should address the limitations listed in the Discussion, "
                    f"in particular the bounds of dataset '{_snip(spec.dataset_identity, cap(10))}' and split '{_snip(spec.split_definition, cap(8))}'",
                    f"Uncertainty recorded: {_snip(str(report['uncertainty']), cap(20))}",
                )
            lines += [
                f"**Background:** The question under study is: {_snip(str(gap['statement']), cap(45))} "
                + (f"(research program: {seed_question})" if seed_question else ""),
                "",
                f"**Gap in Knowledge:** {gap_part}",
                "",
                f"**Contribution:** {contribution_part}",
                "",
                f"**Population & Methods:** {methods_part}",
                "",
                f"**Results:** Metrics: {metric_names}. Measured effects: {effects}. "
                f"Decision gates: {gates}. The deterministic gates classified the outcome as "
                f"**{report['decision']}**.",
                "",
                f"**Conclusion:** {conclusion_part}",
                "",
            ]
            count = _word_count("\n".join(lines))
            if count <= max_words:
                break

        # Floor guard: when the recorded evidence is sparse the abstract could
        # fall below the template's 150-word minimum. Append further VERBATIM
        # evidence fields (never new prose) until the minimum is met; a chain
        # too sparse even for that fails the template audit honestly.
        min_words = _template_parameters()["abstract"]["word_min"]
        while _word_count("\n".join(lines)) < min_words:
            added = False
            for field, text in (
                (
                    "Algorithm outline",
                    str(block.get("algorithm_outline") or "") if block else "",
                ),
                (
                    "Predicted observations (hypotheses, not measurements)",
                    "; ".join(str(item) for item in (block.get("predicted_observations") or []))
                    if block
                    else "",
                ),
                (
                    "Preregistered interpretation rules",
                    "; ".join(str(item) for item in (spec.interpretation_rules if spec else [])),
                ),
                (
                    "Falsification conditions",
                    "; ".join(str(item) for item in (block.get("falsification_conditions") or []))
                    if block
                    else "",
                ),
            ):
                if text and text not in "\n".join(lines):
                    text = " ".join(str(text).split()[:35])
                    lines += [f"Context — {field}: {text}", ""]
                    added = True
                    break
            if not added:
                break
        # Ceiling guard: a long evidence chain can still overshoot the
        # template's 250-word maximum. Remove the verbatim extras (added by
        # the floor loop) in reverse order until within bounds; if the six
        # core parts alone exceed the window, the template audit fails
        # honestly rather than silently truncating measured results.
        max_words = _template_parameters()["abstract"]["word_max"]
        while _word_count("\n".join(lines)) > max_words:
            stripped = False
            for index in range(len(lines) - 1, -1, -1):
                if lines[index].startswith("Context —"):
                    del lines[index : index + 2]
                    stripped = True
                    break
            if not stripped:
                break
        return lines

    def _introduction_markdown(
        self,
        gap,
        counter_claims,
        review_notes,
        candidate,
        spec,
        seed,
        complete,
        gaps,
        *,
        synthesis=None,
    ) -> list[str]:
        lines = ["## 1. Introduction", ""]
        if gap is None:
            lines += ["No completed review exists for this project.", ""]
            if not complete:
                lines.append(f"Missing evidence: {'; '.join(gaps)}")
                lines.append("")
            return lines
        seed_topic = str(seed["topic"]) if seed else ""
        lines += [
            "### Why this topic is relevant now",
            "",
            f"The research question: {self._sanitize(gap['statement'])}",
            "",
        ]
        if seed_topic:
            lines += [f"The question sits within: {seed_topic}.", ""]
        lines += [
            "### Existing state of knowledge and the gap",
            "",
            f"What remains unresolved: {self._sanitize(gap['why_unsolved']) or 'not recorded'}",
            "",
        ]
        if counter_claims:
            lines += ["Recorded critiques and counter-evidence from the review:", ""]
            for item in counter_claims[:4]:
                lines.append(f"- [{item['claim_type']}] {self._sanitize(item['statement'])}")
            lines.append("")
        recorded_limitations: list[str] = []
        for notes in review_notes.values():
            try:
                for item in json.loads(notes.get("limitations_json") or "[]"):
                    if str(item).strip():
                        recorded_limitations.append(self._sanitize(item))
            except (ValueError, TypeError):
                continue
        if recorded_limitations:
            lines += ["Recorded methodological weaknesses of the reviewed studies:", ""]
            for item in recorded_limitations[:4]:
                lines.append(f"- {item}")
            lines.append("")
        lines += [
            self._period_join(
                "The gap emerges from these recorded critiques and counter-evidence",
                f"the falsification test is: "
                f"{self._sanitize(gap['falsification_test']) or 'not recorded'}",
            ),
            "",
        ]
        if synthesis is not None and synthesis.positioning:
            lines += [synthesis.positioning, ""]
        lines += [
            "### What this study did to fill the gap",
            "",
        ]
        method = (
            " ".join(str(candidate.method).split())
            if candidate
            else "the selected candidate method"
        )
        lines += [
            f"To fill this gap, the study froze a methodology around {method} and executed a "
            f"preregistered experiment. "
            + (
                self._period_join(
                    f"Hypothesis: {self._sanitize(spec.hypothesis)}",
                    f"Falsification condition: {self._sanitize(spec.falsification_condition)}",
                    "The experiment was executed only after exact-specification "
                    "approval",
                )
                if spec
                else "No experiment specification was recorded."
            ),
            "",
        ]
        if not complete:
            lines.append(f"This draft is INCOMPLETE. Missing evidence: {'; '.join(gaps)}")
            lines.append("")
        return lines

    def _methodology_markdown(
        self, methodology, candidate, spec, lineage, complete, gaps
    ) -> list[str]:
        lines = ["## 3. Methodology", ""]
        if methodology is None and candidate is None:
            lines += ["No frozen methodology available.", ""]
            if not complete:
                lines.append(f"Missing evidence: {'; '.join(gaps)}")
                lines.append("")
            return lines
        block = (methodology or {}).get("methodology", methodology or {})
        lines += [
            "### Study Design",
            "",
            self._methodology_field(methodology, "study_design"),
            "",
            "### Data Collection",
            "",
            self._methodology_field(methodology, "data_collection"),
            "",
        ]
        if spec is not None:
            lines += [
                f"Recorded data configuration: dataset '{self._sanitize(spec.dataset_identity)}' "
                f"(split '{self._sanitize(spec.split_definition)}').",
                "",
            ]
        lines += [
            "### Sample & Eligibility",
            "",
            self._methodology_field(methodology, "sample_and_eligibility"),
            "",
        ]
        if spec is not None:
            lines += [
                f"Recorded sample configuration: seeds {', '.join(str(s) for s in spec.random_seeds)} "
                f"({len(spec.random_seeds)} of {spec.required_seed_count} required); "
                f"split: {self._sanitize(spec.split_definition)}.",
                "",
            ]
        lines += [
            "### Data Analysis",
            "",
            self._methodology_field(methodology, "data_analysis"),
            "",
        ]
        if spec is not None:
            lines += [
                self._period_join(
                    f"Statistical plan (preregistered): {self._sanitize(spec.statistical_plan)}",
                    f"Interpretation rules (preregistered): "
                    f"{self._sanitize('; '.join(spec.interpretation_rules)) or 'not recorded'}",
                ),
                "",
            ]
        lines += [
            "### Ethical Considerations",
            "",
            self._methodology_field(methodology, "ethical_considerations"),
            "",
        ]
        procedure = [
            str(step) for step in _as_list(block.get("procedure")) if str(step).strip()
        ]
        lines += ["### Cookbook procedure (frozen, linear order)", ""]
        if procedure:
            for number, step in enumerate(procedure, start=1):
                lines.append(f"{number}. {step}")
        else:
            lines.append("No procedure steps recorded at freeze time.")
        lines.append("")
        if block.get("algorithm_outline"):
            lines += [f"Algorithm outline: {block['algorithm_outline']}", ""]
        if block.get("predicted_observations"):
            predicted = block["predicted_observations"]
            if isinstance(predicted, list):
                predicted = "; ".join(str(item) for item in predicted)
            lines += [f"Predicted observations (hypotheses, NOT measurements): {predicted}", ""]
        if block.get("falsification_conditions"):
            falsification = block["falsification_conditions"]
            if isinstance(falsification, list):
                falsification = "; ".join(str(item) for item in falsification)
            lines += [f"Falsification conditions: {falsification}", ""]
        if block.get("expected_resource_demands"):
            lines += [f"Expected resource demands: {block['expected_resource_demands']}", ""]
        lines += [
            "### Why these choices (selection rationale)",
            "",
            f"Every choice above traces to the frozen methodology artifact "
            f"(version {lineage.get('methodology_version')}) in the review → "
            f"novelty → freeze lineage; it is a recorded design, not a post-hoc "
            f"justification. The exact artifact identifiers are listed in "
            f"Appendix A.",
            "",
        ]
        for field in _TEMPLATE_METHODOLOGY_FIELDS:
            value = self._methodology_field(methodology, field)
            if value == _NOT_SPECIFIED:
                lines.append(
                    f"_Methodology field '{field.replace('_', ' ')}' was {_NOT_SPECIFIED} "
                    f"and is reported as a limitation in the Discussion._"
                )
                lines.append("")
        lines.append(
            "_Predicted effects are hypotheses, not measurements._"
        )
        lines.append("")
        return lines

    def _results_markdown(
        self, analysis, report, tables_md, figure_svg, complete
    ) -> list[str]:
        lines = ["## 4. Results", ""]
        if analysis is None:
            lines += ["No validated measurements: results cannot be reported.", ""]
            return lines
        lines += [tables_md, ""]
        if figure_svg:
            lines += [
                f"![Effect sizes per metric]({figure_svg})",
                "",
                f"Figure 1 — Effect sizes (mean difference, candidate minus baseline) per "
                f"metric, derived deterministically from the validated seed observations "
                f"(analysis record in Appendix A).",
                "",
            ]
        gates = ", ".join(
            f"{name}: {'PASS' if passed else 'FAIL'}"
            for name, passed in dict(analysis.decision_gates).items()
        )
        lines += [f"Decision gates: {gates}.", "", "### Statistical tests", ""]
        tests = dict(analysis.statistical_tests)
        if tests:
            lines += ["| Metric | Test | exact p | n | Status |", "|---|---|---:|---:|---|"]
            for metric, test in sorted(tests.items()):
                if not isinstance(test, dict):
                    continue
                observations = test.get("raw_observations") or []
                p_value = test.get("p_value")
                p_text = "—" if p_value is None else p_value
                n_text = test.get("n") or len(observations) or "—"
                lines.append(
                    f"| {metric} | {test.get('test', '—')} | "
                    f"{p_text} | {n_text} | {test.get('status', '—')} |"
                )
            lines.append("")
        else:
            lines += ["No statistical test record was available.", ""]
        lines += [
            f"Hypothesis support: {analysis.hypothesis_support_assessment} "
            f"(report decision: {report['decision'] if report else 'n/a'}).",
            "",
        ]
        if dict(analysis.ablation_contributions):
            lines.append(
                f"Ablation contributions (measured descendant runs): "
                f"{dict(analysis.ablation_contributions)}"
            )
        else:
            lines.append("Ablations: none recorded in this lineage.")
        lines.append("")
        return lines

    def _study_limitations(
        self, report, methodology, spec, analysis, *, readiness=None
    ) -> list[str]:
        """Study Limitations for the Discussion (template-spec.json T90):
        at least 3 distinct, substantive entries. Integrity statements
        ("no fixture was used") are strengths, not limitations; duplicates
        and unsupported boilerplate are removed."""
        limitations: list[str] = []
        seen: set[str] = set()
        semantic_seen: set[str] = set()

        def add(text: str) -> None:
            compact = " ".join(self._sanitize(text).split())
            lowered = compact.lower()
            if not compact or len(compact.split()) < 4:
                return
            if any(phrase in lowered for phrase in (
                "no model prediction",
                "no result is fabricated",
                "fixture, or incompatible cached",
            )):
                return
            words = compact.split()
            if len(words) > 55:
                compact = " ".join(words[:55]).rstrip(".,;:") + "."
                lowered = compact.lower()
            if "derived statistics are limited" in lowered:
                if "derived_stats" in semantic_seen:
                    return
                semantic_seen.add("derived_stats")
            if "findings are bounded to dataset" in lowered:
                if "dataset_bounds" in semantic_seen:
                    return
                semantic_seen.add("dataset_bounds")
            if "findings are bounded to dataset" in lowered:
                key = "dataset_bounds"
            elif "derived statistics are limited" in lowered:
                key = "derived_bounds"
            elif "ablation" in lowered and any(
                marker in lowered for marker in ("not run", "not measured", "unmeasured")
            ):
                key = "ablations"
            elif "robustness" in lowered and "not" in lowered:
                key = "robustness"
            else:
                key = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
            if key and key not in seen:
                seen.add(key)
                limitations.append(compact)

        if report is not None:
            for item in _parse_report_limitations(report.get("limitations")):
                add(item)
            add(str(report.get("uncertainty") or ""))
        if spec is not None:
            add(
                f"Findings are bounded to dataset '{self._sanitize(spec.dataset_identity)}' "
                f"(split '{self._sanitize(spec.split_definition)}', "
                f"{len(spec.random_seeds)} seed(s)); generalization beyond this "
                f"configuration is unverified."
            )
        if report is not None and "ABLATION_NOT_RUN" in str(report.get("ablations") or ""):
            add("Ablations were not run; per-component contributions are unmeasured.")
        if report is not None and "not claimed" in str(report.get("robustness_tests") or ""):
            add("Robustness tests were not run; robustness is not claimed.")
        for field in _TEMPLATE_METHODOLOGY_FIELDS:
            if self._methodology_field(methodology, field) == _NOT_SPECIFIED:
                add(
                    f"Methodology field '{field.replace('_', ' ')}' was "
                    f"{_NOT_SPECIFIED}; the manuscript reports this honestly "
                    f"instead of inventing the field."
                )
        if analysis is not None and not dict(analysis.ablation_contributions):
            add("Ablation contributions were not measured in this lineage.")
        if readiness is not None:
            if readiness.provenance.simulated:
                add(
                    "The reported measurements were generated by a simulation, "
                    "not by running the claimed protocol on real benchmarks; "
                    "empirical claims are limited to the simulation model."
                )
            if not readiness.provenance.real_execution and not readiness.provenance.simulated:
                add(
                    "The measurement provenance is unverified: a real execution "
                    "identity (dataset checksum and implementation commit) was "
                    "not available."
                )
            if not readiness.evidence.power_established:
                add(
                    "The study was not powered to detect the claimed effect; "
                    "negative and null results are inconclusive rather than "
                    "evidence of absence."
                )
            if readiness.evidence.external_baselines == 0:
                add(
                    "No external baseline was evaluated; the comparison is "
                    "internal to the study and does not establish superiority "
                    "over prior methods."
                )
            if readiness.evidence.ablations == 0:
                add(
                    "Ablations were not run; the contribution of individual "
                    "components is unmeasured."
                )
        return limitations

    def _discussion_markdown(
        self,
        decision,
        report,
        limitations,
        gap,
        methodology,
        spec,
        analysis,
        review_notes,
        papers,
        complete,
        gaps,
        *,
        synthesis=None,
        contract=None,
        readiness=None,
    ) -> list[str]:
        lines = ["## 5. Discussion", ""]
        if report is None:
            lines += ["Discussion requires an analyzed experiment report.", ""]
            if not complete:
                lines.append(f"Missing evidence: {'; '.join(gaps)}")
                lines.append("")
            return lines
        lines += [
            "### Recap of what we found",
            "",
            f"The deterministic decision gates classified the outcome as **{decision}**: "
            f"{self._sanitize(report['decision_evidence'])}",
            "",
            f"Observed uncertainty: {self._sanitize(report['uncertainty'])}",
            "",
        ]
        if str(report["decision"]).lower() in {
            "refuted",
            "no_effect",
            "mechanism_contradicted",
        }:
            lines += [
                "This is a negative or null result and is reported as a valid "
                "scientific outcome; it is not a failure of the workflow.",
                "",
            ]
        if synthesis is not None and synthesis.papers:
            lines += ["### Comparison with prior work", ""]
            if synthesis.positioning:
                lines += [synthesis.positioning, ""]
            closest = [paper for paper in synthesis.papers if paper.is_gap_support][:4]
            if closest:
                lines += [
                    "The gap's supporting studies are: "
                    + "; ".join(
                        f"{paper.title} [@{paper.key}]" if paper.key else paper.title
                        for paper in closest
                    )
                    + ".",
                    "",
                ]
        lines += ["### Study Limitations (at least 3)", ""]
        for number, item in enumerate(limitations, start=1):
            lines.append(f"{number}. {item}")
        lines += ["", "### Limitations from the reviewed studies", ""]
        reviewed_limitations: list[str] = []
        for notes in review_notes.values():
            try:
                for item in json.loads(notes.get("limitations_json") or "[]"):
                    if str(item).strip():
                        reviewed_limitations.append(self._sanitize(item))
            except (ValueError, TypeError):
                continue
        if reviewed_limitations:
            for item in reviewed_limitations[:4]:
                lines.append(f"- {item}")
            lines.append("")
        else:
            lines += [
                "This study is a preregistered computational evaluation, not a "
                "systematic review; the template's per-study limitations part applies "
                "to systematic reviews. No per-study limitation rows were recorded by "
                "the review stage.",
                "",
            ]
        lines += ["### Strengths of this study", ""]
        lines += [
            "- Preregistered design: interpretation rules were recorded on the "
            "experiment specification before execution.",
            "- Exact lineage: review gap → methodology freeze → specification → "
            "analysis → report are linked by durable ids and checksums.",
            "- Deterministic numerics: every reported number derives from "
            "stored seed observations through the deterministic analysis; no "
            "number is invented by the writer.",
            "",
            "### Implications for future research",
            "",
        ]
        if spec is not None:
            lines += [
                f"Future work should address the limitations above, in particular the "
                f"bounds of dataset '{self._sanitize(spec.dataset_identity)}' and split "
                f"'{self._sanitize(spec.split_definition)}', and run the ablations and robustness "
                f"tests named in the specification.",
                "",
            ]
        if gap is not None and gap.get("missing_experiment"):
            lines += [
                f"The review's missing experiment remains open for future work: "
                f"{self._sanitize(gap['missing_experiment'])}",
                "",
            ]
        lines += [
            "### Possible interpretations and future work (speculative — clearly labeled)",
            "",
            "Everything in this part is explicitly speculative: hypotheses for future "
            "work, not measured results.",
            "",
        ]
        block = (methodology or {}).get("methodology", methodology or {})
        if block.get("competing_explanations"):
            competing = block["competing_explanations"]
            if isinstance(competing, list):
                competing = "; ".join(str(item) for item in competing)
            lines += [f"Competing explanations recorded at freeze time: {self._sanitize(competing)}", ""]
        if block.get("uncertainty_statement"):
            lines += [f"Uncertainty recorded at freeze time: {self._sanitize(block['uncertainty_statement'])}", ""]
        if self.venue.template == "fasttrack":
            lines += ["### Conclusion", ""]
            if contract is not None and contract.takeaway:
                lines += [
                    f"{contract.takeaway} The preregistered decision is {decision}.",
                    "",
                ]
            else:
                lines += [f"Decision: {decision}.", ""]
        return lines

    def _references_markdown(self, papers, keys) -> list[str]:
        lines = ["## References", ""]
        if not papers:
            lines += ["No supporting papers recorded.", ""]
            return lines
        for paper in papers:
            year = f" ({paper['year']})" if paper.get("year") else ""
            lines.append(
                f"- [@{keys[paper['id']]}]: {paper['title']}{year}."
            )
        lines.append("")
        return lines

    @staticmethod
    def _exact_commands(report) -> list[str]:
        if not report:
            return []
        try:
            parsed = json.loads(str(report.get("exact_commands_json") or "[]"))
        except (ValueError, TypeError):
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    def _appendices_markdown(
        self, project, lineage, papers, claims, analysis, report, complete, gaps
    ) -> list[str]:
        lines = ["## Appendices", ""]
        lines += ["### Appendix A. Evidence bundle", ""]
        def _id(key: str) -> str:
            return str(lineage.get(key) or "not recorded")

        lines += [
            f"- Research gap: {_id('gap_id')} (review run {_id('review_run_id')})",
            f"- Novelty portfolio: {_id('portfolio_id')}",
            f"- Methodology freeze: {_id('methodology_freeze_id')} "
            f"(version {_id('methodology_version')})",
            f"- Experiment specification: {_id('spec_id')}",
            f"- Statistical analysis: {_id('analysis_id')}",
            f"- Experiment report: {_id('report_id')}",
            "",
        ]
        if papers:
            lines += ["Papers in the bundle (title, checksum):", ""]
            for paper in papers:
                lines.append(
                    f"- {paper['title']} — checksum {paper.get('checksum')}"
                )
            lines.append("")
        if claims:
            lines += ["Verified evidence claims in the bundle:", ""]
            for claim in claims:
                lines.append(
                    f"- [{claim['claim_type']}] {claim['statement']} (claim {claim['id']})"
                )
            lines.append("")
        lines += [
            f"The full bundle (lineage ids, paper checksums, claim ids, keywords, "
            f"limitations, exact commands, analysis record) is persisted as "
            f"`evidence_bundle.json` next to this manuscript; every artifact "
            f"checksum is in `manifest.json`.",
            "",
            "### Appendix B. Raw seed metrics",
            "",
        ]
        if analysis is not None and analysis.seed_level_results:
            lines += ["| seed | baseline | candidate |", "|---|---|---|"]
            for index, row in enumerate(analysis.seed_level_results[:20], start=1):
                lines.append(
                    f"| {index} | {json.dumps(row.get('baseline'), sort_keys=True)} | "
                    f"{json.dumps(row.get('candidate', row), sort_keys=True)} |"
                )
            if len(analysis.seed_level_results) > 20:
                lines.append(
                    f"| … | ({len(analysis.seed_level_results) - 20} more rows in "
                    f"evidence_bundle.json) | |"
                )
            lines.append("")
        else:
            lines += ["No validated seed observations available.", ""]
        lines += ["### Appendix C. Exact commands", ""]
        commands = self._exact_commands(report)
        if commands:
            for command in commands:
                lines.append(f"- `{command}`")
            lines.append("")
        else:
            lines += [
                "No execution command was recorded for this lineage (the approval "
                "package and worker identity remain in the evidence bundle). No "
                "command is reconstructed or invented here.",
                "",
            ]
        lines += ["### Appendix D. Data and code availability", ""]
        lines += [
            f"All artifacts are in the local AutoResearch store for project "
            f"{project.id}: papers, verified claims, methodology freeze, experiment "
            f"specification, measurement artifacts (checksummed), statistical "
            f"analysis, and this manuscript's evidence bundle.",
            "",
        ]
        if not complete:
            lines.append(
                f"This manuscript remains INCOMPLETE. Missing evidence: {'; '.join(gaps)}."
            )
            lines.append("")
        return lines

    @staticmethod
    def _svg_escape(value: str) -> str:
        return (
            str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    def _figures(self, analysis, root: Path) -> tuple[str | None, str]:
        """Deterministic figure generation from validated data only: an SVG
        (markdown) and a TikZ block (LaTeX) of per-metric effect sizes. No
        plotting library, no model — pure text emission from the analysis."""
        if analysis is None or not dict(analysis.effect_size or {}):
            return None, ""
        metrics = sorted(dict(analysis.effect_size))
        values = [float(dict(analysis.effect_size)[metric]) for metric in metrics]
        max_abs = max((abs(value) for value in values), default=0.0) or 1.0
        figures_dir = root / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        scale = 180.0 / max_abs
        rows: list[str] = []
        y = 64
        for metric, value in zip(metrics, values):
            label = self._svg_escape(metric)
            rows.append(f'<text x="10" y="{y + 15}" font-size="13">{label}</text>')
            bar_width = max(abs(value) * scale, 2.0)
            x = 200.0 if value >= 0 else 200.0 - bar_width
            color = "#2b6cb0" if value >= 0 else "#c05621"
            rows.append(
                f'<rect x="{x:.1f}" y="{y}" width="{bar_width:.1f}" height="18" fill="{color}"/>'
            )
            rows.append(
                f'<text x="{x + bar_width + 8:.1f}" y="{y + 15}" font-size="12">{value:+.4f}</text>'
            )
            y += 34
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="'
            + str(y + 24)
            + '" font-family="sans-serif">\n'
            + '<rect width="100%" height="100%" fill="white"/>\n'
            + '<text x="10" y="28" font-size="15" font-weight="bold">'
            + "Effect sizes (mean difference, candidate minus baseline) per metric</text>\n"
            + '<text x="10" y="48" font-size="11">'
            + "Figure generated deterministically from the validated statistical "
            + "analysis; no model involvement.</text>\n"
            + "\n".join(rows)
            + "\n</svg>\n"
        )
        svg_path = figures_dir / "effect-sizes.svg"
        svg_path.write_text(svg, encoding="utf-8")
        tikz_lines = [
            "\\begin{figure}[h]\\centering",
            "\\begin{tikzpicture}[scale=0.9]",
            "\\node[anchor=west,font=\\small\\bfseries] at (0,3.6) "
            "{Effect sizes (mean difference) per metric};",
        ]
        for index, (metric, value) in enumerate(zip(metrics, values)):
            label = self._tex(metric)
            bar = max(abs(value) * 3.0, 0.1)
            origin = 3.0 + min(value, 0.0) * 3.0
            end = origin + bar
            color = "blue!60" if value >= 0 else "orange!70"
            tikz_lines.append(
                f"\\node[anchor=east,font=\\small] at (2.7,{3.0 - index * 0.7:.2f}) "
                f"{{{label}}};"
            )
            tikz_lines.append(
                f"\\filldraw[fill={color}] ({origin:.3f},{2.65 - index * 0.7:.2f}) "
                f"rectangle ({end:.3f},{3.05 - index * 0.7:.2f});"
            )
            tikz_lines.append(
                f"\\node[anchor=west,font=\\small] at ({end + 0.15:.3f},"
                f"{2.85 - index * 0.7:.2f}) {{{value:+.4f}}};"
            )
        tikz_lines += [
            "\\end{tikzpicture}",
            "\\caption{Effect sizes (mean difference, candidate minus baseline) per "
            "metric, derived deterministically from the validated per-seed "
            "observations.}",
            "\\end{figure}",
        ]
        return "figures/effect-sizes.svg", "\n".join(tikz_lines)

    _TEX_UNICODE = {
        "μ": r"$\mu$", "µ": r"$\mu$", "±": r"$\pm$", "×": r"$\times$", "·": r"$\cdot$",
        "≥": r"$\geq$", "≤": r"$\leq$", "≠": r"$\neq$", "≈": r"$\approx$", "∞": r"$\infty$",
        "→": r"$\rightarrow$", "←": r"$\leftarrow$", "∈": r"$\in$", "∑": r"$\sum$",
        "α": r"$\alpha$", "β": r"$\beta$", "γ": r"$\gamma$", "δ": r"$\delta$",
        "ε": r"$\epsilon$", "θ": r"$\theta$", "λ": r"$\lambda$", "π": r"$\pi$",
        "ρ": r"$\rho$", "σ": r"$\sigma$", "τ": r"$\tau$", "φ": r"$\phi$", "ψ": r"$\psi$",
        "ω": r"$\omega$", "Δ": r"$\Delta$", "Ω": r"$\Omega$", "Σ": r"$\Sigma$",
        "−": "-", "–": "--", "—": "---", "…": r"\ldots{}", "“": "``", "”": "''",
        "‘": "`", "’": "'", "†": r"\dag{}", "‡": r"\ddag{}", "§": r"\S{}", "¶": r"\P{}",
    }

    @staticmethod
    def _tex(value: Any) -> str:
        """Escape a free-text value for LaTeX text mode.

        CV/ML evidence text routinely carries Unicode math (μ, ±, ≥) and
        markup characters (^, ~). Inserted raw, XeTeX halts with
        'Missing $ inserted' (observed live compiling the AutoFID
        manuscript). Plain-text escapes run FIRST on the raw string, then
        Unicode symbols are converted to math macros (which deliberately
        introduce $ and braces).
        """
        # Escape raw braces FIRST (no macro has been introduced yet), then the
        # remaining plain-text characters; the ^/~ macros below deliberately
        # introduce braces and must come after that pass (observed live:
        # escaping after them produced '\textasciicircum\{\}').
        text = str(value).replace("{", r"\{").replace("}", r"\}")
        text = text.replace("\\", r"\textbackslash{}")
        text = (
            text.replace("&", r"\&")
            .replace("%", r"\%")
            .replace("$", r"\$")
            .replace("#", r"\#")
            .replace("_", r"\_")
            .replace("^", r"\textasciicircum{}")
            .replace("~", r"\textasciitilde{}")
        )
        for symbol, latex in ManuscriptService._TEX_UNICODE.items():
            text = text.replace(symbol, latex)
        return text

    def _latex(
        self,
        *,
        title,
        papers,
        keys,
        tables_tex,
        figure_tikz,
        analysis,
        report,
        gap,
        counter_claims,
        candidate,
        methodology,
        spec,
        limitations,
        keywords,
        gaps,
        complete,
        claims,
        review_notes,
        seed,
        lineage,
    ) -> str:
        decision = str(report["decision"]) if report else "INCOMPLETE"

        def cite(key: str) -> str:
            return "\\cite{" + key + "}"

        # Abstract (six labeled parts; incomplete drafts stay honestly short).
        if complete and analysis is not None and report is not None and gap is not None:
            block = (methodology or {}).get("methodology", methodology or {})
            effects = ", ".join(
                name + " " + f"{value:+.4f}"
                for name, value in sorted(dict(analysis.effect_size).items())
            )
            gates = ", ".join(
                f"{name}: {'PASS' if passed else 'FAIL'}"
                for name, passed in dict(analysis.decision_gates).items()
            )
            procedure = "; ".join(
                str(step) for step in _as_list(block.get("procedure")) if str(step).strip()
            )
            abstract = (
                "\\textbf{Background.} "
                + self._tex(gap["statement"])
                + "\\par\\textbf{Gap in Knowledge.} "
                + self._tex(gap.get("why_unsolved") or "not recorded")
                + "\\par\\textbf{Contribution.} "
                + self._tex(block.get("hypothesis_statement") or (spec.hypothesis if spec else ""))
                + "\\par\\textbf{Population \\& Methods.} "
                + self._tex(
                    "Study design: "
                    + self._methodology_field(methodology, "study_design")
                    + ". Data: "
                    + (spec.dataset_identity if spec else "not recorded")
                    + "; split: "
                    + (spec.split_definition if spec else "not recorded")
                    + ". Analysis: "
                    + (spec.statistical_plan if spec else "not recorded")
                    + ". Procedure: "
                    + (procedure or "not recorded")
                )
                + "\\par\\textbf{Results.} Measured effects: "
                + self._tex(effects)
                + ". Decision gates: "
                + self._tex(gates)
                + ".\\par\\textbf{Conclusion.} "
                + self._tex(report["decision_evidence"])
                + " Future research should address the limitations listed in the "
                + "Discussion."
            )
        elif gap is not None:
            abstract = (
                self._tex(gap["statement"])
                + " No validated measurements; this draft is explicitly incomplete."
            )
        else:
            abstract = "No completed review exists."
        related = (
            "\n".join(
                "\\item "
                + cite(keys[paper["id"]])
                + " "
                + self._tex(paper["title"])
                + " ("
                + str(paper.get("year") or "n.d.")
                + "). "
                + self._tex(str(paper.get("abstract") or "")[:300])
                for paper in papers
            )
            or "\\item No supporting papers recorded."
        )
        claim_items = (
            "\n".join(
                "\\item ["
                + self._tex(claim["claim_type"])
                + "] "
                + self._tex(claim["statement"])
                + " "
                + cite(keys.get(claim["paper_id"], "unknown"))
                for claim in claims[:20]
            )
            or "\\item None."
        )
        if methodology is not None:
            block = methodology.get("methodology", methodology)
            method_fields = "\n".join(
                "\\textbf{"
                + field.replace("_", " ").title()
                + ".} "
                + self._tex(self._methodology_field(methodology, field))
                + "\\par"
                for field in _TEMPLATE_METHODOLOGY_FIELDS
            )
            procedure_tex = "\n".join(
                f"\\item {self._tex(step)}"
                for step in _as_list(block.get("procedure"))
                if str(step).strip()
            )
            method_text = (
                method_fields
                + "\n"
                + (f"Algorithm outline: {self._tex(block['algorithm_outline'])}\n" if block.get("algorithm_outline") else "")
                + (("\\begin{enumerate}\n" + procedure_tex + "\n\\end{enumerate}\n") if procedure_tex else "")
            )
        elif candidate is not None:
            method_text = "Selected candidate: " + self._tex(candidate.method)
        else:
            method_text = "No frozen methodology available."
        if spec is not None:
            setup = (
                "Hypothesis: "
                + self._tex(spec.hypothesis)
                + ". Datasets: "
                + self._tex(spec.dataset_identity)
                + ". Split: "
                + self._tex(spec.split_definition)
                + ". Metrics: "
                + ", ".join(self._tex(metric) for metric in spec.metrics)
                + ". Seeds: "
                + ", ".join(str(seed_value) for seed_value in spec.random_seeds)
                + "."
            )
        else:
            setup = "No experiment specification available."
        discussion = (
            (
                "\\textbf{Recap.} Decision gates classified the outcome as "
                + self._tex(decision)
                + ": "
                + self._tex(report["decision_evidence"])
                + "\n\\textbf{Study Limitations (at least 3).}\n"
                + "\\begin{itemize}\n"
                + "\n".join(f"\\item {self._tex(item)}" for item in limitations)
                + "\n\\end{itemize}\n"
                "\\textbf{Strengths.} Preregistered design; exact lineage; "
                "deterministic numerics from checksummed seed observations.\n"
                "\\textbf{Implications for future research.} Address the limitations "
                "listed above and run the ablations and robustness tests named in "
                "the specification."
            )
            if report
            else "Discussion requires an analyzed experiment report."
        )
        references_tex = (
            "\n".join(f"\\item {self._tex(paper['title'])}" for paper in papers)
            or "\\item None."
        )
        conclusion_tex = ""
        if self.venue.template != "fasttrack":
            conclusion_tex = (
                "\\section{Conclusion}\n"
                + self._tex(str(report["decision_evidence"] if report else decision))
                + "\n"
            )
        return (
            "\\documentclass[11pt]{article}\n"
            "\\usepackage[margin=1in]{geometry}\n"
            "\\usepackage{booktabs}\n"
            "\\usepackage{tikz}\n"
            "\\usepackage[numbers]{natbib}\n"
            "\\newcommand{\\keywords}[1]{\\par\\vspace{0.5em}\\noindent\\textbf{Keywords:} #1\\par}\n"
            "\\title{" + self._tex(title) + "}\n"
            "\\date{\\today}\n"
            "\\begin{document}\n"
            "\\maketitle\n"
            "\\begin{abstract}" + abstract + "\\end{abstract}\n"
            "\\keywords{" + self._tex(", ".join(keywords)) + "}\n"
            "\\section{Introduction}\n"
            + (self._tex(gap["statement"]) if gap else "No completed review exists.")
            + "\n"
            + (self._tex(gap.get("why_unsolved") or "") + "\n" if gap else "")
            + ("\\section{Literature Review}\n\\begin{itemize}\n" + related + "\n\\end{itemize}\n" if papers else "")
            + "Verified evidence claims:\n\\begin{itemize}\n"
            + claim_items
            + "\n\\end{itemize}\n"
            "\\section{Methodology}\n" + method_text + "\n"
            "\\section{Results}\n"
            + (
                tables_tex
                + "\n"
                + figure_tikz
                + "\n"
                if analysis is not None
                else "No validated measurements: results cannot be reported."
            )
            + "\n"
            "\\section{Discussion}\n" + discussion + "\n"
            + conclusion_tex
            + "\\section{References}\n\\begin{itemize}\n" + references_tex + "\n\\end{itemize}\n"
            "\\section{Appendices}\n"
            "\\textbf{Appendix A. Evidence bundle.} Lineage: gap "
            + str(lineage.get("gap_id"))
            + "; freeze "
            + str(lineage.get("methodology_freeze_id"))
            + "; spec "
            + str(lineage.get("spec_id"))
            + "; analysis "
            + str(lineage.get("analysis_id"))
            + "; report "
            + str(lineage.get("report_id"))
            + ". See evidence\\_bundle.json and manifest.json.\n"
            "\\textbf{Appendix B. Raw seed metrics.} Persisted in evidence\\_bundle.json "
            "(seed\\_level\\_results).\n"
            "\\textbf{Appendix C. Exact commands.} "
            + (
                "; ".join(f"\\texttt{{{self._tex(command)}}}" for command in self._exact_commands(report))
                or "No execution command recorded for this lineage."
            )
            + "\n"
            "\\textbf{Appendix D. Data and code availability.} Local AutoResearch "
            "store; checksummed artifacts.\n"
            "\\bibliographystyle{plainnat}\n\\bibliography{references}\n\\end{document}\n"
        )

    def _tables(self, analysis) -> tuple[str, str]:
        if analysis is None:
            return (
                "No validated measurements available.",
                "% No validated measurements available.",
            )
        # M1 upgrade (2026-10-16): mean±std / Cohen's d / exact p / n table
        # engine; deterministic, derived only from the analysis record.
        from autoresearch.application.tables import build_result_tables

        return build_result_tables(analysis)

    @staticmethod
    def _author_names(authors_json: Any) -> str:
        """Parse the stored author list into BibTeX-ready names.

        The corpus stores authors in three shapes across sources: objects
        ({'fullname': ...}), plain strings, and — observed live — a JSON list
        whose entries are Python-repr strings of the object
        ("{'fullname': 'Zhen Wang', 'username': ''}"). Joining the raw JSON
        dumped reprs into the .bib file and XeTeX halted. Every shape is
        normalized to the human name here.
        """
        import ast

        try:
            entries = json.loads(authors_json or "[]")
        except (TypeError, ValueError):
            return "Unknown authors"
        names: list[str] = []
        for entry in entries if isinstance(entries, list) else []:
            candidate = entry
            if isinstance(candidate, str) and candidate.strip().startswith("{"):
                try:
                    candidate = ast.literal_eval(candidate)
                except (ValueError, SyntaxError):
                    candidate = entry
            if isinstance(candidate, dict):
                name = candidate.get("fullname") or candidate.get("name") or candidate.get("username") or ""
            else:
                name = str(candidate)
            if str(name).strip():
                names.append(str(name).strip())
        return " and ".join(names) or "Unknown authors"

    @staticmethod
    def _bibtex_escape(value: Any) -> str:
        text = str(value)
        text = text.replace("\\", r"\textbackslash{}")
        text = text.replace("{", r"\{").replace("}", r"\}")
        text = (
            text.replace("&", r"\&")
            .replace("%", r"\%")
            .replace("#", r"\#")
            .replace("_", r"\_")
            .replace("$", r"\$")
            .replace("~", r"\textasciitilde{}")
            .replace("^", r"\textasciicircum{}")
        )
        return text

    @staticmethod
    def _bibtex(papers: list[dict[str, Any]], keys: dict[str, str]) -> str:
        entries = []
        escape = ManuscriptService._bibtex_escape
        for paper in papers:
            key = keys[paper["id"]]
            authors = escape(
                ManuscriptService._author_names(paper.get("authors_json"))
                or "Unknown authors"
            )
            title = escape(str(paper.get("title") or "Untitled"))
            year = paper.get("year") or "n.d."
            venue = str(paper.get("venue") or "").strip()
            doi = str(paper.get("doi") or "").strip()
            url = str(
                paper.get("url") or paper.get("open_access_pdf_url") or ""
            ).strip()
            fields = [
                f"title = {{{title}}}",
                f"author = {{{authors}}}",
                f"year = {{{year}}}",
            ]
            if venue and venue.lower() not in ("unknown", "none"):
                fields.append(f"journal = {{{escape(venue)}}}")
            if doi:
                fields.append(f"doi = {{{escape(doi)}}}")
            if url:
                fields.append(f"url = {{{escape(url)}}}")
            kind = (
                "article"
                if venue and venue.lower() not in ("unknown", "none")
                else "misc"
            )
            entries.append(f"@{kind}{{{key},\n  " + ",\n  ".join(fields) + ",\n}")
        return "\n\n".join(entries) + "\n"

    def _evidence_bundle(
        self,
        lineage: dict[str, Any],
        papers,
        claims,
        analysis,
        report,
        *,
        keywords: list[str] | None = None,
        limitations: list[str] | None = None,
        exact_commands: list[str] | None = None,
        figure_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        bundle: dict[str, Any] = {"lineage": lineage}
        bundle["papers"] = [
            {
                "id": p["id"],
                "title": p["title"],
                "checksum": p.get("checksum"),
                "doi": p.get("doi"),
                "year": p.get("year"),
            }
            for p in papers
        ]
        bundle["claims"] = [
            {"id": c["id"], "paper_id": c["paper_id"], "excerpt": c["excerpt"]}
            for c in claims
        ]
        if keywords:
            bundle["keywords"] = list(keywords)
        if limitations:
            bundle["limitations"] = list(limitations)
        if exact_commands:
            bundle["exact_commands"] = list(exact_commands)
        if figure_paths:
            bundle["figures"] = list(figure_paths)
        if analysis is not None:
            bundle["analysis"] = {
                "id": analysis.id,
                "baseline_metrics": dict(analysis.baseline_metrics),
                "proposed_metrics": dict(analysis.proposed_metrics),
                "effect_size": dict(analysis.effect_size),
                "decision_gates": dict(analysis.decision_gates),
                "statistical_tests": dict(analysis.statistical_tests),
                "seed_level_results": [
                    dict(row) for row in analysis.seed_level_results
                ],
            }
        if report is not None:
            bundle["report"] = {
                "id": str(report["id"]),
                "decision": str(report["decision"]),
                "decision_evidence": str(report["decision_evidence"]),
                "limitations": _parse_report_limitations(report.get("limitations")),
                "exact_commands": self._exact_commands(report),
            }
        return bundle

    @staticmethod
    def _manifest(root: Path, bundle: dict[str, Any]) -> dict[str, Any]:
        files = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files[str(path.relative_to(root))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return {
            "files": files,
            "evidence_bundle_id": bundle.get("lineage", {}).get("report_id"),
        }

    # ------------------------------------------------------------------
    # Model-assisted writing with deterministic authority (optional mode)
    # ------------------------------------------------------------------

    async def _model_draft(
        self,
        *,
        root,
        project,
        markdown,
        analysis,
        spec,
        papers,
        keys,
        gap,
        counter_claims,
        methodology,
        report,
        gaps,
        tables_md,
        decision,
        limitations,
        keywords,
        figure_svg,
        review_notes,
        seed,
        contract=None,
        synthesis=None,
        ledger=None,
    ) -> dict[str, Any]:
        """Outline → draft → deterministic audit → bounded revision.

        The model writes the scientific ARGUMENT; it never gains numerical
        authority: every number in the draft must appear verbatim in the
        allowed evidence set, every citation key must exist, overclaim
        patterns are rejected, the decision must be reported verbatim, and
        the draft must pass the same FastTrack template audit as the
        deterministic draft (six-part abstract, keywords, section order,
        >= 3 limitations, objective Results). Any surviving violation falls
        back to the deterministic draft — the audit failure is recorded,
        never masked.
        """
        allowed = (
            set(ledger.allowed_numbers)
            if ledger is not None and ledger.allowed_numbers
            else self._allowed_numbers(analysis, spec, papers)
        )
        deterministic_path = root / "manuscript_deterministic.md"
        deterministic_path.write_text(markdown, encoding="utf-8")
        facts = self._evidence_pack(
            analysis, spec, gap, methodology, report, papers, keys, tables_md,
            decision, limitations=limitations, keywords=keywords,
            figure_svg=figure_svg, review_notes=review_notes,
        )
        if contract is not None:
            facts += "\n\nCONTRIBUTION CONTRACT (the paper's argument):\n" + contract.to_markdown()
        if synthesis is not None and synthesis.papers:
            facts += (
                "\n\nLITERATURE SYNTHESIS (themed, from the reviewed corpus; use these "
                "citation keys and only these recorded claims):\n"
                + "\n".join(synthesis.to_markdown_lines())
            )
        if ledger is not None:
            facts += "\n\nCLAIM LEDGER (every factual sentence must trace here):\n" + json.dumps(
                ledger.to_dict(), indent=2, ensure_ascii=False
            )
        template_rules = (
            "The manuscript MUST follow the FastTrack Academic Research Paper "
            "Template (template-spec.json) — these are enforceable rules, not "
            "suggestions: (1) Title: specific, no vague phrases, at most 20 "
            "words, contains a keyword. (2) Abstract: 150-250 words with the six "
            "labeled parts Background; Gap in Knowledge; Contribution; Population "
            "& Methods; Results; Conclusion. (3) Keywords: 3-6 terms, only from "
            "the evidence pack. (4) Introduction in exactly three labeled "
            "subsections: 'Why this topic is relevant now', 'Existing state of "
            "knowledge and the gap', 'What this study did to fill the gap'; use "
            "PEER paragraphs (Point, Evidence, Explanation, Repeat/Link). "
            "(5) Literature Review only when evidence exists. (6) Methodology "
            "with the labeled fields Study Design, Data Collection, Sample & "
            "Eligibility, Data Analysis, Ethical Considerations, written "
            "cookbook-style (linear, reproducible, justifying each choice); a "
            "field not recorded is written as 'not specified at freeze time', "
            "never invented. (7) Results: objective data only — no interpretation "
            "words ('shows that', 'suggests', 'demonstrates the importance', "
            "'because', 'indicates', 'implies'); embed the provided table "
            "verbatim and reference the provided figure file. (8) Discussion: "
            "Recap of what we found, Study Limitations (at least 3), Strengths, "
            "Implications for future research; speculative claims ONLY inside a "
            "clearly labeled 'Possible interpretations and future work' part. "
            "(9) References, then Appendices listing the evidence bundle, raw "
            "seed metrics, and exact commands. (10) Use active voice."
        )
        outline_messages = [
            {
                "role": "system",
                "content": "You are a scientific writer building a contribution outline from validated findings only. You never invent numbers, citations, or results.",
            },
            {
                "role": "user",
                "content": f"[{MANUSCRIPT_WRITER_PROMPT_VERSION}-outline]\n{template_rules}\n\n{facts}\n\nProduce a section plan: for each section, the argument it makes and the exact evidence (ids/tables) supporting it.",
            },
        ]
        outline = (
            await self.router.complete(
                ModelRole.RESEARCH_REASONING,
                messages=outline_messages,
                project_id=project.id,
                prompt_version=MANUSCRIPT_WRITER_PROMPT_VERSION,
                operation="manuscript_outline",
            )
        ).text
        draft_messages = [
            {
                "role": "system",
                "content": (
                    "You are writing a complete scientific manuscript from verified evidence. HARD RULES: "
                    "copy every number EXACTLY as given in the evidence pack (no rounding, no new numbers); "
                    "cite only the provided citation keys; use the decision word verbatim; distinguish "
                    "observation vs interpretation vs hypothesis; report limitations, failures, and null "
                    f"results honestly; never use phrases like {', '.join(_OVERCLAIM_PATTERNS[:4])}; "
                    f"follow the FastTrack template rules.\n\n{template_rules}"
                ),
            },
            {
                "role": "user",
                "content": f"[{MANUSCRIPT_WRITER_PROMPT_VERSION}-draft]\nEVIDENCE PACK (the ONLY source of facts and numbers):\n{facts}\n\nCONTRIBUTION OUTLINE:\n{outline}\n\nWrite the full Markdown manuscript with: title, abstract (six labeled parts, 150-250 words), keywords, introduction (three labeled parts), literature review (when evidence exists), methodology (five labeled fields, cookbook-style), results (embed the provided results table verbatim; reference the figure file), discussion (recap, at least 3 limitations, strengths, future research, labeled speculative part, conclusion), references, appendices (evidence bundle, raw seed metrics, exact commands).",
            },
        ]
        draft_result = await self.router.complete(
            ModelRole.RESEARCH_REASONING,
            messages=draft_messages,
            project_id=project.id,
            prompt_version=MANUSCRIPT_WRITER_PROMPT_VERSION,
            operation="manuscript_draft",
        )
        draft = draft_result.text
        attempt_one_path = root / "manuscript_model_attempt_1.md"
        attempt_one_path.write_text(draft, encoding="utf-8")
        violations = self._audit_draft(draft, allowed, keys, decision, ledger=ledger)
        template_violations = self._template_violations(
            draft, literature_evidence=bool(papers), figure_svg=figure_svg
        )
        audit: dict[str, Any] = {
            "attempts": [],
            "fallback": None,
            "attempt_paths": [str(attempt_one_path)],
        }
        audit["attempts"].append({"violations": violations, "template": template_violations})
        if self._violation_total(violations, template_violations):
            revision_messages = draft_messages[:1] + [
                draft_messages[1],
                {"role": "assistant", "content": draft[:20000]},
                {
                    "role": "user",
                    "content": (
                        "AUDIT REJECTIONS (fix every one; change nothing else):\n"
                        f"- unsupported numbers: {violations['numbers'][:20]}\n"
                        f"- offending sentences: {violations.get('number_details', [])[:10]}\n"
                        f"- unknown citation keys: {violations['citations']}\n"
                        f"- overclaim phrases: {violations['overclaims']}\n"
                        f"- decision missing/changed: {violations['decision_missing']}\n"
                        f"- decision inverted/negated: {violations.get('decision_inverted', False)}\n"
                        f"- template violations: {template_violations}\n"
                        "Rewrite the manuscript obeying the hard rules and the "
                        "FastTrack template rules."
                    ),
                },
            ]
            revised_result = await self.router.complete(
                ModelRole.RESEARCH_REASONING,
                messages=revision_messages,
                project_id=project.id,
                prompt_version=MANUSCRIPT_WRITER_PROMPT_VERSION,
                operation="manuscript_revision",
            )
            draft = revised_result.text
            attempt_two_path = root / "manuscript_model_attempt_2.md"
            attempt_two_path.write_text(draft, encoding="utf-8")
            audit["attempt_paths"].append(str(attempt_two_path))
            violations = self._audit_draft(draft, allowed, keys, decision, ledger=ledger)
            template_violations = self._template_violations(
                draft, literature_evidence=bool(papers), figure_svg=figure_svg
            )
            audit["attempts"].append(
                {"violations": violations, "template": template_violations}
            )
        if self._violation_total(violations, template_violations):
            audit["fallback"] = "deterministic"
            rejected_path = root / "manuscript_model_rejected.md"
            rejected_path.write_text(draft, encoding="utf-8")
            return {
                "mode": "model",
                "used_model_draft": False,
                "audit": audit,
                "reason": "audit violations survived one revision; deterministic draft kept",
                "deterministic_path": str(deterministic_path),
                "rejected_draft_path": str(rejected_path),
            }
        draft_path = root / "manuscript.md"
        draft_path.write_text(draft, encoding="utf-8")
        return {
            "mode": "model",
            "used_model_draft": True,
            "audit": audit,
            "markdown_path": str(draft_path),
            "deterministic_path": str(deterministic_path),
        }

    @staticmethod
    def _allowed_numbers(analysis, spec, papers) -> set[float]:
        """Every number the draft may legitimately contain (float-normalized,
        including rounding variants — the evidence pack formats tables to a
        fixed precision the draft may quote verbatim)."""
        allowed: set[float] = set()

        def _register(value: float) -> None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return
            allowed.add(number)
            for precision in (2, 3, 4, 5, 6):
                allowed.add(round(number, precision))

        if spec is not None:
            for seed in spec.random_seeds:
                _register(seed)
            for value in dict(spec.minimum_meaningful_effect).values():
                _register(value)
            _register(len(spec.random_seeds))
        for paper in papers:
            if paper.get("year"):
                _register(paper["year"])
        if analysis is not None:
            for mapping in (
                analysis.baseline_metrics,
                analysis.proposed_metrics,
                analysis.effect_size,
            ):
                for value in dict(mapping).values():
                    _register(value)
            for bound in dict(analysis.confidence_intervals).values():
                try:
                    for item in bound:
                        _register(item)
                except (TypeError, ValueError):
                    continue
        return allowed

    def _audit_draft(
        self,
        draft: str,
        allowed: set[float],
        keys: dict[str, str],
        decision: str,
        *,
        ledger: ClaimLedger | None = None,
    ) -> dict[str, Any]:
        violations: dict[str, Any] = {
            "numbers": [],
            "number_details": [],
            "citations": [],
            "overclaims": [],
            "decision_missing": False,
            "decision_inverted": False,
        }
        if ledger is not None:
            details = ledger.audit_text(draft)
            violations["number_details"] = details
            violations["numbers"] = list(dict.fromkeys(item["token"] for item in details))
        else:
            for match in re.finditer(r"-?\d+(?:\.\d+)?", draft):
                token = match.group(0)
                value = float(token)
                # Accept the signed value (negative effect sizes / interval
                # bounds) or, as a fallback for date/label ranges like
                # "2019-2023", the unsigned value; both must trace to the
                # allowed evidence set.
                candidates = (value, abs(value))
                if any(c.is_integer() and c <= _STRUCTURAL_INT_MAX for c in candidates):
                    continue
                if any(
                    abs(candidate - allowed_value) < 1e-9
                    for candidate in candidates
                    for allowed_value in allowed
                ):
                    continue
                violations["numbers"].append(token)
        cited = set(re.findall(r"\[@([A-Za-z0-9_-]+)\]", draft))
        violations["citations"] = sorted(cited - set(keys.values()) - {"unknown"})
        lowered = draft.lower()
        violations["overclaims"] = [
            pattern for pattern in _OVERCLAIM_PATTERNS if pattern in lowered
        ]
        violations["decision_missing"] = decision.lower() not in lowered
        violations["decision_inverted"] = bool(
            re.search(
                r"\b(?:not|no|never)\s+(?:\w+\s+){0,2}" + re.escape(decision.lower()),
                lowered,
            )
        )
        return violations

    @staticmethod
    def _violation_total(violations: dict[str, Any], template_violations: list[str]) -> bool:
        return bool(
            violations.get("numbers")
            or violations.get("citations")
            or violations.get("overclaims")
            or violations.get("decision_missing")
            or violations.get("decision_inverted")
            or template_violations
        )

    def _template_violations(
        self, draft: str, *, literature_evidence: bool, figure_svg: str | None
    ) -> list[str]:
        """Run the selected format gate on a model draft."""
        if self.venue.template != "fasttrack":
            return venue_lint(draft, self.venue).violations
        audit = run_template_audit(
            draft,
            complete=True,
            literature_evidence=literature_evidence,
            figure_paths=tuple([figure_svg] if figure_svg else []),
        )
        return audit.violations

    def _evidence_pack(
        self,
        analysis,
        spec,
        gap,
        methodology,
        report,
        papers,
        keys,
        tables_md,
        decision,
        *,
        limitations: list[str] | None = None,
        keywords: list[str] | None = None,
        figure_svg: str | None = None,
        review_notes: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        """The ONLY facts and numbers a model draft may use — pre-formatted."""
        block = (methodology or {}).get("methodology", methodology or {})
        lines = [
            f"RESEARCH QUESTION: {gap['statement'] if gap else ''}",
            f"WHY UNSOLVED: {gap['why_unsolved'] if gap else ''}",
            f"FALSIFICATION CRITERION: {gap['falsification_test'] if gap else ''}",
            f"HYPOTHESIS: {spec.hypothesis if spec else ''}",
            f"CAUSAL MECHANISM: {spec.causal_mechanism if spec else ''}",
            f"METHOD (frozen): algorithm='{block.get('algorithm_outline') or ''}' procedure='{block.get('procedure') or ''}'",
            f"PREDICTED OBSERVATIONS (hypothesis, NOT measurement): {block.get('predicted_observations') or ''}",
            f"DATASET: {spec.dataset_identity if spec else ''} | SPLIT: {spec.split_definition if spec else ''} | SEEDS: {list(spec.random_seeds) if spec else []}",
            f"METRICS: {list(spec.metrics) if spec else []} direction {dict(spec.metric_direction) if spec else {}} minimum meaningful effect {dict(spec.minimum_meaningful_effect) if spec else {}}",
            f"RESULTS TABLE (embed VERBATIM):\n{tables_md}",
            f"DECISION (use this word verbatim): {decision}",
            f"DECISION EVIDENCE: {report['decision_evidence'] if report else ''}",
            f"UNCERTAINTY: {report['uncertainty'] if report else ''}",
            f"LIMITATIONS: {limitations if limitations else _parse_report_limitations(report.get('limitations') if report else None)}",
            f"KEYWORDS (use only these terms): {keywords if keywords else []}",
        ]
        if figure_svg:
            lines.append(
                f"FIGURE FILE (reference it in the Results section): {figure_svg}"
            )
        if report is not None:
            lines.append(
                f"EXACT COMMANDS (Appendix C): {self._exact_commands(report) or 'no command recorded'}"
            )
        for paper in papers:
            paper_id = str(paper.get("id"))
            notes = (review_notes or {}).get(paper_id, {})
            abstract = " ".join(str(paper.get("abstract") or "").split())[:900]
            lines.append(
                f"PAPER [{keys[paper['id']]}] title='{paper['title']}' "
                f"year={paper.get('year') or 'n.d.'} "
                f"venue='{paper.get('venue') or ''}' "
                f"abstract='{abstract}' "
                f"problem='{str(notes.get('problem') or notes.get('research_question') or '')[:300]}' "
                f"method='{str(notes.get('method') or paper.get('method') or '')[:300]}' "
                f"protocol='{str(notes.get('experimental_protocol') or '')[:300]}' "
                f"limitations='{str(notes.get('limitations_json') or '')[:300]}' "
                f"results='{str(notes.get('results_json') or '')[:300]}'"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Verification passes
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_citations(
        markdown: str, latex: str, bib: str, keys: dict[str, str]
    ) -> None:
        import re

        valid = set(keys.values())
        used_md = set(re.findall(r"\[@([A-Za-z0-9_-]+)\]", markdown))
        leaked = re.findall(r"\]\]", markdown)
        if leaked:
            raise ValueError(
                "manuscript_citation_error: malformed citation brackets (']]')"
            )
        used_tex = set(re.findall(r"\\cite\{([A-Za-z0-9_,\s-]+)\}", latex))
        used = used_md | {key.strip() for group in used_tex for key in group.split(",")}
        unknown = used - valid - {"unknown"}
        if unknown:
            raise ValueError(
                f"manuscript_citation_error: unknown citation keys {sorted(unknown)}"
            )
        missing_from_bib = {key for key in valid if f"{{{key}," not in bib}
        if missing_from_bib:
            raise ValueError(
                f"manuscript_bib_error: keys missing from BibTeX {sorted(missing_from_bib)}"
            )

    @staticmethod
    def _verify_consistency(markdown: str, decision: str) -> None:
        lowered = markdown.lower()
        if decision.lower() not in lowered:
            raise ValueError(
                "manuscript_consistency_error: decision missing from manuscript body"
            )
        if re.search(
            r"\b(?:not|no|never)\s+(?:\w+\s+){0,2}" + re.escape(decision.lower()),
            lowered,
        ):
            raise ValueError(
                "manuscript_consistency_error: decision is negated/inverted in the manuscript body"
            )

    # ------------------------------------------------------------------
    # PDF compilation (optional, verified)
    # ------------------------------------------------------------------

    @staticmethod
    def _compile(latex_path: Path) -> Path | None:
        compiler = shutil.which("tectonic") or shutil.which("pdflatex")
        if compiler is None:
            return None
        workdir = latex_path.parent
        commands = (
            [[compiler, latex_path.name]]
            if "tectonic" in compiler
            else [
                [compiler, "-interaction=nonstopmode", latex_path.name],
                [shutil.which("bibtex") or "bibtex", "manuscript"],
                [compiler, "-interaction=nonstopmode", latex_path.name],
                [compiler, "-interaction=nonstopmode", latex_path.name],
            ]
        )
        try:
            for command in commands:
                completed = subprocess.run(
                    command, cwd=str(workdir), capture_output=True, timeout=180
                )
                if completed.returncode != 0 and "bibtex" not in command[0]:
                    return None
            pdf = workdir / "manuscript.pdf"
            return pdf if pdf.is_file() and pdf.stat().st_size > 0 else None
        except (OSError, subprocess.SubprocessError):
            # A missing/broken compiler or a timeout is reported as "no
            # verified PDF", never as a crash after the manuscript was written.
            return None

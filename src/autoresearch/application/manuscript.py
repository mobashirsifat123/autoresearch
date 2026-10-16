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
from pathlib import Path
from typing import Any
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    ManuscriptRecord,
    PipelineKind,
    RunStatus,
)
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.redaction import redact_text
from autoresearch.storage.repository import ResearchRepository

MANUSCRIPT_PROMPT_VERSION = "manuscript-v1-deterministic"
MANUSCRIPT_WRITER_PROMPT_VERSION = "manuscript-writer-v2-model"

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


class ManuscriptService:
    def __init__(
        self,
        repository: ResearchRepository,
        router=None,
        writer_mode: str = "deterministic",
    ) -> None:
        self.repository = repository
        self.router = router
        # "deterministic" (default): field-assembled manuscript, zero model
        # calls. "model": model-assisted scientific argument through the
        # router, gated by a deterministic numeric/citation/overclaim audit
        # that falls back to the deterministic draft on any violation.
        self.writer_mode = (
            writer_mode
            if writer_mode in ("deterministic", "model")
            else "deterministic"
        )

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    async def generate(self, project_id: str) -> dict[str, Any]:
        project = self.repository.get_project(project_id)
        if not project:
            raise ValueError(f"project_not_found: project '{project_id}' not found")
        lineage, gaps = self._resolve_lineage(project_id)
        run = self.repository.create_run(
            project_id, PipelineKind.MANUSCRIPT, {"lineage": lineage}
        )
        try:
            manuscript = await self._build(project, run.id, lineage, gaps)
            self.repository.save_manuscript(manuscript)
            self.repository.finish_run(run.id)
            quality = self._quality_report(manuscript, project_id)
            return {
                "manuscript_id": manuscript.id,
                "project_id": project_id,
                "run_id": run.id,
                "status": manuscript.status,
                "title": manuscript.title,
                "decision": manuscript.decision,
                "lineage": lineage,
                "evidence_gaps": json.loads(manuscript.gaps_json),
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
            report = self.repository.row(
                "SELECT * FROM experiment_reports WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                (spec.run_id,),
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
        papers = self._lineage_papers(gap)
        claims = self._lineage_claims(gap)
        candidate = (
            self.repository.get_novelty_candidate(lineage["candidate_id"])
            if lineage.get("candidate_id")
            else None
        )
        methodology = self._methodology_record(lineage)
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

        complete = not gaps and analysis is not None and report is not None
        decision = str(report["decision"]) if report else "INCOMPLETE"
        title = self._title(candidate, complete)

        keys = {
            paper["id"]: f"paper{index}" for index, paper in enumerate(papers, start=1)
        }
        bib = self._bibtex(papers, keys)
        tables_md, tables_tex = self._tables(analysis)

        markdown = self._markdown(
            project,
            title,
            complete,
            decision,
            gap,
            papers,
            claims,
            keys,
            candidate,
            methodology,
            spec,
            analysis,
            report,
            gaps,
            tables_md,
        )
        latex = self._latex(
            title=title,
            papers=papers,
            keys=keys,
            tables_tex=tables_tex,
            analysis=analysis,
            report=report,
            gap=gap,
            candidate=candidate,
            methodology=methodology,
            spec=spec,
            gaps=gaps,
            complete=complete,
            claims=claims,
        )

        bundle = self._evidence_bundle(lineage, papers, claims, analysis, report)
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

        self._verify_citations(markdown, latex, bib, keys)
        if complete:
            self._verify_consistency(markdown, decision)

        # Model-assisted scientific writing (optional): a model drafts the
        # argument; a deterministic audit keeps numerical/citation authority.
        writer_report: dict[str, Any] = {
            "mode": "deterministic",
            "used_model_draft": False,
            "audit": None,
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
                methodology=methodology,
                report=report,
                gaps=gaps,
                tables_md=tables_md,
                decision=decision,
            )
            if writer_report.get("used_model_draft") and writer_report.get(
                "markdown_path"
            ):
                markdown_path = Path(str(writer_report["markdown_path"]))
                markdown = markdown_path.read_text(encoding="utf-8")
            audit_path = root / "writing_audit.json"
            audit_path.write_text(
                json.dumps(writer_report, indent=2, default=str), encoding="utf-8"
            )

        manifest = self._manifest(root, bundle)
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )

        pdf_path = self._compile(latex_path) if complete else None

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
                "status": "complete" if complete else "incomplete",
                "decision": decision,
                "gaps": gaps,
            },
        )
        return ManuscriptRecord(
            id=manuscript_id,
            project_id=project.id,
            run_id=run_id,
            title=title,
            status="complete" if complete else "incomplete",
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
                "SELECT pr.paper_id AS paper_id, p.checksum AS checksum FROM paper_reviews pr "
                "JOIN papers p ON p.id = pr.paper_id WHERE pr.project_id=? "
                "ORDER BY pr.confidence DESC, pr.paper_id ASC",
                (project_id,),
            )
        except Exception:  # noqa: BLE001 - table/column absence is an honest empty set
            return []
        if not rows:
            return []
        downloads = Path(".autoresearch/data/downloads")
        if not downloads.is_dir():
            return []
        from autoresearch.ingestion.document_parser import DocumentParser

        parser = DocumentParser()
        texts: list[str] = []
        for row in rows:
            if len(texts) >= limit:
                break
            checksum = str(row["checksum"] or "")
            if len(checksum) < 12:
                continue
            candidates = list(downloads.glob(f"**/{checksum[:12]}*.pdf")) or list(
                downloads.glob(f"{checksum[:32]}/*.pdf")
            )
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
            }

        exemplar_texts = self._exemplar_texts(project_id)
        audit = prose_audit(markdown, exemplar_texts)
        report["prose_audit"] = {
            "verdict": audit.verdict,
            "heuristic": audit.heuristic,
            "tell_density": audit.metrics.tell_density if audit.metrics else None,
            "tells": [dict(t) for t in audit.tells],
            "band_deviations": [dict(d) for d in audit.band_deviations],
            "rewrite_guidance": list(audit.rewrite_guidance),
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
    # Deterministic content builders
    # ------------------------------------------------------------------

    @staticmethod
    def _title(candidate, complete: bool) -> str:
        method = (
            " ".join(str(candidate.method).split())
            if candidate
            else "Unspecified method"
        )
        method = method[:140].rstrip(".,;:")
        suffix = "" if complete else " (incomplete draft)"
        return f"{method}: an evidence-backed evaluation{suffix}"

    def _markdown(
        self,
        project,
        title,
        complete,
        decision,
        gap,
        papers,
        claims,
        keys,
        candidate,
        methodology,
        spec,
        analysis,
        report,
        gaps,
        tables_md,
    ) -> str:
        lines: list[str] = [f"# {title}", ""]
        lines += ["## Abstract", ""]
        if complete and analysis is not None and report is not None:
            metric_names = ", ".join(sorted(analysis.proposed_metrics.keys()))
            effects = "; ".join(
                f"{name}: mean difference {value:+.4f}"
                for name, value in sorted(analysis.effect_size.items())
            )
            lines.append(
                f"We investigated the research question: {gap['statement']} "
                f"Using the frozen methodology, we conducted a preregistered paired-seed evaluation "
                f"on metrics ({metric_names}). Measured effects: {effects}. The deterministic "
                f"decision gates classified the hypothesis as **{decision}**. All numerical claims "
                f"trace to checksummed measurement artifacts; see the reproducibility appendix."
            )
        else:
            lines.append(
                "This manuscript is explicitly INCOMPLETE: the experimental evidence chain required for "
                "scientific conclusions is not present in the durable record. Missing: "
                + "; ".join(gaps)
                + ". "
                "No result is fabricated to fill these gaps."
            )
        lines += ["", "## 1. Introduction and Research Questions", ""]
        if gap is not None:
            lines.append(f"**Research question (ID {gap['id']}):** {gap['statement']}")
            lines.append("")
            lines.append(
                f"Why it matters / what remains unresolved: {gap['why_unsolved'] or 'not recorded'}"
            )
            lines.append("")
            lines.append(
                f"Falsification criterion: {gap['falsification_test'] or 'not recorded'}"
            )
            if papers:
                lines.append("")
                lines.append(
                    "The question is grounded in "
                    + ", ".join(f"[@{keys[paper['id']]}]" for paper in papers)
                    + "."
                )
        else:
            lines.append("No completed review exists for this project.")
        lines += ["", "## 2. Related Work", ""]
        if papers:
            for paper in papers:
                key = keys[paper["id"]]
                year = f" ({paper['year']})" if paper.get("year") else ""
                lines.append(
                    f"- **{paper['title']}**{year} [@{key}]: {str(paper.get('abstract') or '')[:400]}"
                )
        else:
            lines.append("No supporting papers recorded.")
        if claims:
            lines += ["", "Verified evidence claims (verbatim-checked excerpts):", ""]
            for claim in claims[:20]:
                lines.append(
                    f"- [{claim['claim_type']}] {claim['statement']} [@{keys.get(claim['paper_id'], 'unknown')}]"
                )
        lines += ["", "## 3. Methodology", ""]
        if methodology is not None:
            block = methodology.get("methodology", methodology)
            for field in (
                "hypothesis_statement",
                "causal_mechanism",
                "algorithm_outline",
                "procedure",
                "predicted_observations",
                "falsification_conditions",
                "expected_resource_demands",
            ):
                value = block.get(field)
                if value:
                    lines.append(f"**{field.replace('_', ' ').title()}:** {value}")
                    lines.append("")
            lines.append("_Predicted effects are hypotheses, not measurements._")
        elif candidate is not None:
            lines.append(f"Selected candidate method: {candidate.method}")
        else:
            lines.append("No frozen methodology available.")
        lines += ["## 4. Experimental Setup", ""]
        if spec is not None:
            lines.append(f"- Hypothesis under test: {spec.hypothesis}")
            lines.append(
                f"- Datasets: {spec.dataset_identity} (checksum {spec.dataset_checksum[:16]}…)"
            )
            lines.append(f"- Split: {spec.split_definition}")
            lines.append(
                f"- Metrics: {', '.join(spec.metrics)} (direction: {dict(spec.metric_direction)})"
            )
            lines.append(f"- Seeds: {', '.join(str(s) for s in spec.random_seeds)}")
            lines.append(f"- Statistical plan: {spec.statistical_plan}")
            lines.append(
                f"- Interpretation rules (preregistered): {'; '.join(spec.interpretation_rules) or 'not recorded'}"
            )
            lines.append(
                f"- Lineage: candidate={spec.candidate_id}; methodology_freeze={spec.methodology_freeze_id}; gap={spec.gap_id}"
            )
        else:
            lines.append("No experiment specification available.")
        lines += ["", "## 5. Results", ""]
        if analysis is not None:
            lines.append(tables_md)
            lines.append("")
            gates = ", ".join(
                f"{name}: {'PASS' if passed else 'FAIL'}"
                for name, passed in dict(analysis.decision_gates).items()
            )
            lines.append(f"Decision gates: {gates}.")
            lines.append("")
            lines.append(
                f"Statistical tests (deterministic, exact): {json.dumps(dict(analysis.statistical_tests), sort_keys=True)}"
            )
            lines.append("")
            lines.append(
                f"**Hypothesis support: {analysis.hypothesis_support_assessment}** (report decision: {decision}; evidence: {report['decision_evidence'] if report else 'n/a'})."
            )
        else:
            lines.append("No validated measurements: results cannot be reported.")
        lines += ["", "## 6. Ablations and Robustness", ""]
        lines.append(
            "Ablation contributions are reported only from measured descendant runs; none are present in this lineage."
            if analysis is not None and not dict(analysis.ablation_contributions)
            else (
                "No analysis available."
                if analysis is None
                else f"Ablations: {dict(analysis.ablation_contributions)}"
            )
        )
        lines += ["", "## 7. Discussion", ""]
        if report is not None:
            lines.append(
                f"The deterministic decision gates classified the outcome as **{report['decision']}**: {report['decision_evidence']}"
            )
            lines.append("")
            lines.append(f"Observed uncertainty: {report['uncertainty']}")
            if str(report["decision"]).lower() in {
                "refuted",
                "no_effect",
                "partially_supported",
            }:
                lines.append("")
                lines.append(
                    "This is a negative/null-leaning result and is reported as a valid scientific outcome; it is not a failure of the workflow."
                )
        else:
            lines.append("Discussion requires an analyzed experiment report.")
        lines += ["", "## 8. Limitations and Threats to Validity", ""]
        lines.append(
            str(report["limitations"])
            if report
            else "Not assessable without an analyzed report."
        )
        lines.append("")
        lines.append(
            "Observations, interpretations, and hypotheses are distinguished throughout; LLM-generated prose was not used to state any numerical result."
        )
        lines += ["", "## 9. Conclusion", ""]
        lines.append(
            f"Decision: {decision}."
            if complete
            else f"This manuscript remains INCOMPLETE. Missing evidence: {'; '.join(gaps)}."
        )
        lines += ["", "## Data and Code Availability", ""]
        lines.append(
            f"All artifacts are in the local AutoResearch store for project {project.id}: papers, verified claims, methodology freeze, experiment specification, measurement artifacts (checksummed), statistical analysis, and this manuscript's evidence bundle."
        )
        lines += ["", "## References", ""]
        for paper in papers:
            lines.append(f"[@{keys[paper['id']]}]")
        lines += ["", "## Reproducibility Appendix", ""]
        lines.append(
            "Evidence bundle: see `evidence_bundle.json` next to this manuscript; manifest with SHA-256 checksums in `manifest.json`."
        )
        lines.append(
            f"Exact worker commands and identities are recorded in the experiment report (`{report['id'] if report else 'n/a'}`)."
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _tex(value: Any) -> str:
        return str(value).replace("&", "\\&").replace("%", "\\%").replace("_", "\\_")

    def _latex(
        self,
        *,
        title,
        papers,
        keys,
        tables_tex,
        analysis,
        report,
        gap,
        candidate,
        methodology,
        spec,
        gaps,
        complete,
        claims,
    ) -> str:
        decision = str(report["decision"]) if report else "INCOMPLETE"

        def cite(key: str) -> str:
            return "\\cite{" + key + "}"

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
        if analysis is not None and gap is not None:
            effects = ", ".join(
                name + " " + f"{value:+.4f}"
                for name, value in sorted(dict(analysis.effect_size).items())
            )
            abstract = (
                self._tex(gap["statement"])
                + " Measured effects: "
                + effects
                + ". Decision: "
                + decision
                + "."
            )
        elif gap is not None:
            abstract = (
                self._tex(gap["statement"])
                + " No validated measurements; this draft is explicitly incomplete."
            )
        else:
            abstract = "No completed review exists."
        if methodology is not None:
            block = methodology.get("methodology", methodology)
            method_text = (
                " ".join(
                    self._tex(value)
                    for value in block.values()
                    if isinstance(value, str)
                )
                or "See the methodology freeze artifact."
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
                + ", ".join(spec.metrics)
                + ". Seeds: "
                + ", ".join(str(seed) for seed in spec.random_seeds)
                + "."
            )
        else:
            setup = "No experiment specification available."
        discussion = (
            (
                "Decision gates classified the outcome as "
                + decision
                + ": "
                + self._tex(report["decision_evidence"])
            )
            if report
            else "Discussion requires an analyzed experiment report."
        )
        limitations = (
            self._tex(report["limitations"])
            if report
            else "Not assessable without an analyzed report."
        )
        conclusion = (
            ("Decision: " + decision + ".")
            if complete
            else (
                "This manuscript remains INCOMPLETE; missing evidence: "
                + self._tex("; ".join(gaps))
                + "."
            )
        )
        return (
            "\\documentclass[11pt]{article}\n"
            "\\usepackage[margin=1in]{geometry}\n"
            "\\usepackage{booktabs}\n"
            "\\usepackage[numbers]{natbib}\n"
            "\\title{" + self._tex(title) + "}\n"
            "\\date{\\today}\n"
            "\\begin{document}\n"
            "\\maketitle\n"
            "\\begin{abstract}" + abstract + "\\end{abstract}\n"
            "\\section{Introduction and Research Questions}\n"
            + (self._tex(gap["statement"]) if gap else "No completed review exists.")
            + "\n"
            "\\section{Related Work}\n\\begin{itemize}\n"
            + related
            + "\n\\end{itemize}\n"
            "Verified evidence claims:\n\\begin{itemize}\n"
            + claim_items
            + "\n\\end{itemize}\n"
            "\\section{Methodology}\n" + method_text + "\n"
            "\\section{Experimental Setup}\n" + setup + "\n"
            "\\section{Results}\n"
            + (
                tables_tex
                if analysis is not None
                else "No validated measurements: results cannot be reported."
            )
            + "\n"
            "\\section{Discussion}\n" + discussion + "\n"
            "\\section{Limitations}\n" + limitations + "\n"
            "\\section{Conclusion}\n" + conclusion + "\n"
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
    def _bibtex(papers: list[dict[str, Any]], keys: dict[str, str]) -> str:
        entries = []
        for paper in papers:
            key = keys[paper["id"]]
            authors = (
                " and ".join(json.loads(paper.get("authors_json") or "[]"))
                or "Unknown authors"
            )
            title = (
                str(paper.get("title") or "Untitled")
                .replace("&", "\\&")
                .replace("{", "\\{")
                .replace("}", "\\}")
            )
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
                fields.append(f"journal = {{{venue}}}")
            if doi:
                fields.append(f"doi = {{{doi}}}")
            if url:
                fields.append(f"url = {{{url}}}")
            kind = (
                "article"
                if venue and venue.lower() not in ("unknown", "none")
                else "misc"
            )
            entries.append(f"@{kind}{{{key},\n  " + ",\n  ".join(fields) + ",\n}")
        return "\n\n".join(entries) + "\n"

    def _evidence_bundle(
        self, lineage: dict[str, Any], papers, claims, analysis, report
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
        methodology,
        report,
        gaps,
        tables_md,
        decision,
    ) -> dict[str, Any]:
        """Outline → draft → deterministic audit → bounded revision.

        The model writes the scientific ARGUMENT; it never gains numerical
        authority: every number in the draft must appear verbatim in the
        allowed evidence set, every citation key must exist, overclaim
        patterns are rejected, and the decision must be reported verbatim.
        Any surviving violation falls back to the deterministic draft — the
        audit failure is recorded, never masked.
        """
        allowed = self._allowed_numbers(analysis, spec, papers)
        deterministic_path = root / "manuscript_deterministic.md"
        deterministic_path.write_text(markdown, encoding="utf-8")
        facts = self._evidence_pack(
            analysis, spec, gap, methodology, report, papers, keys, tables_md, decision
        )
        outline_messages = [
            {
                "role": "system",
                "content": "You are a scientific writer building a contribution outline from validated findings only. You never invent numbers, citations, or results.",
            },
            {
                "role": "user",
                "content": f"[{MANUSCRIPT_WRITER_PROMPT_VERSION}-outline]\n{facts}\n\nProduce a section plan: for each section, the argument it makes and the exact evidence (ids/tables) supporting it.",
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
                    f"results honestly; never use phrases like {', '.join(_OVERCLAIM_PATTERNS[:4])}."
                ),
            },
            {
                "role": "user",
                "content": f"[{MANUSCRIPT_WRITER_PROMPT_VERSION}-draft]\nEVIDENCE PACK (the ONLY source of facts and numbers):\n{facts}\n\nCONTRIBUTION OUTLINE:\n{outline}\n\nWrite the full Markdown manuscript with: title, abstract, introduction and research question, related work, methodology, experimental setup, results (embed the provided results table verbatim), discussion, limitations, conclusion, references.",
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
        violations = self._audit_draft(draft, allowed, keys, decision)
        audit: dict[str, Any] = {"attempts": [], "fallback": None}
        if (
            violations["numbers"]
            or violations["citations"]
            or violations["overclaims"]
            or violations["decision_missing"]
        ):
            revision_messages = draft_messages[:1] + [
                draft_messages[1],
                {"role": "assistant", "content": draft[:20000]},
                {
                    "role": "user",
                    "content": (
                        "AUDIT REJECTIONS (fix every one; change nothing else):\n"
                        f"- unsupported numbers: {violations['numbers'][:20]}\n"
                        f"- unknown citation keys: {violations['citations']}\n"
                        f"- overclaim phrases: {violations['overclaims']}\n"
                        f"- decision missing/changed: {violations['decision_missing']}\n"
                        "Rewrite the manuscript obeying the hard rules."
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
            violations = self._audit_draft(draft, allowed, keys, decision)
        audit["attempts"].append({"violations": violations})
        if (
            violations["numbers"]
            or violations["citations"]
            or violations["overclaims"]
            or violations["decision_missing"]
        ):
            audit["fallback"] = "deterministic"
            return {
                "mode": "model",
                "used_model_draft": False,
                "audit": audit,
                "reason": "audit violations survived one revision; deterministic draft kept",
                "deterministic_path": str(deterministic_path),
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
        self, draft: str, allowed: set[float], keys: dict[str, str], decision: str
    ) -> dict[str, Any]:
        violations: dict[str, Any] = {
            "numbers": [],
            "citations": [],
            "overclaims": [],
            "decision_missing": False,
        }
        for match in re.finditer(r"-?\d+(?:\.\d+)?", draft):
            token = match.group(0)
            value = float(token)
            # Accept the signed value (negative effect sizes / interval bounds)
            # or, as a fallback for date/label ranges like "2019-2023", the
            # unsigned value; both must trace to the allowed evidence set.
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
        return (
            violations
            if (
                violations["numbers"]
                or violations["citations"]
                or violations["overclaims"]
                or violations["decision_missing"]
            )
            else {
                "numbers": [],
                "citations": [],
                "overclaims": [],
                "decision_missing": False,
            }
        )

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
            f"LIMITATIONS: {report['limitations'] if report else ''}",
        ]
        for paper in papers:
            lines.append(
                f"PAPER [{keys[paper['id']]}] title='{paper['title']}' year={paper.get('year') or 'n.d.'} method='{str(paper.get('method') or '')[:200]}'"
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
        if decision not in markdown:
            raise ValueError(
                "manuscript_consistency_error: decision missing from manuscript body"
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
        for command in commands:
            completed = subprocess.run(
                command, cwd=str(workdir), capture_output=True, timeout=180
            )
            if completed.returncode != 0 and "bibtex" not in command[0]:
                return None
        pdf = workdir / "manuscript.pdf"
        return pdf if pdf.is_file() and pdf.stat().st_size > 0 else None

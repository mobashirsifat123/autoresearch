"""Purpose: Freezes a selected novelty candidate into a versioned, structured
experimental methodology.

"Finalize methodology" means: freeze a concrete, testable specification of
what will be implemented and compared. It does NOT certify novelty or
scientific truth — the frozen record always carries an explicit uncertainty
statement and evidence linkage is bookkeeping, not validation.
"""

from __future__ import annotations

import json
from uuid import uuid4

from autoresearch.domain.models import (
    ArtifactProvenance,
    ArtifactReference,
    MethodologyFreeze,
    NoveltyCandidate,
    content_checksum,
    utc_now,
)
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter
from autoresearch.review.evidence_context import build_evidence_context
from autoresearch.storage.repository import ResearchRepository

PROMPT_VERSION = "methodology-freeze-v2"

METHODOLOGY_SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis_statement": {"type": "string"},
        "predicted_observations": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "proposed_mechanism": {"type": "string"},
        "competing_explanations": {"type": "array", "items": {"type": "string"}},
        "falsification_conditions": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "closest_prior_methods": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paper_id": {"type": "string"},
                    "method_summary": {"type": "string"},
                    "proposed_difference": {"type": "string"},
                },
                "required": ["paper_id", "method_summary", "proposed_difference"],
                "additionalProperties": False,
            },
        },
        "algorithm_outline": {"type": "string", "description": "Concrete algorithm: pseudocode, equations, or step-by-step procedure"},
        "inputs": {"type": "string"},
        "outputs": {"type": "string"},
        "components": {"type": "array", "items": {"type": "string"}},
        "procedure": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "Training/analysis steps in order"},
        "implementation_requirements": {"type": "string"},
        "expected_resource_demands": {"type": "string"},
        "supporting_evidence_claim_ids": {"type": "array", "items": {"type": "string"}},
        "uncertainty_statement": {"type": "string", "description": "What remains uncertain about novelty and scientific validity; never empty"},
        # FastTrack template Methodology fields (template-spec.json T061-T069):
        # the manuscript Methodology section renders these verbatim; missing
        # values are filled with an honest marker and reported as limitations.
        "study_design": {"type": "string", "description": "Qualitative, quantitative, or mixed-methods study design"},
        "data_collection": {"type": "string", "description": "Databases, inclusion/exclusion criteria, and search strategy"},
        "sample_and_eligibility": {"type": "string", "description": "Study population and selection process"},
        "data_analysis": {"type": "string", "description": "Analytical approach (thematic analysis, statistical tests, ...)"},
        "ethical_considerations": {"type": "string", "description": "Ethical approvals or considerations (informed consent, ...)"},
        # Publication-grade design (recorded at freeze; missing values become
        # explicit publication_gaps, never fabricated later).
        "contribution_statement": {"type": "string"},
        "novelty_delta": {"type": "string"},
        "datasets": {"type": "array", "items": {"type": "string"}},
        "baselines": {"type": "array", "items": {"type": "string"}},
        "ablations": {"type": "array", "items": {"type": "string"}},
        "robustness_tests": {"type": "array", "items": {"type": "string"}},
        "power_analysis": {"type": "string"},
        "one_dataset_justification": {"type": "string"},
        "expected_figures_tables": {"type": "array", "items": {"type": "string"}},
        "failure_modes": {"type": "array", "items": {"type": "string"}},
        "stop_conditions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "hypothesis_statement", "predicted_observations", "proposed_mechanism",
        "competing_explanations", "falsification_conditions", "closest_prior_methods",
        "algorithm_outline", "inputs", "outputs", "components", "procedure",
        "implementation_requirements", "expected_resource_demands",
        "supporting_evidence_claim_ids", "uncertainty_statement",
    ],
    "additionalProperties": False,
}

_FIELD_CONTRACT = (
    "Return ONLY a JSON object with exactly these keys:\n"
    "- hypothesis_statement: string (precise, testable)\n"
    "- predicted_observations: array of strings (what we expect to measure)\n"
    "- proposed_mechanism: string\n"
    "- competing_explanations: array of strings\n"
    "- falsification_conditions: array of strings\n"
    "- closest_prior_methods: array of {paper_id, method_summary, proposed_difference}\n"
    "- algorithm_outline: string (pseudocode/equations/steps; concrete)\n"
    "- inputs: string\n- outputs: string\n- components: array of strings\n"
    "- procedure: array of strings (ordered training/analysis steps)\n"
    "- implementation_requirements: string\n"
    "- expected_resource_demands: string\n"
    "- supporting_evidence_claim_ids: array of claim IDs from the supplied evidence only\n"
    "- uncertainty_statement: string (mandatory, non-empty)\n"
    "- study_design: string (qualitative/quantitative/mixed-methods; the FastTrack template's Study Design field)\n"
    "- data_collection: string (databases, inclusion/exclusion criteria, search strategy; the template's Data Collection field)\n"
    "- sample_and_eligibility: string (study population and selection process; the template's Sample & Eligibility field)\n"
    "- data_analysis: string (analytical approach; the template's Data Analysis field)\n"
    "- ethical_considerations: string (ethical approvals or considerations; the template's Ethical Considerations field)\n"
    "- contribution_statement: string (one sentence: what this paper contributes)\n"
    "- novelty_delta: string (closest prior work and the precise difference)\n"
    "- datasets: array of strings (>=2, or one_dataset_justification)\n"
    "- baselines: array of strings (>=2 named external baselines)\n"
    "- ablations: array of strings (>=1)\n"
    "- robustness_tests: array of strings (>=1)\n"
    "- power_analysis: string (effect size, variance assumption, target power, required seeds)\n"
    "- one_dataset_justification: string (required only when datasets has one item)\n"
    "- expected_figures_tables: array of strings (main result, ablation, comparison)\n"
    "- failure_modes: array of strings (the strongest ways this design can fail)\n"
    "- stop_conditions: array of strings (when to stop or declare the study inconclusive)\n"
    "Every key must be present. Do not invent paper or claim IDs that were not supplied.\n"
    "If a template field genuinely does not apply to this computational study, write exactly "
    "'not specified at freeze time' — the manuscript reports it as a limitation, never invents it."
)


class MethodologyFreezer:
    def __init__(self, repository: ResearchRepository, router: ModelRouter) -> None:
        self.repository = repository
        self.router = router

    async def freeze(
        self,
        candidate: NoveltyCandidate,
        *,
        selection_rationale: str,
        portfolio_id: str | None = None,
        venue_profile=None,
    ) -> MethodologyFreeze:
        """Freeze the candidate's methodology as the next version.

        Grounded in the same verified evidence context as generation; the
        prior-art closest methods come from the stored assessment.
        """
        gap_row = (
            self.repository.row("SELECT * FROM research_gaps WHERE id=? AND project_id=?", (candidate.gap_id, candidate.project_id))
            if candidate.gap_id else None
        )
        try:
            import json as _json
            supporting_paper_ids = tuple(_json.loads(gap_row["supporting_paper_ids_json"])) if gap_row else tuple(candidate.provenance.source_evidence)
            supporting_claim_ids = tuple(_json.loads(gap_row["supporting_claim_ids_json"])) if gap_row else ()
            counter_ids = tuple(_json.loads(gap_row.get("counter_evidence_claim_ids_json") or "[]")) if gap_row else ()
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"methodology_invalid: malformed gap JSON for gap {candidate.gap_id}"
            ) from exc
        evidence_context = build_evidence_context(
            self.repository,
            project_id=candidate.project_id,
            supporting_paper_ids=supporting_paper_ids,
            supporting_claim_ids=supporting_claim_ids,
            counter_claim_ids=counter_ids,
            candidate_id=candidate.id,
            gap_row=gap_row,
        )
        from autoresearch.review.evidence_context import format_evidence_context
        grounding = format_evidence_context(evidence_context)

        messages = [
            {
                "role": "system",
                "content": (
                    "You convert a selected research candidate into a frozen, versioned experimental "
                    "methodology. Be concrete: algorithmic steps, equations or pseudocode, inputs/outputs, "
                    "and resource demands. Freezing does not certify novelty: the uncertainty_statement "
                    "must state what remains unverified."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Prompt version: {PROMPT_VERSION}\n"
                    f"Candidate strategy: {candidate.strategy}\n"
                    f"Candidate method: {candidate.method}\n"
                    f"Causal mechanism: {candidate.causal_mechanism}\n"
                    f"Difference from existing: {candidate.difference_from_existing}\n"
                    f"Falsification condition: {candidate.falsification_condition}\n"
                    f"Expected effect: {candidate.expected_effect}\n\n{grounding}\n\n{_FIELD_CONTRACT}"
                ),
            },
        ]
        # One bounded repair round: a live model occasionally omits a field;
        # feeding the exact validation error back once is far cheaper than
        # failing the whole workflow run (owner-reported live stall on
        # 'expected_resource_demands').
        payload = await self.router.structured_output(
            ModelRole.RESEARCH_REASONING, messages=messages,
            schema={"name": "methodology", "schema": METHODOLOGY_SCHEMA},
            project_id=candidate.project_id, run_id=candidate.run_id,
            operation="methodology_freeze",
        )
        try:
            payload = _validate(payload, evidence_context)
        except ValueError as exc:
            repair_messages = messages + [
                {"role": "assistant", "content": json.dumps(payload, default=str)[:8000]},
                {"role": "user", "content": f"VALIDATION FAILED: {exc}\nReturn the corrected FULL JSON object with every required key."},
            ]
            payload = await self.router.structured_output(
                ModelRole.RESEARCH_REASONING, messages=repair_messages,
                schema={"name": "methodology", "schema": METHODOLOGY_SCHEMA},
                project_id=candidate.project_id, run_id=candidate.run_id,
                operation="methodology_freeze_repair",
            )
            payload = _validate(payload, evidence_context)

        publication_gaps = publication_design_gaps(payload, profile=venue_profile)
        previous = self.repository.latest_methodology_freeze_for_candidate(candidate.id)
        version = (previous.version + 1) if previous else 1

        provider, model = self.router.identity(ModelRole.RESEARCH_REASONING)
        provenance = ArtifactProvenance.create(
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            source_evidence=(candidate.id, *(c["paper_id"] for c in evidence_context.get("papers", []))),
            provider=provider, model=model,
            prompt_version=PROMPT_VERSION,
            content=payload,
        )
        record = {
            "methodology_freeze_id": uuid4().hex,
            "candidate_id": candidate.id,
            "portfolio_id": portfolio_id,
            "version": version,
            "selection_rationale": selection_rationale,
            "methodology": payload,
            "publication_gaps": publication_gaps,
            "publication_design": {
                key: payload.get(key)
                for key in (
                    "contribution_statement", "novelty_delta", "datasets",
                    "baselines", "ablations", "robustness_tests",
                    "power_analysis", "one_dataset_justification",
                    "expected_figures_tables", "failure_modes",
                    "stop_conditions",
                )
            },
            "provenance": {
                "provider": provenance.provider, "model": provenance.model,
                "prompt_version": PROMPT_VERSION, "created_at": utc_now(),
                "checksum": content_checksum(payload), "source_evidence": list(provenance.source_evidence),
            },
        }
        text = json.dumps(record, indent=2, ensure_ascii=False, default=str)
        root = self.repository.artifact_root / candidate.project_id / candidate.run_id
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"methodology-v{version}.json"
        path.write_text(text, encoding="utf-8")

        freeze = MethodologyFreeze(
            id=record["methodology_freeze_id"],
            project_id=candidate.project_id,
            run_id=candidate.run_id,
            candidate_id=candidate.id,
            version=version,
            selection_rationale=selection_rationale,
            prompt_version=PROMPT_VERSION,
            artifact_path=str(path),
            checksum=content_checksum(text),
        )
        self.repository.save_methodology_freeze(freeze)
        self.repository.save_artifact(ArtifactReference(
            content_checksum(f"methodology|{freeze.id}")[:32],
            project_id=candidate.project_id, run_id=candidate.run_id,
            artifact_type=f"methodology_v{version}_json", path=str(path),
            source_evidence=(candidate.id,),
            provider=provider, model=model,
            prompt_version=PROMPT_VERSION, checksum=freeze.checksum,
        ))
        return freeze


def publication_design_gaps(payload: dict, profile=None) -> list[dict]:
    """Publication-grade design gaps at freeze time.

    These are reported, not fatal to the freeze: exploratory work remains
    possible, but it cannot be presented as a top-venue empirical study.
    """
    from autoresearch.application.venue_profiles import get_venue_profile

    venue = profile or get_venue_profile("generic_journal")
    gaps: list[dict] = []
    block = payload.get("methodology", payload) if isinstance(payload, dict) else {}

    def add(code: str, message: str, fatal: bool = True) -> None:
        gaps.append({
            "code": code, "stage": "novelty", "message": message, "fatal": fatal,
        })

    if not str(block.get("contribution_statement") or "").strip():
        add("contribution_missing", "no one-sentence contribution statement")
    if not str(block.get("novelty_delta") or "").strip():
        add("novelty_delta_missing", "no explicit delta from the closest prior work")
    baselines = [str(item) for item in block.get("baselines") or [] if str(item).strip()]
    if len(baselines) < venue.min_baselines:
        add("baselines_below_minimum", f"baselines {len(baselines)} < {venue.min_baselines}")
    ablations = [str(item) for item in block.get("ablations") or [] if str(item).strip()]
    if len(ablations) < venue.min_ablations:
        add("ablations_missing", f"ablations {len(ablations)} < {venue.min_ablations}")
    robustness = [str(item) for item in block.get("robustness_tests") or [] if str(item).strip()]
    if len(robustness) < venue.min_robustness:
        add("robustness_missing", f"robustness tests {len(robustness)} < {venue.min_robustness}")
    datasets = [str(item) for item in block.get("datasets") or [] if str(item).strip()]
    if len(datasets) < venue.min_datasets and not str(block.get("one_dataset_justification") or "").strip():
        add("datasets_below_minimum", f"datasets {len(datasets)} < {venue.min_datasets} and no justification")
    power = " ".join(str(block.get("power_analysis") or "").split()).lower()
    if venue.require_power and (
        len(power) < 20
        or any(phrase in power for phrase in ("not established", "not performed", "not computed", "no power"))
    ):
        add("power_not_established", "no substantive power analysis")
    if not [item for item in block.get("expected_figures_tables") or [] if str(item).strip()]:
        add("figure_table_plan_missing", "no expected figure/table plan")
    if not [item for item in block.get("failure_modes") or [] if str(item).strip()]:
        add("failure_modes_missing", "no failure modes recorded")
    if not [item for item in block.get("stop_conditions") or [] if str(item).strip()]:
        add("stop_conditions_missing", "no stop conditions recorded")
    return gaps


def publication_gaps_for_freeze(freeze: "MethodologyFreeze") -> list[dict]:
    """Read publication gaps from a frozen methodology artifact."""
    import json
    from pathlib import Path

    try:
        payload = json.loads(Path(freeze.artifact_path).read_text(encoding="utf-8"))
        return list(payload.get("publication_gaps") or [])
    except (OSError, ValueError, TypeError):
        return [{
            "code": "methodology_artifact_unreadable",
            "stage": "novelty",
            "message": "frozen methodology artifact is unreadable",
            "fatal": True,
        }]


def _validate(payload: dict, evidence_context: dict) -> dict:
    """Deterministic schema/enforcement checks — prompt instructions alone do
    not enforce this contract. Raises ValueError on malformed output."""
    # Logistics fields may be filled with explicit, honest defaults when the
    # model omits them: they are planning details, never scientific content.
    # The scientific core (hypothesis, predictions, mechanism, falsification,
    # algorithm, procedure, uncertainty) must come from the model.
    _LOGISTICS_DEFAULTS = {
        "implementation_requirements": "not specified at freeze time; verify against the experiment environment before execution",
        "expected_resource_demands": "not specified at freeze time; estimate before execution approval",
    }
    for field, default in _LOGISTICS_DEFAULTS.items():
        if not payload.get(field):
            payload[field] = default
    # FastTrack template Methodology fields: honest markers when the model
    # omits them. The manuscript Methodology reports these as limitations
    # rather than inventing content (template-spec.json T061-T069).
    _TEMPLATE_FIELD_DEFAULTS = {
        "study_design": "not specified at freeze time",
        "data_collection": "not specified at freeze time",
        "sample_and_eligibility": "not specified at freeze time",
        "data_analysis": "not specified at freeze time",
        "ethical_considerations": "not specified at freeze time",
    }
    for field, default in _TEMPLATE_FIELD_DEFAULTS.items():
        if not str(payload.get(field) or "").strip():
            payload[field] = default
    required = METHODOLOGY_SCHEMA["required"]
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValueError(f"methodology_invalid: missing required fields {missing}")
    for key in ("predicted_observations", "falsification_conditions", "procedure"):
        value = payload.get(key)
        if not isinstance(value, list) or not value:
            raise ValueError(f"methodology_invalid: '{key}' must be a non-empty array")
    supplied_claims = {str(c.get("claim_id")) for c in evidence_context.get("claims", [])}
    supplied_claims |= {str(c.get("claim_id")) for c in evidence_context.get("counter_evidence_claims", [])}
    payload["supporting_evidence_claim_ids"] = [
        str(cid) for cid in payload.get("supporting_evidence_claim_ids", []) if str(cid) in supplied_claims
    ]
    supplied_papers = {str(p.get("paper_id")) for p in evidence_context.get("papers", [])}
    payload["closest_prior_methods"] = [
        entry for entry in payload.get("closest_prior_methods", [])
        if isinstance(entry, dict) and str(entry.get("paper_id")) in supplied_papers
    ]
    # Filtering must not silently empty the linkage: when the evidence context
    # supplied claims, a freeze citing none of them is ungrounded, not valid.
    if supplied_claims and not payload["supporting_evidence_claim_ids"]:
        raise ValueError(
            "methodology_ungrounded: supporting_evidence_claim_ids cites none of the supplied "
            "verified claims; regenerate with the evidence context or freeze without one"
        )
    if supplied_papers and not payload["closest_prior_methods"]:
        raise ValueError(
            "methodology_ungrounded: closest_prior_methods cites none of the supplied papers; "
            "the frozen methodology must state its concrete difference from the reviewed corpus"
        )
    uncertainty = " ".join(str(payload.get("uncertainty_statement") or "").split())
    if len(uncertainty) < 20:
        raise ValueError("methodology_invalid: uncertainty_statement must be a substantive statement")
    return payload

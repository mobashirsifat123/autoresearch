"""Deterministic cross-paper conflict candidates and missing-evidence audit.

A literature review must not silently average contradictory findings. The
existing evidence matrix reports only claims the extractor itself tagged as
contradictory, and only within one paper. This module adds a transparent,
bounded heuristic over the verified claim corpus:

- two claims from *different* papers are a **candidate conflict** when they
  share at least two content terms and carry opposing result polarity
  (improve/increase/higher vs no/not/failed/decrease/worse);
- candidates are explicitly labelled ``candidate_conflict_not_verified`` and
  must be read by a human; they are never promoted to a factual contradiction;
- missing evidence is recorded separately (abstract-only papers, claims without
  a page/section, review failures, absent counter-evidence).

No model is called. This is a retrieval aid, not a semantic entailment engine.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from autoresearch.domain.models import ArtifactReference, content_checksum
from autoresearch.storage.repository import ResearchRepository

AUDIT_VERSION = "review-audit-v1"
MAX_CLAIMS = 2000
MAX_CONFLICTS = 20

# Terms too generic to establish a shared topic.
_STOPWORDS = {
    "the", "and", "for", "with", "without", "from", "that", "this", "these",
    "those", "into", "than", "then", "when", "where", "which", "while", "were",
    "was", "are", "is", "has", "have", "had", "can", "could", "may", "might",
    "study", "studies", "paper", "papers", "method", "methods", "model",
    "models", "result", "results", "performance", "accuracy", "dataset",
    "datasets", "experiment", "experiments", "evaluation", "approach",
    "propose", "proposed", "show", "shows", "showed", "find", "finds", "found",
    "however", "also", "using", "used", "use", "based", "our", "their", "its",
    "compared", "comparison", "significant", "significantly", "effect",
}

_NEGATION = re.compile(
    r"(?:\bno\b|\bnot\b|\bnone\b|\bnever\b|\bwithout\b|\bfail(?:s|ed|ure)?\b|"
    r"\bdid not\b|\bdoes not\b|\bno significant\b|\bno improvement\b|\bno gain\b)",
    re.IGNORECASE,
)
_DOWN = re.compile(
    r"(?:\bdecreas(?:e|ed|es|ing)\b|\blower(?:ed)?\b|\bworse(?:n|ned)?\b|"
    r"\breduc(?:e|ed|es|ing)\b)",
    re.IGNORECASE,
)
_POSITIVE = re.compile(
    r"(?:\bimprov(?:e|ed|es|ing)\b|\bincreas(?:e|ed|es|ing)\b|\bhigher\b|"
    r"\bbetter\b|\bgain(?:s|ed)?\b|\bsuperior\b|\boutperform(?:s|ed)?\b|"
    r"\bboost(?:s|ed)?\b|\bpositive\b)",
    re.IGNORECASE,
)
# "reduces error" is a positive result even though "reduce" is a downward
# verb. "without loss of accuracy" is not a negative result.
_REDUCE_HARM = re.compile(
    r"\b(?:reduc(?:e|ed|es|ing)|lower(?:ed)?|decreas(?:e|ed|es|ing))\b"
    r"[^.]{0,30}\b(?:error|errors|loss|risk|failure|failures|cost|perplexity)\b",
    re.IGNORECASE,
)
_WITHOUT_HARM = re.compile(
    r"\bwithout\s+(?:loss|degradation|change|sacrifice|reduction)\b",
    re.IGNORECASE,
)

_TOKEN = re.compile(r"[a-z][a-z0-9_-]{3,}")


def _terms(text: str) -> set[str]:
    return {
        token for token in _TOKEN.findall(str(text or "").lower())
        if token not in _STOPWORDS
    }


def _polarity(text: str) -> str:
    lowered = str(text or "").lower()
    has_negation = bool(_NEGATION.search(lowered))
    has_down = bool(_DOWN.search(lowered))
    has_positive = bool(_POSITIVE.search(lowered))
    reduce_harm = bool(_REDUCE_HARM.search(lowered))
    without_harm = bool(_WITHOUT_HARM.search(lowered))
    # "did not improve" / "no gain" is a negative result.
    if has_negation and not without_harm:
        return "negative"
    # "reduces error" and "without loss of accuracy" are positive results.
    if reduce_harm or without_harm:
        return "positive"
    # "reduced error and improved accuracy" carries both directions; a
    # polarity heuristic cannot resolve it, so leave it out.
    if has_down and has_positive:
        return "ambiguous"
    if has_down:
        return "negative"
    if has_positive:
        return "positive"
    return "neutral"


def conflict_candidates(
    claims: Sequence[Any],
    *,
    paper_titles: dict[str, str] | None = None,
    limit: int = MAX_CONFLICTS,
) -> list[dict[str, Any]]:
    """Return bounded cross-paper conflict candidates from verified claims.

    Claims may be ``EvidenceClaim`` objects or mapping rows with ``id``,
    ``paper_id``, ``statement`` (or ``excerpt``), and ``excerpt``.
    """
    titles = paper_titles or {}
    prepared: list[dict[str, Any]] = []
    for claim in list(claims)[:MAX_CLAIMS]:
        if isinstance(claim, dict):
            claim_id = str(claim.get("id") or "")
            paper_id = str(claim.get("paper_id") or "")
            text = str(claim.get("statement") or claim.get("excerpt") or "")
            excerpt = str(claim.get("excerpt") or "")
        else:
            claim_id = str(getattr(claim, "id", ""))
            paper_id = str(getattr(claim, "paper_id", ""))
            text = str(getattr(claim, "statement", "") or getattr(claim, "excerpt", ""))
            excerpt = str(getattr(claim, "excerpt", ""))
        if not claim_id or not paper_id or not text.strip():
            continue
        # Use the verbatim excerpt, not a model paraphrase, for both polarity
        # and topic terms. Fall back to the statement only when no excerpt
        # exists (older rows).
        evidence_text = excerpt.strip() or text.strip()
        polarity = _polarity(evidence_text)
        if polarity not in {"positive", "negative"}:
            continue
        prepared.append({
            "claim_id": claim_id,
            "paper_id": paper_id,
            "paper_title": titles.get(paper_id, ""),
            "statement": text.strip(),
            "excerpt": excerpt.strip(),
            "polarity": polarity,
            "terms": _terms(evidence_text),
        })

    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(prepared):
        for right in prepared[index + 1:]:
            if left["paper_id"] == right["paper_id"]:
                continue
            if left["polarity"] == right["polarity"]:
                continue
            shared = sorted(left["terms"] & right["terms"])
            if len(shared) < 2:
                continue
            heuristic_score = min(0.8, 0.35 + 0.08 * len(shared))
            conflicts.append({
                "kind": "candidate_conflict_not_verified",
                "heuristic_score": round(heuristic_score, 3),
                "shared_terms": shared[:8],
                "left": {
                    "claim_id": left["claim_id"], "paper_id": left["paper_id"],
                    "paper_title": left["paper_title"], "polarity": left["polarity"],
                    "statement": left["statement"][:400],
                },
                "right": {
                    "claim_id": right["claim_id"], "paper_id": right["paper_id"],
                    "paper_title": right["paper_title"], "polarity": right["polarity"],
                    "statement": right["statement"][:400],
                },
                "why": "Opposing result polarity plus shared content terms. "
                       "This is a retrieval candidate, not a verified contradiction; "
                       "check datasets, metrics, and evaluation settings before concluding conflict.",
            })
    conflicts.sort(key=lambda item: (-float(item["heuristic_score"]), item["left"]["paper_id"], item["right"]["paper_id"]))
    return conflicts[:limit]


def build_review_audit(
    repository: ResearchRepository,
    *,
    project_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Assemble the conflict and missing-evidence ledger from durable rows."""
    claims = repository.rows(
        "SELECT id,paper_id,claim_type,statement,excerpt,page,section FROM evidence_claims WHERE run_id=?",
        (run_id,),
    )
    papers = repository.rows(
        "SELECT p.id,p.title FROM papers p JOIN paper_reviews r ON r.paper_id=p.id WHERE r.run_id=? GROUP BY p.id",
        (run_id,),
    )
    titles = {str(row["id"]): str(row.get("title") or "") for row in papers}
    reviewed_ids = {str(row["id"]) for row in papers}
    claim_paper_ids = {str(row["paper_id"]) for row in claims}
    failed = repository.rows("SELECT paper_id,reason FROM review_failures WHERE run_id=?", (run_id,))
    downloads = repository.rows(
        "SELECT paper_id,status,path FROM download_records WHERE project_id=?", (project_id,)
    )
    full_text = {str(row["paper_id"]) for row in downloads if str(row["status"]) == "downloaded" and row.get("path")}
    gap = repository.gap_for_run(run_id)
    conflicts = conflict_candidates(claims, paper_titles=titles)

    missing: list[dict[str, Any]] = []
    without_page = [str(row["id"]) for row in claims if row.get("page") is None]
    without_section = [str(row["id"]) for row in claims if not str(row.get("section") or "").strip()]
    if without_page:
        missing.append({
            "code": "claims_without_page",
            "count": len(without_page),
            "claim_ids": without_page[:20],
            "message": "Claims were verified verbatim but lack a page anchor; citation precision is limited.",
        })
    if without_section:
        missing.append({
            "code": "claims_without_section",
            "count": len(without_section),
            "claim_ids": without_section[:20],
            "message": "Claims lack a section label; provenance is weaker than a page+section anchor.",
        })
    no_claims = sorted(reviewed_ids - claim_paper_ids)
    if no_claims:
        missing.append({
            "code": "reviewed_papers_without_claims",
            "count": len(no_claims),
            "paper_ids": no_claims[:20],
            "message": "Reviewed papers contributed no verbatim-verifiable claims.",
        })
    abstract_only = sorted(reviewed_ids - full_text)
    if abstract_only:
        missing.append({
            "code": "abstract_only_papers",
            "count": len(abstract_only),
            "paper_ids": abstract_only[:20],
            "message": "No downloaded full text; extraction may be limited to the abstract.",
        })
    if failed:
        missing.append({
            "code": "review_failures",
            "count": len(failed),
            "paper_ids": [str(row["paper_id"]) for row in failed][:20],
            "message": "These papers failed extraction; coverage is incomplete.",
        })
    if gap is None or not gap.counter_evidence_claim_ids:
        missing.append({
            "code": "no_explicit_counter_evidence",
            "count": 1,
            "message": "No explicit counter-evidence claim was recorded for the selected gap. "
                       "Absence is not proof of absence.",
        })

    type_counts = Counter(str(row.get("claim_type") or "unknown") for row in claims)
    return {
        "kind": "review_audit",
        "version": AUDIT_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "claims": {
            "total": len(claims),
            "by_type": dict(sorted(type_counts.items())),
            "papers_with_claims": len(claim_paper_ids),
            "reviewed_papers": len(reviewed_ids),
        },
        "cross_paper_conflicts": conflicts,
        "conflict_count": len(conflicts),
        "missing_evidence": missing,
        "conflict_policy": "Candidates are heuristic retrieval aids (shared terms + opposing polarity); "
                           "they require verification against datasets, metrics, and protocols.",
    }


def save_review_audit(
    repository: ResearchRepository,
    *,
    project_id: str,
    run_id: str,
) -> tuple[dict[str, Any], ArtifactReference]:
    audit = build_review_audit(repository, project_id=project_id, run_id=run_id)
    root = Path(repository.artifact_root) / project_id / run_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / "review_audit.json"
    text = json.dumps(audit, indent=2, sort_keys=True, default=str)
    path.write_text(text, encoding="utf-8")
    artifact = ArtifactReference(
        uuid4().hex, project_id=project_id, run_id=run_id,
        artifact_type="review_audit", path=str(path),
        source_evidence=tuple(
            claim_id
            for item in (audit.get("cross_paper_conflicts") or [])
            for claim_id in (item["left"]["claim_id"], item["right"]["claim_id"])
        ) or (run_id,),
        provider="deterministic", model="review-audit",
        prompt_version=AUDIT_VERSION, checksum=content_checksum(text),
    )
    repository.save_artifact(artifact)
    return audit, artifact

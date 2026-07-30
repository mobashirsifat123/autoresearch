from autoresearch.domain.models import ArtifactProvenance, EvidenceClaim, GapClassification, PaperRecord
from autoresearch.review.gap_analysis import validate_gap_payload


def _provenance():
    return ArtifactProvenance.create(project_id="p", run_id="r", source_evidence=("paper:x",), provider="fake", model="m", prompt_version="v", content="evidence")


def _payload(papers, claims):
    return {"gap_statement": "Cross-domain evaluation is missing.", "supporting_paper_ids": papers, "supporting_claim_ids": claims, "why_existing_work_does_not_solve_it": "Existing studies use one dataset.", "missing_experiment": "Matched multi-domain benchmark.", "falsification_test": "Run three domains and reject if gains are consistent already.", "confidence": 0.8, "counter_evidence_claim_ids": []}


def test_gap_with_valid_evidence_is_supported():
    paper = PaperRecord("paper", "p", "d", "Paper", "Abstract", (), 2025, None, None, None, None, ("source",))
    claim = EvidenceClaim("claim", "p", "r", "paper", "limitation", "One dataset", "Only one dataset was used.", 1, "Limitations", 0.9, _provenance())
    gap = validate_gap_payload(_payload(["paper"], ["claim"]), project_id="p", run_id="r", valid_papers={"paper": paper}, valid_claims={"claim": claim}, provider="fake", model="m")
    assert gap.classification is GapClassification.EVIDENCE_SUPPORTED
    assert gap.supporting_claim_ids == ("claim",)


def test_gap_with_unknown_evidence_is_labeled_hypothesis():
    gap = validate_gap_payload(_payload(["missing"], ["missing"]), project_id="p", run_id="r", valid_papers={}, valid_claims={}, provider="fake", model="m")
    assert gap.classification is GapClassification.HYPOTHESIS
    assert gap.supporting_claim_ids == ()

"""Cross-paper conflict candidates and missing-evidence ledger."""

from __future__ import annotations

from autoresearch.application.literature_synthesis import LiteratureSynthesis, SynthesizedPaper
from autoresearch.review.conflicts import build_review_audit, conflict_candidates
from autoresearch.review.evidence_context import format_evidence_context


def _claim(claim_id: str, paper_id: str, statement: str) -> dict:
    return {"id": claim_id, "paper_id": paper_id, "statement": statement, "excerpt": statement}


def test_opposing_results_on_a_shared_topic_are_flagged_as_candidates():
    claims = [
        _claim("c1", "p1", "Adaptive fusion improves cross-domain robustness significantly."),
        _claim("c2", "p2", "Adaptive fusion did not improve cross-domain robustness."),
        _claim("c3", "p3", "A database index speeds up retrieval."),
    ]
    conflicts = conflict_candidates(claims)
    assert len(conflicts) == 1
    item = conflicts[0]
    assert item["kind"] == "candidate_conflict_not_verified"
    assert {item["left"]["claim_id"], item["right"]["claim_id"]} == {"c1", "c2"}
    assert "not a verified contradiction" in item["why"].lower() or "not a verified" in item["why"].lower()
    assert "adaptive" in item["shared_terms"]


def test_same_paper_and_unrelated_claims_are_not_conflicts():
    claims = [
        _claim("c1", "p1", "Adaptive fusion improves robustness."),
        _claim("c2", "p1", "Adaptive fusion did not improve robustness."),
        _claim("c3", "p2", "A database index speeds up retrieval."),
        _claim("c4", "p3", "A database index slows down retrieval."),
    ]
    conflicts = conflict_candidates(claims)
    # The same-paper pair is excluded. The database pair uses neutral verbs,
    # so it does not meet the opposing-polarity bar.
    assert all(
        not (item["left"]["claim_id"] == "c1" and item["right"]["claim_id"] == "c2")
        for item in conflicts
    )
    assert all(
        not (item["left"]["claim_id"] == "c3" and item["right"]["claim_id"] == "c4")
        for item in conflicts
    )


def test_reduce_harm_and_without_loss_are_positive_results():
    claims = [
        _claim("c1", "p1", "The method reduces error on the shared benchmark."),
        _claim("c2", "p2", "The method did not reduce error on the shared benchmark."),
        _claim("c3", "p3", "The method preserves accuracy without loss of robustness."),
        _claim("c4", "p4", "The method loses robustness."),
    ]
    conflicts = conflict_candidates(claims)
    pairs = {
        frozenset((item["left"]["claim_id"], item["right"]["claim_id"]))
        for item in conflicts
    }
    assert frozenset(("c1", "c2")) in pairs
    # "without loss" must not be misread as a negative result.
    assert all("c3" not in pair for pair in pairs)


class _StubRepository:
    def __init__(self):
        self.tables = {
            "claims": [
                {"id": "c1", "paper_id": "p1", "claim_type": "finding",
                 "statement": "Adaptive fusion improves robustness.", "excerpt": "Adaptive fusion improves robustness.",
                 "page": 3, "section": "Results"},
                {"id": "c2", "paper_id": "p2", "claim_type": "finding",
                 "statement": "Adaptive fusion did not improve robustness.", "excerpt": "Adaptive fusion did not improve robustness.",
                 "page": None, "section": None},
            ],
            "papers": [{"id": "p1", "title": "A"}, {"id": "p2", "title": "B"}],
            "failures": [],
            "downloads": [{"paper_id": "p1", "status": "downloaded", "path": "/x"}],
        }

    def rows(self, sql, params=()):
        if "FROM evidence_claims" in sql:
            return self.tables["claims"]
        if "FROM papers" in sql:
            return self.tables["papers"]
        if "FROM review_failures" in sql:
            return self.tables["failures"]
        if "FROM download_records" in sql:
            return self.tables["downloads"]
        return []

    def gap_for_run(self, run_id):
        return None


def test_missing_evidence_ledger_records_page_and_coverage_gaps():
    audit = build_review_audit(_StubRepository(), project_id="p", run_id="r")
    codes = {item["code"] for item in audit["missing_evidence"]}
    assert "claims_without_page" in codes
    assert "claims_without_section" in codes
    assert "abstract_only_papers" in codes
    assert "no_explicit_counter_evidence" in codes
    assert audit["conflict_count"] == 1


def test_conflict_candidates_are_rendered_for_novelty_and_manuscript():
    context = {
        "cross_paper_conflicts": [{
            "left": {"claim_id": "c1", "paper_id": "p1", "statement": "improves robustness"},
            "right": {"claim_id": "c2", "paper_id": "p2", "statement": "did not improve robustness"},
            "shared_terms": ["robustness"],
        }]
    }
    rendered = format_evidence_context(context)
    assert "Candidate cross-paper conflicts" in rendered
    assert "NOT verified contradictions" in rendered

    synthesis = LiteratureSynthesis(
        papers=(SynthesizedPaper(
            paper_id="p1", key="p1", title="A", year=2025, venue="V",
            problem="", method="m", datasets=(), results=(), limitations=(),
            theme="Other", is_gap_support=True,
        ),),
        themes=("Other",),
        conflicts=({"left": {"paper_id": "p1", "statement": "improves", "polarity": "positive"},
                    "right": {"paper_id": "p2", "statement": "did not improve", "polarity": "negative"},
                    "shared_terms": ["robustness"]},),
    )
    lines = synthesis.to_markdown_lines()
    assert any("### Conflicting evidence" in line for line in lines)
    assert any("heuristic retrieval candidates" in line for line in lines)

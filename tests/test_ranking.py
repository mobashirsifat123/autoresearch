from autoresearch.papers.deduplication import DeduplicatedCandidate, SearchProvenance
from autoresearch.papers.metadata_sources import MetadataPaper
from autoresearch.papers.ranking import rank_candidates


def test_candidate_ranking_exposes_components_and_is_deterministic():
    provenance = (SearchProvenance("source", "topic", "adaptive fusion", 1, "c"),)
    related = DeduplicatedCandidate(MetadataPaper("Adaptive fusion for robust vision", "adaptive fusion improves robust vision", year=2025, citation_count=5, is_open_access=True), provenance)
    unrelated = DeduplicatedCandidate(MetadataPaper("Marine biology survey", "ocean ecosystems", year=2025, citation_count=100), provenance)
    ranked = rank_candidates([unrelated, related], seed_text="adaptive fusion robust vision", seed_methods=("adaptive fusion",), current_year=2026)
    assert ranked[0][0].paper.title.startswith("Adaptive")
    components = ranked[0][1].components()
    assert set(components) == {"relevance", "method_overlap", "recency", "influence", "source_agreement", "open_access", "total"}
    assert 0 <= components["total"] <= 1

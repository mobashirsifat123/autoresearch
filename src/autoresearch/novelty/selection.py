"""Future novelty-selection boundary; implementation is outside milestone one."""

from typing import Protocol, Sequence

from autoresearch.domain.models import ArtifactReference


class NoveltySelector(Protocol):
    def select(self, candidates: Sequence[ArtifactReference]) -> ArtifactReference: ...

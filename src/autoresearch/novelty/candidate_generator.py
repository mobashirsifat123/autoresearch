"""Future novelty-candidate boundary; implementation is outside milestone one."""

from typing import Protocol

from autoresearch.domain.models import ArtifactReference, ResearchGap


class NoveltyCandidateGenerator(Protocol):
    async def generate(self, gap: ResearchGap) -> tuple[ArtifactReference, ...]: ...

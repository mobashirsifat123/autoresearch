"""Future novelty-critic boundary; implementation is outside milestone one."""

from typing import Protocol

from autoresearch.domain.models import ArtifactReference


class NoveltyCritic(Protocol):
    async def evaluate(self, candidate: ArtifactReference) -> ArtifactReference: ...

"""Future prior-art-check boundary; implementation is outside milestone one."""

from typing import Protocol

from autoresearch.domain.models import ArtifactReference


class PriorArtChecker(Protocol):
    async def check(self, candidate: ArtifactReference) -> ArtifactReference: ...

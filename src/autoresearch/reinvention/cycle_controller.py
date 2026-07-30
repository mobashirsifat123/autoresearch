"""Future reinvention-cycle boundary; implementation is outside milestone one."""

from typing import Protocol

from autoresearch.domain.models import ArtifactReference


class ReinventionCycleController(Protocol):
    async def advance(self, result: ArtifactReference) -> ArtifactReference: ...

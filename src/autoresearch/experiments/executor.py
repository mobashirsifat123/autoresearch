"""Future experiment-execution boundary; implementation is outside milestone one."""

from typing import Protocol

from autoresearch.domain.models import ArtifactReference


class ExperimentExecutor(Protocol):
    async def execute(self, plan: ArtifactReference, environment: ArtifactReference) -> ArtifactReference: ...

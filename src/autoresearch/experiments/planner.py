"""Future experiment-planning boundary; implementation is outside milestone one."""

from typing import Protocol

from autoresearch.domain.models import ArtifactReference


class ExperimentPlanner(Protocol):
    async def plan(self, novelty: ArtifactReference) -> ArtifactReference: ...

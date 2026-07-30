"""Future result-analysis boundary; implementation is outside milestone one."""

from typing import Protocol

from autoresearch.domain.models import ArtifactReference


class ExperimentResultAnalyzer(Protocol):
    async def analyze(self, result: ArtifactReference) -> ArtifactReference: ...

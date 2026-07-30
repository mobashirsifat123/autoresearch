"""Future experiment-report boundary; implementation is outside milestone one."""

from typing import Protocol

from autoresearch.domain.models import ArtifactReference


class ExperimentReportWriter(Protocol):
    def write(self, analysis: ArtifactReference) -> ArtifactReference: ...

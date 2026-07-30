"""Future reproducible-environment boundary; implementation is outside milestone one."""

from pathlib import Path
from typing import Protocol

from autoresearch.domain.models import ArtifactReference


class EnvironmentBuilder(Protocol):
    async def prepare(self, plan: ArtifactReference, workspace: Path) -> ArtifactReference: ...

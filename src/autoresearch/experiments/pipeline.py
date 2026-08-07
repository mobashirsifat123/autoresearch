"""Orchestrator for the Experiment Pipeline.
Now with multi-shot planning, adversarial falsification analysis,
power analysis, Bayesian statistics, and validity assessment."""

from __future__ import annotations

import logging

from autoresearch.domain.models import ExperimentReport, NoveltyCandidate
from autoresearch.experiments.environment import IsolatedEnvironmentBuilder
from autoresearch.experiments.executor import DurableExperimentExecutor
from autoresearch.experiments.planner import ExperimentSpecGenerator
from autoresearch.experiments.report_writer import ScientificReportWriter
from autoresearch.experiments.result_analysis import StatisticalAnalyzer
from autoresearch.storage.repository import ResearchRepository

logger = logging.getLogger(__name__)

# Validity assessment schema constants
VALIDITY_ASSESSMENT_PROMPT_VERSION = "validity-assessment-v1"


class ExperimentPipeline:
    def __init__(
        self,
        repository: ResearchRepository,
        planner: ExperimentSpecGenerator,
        environment_builder: IsolatedEnvironmentBuilder,
        executor: DurableExperimentExecutor,
        analyzer: StatisticalAnalyzer,
        report_writer: ScientificReportWriter,
    ) -> None:
        self.repository = repository
        self.planner = planner
        self.environment_builder = environment_builder
        self.executor = executor
        self.analyzer = analyzer
        self.report_writer = report_writer

    async def execute_candidate(self, candidate: NoveltyCandidate) -> ExperimentReport:
        logger.info(f"Starting experiment pipeline for candidate {candidate.id}")

        # 1. Plan Experiment (multi-shot with adversarial analysis)
        spec = await self.planner.plan(candidate)
        self.repository.save_experiment_spec(spec)
        logger.info(f"Experiment spec generated: {spec.id}")

        # 2. Build Environment
        manifest = await self.environment_builder.prepare(spec)
        self.repository.save_environment_manifest(manifest)
        logger.info(f"Environment manifest prepared: {manifest.id}")

        # 3. Execute
        result = await self.executor.run(spec, manifest)
        logger.info(f"Execution completed. Exit code: {result.exit_code}")

        # 4. Analyze Results (frequentist + Bayesian)
        analysis = await self.analyzer.analyze(spec, result)
        self.repository.save_statistical_analysis(analysis)
        logger.info(f"Analysis completed: {analysis.id}")

        # 5. Write Report
        report = await self.report_writer.write(spec, analysis)
        self.repository.save_experiment_report(report)
        logger.info(f"Final report generated: {report.decision}")

        return report

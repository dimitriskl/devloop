from __future__ import annotations

from pathlib import Path

from devloop.components.contracts import (
    ComponentManifest,
    ComponentPort,
    PortDirection,
    StepExecutionPolicy,
    package_source_hash,
)
from devloop.domain.identifiers import DataContractId, StepComponentId

FINALIZATION_COMPONENT_ID = StepComponentId("workspace-finalization")
FINALIZATION_COMPONENT_VERSION = "1.0.0"
FINALIZATION_DISTRIBUTION = "devloop-codexcli"
AGGREGATED_RESULTS_CONTRACT = DataContractId("devloop.aggregated-results/v1")
HANDOFF_SUMMARY_CONTRACT = DataContractId("devloop.handoff-summary/v1")


class FinalizationComponentRunner:
    @property
    def component_id(self) -> StepComponentId:
        return FINALIZATION_COMPONENT_ID


def finalization_component() -> tuple[ComponentManifest, FinalizationComponentRunner]:
    runner = FinalizationComponentRunner()
    return (
        ComponentManifest(
            "devloop.step-component/v1",
            FINALIZATION_COMPONENT_ID,
            FINALIZATION_COMPONENT_VERSION,
            FINALIZATION_DISTRIBUTION,
            package_source_hash(Path(__file__).resolve().parents[1]),
            StepExecutionPolicy.LOCAL_FINALIZATION,
            (
                ComponentPort(
                    "aggregated_results",
                    AGGREGATED_RESULTS_CONTRACT,
                    PortDirection.INPUT,
                ),
                ComponentPort(
                    "handoff_summary",
                    HANDOFF_SUMMARY_CONTRACT,
                    PortDirection.OUTPUT,
                ),
            ),
        ),
        runner,
    )

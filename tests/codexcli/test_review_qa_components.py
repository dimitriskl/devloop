from __future__ import annotations

from devloop.components.builtin import builtin_component_registry
from devloop.components.contracts import PortDirection, StepExecutionPolicy
from devloop.components.qa import QA_COMPONENT_ID
from devloop.components.review import CODE_REVIEW_COMPONENT_ID
from devloop.domain.identifiers import StepInstanceId
from devloop.workflow.definition import load_standard_workflow, validate_component_ports


def test_review_and_qa_are_independent_policy_bound_components() -> None:
    registry = builtin_component_registry()
    workflow = load_standard_workflow()

    review_manifest, review_runner = registry.resolve(CODE_REVIEW_COMPONENT_ID)
    qa_manifest, qa_runner = registry.resolve(QA_COMPONENT_ID)

    assert review_runner is not qa_runner
    assert review_manifest.execution_policy is StepExecutionPolicy.READ_ONLY
    assert qa_manifest.execution_policy is StepExecutionPolicy.VERIFICATION_ONLY
    assert {
        port.name for port in review_manifest.ports if port.direction is PortDirection.INPUT
    } == {
        "issue",
        "workspace",
        "implementation",
    }
    assert {port.name for port in qa_manifest.ports if port.direction is PortDirection.INPUT} == {
        "issue",
        "workspace",
        "implementation",
        "review",
    }
    assert {
        port.name for port in review_manifest.ports if port.direction is PortDirection.OUTPUT
    } == {
        "review",
        "rework_request",
    }
    assert {port.name for port in qa_manifest.ports if port.direction is PortDirection.OUTPUT} == {
        "qa_result",
        "rework_request",
    }
    validate_component_ports(workflow.step(StepInstanceId("code-review")), review_manifest)
    validate_component_ports(workflow.step(StepInstanceId("qa")), qa_manifest)

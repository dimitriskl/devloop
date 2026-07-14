from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from devloop.components.contracts import (
    ComponentManifest,
    ComponentPort,
    PortDirection,
    StepExecutionPolicy,
    package_source_hash,
)
from devloop.domain.development import WorkspaceChoice, WorkspaceKind, WorkspaceRef
from devloop.domain.identifiers import DataContractId, StepComponentId
from devloop.infrastructure.git import (
    capture_workspace_baseline,
    create_worktree,
    current_branch,
    head_commit,
    repository_root,
)

WORKSPACE_COMPONENT_ID = StepComponentId("workspace-preparation")
WORKSPACE_COMPONENT_VERSION = "1.0.0"
WORKSPACE_COMPONENT_SCHEMA = "devloop.step-component/v1"
WORKSPACE_DISTRIBUTION = "devloop-codexcli"
REPOSITORY_REF_CONTRACT = DataContractId("devloop.repository-ref/v1")
PRD_PACKAGE_CONTRACT = DataContractId("devloop.prd-package/v1")
ISSUE_SET_CONTRACT = DataContractId("devloop.issue-set/v1")
WORKSPACE_REF_CONTRACT = DataContractId("devloop.workspace-ref/v1")


@dataclass(frozen=True)
class WorkspaceProposal:
    repository: Path
    current_path: Path
    dedicated_path: Path
    dedicated_branch: str
    base_commit: str


class WorkspacePreparationCancelled(RuntimeError):
    pass


class WorkspaceComponentRunner:
    @property
    def component_id(self) -> StepComponentId:
        return WORKSPACE_COMPONENT_ID

    def propose(
        self,
        repository: Path,
        feature_slug: str,
        *,
        worktree_parent: Path | None = None,
    ) -> WorkspaceProposal:
        root = repository_root(repository)
        base = head_commit(root)
        parent = root.parent / "worktrees" if worktree_parent is None else worktree_parent.resolve()
        return WorkspaceProposal(
            repository=root,
            current_path=root,
            dedicated_path=parent / f"{root.name}-{feature_slug}",
            dedicated_branch=f"devloop/{feature_slug}",
            base_commit=base,
        )

    def prepare(
        self,
        proposal: WorkspaceProposal,
        choice: WorkspaceChoice,
    ) -> WorkspaceRef:
        if choice is WorkspaceChoice.CANCEL:
            raise WorkspacePreparationCancelled("Workspace preparation was cancelled by the user.")
        if choice is WorkspaceChoice.CURRENT_CHECKOUT:
            return WorkspaceRef(
                WorkspaceKind.CURRENT_CHECKOUT,
                str(proposal.repository),
                str(proposal.current_path),
                current_branch(proposal.current_path),
                proposal.base_commit,
                capture_workspace_baseline(proposal.current_path),
            )
        create_worktree(
            proposal.repository,
            proposal.dedicated_path,
            proposal.dedicated_branch,
            proposal.base_commit,
        )
        return WorkspaceRef(
            WorkspaceKind.DEDICATED_WORKTREE,
            str(proposal.repository),
            str(proposal.dedicated_path),
            proposal.dedicated_branch,
            proposal.base_commit,
            capture_workspace_baseline(proposal.dedicated_path),
        )


def workspace_component() -> tuple[ComponentManifest, WorkspaceComponentRunner]:
    runner = WorkspaceComponentRunner()
    return (
        ComponentManifest(
            WORKSPACE_COMPONENT_SCHEMA,
            WORKSPACE_COMPONENT_ID,
            WORKSPACE_COMPONENT_VERSION,
            WORKSPACE_DISTRIBUTION,
            package_source_hash(Path(__file__).resolve().parents[1]),
            StepExecutionPolicy.EXPLICIT_GIT,
            (
                ComponentPort("repository", REPOSITORY_REF_CONTRACT, PortDirection.INPUT),
                ComponentPort("prd_package", PRD_PACKAGE_CONTRACT, PortDirection.INPUT),
                ComponentPort("issue_set", ISSUE_SET_CONTRACT, PortDirection.INPUT),
                ComponentPort("workspace", WORKSPACE_REF_CONTRACT, PortDirection.OUTPUT),
            ),
        ),
        runner,
    )

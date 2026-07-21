from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Sequence

from .issue_pack import Issue


class SchedulingPhase(str, Enum):
    NORMAL_SCHEDULING = "NORMAL_SCHEDULING"
    BLOCKER_RESOLUTION = "BLOCKER_RESOLUTION"
    COMPLETE = "COMPLETE"
    EXHAUSTED = "EXHAUSTED"
    RUN_PAUSED = "RUN_PAUSED"


@dataclass(frozen=True)
class IssueDependencyNode:
    issue: Issue
    index: int

    @property
    def number(self) -> str:
        return self.issue.number

    @property
    def dependencies(self) -> tuple[str, ...]:
        return self.issue.dependencies


class IssueDependencyGraph:
    def __init__(self, issues: Sequence[Issue]) -> None:
        self._nodes = tuple(
            IssueDependencyNode(issue=issue, index=index)
            for index, issue in enumerate(issues)
        )
        self._nodes_by_number = {node.number: node for node in self._nodes}
        if len(self._nodes_by_number) != len(self._nodes):
            raise ValueError("Issue index contains duplicate issue identifiers.")
        self._validate_dependencies()
        self._validate_acyclic()

    @property
    def nodes(self) -> tuple[IssueDependencyNode, ...]:
        return self._nodes

    def node(self, issue_number: str) -> IssueDependencyNode:
        try:
            return self._nodes_by_number[issue_number]
        except KeyError as error:
            raise KeyError(f"Unknown issue {issue_number}.") from error

    def _validate_dependencies(self) -> None:
        for node in self._nodes:
            if len(set(node.dependencies)) != len(node.dependencies):
                duplicate = next(
                    dependency
                    for dependency in node.dependencies
                    if node.dependencies.count(dependency) > 1
                )
                raise ValueError(
                    f"Issue {node.number} ({node.issue.path}) declares duplicate "
                    f"dependency {duplicate}."
                )
            for dependency in node.dependencies:
                if dependency == node.number:
                    raise ValueError(
                        f"Issue {node.number} ({node.issue.path}) depends on itself."
                    )
                if dependency not in self._nodes_by_number:
                    raise ValueError(
                        f"Issue {node.number} ({node.issue.path}) depends on unknown "
                        f"issue {dependency}."
                    )

    def _validate_acyclic(self) -> None:
        visited: set[str] = set()
        active: list[str] = []

        def visit(issue_number: str) -> None:
            if issue_number in active:
                cycle_start = active.index(issue_number)
                cycle = [*active[cycle_start:], issue_number]
                files = ", ".join(
                    str(self.node(number).issue.path)
                    for number in cycle[:-1]
                )
                raise ValueError(
                    "Issue dependency cycle detected: "
                    + " -> ".join(cycle)
                    + f" (files: {files})"
                )
            if issue_number in visited:
                return
            active.append(issue_number)
            for dependency in self.node(issue_number).dependencies:
                visit(dependency)
            active.pop()
            visited.add(issue_number)

        for node in self._nodes:
            visit(node.number)

    def validate_selection(self, selected: Iterable[Issue]) -> None:
        selected_numbers = {issue.number for issue in selected}
        for issue_number in selected_numbers:
            node = self.node(issue_number)
            for dependency_number in node.dependencies:
                dependency = self.node(dependency_number).issue
                if dependency.completed or dependency_number in selected_numbers:
                    continue
                raise ValueError(
                    f"Selected issue {issue_number} requires unfinished issue "
                    f"{dependency_number}, which is not selected."
                )


@dataclass(frozen=True)
class SchedulingProjection:
    phase: SchedulingPhase
    next_normal: IssueDependencyNode | None
    next_blocker: IssueDependencyNode | None
    blocker_round: int | None
    ready: tuple[IssueDependencyNode, ...]
    waiting_dependencies: Mapping[str, tuple[str, ...]]
    exhausted_blockers: tuple[IssueDependencyNode, ...]


class DependencyScheduler:
    def __init__(
        self,
        graph: IssueDependencyGraph,
        selected_issue_numbers: Iterable[str] | None = None,
        blocker_resolution_passes: int = 5,
    ) -> None:
        if blocker_resolution_passes < 0:
            raise ValueError("Blocker Resolution passes cannot be negative.")
        self._graph = graph
        self._blocker_resolution_passes = blocker_resolution_passes
        selected = (
            {node.number for node in graph.nodes}
            if selected_issue_numbers is None
            else set(selected_issue_numbers)
        )
        for issue_number in selected:
            graph.node(issue_number)
        self._selected = frozenset(selected)

    def project(
        self,
        *,
        completed: Iterable[str],
        normal_attempted: Iterable[str],
        additional_passes: Mapping[str, int] | None = None,
        non_retryable: Iterable[str] = (),
        allow_blocker_resolution: bool = True,
    ) -> SchedulingProjection:
        completed_numbers = frozenset(completed)
        attempted_numbers = frozenset(normal_attempted)
        additional = additional_passes or {}
        non_retryable_numbers = frozenset(non_retryable)
        ready: list[IssueDependencyNode] = []
        waiting: dict[str, tuple[str, ...]] = {}
        for node in self._graph.nodes:
            if node.number not in self._selected or node.number in completed_numbers:
                continue
            incomplete = tuple(
                dependency
                for dependency in node.dependencies
                if dependency not in completed_numbers
            )
            if incomplete:
                waiting[node.number] = incomplete
            else:
                ready.append(node)
        next_normal = next(
            (
                node
                for node in ready
                if node.number not in attempted_numbers
                and node.number not in non_retryable_numbers
            ),
            None,
        )
        eligible_blockers = [
            node
            for node in ready
            if node.number in attempted_numbers
            and node.number not in non_retryable_numbers
            and allow_blocker_resolution
            and additional.get(node.number, 0) < self._blocker_resolution_passes
        ]
        next_blocker = min(
            eligible_blockers,
            key=lambda node: (additional.get(node.number, 0), node.index),
            default=None,
        )
        exhausted_blockers = tuple(
            node
            for node in ready
            if node.number in attempted_numbers
            and additional.get(node.number, 0) >= self._blocker_resolution_passes
        )
        if next_normal is not None:
            phase = SchedulingPhase.NORMAL_SCHEDULING
        elif next_blocker is not None:
            phase = SchedulingPhase.BLOCKER_RESOLUTION
        elif not ready and not waiting:
            phase = SchedulingPhase.COMPLETE
        else:
            phase = SchedulingPhase.EXHAUSTED
        return SchedulingProjection(
            phase=phase,
            next_normal=next_normal,
            next_blocker=next_blocker,
            blocker_round=(
                additional.get(next_blocker.number, 0) + 1
                if next_blocker is not None
                else None
            ),
            ready=tuple(ready),
            waiting_dependencies=waiting,
            exhausted_blockers=exhausted_blockers,
        )

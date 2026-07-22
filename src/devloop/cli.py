from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .codex_runner import CodexRunner, RoleResult, RunWideBlockerError
from .issue_pack import Issue, find_repo_root, parse_issue_index, select_issues
from .issue_scheduler import (
    DependencyScheduler,
    IssueDependencyGraph,
    SchedulingProjection,
    SchedulingPhase,
)
from .lineeditor import LineEditor
from .model_catalog import (
    CatalogDiscoveryError,
    CodexModelCatalog,
    CodexModelCatalogAdapter,
)
from .product_scope import require_portable_target
from .portable_component_catalog import build_portable_component_catalog
from .portable_workflow import (
    IssueStatus,
    PortableStepComponentCatalog,
    PortableWorkflowCheckpoint,
    PortableWorkflowExecutor,
    StepScope,
    WorkflowDefinition,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
    parse_issue_status,
    preflight_codex_execution_settings,
)
from .self_improvement_wiki import (
    DEFAULT_SELF_IMPROVEMENT_WIKI_PATH,
    ensure_self_improvement_wiki,
    resolve_self_improvement_wiki_path,
    write_self_improvement_context,
)
from .state import (
    IssueResumeCursor,
    LoopStateWriter,
    ResumeRole,
    mark_issue_completed,
    mark_portable_issue_completed,
)
from .subprocess_utils import run_captured_text
from .terminal_text import compact_terminal_text, sanitize_terminal_text
from .templates import BundleContext, load_preset
from .worktree import resolve_worktree
from .workflow_defaults import (
    WorkflowDefaultStore,
    portable_planner_configuration_path,
)
from .workflow_editor import run_workflow_editor
from . import statusui
from .statusui import Stage

_PROMPT_EDITOR: LineEditor | None = None
_MAX_ISSUE_IDENTIFIER_LENGTH = 64
_MAX_ISSUE_TITLE_LENGTH = 160
_MAX_ATTEMPT_LABEL_LENGTH = 80
DEFAULT_BLOCKER_RESOLUTION_PASSES = 5


@dataclass(frozen=True)
class DependencyScheduleResult:
    completed: bool
    unresolved_blockers: tuple[str, ...] = ()
    waiting_dependencies: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def execute_dependency_schedule(
    *,
    issues: list[Issue],
    graph: IssueDependencyGraph,
    state_writer: LoopStateWriter,
    execute_issue: Callable[[Issue, SchedulingPhase, int], RoleResult],
    projection_callback: Callable[[SchedulingProjection], None] | None = None,
    allow_blocker_resolution: bool = True,
    blocker_resolution_passes: int = 5,
    simulation: bool = False,
) -> DependencyScheduleResult:
    issue_by_number = {issue.number: issue for issue in issues}
    scheduler = DependencyScheduler(
        graph,
        selected_issue_numbers=issue_by_number,
        blocker_resolution_passes=blocker_resolution_passes,
    )
    selected_numbers = frozenset(issue_by_number)
    session_completed = {
        node.number for node in graph.nodes if node.issue.completed
    }
    session_completed.update(
        issue.number
        for issue in issues
        if parse_issue_status(state_writer.issue_state(issue).get("status"))
        is IssueStatus.COMPLETED
    )
    last_projection_signature: tuple[object, ...] | None = None
    simulated_normal_attempted: set[str] = set()

    while True:
        normal_attempted = (
            set(simulated_normal_attempted)
            if simulation
            else set(state_writer.normal_attempted_issues())
        )
        non_retryable: set[str] = set()
        if not simulation:
            for issue in issues:
                status = parse_issue_status(
                    state_writer.issue_state(issue).get("status")
                )
                if status in {
                    IssueStatus.BLOCKED,
                    IssueStatus.FAILED,
                    IssueStatus.CHANGES_REQUESTED,
                    IssueStatus.COMPLETED,
                    IssueStatus.CANCELLED,
                    IssueStatus.WAITING_FOR_INPUT,
                }:
                    normal_attempted.add(issue.number)
                if status in {
                    IssueStatus.CANCELLED,
                    IssueStatus.WAITING_FOR_INPUT,
                }:
                    non_retryable.add(issue.number)

        additional_passes = {} if simulation else state_writer.additional_passes()
        projection = scheduler.project(
            completed=session_completed,
            normal_attempted=normal_attempted,
            additional_passes=additional_passes,
            non_retryable=non_retryable,
            allow_blocker_resolution=allow_blocker_resolution,
        )
        state_writer.record_dependency_projection(
            issues,
            ready=(node.number for node in projection.ready),
            waiting=dict(projection.waiting_dependencies),
            phase=projection.phase,
        )
        projection_signature = (
            projection.next_normal.number if projection.next_normal else None,
            projection.next_blocker.number if projection.next_blocker else None,
            projection.blocker_round,
            tuple(node.number for node in projection.ready),
            tuple(projection.waiting_dependencies.items()),
            tuple(node.number for node in projection.exhausted_blockers),
        )
        if (
            projection_callback is not None
            and projection_signature != last_projection_signature
        ):
            projection_callback(projection)
        last_projection_signature = projection_signature

        active = None if simulation else state_writer.active_scheduling_attempt()
        if active is not None:
            active_issue = issue_by_number.get(active["issue"])
            if active_issue is None:
                raise ValueError(
                    "Active scheduling attempt references an unselected issue."
                )
            active_status = parse_issue_status(
                state_writer.issue_state(active_issue).get("status")
            )
            if active_status is IssueStatus.COMPLETED:
                state_writer.complete_scheduling_attempt(
                    active_issue,
                    outcome=IssueStatus.COMPLETED,
                )
                session_completed.add(active_issue.number)
                continue

        if selected_numbers.issubset(session_completed):
            return DependencyScheduleResult(completed=True)

        if active is not None:
            issue = issue_by_number.get(active["issue"])
            assert issue is not None
            phase = SchedulingPhase(active["phase"])
            ordinal = active["ordinal"]
        elif projection.next_normal is not None:
            issue = issue_by_number[projection.next_normal.number]
            phase = SchedulingPhase.NORMAL_SCHEDULING
            ordinal = 1
            if not simulation:
                state_writer.reserve_scheduling_attempt(
                    issue,
                    phase=phase,
                    ordinal=ordinal,
                )
        elif allow_blocker_resolution and projection.next_blocker is not None:
            issue = issue_by_number[projection.next_blocker.number]
            phase = SchedulingPhase.BLOCKER_RESOLUTION
            ordinal = projection.blocker_round or 1
            if not simulation:
                state_writer.reserve_scheduling_attempt(
                    issue,
                    phase=phase,
                    ordinal=ordinal,
                )
        else:
            return DependencyScheduleResult(
                completed=False,
                unresolved_blockers=tuple(
                    node.number
                    for node in projection.ready
                    if node.number in normal_attempted
                ),
                waiting_dependencies=projection.waiting_dependencies,
            )

        result = execute_issue(issue, phase, ordinal)
        persisted_outcome = parse_issue_status(
            state_writer.issue_state(issue).get("status")
        )
        if persisted_outcome in {
            IssueStatus.CANCELLED,
            IssueStatus.WAITING_FOR_INPUT,
        }:
            if not simulation:
                state_writer.release_scheduling_attempt(
                    issue,
                    outcome=persisted_outcome,
                )
            continue
        outcome = (
            persisted_outcome
            if persisted_outcome in {
                IssueStatus.COMPLETED,
                IssueStatus.CHANGES_REQUESTED,
                IssueStatus.BLOCKED,
                IssueStatus.FAILED,
            }
            else {
                "PASS": IssueStatus.COMPLETED,
                "FAIL": IssueStatus.FAILED,
                "BLOCKED": IssueStatus.BLOCKED,
            }.get(result.status.upper(), IssueStatus.BLOCKED)
        )
        if simulation:
            simulated_normal_attempted.add(issue.number)
        else:
            state_writer.complete_scheduling_attempt(issue, outcome=outcome)
        if outcome is IssueStatus.COMPLETED:
            session_completed.add(issue.number)


def report_unresolved_dependency_cut(result: DependencyScheduleResult) -> None:
    blockers = ", ".join(result.unresolved_blockers) or "none"
    print(
        f"Blocker Resolution exhausted; unresolved root blockers: {blockers}",
        file=sys.stderr,
    )
    for issue_number, dependencies in result.waiting_dependencies.items():
        waiting_on = ", ".join(dependencies)
        print(
            f"  {issue_number} WAITING_ON_DEPENDENCY: {waiting_on}",
            file=sys.stderr,
        )


def publish_portable_screen(content: str) -> None:
    from .portable_runtime import active_portable_runtime

    portable_runtime = active_portable_runtime()
    if portable_runtime is not None:
        portable_runtime.show_screen(content)


def main(
    argv: list[str] | None = None,
    *,
    workflow_snapshot: WorkflowDefinition | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    from .portable_presentation import PortableUiMode, select_portable_ui_mode
    from .portable_runtime import active_portable_runtime

    ui_mode = select_portable_ui_mode(
        force_plain=args.plain,
        stdin_is_tty=sys.stdin.isatty(),
        stdout_is_tty=sys.stdout.isatty(),
        term=os.environ.get("TERM"),
    )
    operation = lambda: _run_devloop(parser, args, workflow_snapshot)
    if ui_mode is PortableUiMode.APPLICATION and active_portable_runtime() is None:
        try:
            from .portable_ui.app import run_portable_application
        except ModuleNotFoundError as error:
            if error.name != "textual":
                raise
            print(
                "Dev Loop terminal UI is unavailable. Rerun the Dev Loop "
                "installer to repair its runtime, or use --plain.",
                file=sys.stderr,
            )
            return 78

        return run_portable_application(operation)
    return operation()


def _run_devloop(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    workflow_snapshot: WorkflowDefinition | None,
) -> int:

    if args.self_improvement_max_lessons < 1:
        parser.error("--self-improvement-max-lessons must be at least 1")
    if args.blocked_retry_rounds < 0:
        parser.error("--blocked-retry-rounds must be 0 or greater")
    if args.blocked_retry_max_passes < 1:
        parser.error("--blocked-retry-max-passes must be at least 1")

    prd_path = Path(args.prd).expanduser().resolve()
    issues_index = Path(args.issues).expanduser().resolve()

    if not prd_path.is_file():
        parser.error(f"PRD file not found: {prd_path}")

    try:
        validate_prd_target_product(prd_path)
    except ValueError as exc:
        parser.error(str(exc))

    if not issues_index.is_file():
        parser.error(f"Issue README/index file not found: {issues_index}")

    source_repo = find_repo_root(issues_index.parent)
    source_branch = git_current_branch(source_repo)
    try:
        source_issues = parse_issue_index(issues_index)
        source_issue_graph = IssueDependencyGraph(source_issues)
    except ValueError as exc:
        parser.error(f"Issue dependency preflight failed: {exc}")

    if not source_issues:
        parser.error(f"No local issue links were found in {issues_index}")

    for issue in source_issues:
        try:
            validate_issue_target_product(issue.path)
        except ValueError as exc:
            parser.error(str(exc))

    selected_source_issues = select_issues(
        source_issues,
        run_all=args.all,
        start_issue=args.start_issue,
    )

    try:
        source_issue_graph.validate_selection(selected_source_issues)
    except ValueError as exc:
        parser.error(f"Issue selection preflight failed: {exc}")

    if not selected_source_issues:
        print("No pending issues selected.")
        return 0

    pending_numbers = ", ".join(
        _terminal_issue_identifier(issue.number)
        for issue in selected_source_issues
    )
    print(f"Selected issues: {pending_numbers}")

    worktree = resolve_worktree(
        source_repo=source_repo,
        create_worktree=args.create_worktree,
        no_worktree=args.no_worktree,
        worktree_path=Path(args.worktree_path).expanduser().resolve()
        if args.worktree_path
        else None,
        branch_name=args.branch_name,
        interactive=not args.non_interactive,
        dry_run=args.dry_run,
    )

    if worktree.created:
        print(f"Created implementation worktree: {worktree.repo_root}")
    elif worktree.repo_root != source_repo:
        print(f"Using implementation worktree: {worktree.repo_root}")

    repo_root = worktree.repo_root
    prd_in_repo = map_path_to_worktree(prd_path, source_repo, repo_root)
    issues_index_in_repo = map_path_to_worktree(issues_index, source_repo, repo_root)
    if repo_root != source_repo and not args.dry_run:
        ensure_planning_artifacts_in_worktree(
            prd_path=prd_path,
            issues_index=issues_index,
            source_repo=source_repo,
            target_repo=repo_root,
        )
    issues = map_selected_issues_to_worktree(
        selected_source_issues,
        source_repo,
        repo_root,
    )

    if not issues:
        print("No pending issues selected in implementation worktree.")
        return 0

    report_mapped_selection(selected_source_issues, issues)

    bundle = BundleContext.from_file(Path(__file__).resolve())
    preset = load_preset(resolve_bundle_path(bundle.root, args.preset))
    component_catalog = build_portable_component_catalog(bundle.root, preset.roles)
    state_writer = LoopStateWriter(issues_index_in_repo)
    runner = CodexRunner(
        bundle=bundle,
        repo_root=repo_root,
        prd_path=prd_in_repo,
        issues_index=issues_index_in_repo,
        preset=preset,
        codex=args.codex,
        sandbox=args.sandbox,
        approval_policy=args.approval_policy,
        dry_run=args.dry_run,
        use_self_improvement_wiki=args.self_improvement_wiki,
    )

    try:
        if args.dry_run:
            resolve_run_workflow(
                state_writer,
                component_catalog,
                user_workflow_path=portable_planner_configuration_path(),
                workflow_snapshot=workflow_snapshot,
            )
        else:
            resolved_workflow = resolve_run_workflow_with_repair(
                state_writer,
                component_catalog,
                user_workflow_path=portable_planner_configuration_path(),
                model_catalog_loader=CodexModelCatalogAdapter(
                    runner.codex,
                    cwd=repo_root,
                ).discover,
                read_line=read_prompt,
                write=print,
                workflow_snapshot=workflow_snapshot,
                allow_interactive_repair=not args.non_interactive,
            )
            if resolved_workflow is None:
                print("Run cancelled before Codex Execution Settings were authorized.")
                return 0
    except ValueError as exc:
        parser.error(f"Codex Execution Settings preflight failed: {exc}")
    state_writer.record_run_start(
        repo_root=repo_root,
        prd_path=prd_in_repo,
        issues=[issue.number for issue in issues],
        dry_run=args.dry_run,
    )
    print(f"Loop state: {state_writer.board_path}")

    persisted_issue_states = state_writer.state.get("issues", {})
    seeded_issue_history = tuple(
        statusui.IssueResultSummary(
            issue_number=issue.number,
            status=statusui.DashboardStatus.PASS,
            pass_number=1,
            elapsed_seconds=0.0,
        )
        for issue in source_issues
        if issue.completed
        or (
            isinstance(persisted_issue_states, dict)
            and isinstance(persisted_issue_states.get(issue.number), dict)
            and parse_issue_status(
                persisted_issue_states[issue.number].get("status")
            )
            is IssueStatus.COMPLETED
        )
    )

    delivery_dashboard = (
        None
        if args.dry_run
        else statusui.IssueDashboard(
            issue_number=issues[0].number,
            issue_title=issues[0].title,
            position=1,
            total=len(issues),
            issue_history=seeded_issue_history,
        )
    )
    current_schedule_position = 1

    def execute_scheduled_issue(
        issue: Issue,
        phase: SchedulingPhase,
        ordinal: int,
    ) -> RoleResult:
        position = current_schedule_position
        blocker_resolution = phase is SchedulingPhase.BLOCKER_RESOLUTION
        attempt_label = (
            f"blocker-resolution-{ordinal}" if blocker_resolution else None
        )
        if blocker_resolution:
            report_blocker_resolution_start(
                delivery_dashboard,
                issue.number,
                ordinal,
                blocker_resolution_budget,
            )
        result = run_issue(
            issue=issue,
            runner=runner,
            state_writer=state_writer,
            max_passes=1 if blocker_resolution else args.max_passes,
            initial_fix_list=(
                build_clean_retry_fix_list(state_writer, issue, ordinal)
                if blocker_resolution
                else None
            ),
            attempt_label=attempt_label,
            retry_round=ordinal if blocker_resolution else None,
            progress=(
                f"blocker resolution {ordinal}/{blocker_resolution_budget} · "
                f"{issue_progress_label(position, len(issues), issue.number)}"
                if blocker_resolution
                else issue_progress_label(position, len(issues), issue.number)
            ),
            activity_progress=issue_activity_label(
                position,
                len(issues),
                issue.number,
            ),
            dashboard_position=position,
            dashboard_total=len(issues),
            dashboard=delivery_dashboard,
            component_catalog=component_catalog,
        )
        state_writer.clear_run_pause()
        return result

    blocker_resolution_budget = min(
        DEFAULT_BLOCKER_RESOLUTION_PASSES,
        args.blocked_retry_rounds,
    )

    def show_scheduler_projection(projection: SchedulingProjection) -> None:
        nonlocal current_schedule_position
        unresolved_count = len(projection.ready) + len(
            projection.waiting_dependencies
        )
        current_schedule_position = min(
            len(issues),
            max(1, len(issues) - unresolved_count + 1),
        )
        if projection.phase is SchedulingPhase.NORMAL_SCHEDULING:
            phase_label = "NORMAL SCHEDULING"
            assert projection.next_normal is not None
            next_issue = projection.next_normal.number
        elif projection.phase is SchedulingPhase.BLOCKER_RESOLUTION:
            assert projection.next_blocker is not None
            phase_label = (
                "BLOCKER RESOLUTION · "
                f"round {projection.blocker_round}/{blocker_resolution_budget}"
            )
            next_issue = projection.next_blocker.number
        elif projection.phase is SchedulingPhase.COMPLETE:
            phase_label = "COMPLETE"
            next_issue = "none"
        else:
            phase_label = "EXHAUSTED"
            next_issue = "none"
        summary = (
            f"SCHEDULER · {phase_label} · {len(projection.ready)} ready · "
            f"{len(projection.waiting_dependencies)} waiting · next {next_issue}"
        )
        if delivery_dashboard is not None:
            delivery_dashboard.show_scheduler_status(summary)
            if delivery_dashboard.enabled:
                return
        print(summary)

    schedule_result: DependencyScheduleResult | None = None
    try:
        schedule_result = execute_dependency_schedule(
            issues=issues,
            graph=source_issue_graph,
            state_writer=state_writer,
            execute_issue=execute_scheduled_issue,
            projection_callback=show_scheduler_projection,
            allow_blocker_resolution=(
                not args.no_blocked_retry
                and args.blocked_retry_rounds > 0
                and not args.dry_run
            ),
            blocker_resolution_passes=blocker_resolution_budget,
            simulation=args.dry_run,
        )
    except RunWideBlockerError as error:
        state_writer.record_run_paused(error.blocker)
        paused_status = statusui.render_status("BLOCKED", stream=sys.stderr)
        print(
            f"RUN PAUSED · {paused_status} · {error.blocker.kind.value} · "
            f"{error.blocker.summary}",
            file=sys.stderr,
        )
    finally:
        if delivery_dashboard is not None:
            delivery_dashboard.close()

    if schedule_result is None:
        return 75

    overall_status = 0 if schedule_result.completed else 2
    if not schedule_result.completed:
        report_unresolved_dependency_cut(schedule_result)

    if args.self_improvement_wiki:
        publish_portable_screen(
            "Dev Loop > Post-run Tasks > Self-improvement Wiki\n\n"
            "Updating durable self-improvement lessons.\n"
            "Repeated repository operations are coalesced in the activity feed."
        )
        if args.dry_run:
            print("Dev Loop self-improvement wiki update skipped for dry run.")
        else:
            try:
                wiki_root = resolve_self_improvement_wiki_path(bundle.root, args.self_improvement_wiki_path)
            except ValueError as exc:
                parser.error(str(exc))

            ensure_self_improvement_wiki(wiki_root)
            context_path = write_self_improvement_context(
                wiki_root,
                state=state_writer.state,
                state_path=state_writer.state_path,
                board_path=state_writer.board_path,
                target_repo_root=repo_root,
                prd_path=prd_in_repo,
                issues_index=issues_index_in_repo,
            )
            print(f"Dev Loop self-improvement wiki: {wiki_root}")
            memory_result = runner.run_self_improvement_compiler(
                state_path=state_writer.state_path,
                board_path=state_writer.board_path,
                wiki_root=wiki_root,
                max_lessons=args.self_improvement_max_lessons,
                compiler_repo_root=bundle.root,
                run_context_path=context_path,
            )
            state_writer.record_self_improvement_wiki_result(wiki_root, memory_result)
            safe_summary = sanitize_terminal_text(
                memory_result.summary,
                preserve_newlines=False,
            )

            if memory_result.status == "PASS":
                print(f"Dev Loop self-improvement wiki updated: {safe_summary}")
            else:
                print(
                    f"Dev Loop self-improvement wiki update "
                    f"{memory_result.status}: {safe_summary}",
                    file=sys.stderr,
                )

    if overall_status == 0:
        publish_portable_screen(
            "Dev Loop > Final Result\n\n"
            "Workflow completed.\n"
            f"Issues processed: {len(issues)}\n"
            f"Loop state: {state_writer.board_path}"
        )
        print("Dev loop finished.")
        offer_merge_followup(
            source_repo=source_repo,
            implementation_repo=repo_root,
            source_branch=source_branch,
            interactive=not args.non_interactive and not args.dry_run,
        )
    else:
        publish_portable_screen(
            "Dev Loop > Final Result\n\n"
            "Workflow finished with blocked or failed Issues.\n"
            f"Issues processed: {len(issues)}\n"
            f"Loop state: {state_writer.board_path}\n\n"
            "Use F4 to inspect captured activity and diagnostics."
        )
        print("Dev loop finished with blocked or failed issues.", file=sys.stderr)

    return overall_status


def validate_prd_target_product(prd_path: Path) -> None:
    require_portable_target(prd_path, artifact_name="PRD")


def validate_issue_target_product(issue_path: Path) -> None:
    require_portable_target(issue_path, artifact_name="issue")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devloop",
        description="Run local PRD + issue-pack tasks through Codex coder, review, and QA gates.",
    )
    parser.add_argument("--prd", required=True, help="Path to the parent PRD Markdown file.")
    parser.add_argument("--issues", required=True, help="Path to the local issue README/index Markdown file.")
    parser.add_argument("--preset", default="presets/generic-minimal.json", help="Preset JSON path. Relative paths are resolved from the bundle root.")
    parser.add_argument("--all", action="store_true", help="Run dependency-ready issues until completion or bounded exhaustion.")
    parser.add_argument("--start-issue", help="Issue number or filename prefix to start from.")
    parser.add_argument("--max-passes", type=int, default=3, help="Maximum coder passes per issue.")
    parser.add_argument("--blocked-retry-rounds", type=int, default=DEFAULT_BLOCKER_RESOLUTION_PASSES, help="Maximum fair Blocker Resolution passes per ready issue, capped at 5. Default: 5.")
    parser.add_argument("--blocked-retry-max-passes", type=int, default=1, help="Deprecated compatibility option; each Blocker Resolution attempt always consumes one workflow pass.")
    parser.add_argument("--no-blocked-retry", action="store_true", help="Disable Blocker Resolution.")
    parser.add_argument("--dry-run", action="store_true", help="Render prompts and state without invoking Codex or modifying issues.")
    parser.add_argument("--plain", action="store_true", help="Use line-oriented output instead of the full-screen terminal application.")
    parser.add_argument("--codex", default="codex", help="Codex executable path or command name.")
    parser.add_argument("--sandbox", default="workspace-write", help="Codex sandbox mode. Default: workspace-write.")
    parser.add_argument("--approval-policy", default="never", choices=["never", "on-request", "untrusted", "on-failure"], help="Codex approval policy. Default: never.")
    parser.add_argument("--self-improvement-wiki-path", default=DEFAULT_SELF_IMPROVEMENT_WIKI_PATH, help=f"Bundle-relative path to the Dev Loop self-improvement wiki. Default: {DEFAULT_SELF_IMPROVEMENT_WIKI_PATH}.")
    parser.add_argument("--self-improvement-max-lessons", dest="self_improvement_max_lessons", type=int, default=5, help="Maximum durable self-improvement lessons to add or update after a run. Default: 5.")
    wiki_group = parser.add_mutually_exclusive_group()
    wiki_group.add_argument("--self-improvement-wiki", dest="self_improvement_wiki", action="store_true", default=True, help="Read and update the Dev Loop self-improvement wiki. This is the default.")
    wiki_group.add_argument("--no-self-improvement-wiki", dest="self_improvement_wiki", action="store_false", help="Do not read or update the Dev Loop self-improvement wiki.")
    parser.add_argument("--create-worktree", action="store_true", help="Create a dedicated implementation worktree.")
    parser.add_argument("--no-worktree", action="store_true", help="Use the issue worktree directly.")
    parser.add_argument("--worktree-path", help="Path for a new implementation worktree.")
    parser.add_argument("--branch-name", help="Branch name for a new implementation worktree.")
    parser.add_argument("--non-interactive", action="store_true", help="Do not prompt for missing worktree decisions.")
    return parser


def issue_progress_label(position: int, total: int, issue_number: str) -> str:
    after_current = max(0, total - position)
    return (
        f"issue {_terminal_issue_identifier(issue_number)} ({position}/{total}; "
        f"{after_current} after current)"
    )


def issue_activity_label(position: int, total: int, issue_number: str) -> str:
    after_current = max(0, total - position)
    return (
        f"{_terminal_issue_identifier(issue_number)} "
        f"{position}/{total} +{after_current}"
    )


def report_blocker_resolution_start(
    dashboard: statusui.IssueDashboard | None,
    issue_number: str,
    ordinal: int,
    budget: int,
) -> None:
    if dashboard is not None and dashboard.enabled:
        return
    print(
        f"\nBlocker Resolution pass {ordinal}/{budget}: "
        f"{_terminal_issue_identifier(issue_number)}"
    )


def _terminal_issue_identifier(value: object) -> str:
    return compact_terminal_text(
        value,
        max_length=_MAX_ISSUE_IDENTIFIER_LENGTH,
    )


def _terminal_issue_title(value: object) -> str:
    return compact_terminal_text(value, max_length=_MAX_ISSUE_TITLE_LENGTH)


def run_issue(
    issue: Issue,
    runner: CodexRunner,
    state_writer: LoopStateWriter,
    max_passes: int,
    initial_fix_list: list[str] | None = None,
    attempt_label: str | None = None,
    retry_round: int | None = None,
    *,
    progress: str = "",
    activity_progress: str = "",
    dashboard_position: int = 1,
    dashboard_total: int = 1,
    dashboard: statusui.IssueDashboard | None = None,
    component_catalog: PortableStepComponentCatalog | None = None,
) -> RoleResult:
    catalog = component_catalog or _portable_catalog_for_runner(runner)
    display_issue_number = _terminal_issue_identifier(issue.number)
    display_issue_title = _terminal_issue_title(issue.title)
    resume_cursor = IssueResumeCursor()
    fix_list = list(initial_fix_list or [])
    start_pass = resume_cursor.pass_number
    last_coder = resume_cursor.coder_result
    last_review = resume_cursor.reviewer_result
    last_qa = resume_cursor.qa_result
    portable_recovery: PortableWorkflowCheckpoint | None = None
    completed_execution = None
    portable_workflow: WorkflowDefinition | None = None
    if (
        not runner.dry_run
        and resume_cursor == IssueResumeCursor()
        and state_writer.has_resolved_workflow()
    ):
        portable_workflow = resolve_run_workflow(state_writer, catalog)
        completed_execution = state_writer.completed_portable_workflow(
            issue,
            portable_workflow,
        )
        portable_recovery = state_writer.resume_portable_workflow(
            issue,
            portable_workflow,
        )
        if portable_recovery is None:
            portable_recovery = state_writer.retry_portable_workflow(
                issue,
                portable_workflow,
                pass_number=start_pass,
            )
    if (
        attempt_label is None
        and initial_fix_list is None
        and resume_cursor == IssueResumeCursor()
        and completed_execution is None
        and portable_recovery is None
    ):
        resume_cursor = state_writer.resume_issue(issue)
        start_pass = resume_cursor.pass_number
        last_coder = resume_cursor.coder_result
        last_review = resume_cursor.reviewer_result
        last_qa = resume_cursor.qa_result
        fix_list = list(resume_cursor.fix_list)
    if (
        not runner.dry_run
        and resume_cursor == IssueResumeCursor()
        and portable_workflow is None
    ):
        portable_workflow = resolve_run_workflow(state_writer, catalog)
        completed_execution = state_writer.completed_portable_workflow(
            issue,
            portable_workflow,
        )
        portable_recovery = state_writer.resume_portable_workflow(
            issue,
            portable_workflow,
        )
        if portable_recovery is None:
            portable_recovery = state_writer.retry_portable_workflow(
                issue,
                portable_workflow,
                pass_number=start_pass,
            )
    if completed_execution is not None:
        assert portable_workflow is not None
        mark_portable_issue_completed(
            issue.path,
            portable_workflow,
            completed_execution,
        )
        state_writer.record_portable_execution_result(
            issue,
            completed_execution,
            attempt_label=attempt_label,
            retry_round=retry_round,
        )
        print(f"[{display_issue_number}] Completed from the persisted workflow result.")
        return completed_execution.role_result

    state_writer.record_issue_start(issue, attempt_label=attempt_label, retry_round=retry_round)
    if attempt_label:
        display_attempt_label = compact_terminal_text(
            attempt_label,
            max_length=_MAX_ATTEMPT_LABEL_LENGTH,
        )
        display_issue_title = _terminal_issue_title(
            f"{display_issue_title} ({display_attempt_label})"
        )
    title = display_issue_title

    if runner.dry_run:
        print(f"\n[{display_issue_number}] {display_issue_title}")
        workflow = resolve_run_workflow(state_writer, catalog)
        runner.render_dry_run_prompts(
            issue,
            (
                (
                    catalog.resolve(step.component_id).adapter.role,
                    catalog.resolve(step.component_id).adapter.execution_role,
                    step.display_name,
                    str(step.instance_id),
                    step.capability_profile.skills,
                    step.capability_profile.agent_references,
                    step.guidance.text if step.guidance is not None else None,
                )
                for step in workflow.primary_path()
                if catalog.resolve(step.component_id).scope is StepScope.ISSUE
            ),
        )
        state_writer.record_issue_dry_run(issue)
        print(f"[{display_issue_number}] Dry run prompts rendered.")
        return RoleResult(status="PASS", summary="Dry run prompts rendered.")

    owns_dashboard = dashboard is None
    if dashboard is None:
        dashboard = statusui.IssueDashboard(
            issue_number=issue.number,
            issue_title=title,
            position=dashboard_position,
            total=dashboard_total,
        )
    dashboard.show_issue(
        issue_number=issue.number,
        issue_title=title,
        position=dashboard_position,
        total=dashboard_total,
    )
    if last_coder is not None:
        dashboard.restore_role(Stage.DEVELOPMENT, last_coder.status)
    if last_review is not None:
        dashboard.restore_role(Stage.REVIEW, last_review.status)
    if last_qa is not None:
        dashboard.restore_role(Stage.QA, last_qa.status)
    if not dashboard.enabled:
        print(f"\n[{display_issue_number}] {title}")

    if resume_cursor.next_role == ResumeRole.COMPLETE:
        if last_coder is None or last_review is None or last_qa is None:
            raise RuntimeError(f"Issue {issue.number} cannot resume completion without all gate results.")
        mark_issue_completed(issue.path, last_coder, last_review, last_qa)
        state_writer.record_issue_completed(issue, last_coder, last_review, last_qa)
        dashboard.finish_issue("PASS", "Completed from the persisted QA result.")
        if owns_dashboard:
            dashboard.close()
        if not dashboard.enabled:
            print(f"[{display_issue_number}] Completed from the persisted QA result.")
        return RoleResult(status="PASS", summary=f"Issue {issue.number} completed.")

    if resume_cursor == IssueResumeCursor():
        return run_fresh_portable_issue(
            issue=issue,
            runner=runner,
            state_writer=state_writer,
            pass_number=start_pass,
            max_passes=max_passes,
            initial_fix_list=fix_list,
            attempt_label=attempt_label,
            retry_round=retry_round,
            progress=progress,
            activity_progress=activity_progress,
            dashboard=dashboard,
            owns_dashboard=owns_dashboard,
            recovery=portable_recovery,
            catalog=catalog,
        )

    for pass_number in range(start_pass, max_passes + 1):
        context = f"{progress or f'issue {issue.number}'} / pass {pass_number}"
        role_progress = activity_progress or f"issue {issue.number}"
        next_role = resume_cursor.next_role if pass_number == start_pass else ResumeRole.CODER

        if next_role == ResumeRole.CODER:
            begin_role_output(
                dashboard,
                Stage.DEVELOPMENT,
                context,
                issue.number,
                pass_number,
                "coder",
            )
            try:
                last_coder = runner.run_role(
                    role="coder",
                    issue=issue,
                    pass_number=pass_number,
                    fix_list=fix_list,
                    attempt_label=attempt_label,
                    progress=role_progress,
                    activity_callback=(
                        dashboard.notify_activity if dashboard.enabled else None
                    ),
                )
            except BaseException:
                dashboard.close("Development interrupted.")
                raise
            state_writer.record_role_result(
                issue,
                "coder",
                pass_number,
                last_coder,
                attempt_label=attempt_label,
                retry_round=retry_round,
            )
            finish_role_output(
                dashboard,
                Stage.DEVELOPMENT,
                issue.number,
                "coder",
                last_coder,
            )

            if last_coder.status != "PASS":
                state_writer.record_issue_blocked(
                    issue,
                    "coder",
                    last_coder,
                    attempt_label=attempt_label,
                    retry_round=retry_round,
                )
                dashboard.finish_issue(last_coder.status, last_coder.summary)
                if owns_dashboard:
                    dashboard.close()
                return last_coder

        if last_coder is None:
            raise RuntimeError(f"Issue {issue.number} cannot resume review without a coder result.")

        if next_role in {ResumeRole.CODER, ResumeRole.REVIEWER}:
            begin_role_output(
                dashboard,
                Stage.REVIEW,
                context,
                issue.number,
                pass_number,
                "reviewer",
            )
            try:
                last_review = runner.run_role(
                    role="reviewer",
                    issue=issue,
                    pass_number=pass_number,
                    coder_result=last_coder,
                    attempt_label=attempt_label,
                    progress=role_progress,
                    activity_callback=(
                        dashboard.notify_activity if dashboard.enabled else None
                    ),
                )
            except BaseException:
                dashboard.close("Review interrupted.")
                raise
            state_writer.record_role_result(
                issue,
                "reviewer",
                pass_number,
                last_review,
                attempt_label=attempt_label,
                retry_round=retry_round,
            )
            finish_role_output(
                dashboard,
                Stage.REVIEW,
                issue.number,
                "reviewer",
                last_review,
            )

            if last_review.status != "PASS":
                fix_list = last_review.fix_list or last_review.findings
                continue

        if last_review is None:
            raise RuntimeError(f"Issue {issue.number} cannot resume QA without a reviewer result.")

        begin_role_output(
            dashboard,
            Stage.QA,
            context,
            issue.number,
            pass_number,
            "qa",
        )
        try:
            last_qa = runner.run_role(
                role="qa",
                issue=issue,
                pass_number=pass_number,
                coder_result=last_coder,
                review_result=last_review,
                attempt_label=attempt_label,
                progress=role_progress,
                activity_callback=(
                    dashboard.notify_activity if dashboard.enabled else None
                ),
            )
        except BaseException:
            dashboard.close("QA interrupted.")
            raise
        state_writer.record_role_result(
            issue,
            "qa",
            pass_number,
            last_qa,
            attempt_label=attempt_label,
            retry_round=retry_round,
        )
        finish_role_output(
            dashboard,
            Stage.QA,
            issue.number,
            "qa",
            last_qa,
        )

        if last_qa.status != "PASS":
            fix_list = last_qa.fix_list or last_qa.findings
            continue

        mark_issue_completed(issue.path, last_coder, last_review, last_qa)
        state_writer.record_issue_completed(
            issue,
            last_coder,
            last_review,
            last_qa,
            attempt_label=attempt_label,
            retry_round=retry_round,
        )
        dashboard.finish_issue("PASS", "Issue completed.")
        if owns_dashboard:
            dashboard.close()
        if not dashboard.enabled:
            print(f"[{display_issue_number}] Completed.")
        return RoleResult(status="PASS", summary=f"Issue {issue.number} completed.")

    blocked_summary = f"Issue {issue.number} reached max passes ({max_passes})."
    if attempt_label:
        blocked_summary = f"Issue {issue.number} reached max passes ({max_passes}) during {attempt_label}."
    blocked = RoleResult(
        status="BLOCKED",
        summary=blocked_summary,
        fix_list=fix_list,
    )
    state_writer.record_issue_blocked(
        issue,
        "max-passes",
        blocked,
        attempt_label=attempt_label,
        retry_round=retry_round,
    )
    dashboard.finish_issue(blocked.status, blocked.summary)
    if owns_dashboard:
        dashboard.close()
    if not dashboard.enabled:
        report_role_result(issue.number, "max-passes", blocked)
    return blocked


def run_fresh_portable_issue(
    *,
    issue: Issue,
    runner: CodexRunner,
    state_writer: LoopStateWriter,
    pass_number: int,
    max_passes: int,
    initial_fix_list: list[str],
    attempt_label: str | None,
    retry_round: int | None,
    progress: str,
    activity_progress: str,
    dashboard: statusui.IssueDashboard,
    owns_dashboard: bool,
    recovery: PortableWorkflowCheckpoint | None,
    catalog: PortableStepComponentCatalog,
) -> RoleResult:
    workflow = resolve_run_workflow(state_writer, catalog)
    role_runner = _PortableConsoleRoleRunner(
        runner=runner,
        issue=issue,
        dashboard=dashboard,
        progress=progress,
        activity_progress=activity_progress,
        initial_fix_list=initial_fix_list,
        attempt_label=attempt_label,
    )

    def record_checkpoint(checkpoint: PortableWorkflowCheckpoint) -> None:
        state_writer.record_portable_checkpoint(issue, checkpoint)
        dashboard.show_workflow_progress(
            statusui.project_workflow_progress(
                workflow,
                catalog,
                checkpoint.runtime_states,
                checkpoint.attempts,
                issue_id=issue.number,
                issue_title=issue.title,
            )
        )

    execution = PortableWorkflowExecutor(workflow, catalog, role_runner).run(
        issue,
        pass_number=pass_number,
        max_passes=max_passes,
        recovery=recovery,
        checkpoint=record_checkpoint,
    )
    if execution.issue_status is IssueStatus.COMPLETED:
        mark_portable_issue_completed(issue.path, workflow, execution)
    state_writer.record_portable_execution_result(
        issue,
        execution,
        attempt_label=attempt_label,
        retry_round=retry_round,
    )
    step_progress = statusui.project_workflow_progress(
        workflow,
        catalog,
        execution.runtime_states,
        execution.attempts,
        issue_id=issue.number,
        issue_title=issue.title,
    )
    dashboard.show_workflow_progress(step_progress)

    dashboard_status = {
        IssueStatus.COMPLETED: "PASS",
        IssueStatus.BLOCKED: "BLOCKED",
        IssueStatus.FAILED: "FAIL",
        IssueStatus.CANCELLED: "BLOCKED",
        IssueStatus.CHANGES_REQUESTED: "FAIL",
        IssueStatus.IN_PROGRESS: "FAIL",
        IssueStatus.PENDING: "FAIL",
        IssueStatus.READY: "FAIL",
        IssueStatus.WAITING_FOR_INPUT: "BLOCKED",
        IssueStatus.SKIPPED: "BLOCKED",
    }[execution.issue_status]
    if execution.issue_status is IssueStatus.COMPLETED:
        dashboard.finish_issue(dashboard_status, "Issue completed.")
        if not dashboard.enabled:
            print(
                f"[{_terminal_issue_identifier(issue.number)}] "
                "Completed."
            )
    else:
        dashboard.finish_issue(dashboard_status, execution.role_result.summary)

    if owns_dashboard:
        dashboard.close()
    return execution.role_result


def resolve_run_workflow(
    state_writer: LoopStateWriter,
    catalog: PortableStepComponentCatalog,
    *,
    user_workflow_path: Path | None = None,
    live_model_catalog: CodexModelCatalog | None = None,
    require_codex_preflight: bool = False,
    workflow_snapshot: WorkflowDefinition | None = None,
) -> WorkflowDefinition:
    if (
        "resolved_workflow" in state_writer.state
        or "resolved_workflow_hash" in state_writer.state
    ):
        workflow = state_writer.resolved_workflow(catalog)
        if workflow_snapshot is not None:
            state_writer.record_resolved_workflow(workflow_snapshot, catalog)
        if require_codex_preflight:
            if live_model_catalog is None:
                raise ValueError("A fresh live Codex Model Catalog is required.")
            preflight_codex_execution_settings(
                workflow,
                catalog,
                live_model_catalog,
            )
        return workflow
    if workflow_snapshot is not None:
        workflow = load_portable_workflow(workflow_snapshot.to_dict(), catalog)
    else:
        workflow = (
            WorkflowDefaultStore(user_workflow_path, catalog).load()
            if user_workflow_path is not None
            else default_portable_workflow()
        )
    if require_codex_preflight:
        if live_model_catalog is None:
            raise ValueError("A fresh live Codex Model Catalog is required.")
        preflight_codex_execution_settings(
            workflow,
            catalog,
            live_model_catalog,
        )
    return state_writer.record_resolved_workflow(workflow, catalog)


def resolve_run_workflow_with_repair(
    state_writer: LoopStateWriter,
    catalog: PortableStepComponentCatalog,
    *,
    user_workflow_path: Path,
    model_catalog_loader: Callable[[], CodexModelCatalog],
    read_line: Callable[[str], str] | None = None,
    write: Callable[[str], None] | None = None,
    workflow_snapshot: WorkflowDefinition | None = None,
    allow_interactive_repair: bool = True,
) -> WorkflowDefinition | None:
    """Authorize and snapshot a run, with a reachable terminal repair loop."""
    reader = read_line or input
    writer = write or print
    immutable_snapshot = workflow_snapshot is not None or (
        "resolved_workflow" in state_writer.state
        or "resolved_workflow_hash" in state_writer.state
    )
    while True:
        try:
            live_model_catalog = model_catalog_loader()
            return resolve_run_workflow(
                state_writer,
                catalog,
                user_workflow_path=user_workflow_path,
                live_model_catalog=live_model_catalog,
                require_codex_preflight=True,
                workflow_snapshot=workflow_snapshot,
            )
        except (CatalogDiscoveryError, ValueError) as error:
            safe_error = sanitize_terminal_text(error, preserve_newlines=False)
            if not allow_interactive_repair:
                raise ValueError(
                    f"{safe_error} Retry after restoring live catalog availability or "
                    "repair the User Workflow Default before starting the command."
                ) from error
            writer(f"Codex Execution Settings preflight failed: {safe_error}")
            if immutable_snapshot:
                writer(
                    "The Current Run snapshot is immutable. retry-catalog retries "
                    "live discovery; /quit leaves it unchanged."
                )
            else:
                writer(
                    "Recovery: /options opens the Workflow Editor; retry-catalog "
                    "retries live discovery; /quit stops the run."
                )
        action = reader(
            "Preflight action [/options/retry-catalog/quit]: "
        ).strip().casefold()
        if action == "/options" and not immutable_snapshot:
            run_workflow_editor(
                user_workflow_path,
                read_line=reader,
                write=writer,
                terminal_width=max(
                    40,
                    shutil.get_terminal_size(fallback=(100, 24)).columns,
                ),
                terminal_height=max(
                    10,
                    shutil.get_terminal_size(fallback=(100, 24)).lines,
                ),
                catalog=catalog,
                model_catalog_loader=model_catalog_loader,
            )
        elif action in {"retry-catalog", "/retry-catalog"}:
            continue
        elif action in {"quit", "/quit"}:
            return None
        elif action == "/options":
            writer(
                "Current Run settings cannot be edited; use retry-catalog or /quit."
            )
        else:
            writer("Choose /options, retry-catalog, or /quit.")


def _portable_catalog_for_runner(runner: CodexRunner) -> PortableStepComponentCatalog:
    bundle = getattr(runner, "bundle", None)
    preset = getattr(runner, "preset", None)
    bundle_root = getattr(bundle, "root", None)
    roles = getattr(preset, "roles", None)
    if isinstance(bundle_root, Path) and isinstance(roles, Mapping):
        return build_portable_component_catalog(bundle_root, roles)
    return default_portable_component_catalog()


class _PortableConsoleRoleRunner:
    def __init__(
        self,
        *,
        runner: CodexRunner,
        issue: Issue,
        dashboard: statusui.IssueDashboard,
        progress: str,
        activity_progress: str,
        initial_fix_list: list[str],
        attempt_label: str | None,
    ) -> None:
        self._runner = runner
        self._issue = issue
        self._dashboard = dashboard
        self._progress = progress
        self._activity_progress = activity_progress
        self._initial_fix_list = list(initial_fix_list)
        self._attempt_label = attempt_label
        self._development_started = False

    def run_role(self, **arguments: Any) -> RoleResult:
        role = str(arguments["role"])
        execution_role = str(arguments.get("role_adapter", role))
        pass_number = int(arguments["pass_number"])
        display_name = str(arguments["step_display_name"])
        stage = {
            "coder": Stage.DEVELOPMENT,
            "reviewer": Stage.REVIEW,
            "qa": Stage.QA,
        }[execution_role]
        context = f"{self._progress or f'issue {self._issue.number}'} / pass {pass_number}"
        begin_role_output(
            self._dashboard,
            stage,
            context,
            self._issue.number,
            pass_number,
            display_name,
        )
        arguments["progress"] = self._activity_progress or f"issue {self._issue.number}"
        if execution_role == "coder" and not self._development_started:
            if self._initial_fix_list and not arguments.get("fix_list"):
                arguments["fix_list"] = self._initial_fix_list
            self._development_started = True
        if self._attempt_label is not None:
            arguments["attempt_label"] = self._attempt_label
        arguments["activity_callback"] = self._dashboard.notify_activity
        try:
            result = self._runner.run_role(**arguments)
        except BaseException:
            self._dashboard.close(f"{display_name} interrupted.")
            raise
        finish_role_output(
            self._dashboard,
            stage,
            self._issue.number,
            display_name,
            result,
        )
        return result


def retry_blocked_issues(
    blocked_issues: list[Issue],
    runner: CodexRunner,
    state_writer: LoopStateWriter,
    max_passes: int,
    max_rounds: int,
    component_catalog: PortableStepComponentCatalog | None = None,
) -> list[Issue]:
    remaining = list(blocked_issues)

    for retry_round in range(1, max_rounds + 1):
        if not remaining:
            break

        issue_numbers = ", ".join(
            _terminal_issue_identifier(issue.number) for issue in remaining
        )
        print(f"\nBlocked retry round {retry_round}/{max_rounds}: {issue_numbers}")
        state_writer.record_blocked_retry_round_start(
            retry_round=retry_round,
            issues=[issue.number for issue in remaining],
        )

        next_remaining: list[Issue] = []
        retry_dashboard = statusui.IssueDashboard(
            issue_number=remaining[0].number,
            issue_title=remaining[0].title,
            position=1,
            total=len(remaining),
        )
        for position, issue in enumerate(remaining, start=1):
            attempt_label = f"clean-retry-{retry_round}"
            retry_fix_list = build_clean_retry_fix_list(state_writer, issue, retry_round)
            issue_result = run_issue(
                issue=issue,
                runner=runner,
                state_writer=state_writer,
                max_passes=max_passes,
                initial_fix_list=retry_fix_list,
                attempt_label=attempt_label,
                retry_round=retry_round,
                progress=(
                    f"retry {retry_round}/{max_rounds} · "
                    f"{issue_progress_label(position, len(remaining), issue.number)}"
                ),
                activity_progress=(
                    f"r{retry_round} "
                    f"{issue_activity_label(position, len(remaining), issue.number)}"
                ),
                dashboard_position=position,
                dashboard_total=len(remaining),
                dashboard=retry_dashboard,
                component_catalog=component_catalog,
            )
            if issue_result.status in {"BLOCKED", "FAIL"}:
                next_remaining.append(issue)

        retry_dashboard.close()

        remaining = next_remaining

    if remaining:
        issue_numbers = ", ".join(
            _terminal_issue_identifier(issue.number) for issue in remaining
        )
        print(f"Blocked retry exhausted; still blocked: {issue_numbers}", file=sys.stderr)
    else:
        print("Blocked retry completed all previously blocked issues.")

    return remaining


def build_clean_retry_fix_list(
    state_writer: LoopStateWriter,
    issue: Issue,
    retry_round: int,
) -> list[str]:
    issue_state = state_writer.issue_state(issue)
    lines = [
        f"Clean retry round {retry_round} for previously blocked issue {issue.number}.",
        "Start from the current repository state; do not assume any prior attempted fix is correct.",
        "Keep context minimal: read the PRD, issue file, current diff, and this compact blocker summary.",
    ]

    gate = compact_line(issue_state.get("blocked_gate") or "unknown")
    summary = compact_line(issue_state.get("blocked_summary") or "")
    if summary:
        lines.append(f"Previous blocked gate: {gate}. Summary: {summary}")

    blocker_items = list_of_state_strings(issue_state.get("fix_list"))
    if not blocker_items:
        blocker_items = latest_blocker_items(issue_state)

    if blocker_items:
        lines.extend(f"Blocker detail: {compact_line(item)}" for item in blocker_items[:4])
    else:
        lines.append("Blocker detail: no actionable blocker was recorded; inspect current diff and acceptance criteria.")

    return lines


def latest_blocker_items(issue_state: dict[str, Any]) -> list[str]:
    passes = issue_state.get("passes")
    if not isinstance(passes, list):
        return []

    for pass_entry in reversed(passes):
        if not isinstance(pass_entry, dict):
            continue
        result = pass_entry.get("result")
        if not isinstance(result, dict) or result.get("status") == "PASS":
            continue

        role = pass_entry.get("role", "unknown-role")
        pass_number = pass_entry.get("pass", "unknown-pass")
        items = list_of_state_strings(result.get("fix_list"))
        if not items:
            items = list_of_state_strings(result.get("findings"))
        if not items:
            items = list_of_state_strings(result.get("residual_risks"))

        summary = compact_line(result.get("summary") or "")
        if summary:
            return [f"{role} pass {pass_number}: {summary}", *items]
        return items

    return []


def list_of_state_strings(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def compact_line(value: Any, max_length: int = 300) -> str:
    text = " ".join(str(value).split())
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."


def begin_role_output(
    dashboard: statusui.IssueDashboard,
    stage: Stage,
    context: str,
    issue_number: str,
    pass_number: int,
    role: str,
) -> None:
    if dashboard.enabled or dashboard.has_workflow_progress:
        dashboard.begin_role(stage, pass_number)
    if dashboard.enabled:
        return
    if not dashboard.has_workflow_progress:
        print(statusui.render_banner(stage, context))
    safe_issue_number = _terminal_issue_identifier(issue_number)
    safe_role = _terminal_issue_title(role)
    print(f"[{safe_issue_number}] Pass {pass_number}: {safe_role}")


def finish_role_output(
    dashboard: statusui.IssueDashboard,
    stage: Stage,
    issue_number: str,
    role: str,
    result: RoleResult,
) -> None:
    if dashboard.enabled or dashboard.has_workflow_progress:
        dashboard.finish_role(stage, result.status, result.summary)
    if dashboard.enabled:
        return
    report_role_result(issue_number, role, result)


def report_role_result(issue_number: str, role: str, result: RoleResult) -> None:
    safe_issue_number = _terminal_issue_identifier(issue_number)
    safe_role = _terminal_issue_title(role)
    safe_summary = sanitize_terminal_text(
        result.summary,
        preserve_newlines=False,
    )
    if result.status == "PASS":
        rendered_status = statusui.render_status(result.status, sys.stdout)
        if safe_summary:
            print(
                f"[{safe_issue_number}] {safe_role}: {rendered_status} - {safe_summary}"
            )
        else:
            print(f"[{safe_issue_number}] {safe_role}: {rendered_status}")
        return

    rendered_status = statusui.render_status(result.status, sys.stderr)
    message = f"[{safe_issue_number}] {safe_role}: {rendered_status}"
    if safe_summary:
        message = f"{message} - {safe_summary}"
    print(message, file=sys.stderr)


def resolve_bundle_path(bundle_root: Path, path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return (bundle_root / path).resolve()


def ensure_planning_artifacts_in_worktree(
    *,
    prd_path: Path,
    issues_index: Path,
    source_repo: Path,
    target_repo: Path,
) -> None:
    target_prd = map_path_to_worktree(prd_path, source_repo, target_repo)
    target_issues_index = map_path_to_worktree(issues_index, source_repo, target_repo)
    if target_prd.is_file() and target_issues_index.is_file():
        return

    for source_path in planning_artifact_roots(prd_path, issues_index):
        copy_path_to_worktree(source_path, source_repo, target_repo)


def planning_artifact_roots(prd_path: Path, issues_index: Path) -> list[Path]:
    prd_folder = prd_path.parent.resolve()
    try:
        issues_index.resolve().relative_to(prd_folder)
    except ValueError:
        return [prd_path, issues_index.parent]
    return [prd_folder]


def copy_path_to_worktree(source_path: Path, source_repo: Path, target_repo: Path) -> None:
    try:
        relative = source_path.resolve().relative_to(source_repo.resolve())
    except ValueError:
        return

    target_path = target_repo / relative
    if source_path.is_dir():
        shutil.copytree(source_path, target_path, dirs_exist_ok=True)
    elif source_path.is_file():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
    else:
        return

    print(f"Copied planning artifact into implementation worktree: {target_path}")


def map_path_to_worktree(path: Path, source_repo: Path, target_repo: Path) -> Path:
    try:
        relative = path.resolve().relative_to(source_repo.resolve())
    except ValueError:
        return path
    return (target_repo / relative).resolve()


def map_issue_to_worktree(issue: Issue, source_repo: Path, target_repo: Path) -> Issue:
    mapped_path = map_path_to_worktree(issue.path, source_repo, target_repo)
    return Issue(
        number=issue.number,
        title=issue.title,
        path=mapped_path,
        completed=Issue.is_completed_file(mapped_path),
        dependencies=issue.dependencies,
    )


def map_selected_issues_to_worktree(
    selected_source_issues: list[Issue],
    source_repo: Path,
    target_repo: Path,
) -> list[Issue]:
    mapped = [
        map_issue_to_worktree(issue, source_repo, target_repo)
        for issue in selected_source_issues
    ]
    return [issue for issue in mapped if not issue.completed]


def offer_merge_followup(
    *,
    source_repo: Path,
    implementation_repo: Path,
    source_branch: str,
    interactive: bool,
) -> None:
    if not interactive:
        return

    implementation_branch = git_current_branch(implementation_repo)
    if not implementation_branch:
        print("Merge prompt skipped because the implementation branch could not be detected.", file=sys.stderr)
        return

    if not ask_yes_no(
        f"Development completed. Merge implementation branch '{implementation_branch}' into another branch now?",
        default=False,
    ):
        return

    target_default = source_branch if source_branch and source_branch != implementation_branch else "development"
    target_branch = ask_required("Target branch", default=target_default)
    merge_implementation_branch(
        source_repo=source_repo,
        implementation_repo=implementation_repo,
        implementation_branch=implementation_branch,
        target_branch=target_branch,
    )


def report_mapped_selection(source_issues: list[Issue], mapped_issues: list[Issue]) -> None:
    source_numbers = [issue.number for issue in source_issues]
    mapped_numbers = [issue.number for issue in mapped_issues]
    if mapped_numbers != source_numbers:
        print(
            "Selected issues in implementation worktree: "
            f"{', '.join(_terminal_issue_identifier(item) for item in mapped_numbers)}"
        )


def merge_implementation_branch(
    *,
    source_repo: Path,
    implementation_repo: Path,
    implementation_branch: str,
    target_branch: str,
) -> None:
    implementation_status = git_status_porcelain(implementation_repo)
    if implementation_status:
        print(
            "Automatic merge skipped because the implementation worktree has uncommitted changes. "
            "Commit or stash them first, then merge the branch.",
            file=sys.stderr,
        )
        print(f"Implementation worktree: {implementation_repo}")
        print(f"Target branch: {target_branch}")
        print(f"Implementation branch: {implementation_branch}")
        return

    target_status = git_status_porcelain(source_repo)
    if target_status:
        print(
            "Automatic merge skipped because the target checkout has uncommitted changes. "
            "Clean or stash that checkout first.",
            file=sys.stderr,
        )
        print(f"Target checkout: {source_repo}")
        return

    checkout = run_captured_text(["git", "checkout", target_branch], cwd=source_repo)
    if checkout.returncode != 0:
        print(f"git checkout {target_branch} failed: {checkout.stderr.strip()}", file=sys.stderr)
        return

    merge = run_captured_text(["git", "merge", implementation_branch], cwd=source_repo)
    if merge.returncode != 0:
        print(f"git merge {implementation_branch} failed: {merge.stderr.strip()}", file=sys.stderr)
        return

    print(f"Merged {implementation_branch} into {target_branch}.")


def git_current_branch(repo_root: Path) -> str:
    result = run_captured_text(["git", "branch", "--show-current"], cwd=repo_root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_status_porcelain(repo_root: Path) -> str:
    result = run_captured_text(["git", "status", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        return result.stderr.strip() or result.stdout.strip()
    return result.stdout.strip()


def ask_yes_no(prompt: str, *, default: bool) -> bool:
    from .portable_runtime import active_portable_runtime

    portable_runtime = active_portable_runtime()
    if portable_runtime is not None:
        selected = portable_runtime.choose(
            (("yes", "Yes"), ("no", "No")),
            default_key="yes" if default else "no",
            cancel_key="no",
            render=lambda choice: portable_runtime.show_screen(
                f"{prompt}\n\nSelected: {choice.title()}"
            ),
        )
        return selected == "yes"
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = read_prompt(f"{prompt} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Expected yes or no.", file=sys.stderr)


def ask_required(prompt: str, *, default: str | None = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = read_prompt(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        print("Value is required.", file=sys.stderr)


def read_prompt(prompt: str) -> str:
    global _PROMPT_EDITOR
    if _PROMPT_EDITOR is None:
        _PROMPT_EDITOR = LineEditor(on_paste_image=lambda: None, fallback_hint=None)
    return _PROMPT_EDITOR.read_line(prompt)


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))

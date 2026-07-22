from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import catalog as catalog_module
from . import statusui
from .chat_loop import ChatCallbacks, ChatConfig, run_planning_chat
from .gitrefs import sanitize_branch_name
from .github_install import install_from_github
from .issue_pack import parse_issue_index, select_issues
from .lineeditor import LineEditor
from .model_catalog import (
    CatalogDiscoveryError,
    CodexModelCatalog,
    CodexModelCatalogAdapter,
)
from .portable_workflow import (
    IssueStatus,
    PortableStepComponentCatalog,
    StepInstanceId,
    StepRuntimeState,
    StepRuntimeStatus,
    WorkflowDefinition,
    default_portable_component_catalog,
    parse_issue_status,
    planning_workflow_step,
    preflight_codex_execution_settings,
)
from .portable_component_catalog import build_portable_component_catalog
from .product_scope import TargetProduct, detect_target_product
from .self_improvement_wiki import DEFAULT_SELF_IMPROVEMENT_WIKI_PATH
from .statusui import Stage
from .step_configuration import STEP_GUIDANCE_PRECEDENCE
from .step_configuration import CapabilityKind, CapabilityReference
from .state import LoopStateWriter
from .terminal_text import sanitize_terminal_text
from .templates import BundleContext
from .cli_ui import (
    CAPABILITY_ACTION_BAR,
    CAPABILITY_TOGGLE_COMMAND_GROUPS,
    RESUME_ACTION_BAR,
    STARTUP_ACTION_BAR,
    render_choice_menu,
    render_context_path,
    render_screen_frame,
    terminal_dimensions,
)
from .terminal_menu import choose_menu_option, read_workflow_command, render_app_screen
from .worktree import (
    branch_exists,
    build_worktree_add_command,
    resolve_existing_worktree,
)
from .workflow_editor import (
    WORKFLOW_ACTIONS,
    EditorResult,
    SelectionMenu,
    WorkflowDraft,
    run_workflow_editor,
)
from .workflow_defaults import (
    PORTABLE_PLANNER_CONFIGURATION_FILE,
    WorkflowDefaultStore,
    atomic_write_planner_configuration,
    portable_planner_configuration_path,
)

PLAN_STATE_FILE = PORTABLE_PLANNER_CONFIGURATION_FILE
TARGET_REPO_STATE_KEY = "target_repo"
TARGET_REPO_CONFIRMED_KEY = "target_repo_confirmed"
LAST_WORKTREE_PARENT_STATE_KEY = "last_worktree_parent"
_PROMPT_EDITOR: LineEditor | None = None

# A PRD/issue pair counts as "fresh" if its newest file mtime is within this many
# seconds of the moment planning started. Shared by find_artifacts (resolution
# paths) and find_new_artifacts (the live probe).
ARTIFACT_FRESHNESS_SLACK_SECONDS = 5


@dataclass(frozen=True)
class PlanningArtifacts:
    prd_path: Path
    issues_index: Path


@dataclass(frozen=True)
class StartupMenuResult:
    artifacts: PlanningArtifacts | None = None
    exit_requested: bool = False


@dataclass(frozen=True)
class ResumeMenuResult:
    artifacts: PlanningArtifacts | None = None
    exit_requested: bool = False


@dataclass(frozen=True)
class ResumeCandidate:
    artifacts: PlanningArtifacts
    completed_issues: int
    pending_issues: int
    total_issues: int
    active_issue: str | None
    active_status: str | None
    updated_at: float


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run_planning(parser, args)
    except KeyboardInterrupt:
        # Top-level backstop: covers the chat loop, the /options menus, and the
        # handoff prompts so a mid-run Ctrl+C exits cleanly.
        print("\nAborted.")
        return 130


def _run_planning(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.native_editor:
        os.environ["DEVLOOP_EDITOR"] = "native"

    bundle = BundleContext.from_file(Path(__file__).resolve())
    state_path = plan_state_path()
    selection = catalog_module.load_selection(state_path)

    if args.prd:
        try:
            artifacts = resolve_existing_prd_artifacts(args.prd)
            repo_root = git_repo_root(artifacts.prd_path.parent)
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))

        print()
        print(f"Target checkout: {repo_root}")
        print(f"Current branch: {current_branch(repo_root) or 'unknown'}")
        print(f"PRD: {artifacts.prd_path}")
        print(f"Issue index: {artifacts.issues_index}")
        print_prd_status(artifacts)
        return run_handoff(
            bundle.root,
            repo_root,
            artifacts,
            selection,
            state_path,
            codex=args.codex,
        )

    repo_root = choose_target_repo(args.repo)
    if not args.goal:
        startup_result = choose_startup_artifacts(
            repo_root,
            bundle_root=bundle.root,
            selection=selection,
            state_path=state_path,
            codex=args.codex,
        )
        if startup_result.exit_requested:
            return 0
        if startup_result.artifacts is not None:
            resumed_artifacts = startup_result.artifacts
            print()
            print(f"Target checkout: {repo_root}")
            print(f"Current branch: {current_branch(repo_root) or 'unknown'}")
            print(f"PRD: {resumed_artifacts.prd_path}")
            print(f"Issue index: {resumed_artifacts.issues_index}")
            print_prd_status(resumed_artifacts)
            return run_handoff(
                bundle.root,
                repo_root,
                resumed_artifacts,
                selection,
                state_path,
                codex=args.codex,
            )
    repo_root = apply_branch_strategy(repo_root)

    goal = args.goal.strip() if args.goal else ""
    collect_initial_message = not goal
    started_at = time.time()
    # Snapshot pre-existing PRD/issue pairs before the chat begins. `git worktree
    # add` (branch strategy 3) materializes old pairs with fresh checkout mtimes,
    # so the live probe must ignore anything in this snapshot unless its files are
    # modified past their snapshotted mtime.
    baseline = snapshot_artifacts(repo_root)

    wiki_index = bundle.root / DEFAULT_SELF_IMPROVEMENT_WIKI_PATH / "index.md"
    component_catalog = build_portable_component_catalog(bundle.root)
    model_catalog_adapter = CodexModelCatalogAdapter(
        args.codex,
        cwd=repo_root,
    )
    workflow_snapshot = preflight_analysis_workflow(
        bundle_root=bundle.root,
        state_path=state_path,
        selection=selection,
        component_catalog=component_catalog,
        model_catalog_loader=model_catalog_adapter.discover,
    )
    if workflow_snapshot is None:
        print("Planning aborted before Analysis execution.")
        return 0
    planning_step = planning_workflow_step(workflow_snapshot, component_catalog)
    initial_prompt = build_planning_prompt(
        repo_root=repo_root,
        bundle_root=bundle.root,
        goal=goal,
        skill_paths=[
            bundle.root / path for path in planning_step.capability_profile.skills
        ],
        agent_paths=[
            bundle.root / path
            for path in planning_step.capability_profile.agent_references
        ],
        step_guidance=(
            planning_step.guidance.text if planning_step.guidance is not None else None
        ),
        wiki_index=wiki_index,
    )
    planning_settings = planning_step.codex_settings
    assert planning_settings is not None

    config = ChatConfig(
        codex=args.codex,
        repo_root=repo_root,
        bundle_root=bundle.root,
        sandbox=args.sandbox,
        approval_policy=args.approval_policy,
        codex_settings=planning_settings,
        execution_budget=planning_step.execution_budget,
        workflow_progress=statusui.project_workflow_progress(
            workflow_snapshot,
            component_catalog,
            (
                StepRuntimeState(
                    step_instance_id=planning_step.instance_id,
                    issue_id=None,
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id=None,
            activity="Planning the PRD and issue pack.",
        ),
    )
    callbacks = ChatCallbacks(
        probe_artifacts=lambda: _first_or_none(find_new_artifacts(repo_root, started_at, baseline)),
        manual_artifacts=lambda: _manual_artifacts(),
        open_options=lambda: run_options_menu(
            bundle.root,
            selection,
            state_path,
            current_workflow=workflow_snapshot,
            component_catalog=component_catalog,
            model_catalog_loader=model_catalog_adapter.discover,
        ),
        status_summary=lambda: _status_summary(repo_root, selection),
        resume_artifacts=lambda: choose_resume_artifacts(repo_root).artifacts,
    )

    artifacts = run_planning_chat(
        config=config,
        initial_prompt=initial_prompt,
        callbacks=callbacks,
        collect_initial_message=collect_initial_message,
    )
    if artifacts is None:
        print("Planning aborted.")
        return 0

    if isinstance(artifacts, list):
        artifacts = _choose_artifacts(artifacts)

    print()
    print(f"PRD: {artifacts.prd_path}")
    print(f"Issue index: {artifacts.issues_index}")
    return run_handoff(
        bundle.root,
        repo_root,
        artifacts,
        selection,
        state_path,
        codex=args.codex,
        workflow_snapshot=workflow_snapshot,
    )


def preflight_analysis_workflow(
    *,
    bundle_root: Path,
    state_path: Path,
    selection: "catalog_module.Selection",
    component_catalog: PortableStepComponentCatalog,
    model_catalog_loader: Callable[[], CodexModelCatalog],
) -> WorkflowDefinition | None:
    """Return the exact workflow authorized for Analysis after interactive repair."""
    while True:
        try:
            workflow = WorkflowDefaultStore(state_path, component_catalog).load()
            planning_step = planning_workflow_step(workflow, component_catalog)
            live_model_catalog = model_catalog_loader()
            preflight_codex_execution_settings(
                workflow,
                component_catalog,
                live_model_catalog,
            )
            if planning_step.codex_settings is None:
                raise ValueError(
                    f"Planning step {planning_step.display_name!r} has no Codex "
                    "Execution Settings."
                )
            return workflow
        except (CatalogDiscoveryError, KeyError, ValueError) as error:
            safe_error = sanitize_terminal_text(error, preserve_newlines=False)
            print(
                "Codex Execution Settings preflight failed before Analysis: "
                f"{safe_error}"
            )
            print(
                "Recovery: /options opens the Workflow Editor; retry-catalog "
                "retries live discovery; /quit stops planning."
            )
        action = read_prompt(
            "Preflight action [/options/retry-catalog/quit]: "
        ).strip().casefold()
        if action == "/options":
            run_options_menu(
                bundle_root,
                selection,
                state_path,
                component_catalog=component_catalog,
                model_catalog_loader=model_catalog_loader,
            )
        elif action in {"retry-catalog", "/retry-catalog"}:
            continue
        elif action in {"quit", "/quit"}:
            return None
        else:
            print("Choose /options, retry-catalog, or /quit.")


def _first_or_none(candidates: list[PlanningArtifacts]) -> "PlanningArtifacts | list[PlanningArtifacts] | None":
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return candidates


def read_prompt(prompt: str) -> str:
    global _PROMPT_EDITOR
    if _PROMPT_EDITOR is None:
        _PROMPT_EDITOR = LineEditor(on_paste_image=lambda: None, fallback_hint=None)
    return _PROMPT_EDITOR.read_line(prompt)


def _choose_artifacts(candidates: list[PlanningArtifacts]) -> PlanningArtifacts:
    print()
    print("Detected multiple PRD / issue-pack pairs:")
    for index, candidate in enumerate(candidates, start=1):
        print(f"  {index}. {candidate.prd_path.name} -> {candidate.issues_index}")
    choice = ask_choice(
        "Select artifact pair",
        {str(i) for i in range(1, len(candidates) + 1)},
        default="1",
    )
    return candidates[int(choice) - 1]


def _manual_artifacts() -> PlanningArtifacts:
    print()
    print("Enter the artifact paths manually.")
    prd_path = ask_existing_file("PRD path")
    issues_index = ask_existing_file("Issue README path")
    return PlanningArtifacts(prd_path=prd_path, issues_index=issues_index)


def choose_startup_artifacts(
    repo_root: Path,
    *,
    bundle_root: Path,
    selection: "catalog_module.Selection",
    state_path: Path,
    codex: str = "codex",
) -> StartupMenuResult:
    component_catalog = build_portable_component_catalog(bundle_root)
    model_catalog_adapter = CodexModelCatalogAdapter(codex, cwd=repo_root)
    while True:
        candidates = find_resume_candidates(repo_root)
        width, height = terminal_dimensions()
        menu_choices = (
            ("1", "Start a new change"),
            ("2", f"Resume an unfinished PRD ({len(candidates)} found)"),
            ("3", "Workflow options"),
        )
        choice = choose_menu_option(
            (*menu_choices, ("q", "Exit")),
            default_key="1",
            cancel_key="q",
            render=lambda selected: render_app_screen(
                render_choice_menu(
                    path=render_context_path("Startup"),
                    section_title="What would you like to do?",
                    choices=menu_choices,
                    footer=(("q", "Exit"),),
                    selected_key=selected,
                    action_bar=STARTUP_ACTION_BAR,
                    width=width,
                    height=height,
                )
            ),
            fallback=lambda: ask_choice(
                "Select",
                {"1", "2", "3", "q"},
                default="1",
            ),
        )
        if choice == "q":
            return StartupMenuResult(exit_requested=True)
        if choice == "1":
            return StartupMenuResult()
        if choice == "3":
            run_options_menu(
                bundle_root,
                selection,
                state_path,
                component_catalog=component_catalog,
                model_catalog_loader=model_catalog_adapter.discover,
            )
            continue
        resume_result = choose_resume_artifacts(
            repo_root,
            candidates,
            allow_exit=True,
        )
        if resume_result.exit_requested:
            return StartupMenuResult(exit_requested=True)
        if resume_result.artifacts is not None:
            return StartupMenuResult(artifacts=resume_result.artifacts)


def choose_resume_artifacts(
    repo_root: Path,
    candidates: list[ResumeCandidate] | None = None,
    *,
    allow_exit: bool = False,
) -> ResumeMenuResult:
    available = find_resume_candidates(repo_root) if candidates is None else candidates
    if not available:
        width, height = terminal_dimensions()
        footer = (("q", "Exit"),) if allow_exit else ()
        render_app_screen(
            render_choice_menu(
                path=render_context_path("Startup", "Resume"),
                section_title="No unfinished PRD issue packs were found in this project.",
                choices=(),
                footer=footer,
                action_bar=RESUME_ACTION_BAR,
                width=width,
                height=height,
            )
        )
        prompt = (
            "Press Enter to return, or q to exit: "
            if allow_exit
            else "Press Enter to return to the main menu: "
        )
        while True:
            raw = read_prompt(prompt).strip().casefold()
            if raw in {"", "b"}:
                return ResumeMenuResult()
            if allow_exit and raw == "q":
                return ResumeMenuResult(exit_requested=True)
            if allow_exit:
                print("Expected Enter, b, or q.", file=sys.stderr)
            else:
                print("Press Enter to return to the main menu.", file=sys.stderr)

    width, height = terminal_dimensions()
    choices = tuple(
        (str(index), format_resume_candidate(candidate))
        for index, candidate in enumerate(available, start=1)
    )
    footer: list[tuple[str, str]] = [("b", "Back")]
    if allow_exit:
        footer.append(("q", "Exit"))
    menu_options = (*choices, *footer)
    allowed_choices = {str(index) for index in range(1, len(available) + 1)} | {"b"}
    if allow_exit:
        allowed_choices.add("q")
    choice = choose_menu_option(
        menu_options,
        default_key="1",
        cancel_key="b",
        render=lambda selected: render_app_screen(
            render_choice_menu(
                path=render_context_path("Startup", "Resume"),
                section_title="Unfinished PRDs",
                choices=choices,
                footer=tuple(footer),
                selected_key=selected,
                action_bar=RESUME_ACTION_BAR,
                width=width,
                height=height,
            )
        ),
        fallback=lambda: ask_choice(
            "Select PRD to resume",
            allowed_choices,
            default="1",
        ),
    )
    if choice == "b":
        return ResumeMenuResult()
    if choice == "q":
        return ResumeMenuResult(exit_requested=True)
    return ResumeMenuResult(artifacts=available[int(choice) - 1].artifacts)


def format_resume_candidate(candidate: ResumeCandidate) -> str:
    slug = artifact_slug(candidate.artifacts)
    progress = (
        f"{candidate.completed_issues}/{candidate.total_issues} completed · "
        f"{candidate.pending_issues} remaining"
    )
    active = "not started"
    if candidate.active_issue is not None:
        active = f"issue {candidate.active_issue} · {candidate.active_status or 'unfinished'}"
    updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(candidate.updated_at))
    return f"{slug} · {progress} · {active} · updated {updated}"


def _status_summary(repo_root: Path, selection: "catalog_module.Selection") -> str:
    lines = [
        f"Repository: {repo_root}",
        f"Planning skills: {', '.join(selection.planning_skills)}",
    ]
    if selection.has_role_overrides():
        lines.append("Role overrides: customized via /options")
    else:
        lines.append("Role agents/skills: embedded defaults")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devloop-plan",
        description=(
            "Interactively plan a change with Codex, publish a PRD folder, "
            "then optionally start the Dev Loop implementation runner."
        ),
    )
    parser.add_argument("--repo", help="Target project checkout. Defaults to an interactive prompt.")
    parser.add_argument("--prd", help="Existing PRD file or PRD folder to resume directly. Skips planning and starts from the development prompts.")
    parser.add_argument("--goal", help="Initial feature or fix description. If omitted, Dev Loop asks before Codex starts.")
    parser.add_argument("--codex", default="codex", help="Codex executable path or command name. Default: codex.")
    parser.add_argument(
        "--native-editor",
        action="store_true",
        help="Use terminal-native line input instead of Dev Loop raw key handling. Use /paste for screenshots.",
    )
    parser.add_argument("--sandbox", default="workspace-write", help="Codex sandbox mode. Default: workspace-write.")
    parser.add_argument(
        "--approval-policy",
        default="never",
        choices=["never", "on-request", "untrusted", "on-failure"],
        help="Codex approval policy for planning turns. Default: never.",
    )
    return parser


def choose_target_repo(repo_arg: str | None) -> Path:
    if repo_arg is not None and not repo_arg.strip():
        repo_arg = None
    default = load_last_target_repo()
    while True:
        raw = repo_arg
        if raw is None:
            if default is None:
                raw = ask_required("Target project root")
            else:
                raw = read_prompt(f"Target project root [{default}]: ").strip()
        if raw:
            candidate = Path(raw).expanduser().resolve()
        elif default is not None:
            candidate = default
        else:
            repo_arg = None
            continue
        created = ensure_target_directory(candidate)
        if not candidate.is_dir():
            repo_arg = None
            continue

        try:
            repo_root = git_repo_root(candidate)
        except RuntimeError as exc:
            if created:
                print("The target project must be a Git checkout before Dev Loop can continue.", file=sys.stderr)
                if ask_yes_no("Initialize a Git repository in the new folder?", default=True):
                    run_git(["init"], cwd=candidate)
                    repo_root = git_repo_root(candidate)
                    save_last_target_repo(repo_root)
                    return repo_root
            print(str(exc), file=sys.stderr)
            repo_arg = None
            continue

        if repo_root != candidate:
            print(f"Using Git repo root: {repo_root}")
        save_last_target_repo(repo_root)
        return repo_root


def ensure_target_directory(path: Path) -> bool:
    if path.is_dir():
        return False

    if path.exists():
        print(f"Path exists but is not a directory: {path}", file=sys.stderr)
        return False

    print(f"Directory not found: {path}", file=sys.stderr)
    if not ask_yes_no("Create this project folder?", default=False):
        return False

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Could not create project folder {path}: {exc}", file=sys.stderr)
        return False

    print(f"Created project folder: {path}")
    return True


def load_last_target_repo() -> Path | None:
    state = load_plan_state()
    if state.get(TARGET_REPO_CONFIRMED_KEY) is not True:
        return None

    raw = state.get(TARGET_REPO_STATE_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None

    candidate = Path(raw).expanduser()
    if not candidate.is_dir():
        return None
    return candidate.resolve()


def save_last_target_repo(repo_root: Path) -> None:
    save_plan_state_value(TARGET_REPO_STATE_KEY, str(repo_root), "target project default")
    save_plan_state_value(TARGET_REPO_CONFIRMED_KEY, True, "target project confirmation")


def load_last_worktree_parent() -> Path | None:
    raw = load_plan_state().get(LAST_WORKTREE_PARENT_STATE_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser().resolve()


def save_last_worktree_parent(parent: Path) -> None:
    save_plan_state_value(LAST_WORKTREE_PARENT_STATE_KEY, str(parent.resolve()), "worktree parent default")


def load_plan_state() -> dict[str, object]:
    try:
        data = json.loads(plan_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_plan_state_value(key: str, value: object, description: str) -> None:
    state_path = plan_state_path()
    data = load_plan_state()
    data[key] = value
    try:
        atomic_write_planner_configuration(state_path, data)
    except OSError as exc:
        print(f"Could not save {description}: {exc}", file=sys.stderr)


@dataclass
class HandoffParams:
    start_issue: str | None
    run_all: bool
    use_worktree: bool
    worktree_path: Path
    branch_name: str


def choose_workflow_selection(menu: SelectionMenu) -> str:
    width, height = terminal_dimensions()
    choices = tuple(
        option for option in menu.options if option[0] != menu.cancel_key
    )
    footer = tuple(
        option for option in menu.options if option[0] == menu.cancel_key
    )
    return choose_menu_option(
        menu.options,
        default_key=menu.default_key,
        cancel_key=menu.cancel_key,
        render=lambda selected: render_app_screen(
            render_choice_menu(
                path=render_context_path("Workflow Editor", menu.title),
                section_title="Choose an option",
                description=menu.description,
                choices=choices,
                footer=footer,
                selected_key=selected,
                action_bar=(
                    ("Up/Down", "Choose"),
                    ("Enter", "Select"),
                    ("Esc", "Back"),
                ),
                width=width,
                height=height,
            )
        ),
        fallback=lambda: ask_choice(
            menu.title,
            {key for key, _label in menu.options},
            default=menu.default_key,
        ),
    )


def read_workflow_value(prompt: str) -> str:
    if not sys.stdout.isatty():
        return read_prompt(prompt)
    width, height = terminal_dimensions()
    label = prompt.strip() or "Enter a value"
    render_app_screen(
        render_screen_frame(
            path=render_context_path("Workflow Editor", "Input"),
            body=(label,),
            action_bar=(("Enter", "Confirm"),),
            width=width,
            height=height,
        )
    )
    return read_prompt("> ")


def run_options_menu(
    bundle_root: Path,
    selection: "catalog_module.Selection",
    state_path: Path,
    *,
    current_workflow: WorkflowDefinition | None = None,
    component_catalog: PortableStepComponentCatalog | None = None,
    model_catalog_loader: Callable[[], CodexModelCatalog] | None = None,
) -> None:
    draft_selection = catalog_module.Selection.from_dict(selection.to_dict())
    installed_components = component_catalog or build_portable_component_catalog(
        bundle_root
    )
    width, height = terminal_dimensions()
    result = run_workflow_editor(
        state_path,
        read_line=read_workflow_value,
        read_command=lambda prompt: read_workflow_command(
            prompt,
            fallback=read_prompt,
            actions=WORKFLOW_ACTIONS,
        ),
        write=print,
        terminal_width=width,
        terminal_height=height,
        current_workflow=current_workflow,
        catalog=installed_components,
        open_capabilities=lambda draft, step_id: run_capability_options_menu(
            bundle_root,
            draft_selection,
            draft,
            step_id,
            installed_components,
        ),
        configuration_updates=lambda: {"selection": draft_selection.to_dict()},
        model_catalog_loader=model_catalog_loader,
        select_option=choose_workflow_selection,
    )
    if result is EditorResult.APPLIED:
        selection.planning_skills = list(draft_selection.planning_skills)
        selection.role_skills = {
            role: list(paths) for role, paths in draft_selection.role_skills.items()
        }
        selection.role_agents = {
            role: list(paths) for role, paths in draft_selection.role_agents.items()
        }


def terminal_width() -> int:
    return max(40, shutil.get_terminal_size(fallback=(100, 24)).columns)


def load_current_run_workflow(
    issues_index: Path,
    *,
    component_catalog: PortableStepComponentCatalog | None = None,
) -> WorkflowDefinition | None:
    state_writer = LoopStateWriter(issues_index)
    if (
        "resolved_workflow" not in state_writer.state
        and "resolved_workflow_hash" not in state_writer.state
    ):
        return None
    return state_writer.resolved_workflow(
        component_catalog or default_portable_component_catalog()
    )


def load_handoff_current_workflow(
    repo_root: Path,
    artifacts: PlanningArtifacts,
    params: HandoffParams,
    *,
    component_catalog: PortableStepComponentCatalog | None = None,
) -> WorkflowDefinition | None:
    issues_index = artifacts.issues_index
    if params.use_worktree and params.worktree_path.exists():
        existing_worktree = resolve_existing_worktree(
            repo_root,
            params.worktree_path,
            params.branch_name,
        )
        if existing_worktree is not None:
            try:
                relative_index = issues_index.resolve().relative_to(
                    repo_root.resolve()
                )
            except ValueError:
                pass
            else:
                issues_index = existing_worktree / relative_index
    return load_current_run_workflow(
        issues_index,
        component_catalog=component_catalog,
    )


def run_capability_options_menu(
    bundle_root: Path,
    selection: "catalog_module.Selection",
    draft: WorkflowDraft,
    step_id: StepInstanceId,
    component_catalog: PortableStepComponentCatalog,
) -> None:
    found = catalog_module.discover(bundle_root)
    while True:
        step = draft.workflow.step(step_id)
        width, height = terminal_dimensions()
        menu_choices = (
            ("1", "Search and toggle capabilities for this step"),
            ("2", "Reset this step to component defaults"),
            ("3", "Add skill or agent from GitHub"),
            ("4", "Back to Workflow Editor"),
        )
        choice = choose_menu_option(
            menu_choices,
            default_key="4",
            cancel_key="4",
            render=lambda selected: render_app_screen(
                render_choice_menu(
                    path=render_context_path(
                        "Workflow Editor",
                        "Capabilities",
                        step.display_name,
                    ),
                    section_title="Capability options",
                    choices=menu_choices,
                    selected_key=selected,
                    action_bar=CAPABILITY_ACTION_BAR,
                    width=width,
                    height=height,
                )
            ),
            fallback=lambda: ask_choice(
                "Select",
                {"1", "2", "3", "4"},
                default="4",
            ),
        )
        if choice == "4":
            return
        if choice == "1":
            edit_step_capabilities(
                bundle_root,
                found,
                draft,
                step_id,
                component_catalog,
            )
        elif choice == "2":
            draft.reset_capabilities(step_id)
            print("Step capability defaults restored in the workflow draft.")
        elif choice == "3":
            url = ask_required("GitHub URL (optionally #subpath)")
            result = install_from_github(
                url,
                bundle_root,
                confirm=lambda message: ask_yes_no(
                    f"{message}\nProceed?",
                    default=False,
                ),
            )
            print(result.message)
            found = catalog_module.discover(bundle_root)


def edit_step_capabilities(
    bundle_root: Path,
    found: "catalog_module.Catalog",
    draft: WorkflowDraft,
    step_id: StepInstanceId,
    component_catalog: PortableStepComponentCatalog,
) -> None:
    query = read_prompt("Search skills and agent references []: ").strip().casefold()
    entries = [
        entry
        for entry in (*found.skills, *found.agents)
        if not query
        or query in entry.name.casefold()
        or query in str(entry.path).casefold()
    ]
    step = draft.workflow.step(step_id)
    component = component_catalog.resolve(step.component_id)
    references = [
        _catalog_capability_reference(bundle_root, entry)
        for entry in entries
    ]
    width, height = terminal_dimensions()
    body = [
        "Enter a capability number to toggle, or cancel.",
        "",
    ]
    entry_lines: list[str] = []
    for index, (entry, reference) in enumerate(zip(entries, references), start=1):
        reason = component.required_capability_reason(reference)
        if reason is not None:
            marker = "required, locked"
            explanation = f" — {reason}"
        elif step.capability_profile.contains(reference):
            marker = "enabled"
            explanation = ""
        else:
            marker = "disabled"
            explanation = ""
        entry_lines.append(
            f"  {index}. [{marker}] {entry.kind}: {entry.name}{explanation}"
        )
    body.extend(entry_lines)
    render_app_screen(
        render_screen_frame(
            path=render_context_path("Workflow Editor", "Capabilities", step.display_name),
            body=body,
            command_groups=CAPABILITY_TOGGLE_COMMAND_GROUPS,
            width=width,
            height=height,
        )
    )
    raw = read_prompt("Capability number to toggle (or cancel): ").strip()
    if raw.casefold() == "cancel":
        return
    if not raw.isdecimal() or not 1 <= int(raw) <= len(references):
        print("Choose a listed capability number, or cancel.")
        return
    try:
        draft.toggle_capability(step_id, references[int(raw) - 1])
    except ValueError as error:
        print(f"Cannot toggle capability: {error}")


def _catalog_capability_reference(
    bundle_root: Path,
    entry: "catalog_module.CatalogEntry",
) -> CapabilityReference:
    relative_path = entry.path.resolve().relative_to(bundle_root.resolve()).as_posix()
    kind = (
        CapabilityKind.SKILL
        if entry.kind == "skill"
        else CapabilityKind.AGENT_REFERENCE
    )
    return CapabilityReference(kind, relative_path)


def edit_planning_skills(found: "catalog_module.Catalog", selection: "catalog_module.Selection") -> None:
    print()
    print("Available skills (Enter keeps the current selection):")
    for index, entry in enumerate(found.skills, start=1):
        marker = "*" if entry.name in selection.planning_skills else " "
        print(f"  [{marker}] {index}. {entry.name}")
    raw = read_prompt("Comma-separated numbers for planning skills []: ").strip()
    if not raw:
        return
    chosen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(found.skills):
            chosen.append(found.skills[int(part) - 1].name)
    if chosen:
        selection.planning_skills = chosen


def edit_role_defaults(found: "catalog_module.Catalog", selection: "catalog_module.Selection") -> None:
    role = ask_choice("Role to edit (coder/reviewer/qa)", {"coder", "reviewer", "qa"}, default="coder")
    print()
    print("Available skills (Enter keeps the embedded preset):")
    for index, entry in enumerate(found.skills, start=1):
        print(f"  {index}. {entry.name}")
    raw = read_prompt(f"Comma-separated skill numbers for {role} []: ").strip()
    if raw:
        paths: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(found.skills):
                paths.append(f"skills/codex/{found.skills[int(part) - 1].name}/SKILL.md")
        if paths:
            selection.role_skills[role] = paths
    print("Available agents (Enter keeps the embedded preset):")
    for index, entry in enumerate(found.agents, start=1):
        print(f"  {index}. {entry.name}")
    raw = read_prompt(f"Comma-separated agent numbers for {role} []: ").strip()
    if raw:
        agent_paths: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(found.agents):
                agent_paths.append(f"agents/codex/{found.agents[int(part) - 1].name}.md")
        if agent_paths:
            selection.role_agents[role] = agent_paths


def build_devloop_args(
    params: HandoffParams,
    artifacts: PlanningArtifacts,
    preset_path: Path | None,
    codex: str = "codex",
) -> list[str]:
    args = [
        "--prd",
        str(artifacts.prd_path),
        "--issues",
        str(artifacts.issues_index),
        "--self-improvement-wiki",
        "--codex",
        codex,
    ]
    if preset_path is not None:
        args.extend(["--preset", str(preset_path)])
    if params.start_issue:
        args.extend(["--start-issue", params.start_issue])
    if params.run_all:
        args.append("--all")
    if params.use_worktree:
        args.extend(
            [
                "--create-worktree",
                "--worktree-path",
                str(params.worktree_path),
                "--branch-name",
                params.branch_name,
            ]
        )
    else:
        args.append("--no-worktree")
    return args


def handoff_issue_summary(
    params: HandoffParams,
    artifacts: PlanningArtifacts,
) -> str:
    issues = parse_issue_index(artifacts.issues_index)
    try:
        selected = select_issues(
            issues,
            run_all=params.run_all,
            start_issue=params.start_issue,
        )
    except ValueError:
        return f"invalid start issue ({params.start_issue})"
    if not selected:
        return "0 pending"
    if params.run_all:
        suffix = f" from {params.start_issue}" if params.start_issue else ""
        return f"{len(selected)} pending{suffix}"
    return f"1 selected ({selected[0].number})"


def run_handoff(
    bundle_root: Path,
    repo_root: Path,
    artifacts: PlanningArtifacts,
    selection: "catalog_module.Selection",
    state_path: Path,
    *,
    codex: str = "codex",
    workflow_snapshot: WorkflowDefinition | None = None,
) -> int:
    component_catalog = build_portable_component_catalog(bundle_root)
    slug = artifact_slug(artifacts)
    params = HandoffParams(
        start_issue=None,
        run_all=True,
        use_worktree=True,
        worktree_path=default_worktree_path(repo_root, slug, parent=load_last_worktree_parent()),
        branch_name=sanitize_branch_name(f"devloop/{slug}"),
    )

    while True:
        print()
        print(statusui.render_banner(Stage.DEVELOPMENT))
        print(f"PRD:            {artifacts.prd_path}")
        print(f"Issue index:    {artifacts.issues_index}")
        print(f"Issues to run:  {handoff_issue_summary(params, artifacts)}")
        print(f"Worktree:       {params.worktree_path if params.use_worktree else 'disabled (work in checkout)'}")
        if params.use_worktree:
            print(f"Branch:         {params.branch_name}")
        print("Wiki:           always on (read + updated)")
        if selection.has_role_overrides():
            print("Preset:         session role overrides (via /options)")
        else:
            print("Preset:         embedded defaults")
        raw = read_prompt(
            "Press Enter to start development, /options for workflow defaults, "
            "/run-options to adjust this launch, "
            "/reset-roles to clear role overrides, /quit to stop: "
        ).strip().lower()
        if raw == "":
            break
        if raw == "/quit":
            return 0
        if raw == "/options":
            current_workflow = load_handoff_current_workflow(
                repo_root,
                artifacts,
                params,
                component_catalog=component_catalog,
            )
            if current_workflow is None:
                current_workflow = workflow_snapshot
            run_options_menu(
                bundle_root,
                selection,
                state_path,
                current_workflow=current_workflow,
                component_catalog=component_catalog,
                model_catalog_loader=CodexModelCatalogAdapter(
                    codex,
                    cwd=(
                        params.worktree_path
                        if params.use_worktree and params.worktree_path.is_dir()
                        else repo_root
                    ),
                ).discover,
            )
            continue
        if raw == "/run-options":
            adjust_handoff_params(params)
            continue
        if raw == "/reset-roles":
            selection.role_skills = {}
            selection.role_agents = {}
            catalog_module.save_selection(state_path, selection)
            print("Role overrides cleared; using embedded defaults.")
            continue
        print(
            "Unrecognized input. Press Enter, or type /options, /run-options, "
            "/reset-roles, or /quit."
        )

    preset_path = catalog_module.write_session_preset(
        bundle_root,
        selection,
        artifacts.prd_path.parent / "devloop.session.preset.json",
    )
    args = build_devloop_args(params, artifacts, preset_path, codex)

    from .cli import main as devloop_main

    print()
    print("Starting Dev Loop development.")
    return devloop_main(args, workflow_snapshot=workflow_snapshot)


def adjust_handoff_params(params: HandoffParams) -> None:
    start_issue = normalize_start_issue(read_prompt('Start issue, or "all" for every pending issue [all]: '))
    params.start_issue = start_issue
    params.run_all = start_issue is None or ask_yes_no(
        "Run all pending issues from the selected start issue?", default=True
    )
    params.use_worktree = ask_yes_no("Use a dedicated implementation worktree?", default=True)
    if params.use_worktree:
        params.worktree_path = ask_worktree_location(
            "Implementation worktree",
            default=params.worktree_path,
            remember_parent=True,
        )
        params.branch_name = ask_branch_name("Implementation branch name", default=params.branch_name)


def plan_state_path() -> Path:
    return portable_planner_configuration_path()


def apply_branch_strategy(repo_root: Path) -> Path:
    branch = current_branch(repo_root)
    print()
    print(f"Target checkout: {repo_root}")
    print(f"Current branch: {branch or 'unknown'}")
    print()
    print("Where should the planning artifacts be created?")
    print("  1. Current branch")
    print("  2. Branch in this checkout (create or reuse)")
    print("  3. New worktree")

    choice = ask_choice("Select 1, 2, or 3", {"1", "2", "3"}, default="1")
    if choice == "1":
        return repo_root

    if choice == "2":
        branch_name = ask_branch_name("Branch name")
        if branch_name == branch:
            print(f"Using existing branch: {branch_name}")
            return repo_root
        if branch_exists(repo_root, branch_name):
            print(f"Using existing branch: {branch_name}")
            run_git(["checkout", branch_name], cwd=repo_root)
            return repo_root
        run_git(["checkout", "-b", branch_name], cwd=repo_root)
        return repo_root

    worktree_path = ask_worktree_location(
        "New worktree",
        default_parent=load_last_worktree_parent(),
        remember_parent=True,
    )
    branch_name = ask_branch_name("New worktree branch name")
    return create_or_reuse_worktree(repo_root, worktree_path, branch_name)


def create_or_reuse_worktree(repo_root: Path, worktree_path: Path, branch_name: str) -> Path:
    worktree_path = worktree_path.resolve()
    try:
        existing_worktree = resolve_existing_worktree(repo_root, worktree_path, branch_name)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    if existing_worktree is not None:
        return existing_worktree.resolve()

    command = build_worktree_add_command(repo_root, worktree_path, branch_name)
    run_git(command[1:], cwd=repo_root)
    return worktree_path


def build_planning_prompt(
    *,
    repo_root: Path,
    bundle_root: Path,
    goal: str,
    skill_paths: list[Path],
    agent_paths: list[Path] | None = None,
    step_guidance: str | None = None,
    wiki_index: Path,
) -> str:
    skills_block = "\n".join(f"- {path}" for path in skill_paths)
    agents_block = "\n".join(f"- {path}" for path in (agent_paths or []))
    return f"""You are running the Dev Loop interactive planning intake for this repository.

Repository root: {repo_root}
Dev Loop bundle root: {bundle_root}

Target product: devloop-plan + devloop
- Plan and implement changes for the portable `devloop-plan.sh` / `devloop-plan.ps1`
  planning intake and the `devloop.sh` / `devloop.ps1` issue runner.
- codexcli is a separate application in this repository with a different Textual UI,
  persistence model, and execution architecture. Do not target codexcli, its RunStore,
  or its application/domain/UI modules unless the user explicitly names codexcli.
- Every generated PRD and issue must include a `Target Product` section whose
  first content line is exactly `Product: devloop-plan + devloop`, followed by
  the relevant portable-runner modules.

Use these bundled Codex skill instructions:
{skills_block}

Read these bundled Codex agent-reference instructions:
{agents_block or '- None'}

Step Guidance:
Precedence: {STEP_GUIDANCE_PRECEDENCE}

{step_guidance or 'No additional Step Guidance.'}

Read the Dev Loop self-improvement wiki index and apply relevant lessons to this planning session:
- {wiki_index}

Required workflow:
1. Inspect the existing analysis, glossary, ADRs, PRDs, and issue packs before asking a question.
2. If the existing analysis is already settled, do not repeat the interview. Move directly to $to-prd and then $to-issues.
3. Otherwise use $grill-with-docs. Interview the user until the requested change is sharp enough to build.
4. Use domain-modeling during the grill. Update glossary or ADR files only when the skill rules justify it.
5. After the user confirms the design, use $to-prd. Save the canonical PRD as {repo_root / "prd" / "<prd-name>" / "<prd-name>.md"}.
6. Then use $to-issues. Save the issue pack inside the same PRD folder at {repo_root / "prd" / "<prd-name>" / "issues" / "README.md"}.
7. Keep PRD-specific execution information inside {repo_root / "prd" / "<prd-name>"} unless a repository-wide glossary or ADR update is genuinely required.
8. The issue README must contain real Markdown links to numbered issue files.
9. Do not start implementation and do not run Dev Loop yourself from inside Codex.
10. The Dev Loop wrapper watches the repository and continues automatically once the PRD and issue README exist. Never ask the user to exit or close anything. When the artifacts are ready, report only the exact PRD path and issue README path.

Issue self-containment rules (critical):
- Each issue is later executed by a fresh Codex session with no memory of this conversation, so the full context window is preserved for development.
- Every issue file must be self-contained: state the goal, acceptance criteria, verification steps, relevant file paths, and the PRD path plus the specific PRD sections that apply.
- Never write "as discussed" or refer back to this chat.
- Keep each issue a thin vertical slice sized for one clean context window; split any issue whose required context grows too large.
- Save screenshots that matter for implementation into the PRD folder and link them by relative path from the issues that need them.

{initial_goal_block(goal)}
"""


def initial_goal_block(goal: str) -> str:
    if goal:
        return f"Initial user goal:\n{goal}"

    return (
        "No initial user goal was supplied on the command line.\n"
        "Dev Loop appends the user's typed change request before starting this Codex turn. "
        "Attached images arrive with that first message."
    )


def resolve_existing_prd_artifacts(prd_arg: str) -> PlanningArtifacts:
    path = Path(prd_arg).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"PRD path not found: {path}")

    if path.is_dir():
        prd_folder = path
        prd_path = find_prd_file_in_folder(prd_folder)
        if prd_path is None:
            raise ValueError(f"No PRD Markdown file found in: {prd_folder}")
    elif path.is_file():
        if path.name.lower() == "readme.md" and path.parent.name.lower() == "issues":
            prd_folder = path.parent.parent
            prd_path = find_prd_file_in_folder(prd_folder)
            if prd_path is None:
                raise ValueError(f"No PRD Markdown file found next to issue folder: {prd_folder}")
            return PlanningArtifacts(prd_path=prd_path.resolve(), issues_index=path.resolve())

        prd_path = path
        prd_folder = path.parent
    else:
        raise ValueError(f"PRD path is not a file or directory: {path}")

    issues_index = find_issue_index_for_prd(prd_folder, prd_path)
    if issues_index is None:
        raise ValueError(
            "Could not find issue index for PRD. Expected "
            f"{prd_folder / 'issues' / 'README.md'}"
        )

    return PlanningArtifacts(prd_path=prd_path.resolve(), issues_index=issues_index.resolve())


def find_issue_index_for_prd(prd_folder: Path, prd_path: Path) -> Path | None:
    for candidate in (prd_folder / "issues" / "README.md", prd_folder / "README.md"):
        if candidate.is_file():
            return candidate

    try:
        repo_root = git_repo_root(prd_folder)
    except RuntimeError:
        return None

    legacy_index = repo_root / "issues" / prd_path.stem / "README.md"
    if legacy_index.is_file():
        return legacy_index

    return None


def print_prd_status(artifacts: PlanningArtifacts) -> None:
    state_path = find_status_state_path(artifacts)
    print()
    if state_path is None:
        print("Status: no Dev Loop status file yet. Completed issue files will still be skipped.")
        return

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Status file could not be read: {state_path} ({exc})", file=sys.stderr)
        return

    issues = state.get("issues", {})
    if not isinstance(issues, dict):
        issues = {}

    completed = sorted(
        number
        for number, details in issues.items()
        if isinstance(details, dict)
        and parse_issue_status(details.get("status")) is IssueStatus.COMPLETED
    )
    blocked = sorted(
        number
        for number, details in issues.items()
        if isinstance(details, dict)
        and parse_issue_status(details.get("status")) is IssueStatus.BLOCKED
    )
    in_progress = sorted(
        number
        for number, details in issues.items()
        if isinstance(details, dict)
        and parse_issue_status(details.get("status")) is IssueStatus.IN_PROGRESS
    )

    print(f"Status file: {state_path}")
    print(f"Completed issues: {format_issue_list(completed)}")
    print(f"Blocked issues: {format_issue_list(blocked)}")
    print(f"In-progress issues: {format_issue_list(in_progress)}")


def find_status_state_path(artifacts: PlanningArtifacts) -> Path | None:
    candidates = [
        artifacts.prd_path.parent / "devloop.status.json",
        artifacts.issues_index.with_name(f"{artifacts.issues_index.stem}.loop.state.json"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def format_issue_list(issues: list[str]) -> str:
    return ", ".join(issues) if issues else "none"


def find_artifacts(repo_root: Path, started_at: float) -> list[PlanningArtifacts]:
    candidates = discover_artifacts(repo_root)
    recent: list[PlanningArtifacts] = []
    older: list[PlanningArtifacts] = []
    for candidate in sorted(candidates, key=artifact_mtime, reverse=True):
        newest_mtime = artifact_mtime(candidate)
        if newest_mtime >= started_at - ARTIFACT_FRESHNESS_SLACK_SECONDS:
            recent.append(candidate)
        else:
            older.append(candidate)
    return recent or older[:3]


def discover_artifacts(repo_root: Path) -> list[PlanningArtifacts]:
    prd_dir = repo_root / "prd"
    candidates: list[PlanningArtifacts] = []
    if prd_dir.is_dir():
        candidates.extend(find_prd_folder_artifacts(repo_root, prd_dir))
        candidates.extend(find_split_layout_artifacts(repo_root, prd_dir))
    candidates.extend(find_flat_issue_artifacts(repo_root))

    unique: dict[tuple[Path, Path], PlanningArtifacts] = {}
    for candidate in candidates:
        key = (candidate.prd_path.resolve(), candidate.issues_index.resolve())
        unique[key] = PlanningArtifacts(*key)
    return list(unique.values())


def find_resume_candidates(repo_root: Path) -> list[ResumeCandidate]:
    candidates: list[ResumeCandidate] = []
    for artifacts in discover_artifacts(repo_root):
        target = detect_target_product(artifacts.prd_path)
        if target in {TargetProduct.CODEXCLI, TargetProduct.INVALID}:
            continue
        issues = parse_issue_index(artifacts.issues_index)
        if not issues:
            continue
        pending = [issue for issue in issues if not issue.completed]
        if not pending:
            continue
        active_issue, active_status = resume_activity(artifacts)
        paths = [artifacts.prd_path, artifacts.issues_index]
        paths.extend(issue.path for issue in issues)
        state_path = find_status_state_path(artifacts)
        if state_path is not None:
            paths.append(state_path)
        candidates.append(
            ResumeCandidate(
                artifacts=artifacts,
                completed_issues=len(issues) - len(pending),
                pending_issues=len(pending),
                total_issues=len(issues),
                active_issue=active_issue,
                active_status=active_status,
                updated_at=max(path.stat().st_mtime for path in paths),
            )
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.updated_at,
            artifact_slug(candidate.artifacts).casefold(),
        ),
        reverse=True,
    )


def resume_activity(artifacts: PlanningArtifacts) -> tuple[str | None, str | None]:
    state_path = find_status_state_path(artifacts)
    if state_path is None:
        return None, None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    issues = state.get("issues")
    if not isinstance(issues, dict):
        return None, None
    priorities = (IssueStatus.IN_PROGRESS, IssueStatus.BLOCKED)
    for expected_status in priorities:
        for number, details in issues.items():
            if not isinstance(details, dict):
                continue
            status = str(details.get("status", ""))
            if parse_issue_status(status) is expected_status:
                return str(number), status
    return None, None


def snapshot_artifacts(repo_root: Path) -> dict[Path, float]:
    """Record PRD/issue pairs that exist before planning starts.

    Pairs in this snapshot are ignored by the live probe unless their
    files are modified after the chat begins, regardless of checkout
    mtimes (git worktree add materializes old files with fresh mtimes).
    """
    return {
        artifacts.prd_path: artifact_mtime(artifacts)
        for artifacts in find_artifacts(repo_root, 0.0)
    }


def find_new_artifacts(
    repo_root: Path,
    started_at: float,
    baseline: dict[Path, float],
) -> list[PlanningArtifacts]:
    """Return only PRD/issue pairs that genuinely appeared/changed after start.

    A pair is "new" when its newest mtime is fresh (>= started_at - slack) AND it
    is either absent from the pre-chat snapshot or has advanced past its
    snapshotted mtime. Requiring a real ``issues/`` directory keeps the probe from
    firing on the ``prd/<name>/README.md`` fallback (which the --prd/manual paths
    still accept).
    """
    fresh: list[PlanningArtifacts] = []
    for artifacts in find_artifacts(repo_root, started_at):
        if artifacts.issues_index.parent.name != "issues":
            continue
        mtime = artifact_mtime(artifacts)
        if mtime < started_at - ARTIFACT_FRESHNESS_SLACK_SECONDS:
            continue
        known = baseline.get(artifacts.prd_path)
        if known is not None and mtime <= known:
            continue
        fresh.append(artifacts)
    return fresh


def find_prd_folder_artifacts(repo_root: Path, prd_dir: Path) -> list[PlanningArtifacts]:
    artifacts: list[PlanningArtifacts] = []
    for prd_folder in sorted((path for path in prd_dir.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime, reverse=True):
        prd_path = find_prd_file_in_folder(prd_folder)
        if prd_path is None:
            continue

        issues_index = prd_folder / "issues" / "README.md"
        if not issues_index.is_file():
            issues_index = prd_folder / "README.md"
        if not issues_index.is_file():
            continue

        try:
            prd_path.resolve().relative_to(repo_root.resolve())
            issues_index.resolve().relative_to(repo_root.resolve())
        except ValueError:
            continue

        artifacts.append(
            PlanningArtifacts(
                prd_path=prd_path.resolve(),
                issues_index=issues_index.resolve(),
            )
        )
    return artifacts


def find_prd_file_in_folder(prd_folder: Path) -> Path | None:
    preferred = [
        prd_folder / f"{prd_folder.name}.md",
        prd_folder / "PRD.md",
        prd_folder / "prd.md",
    ]
    for path in preferred:
        if path.is_file():
            return path

    candidates = [
        path
        for path in prd_folder.glob("*.md")
        if path.name.lower() != "readme.md"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def find_split_layout_artifacts(repo_root: Path, prd_dir: Path) -> list[PlanningArtifacts]:
    issues_dir = repo_root / "issues"
    if not issues_dir.is_dir():
        return []

    artifacts: list[PlanningArtifacts] = []
    prds = sorted(prd_dir.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    for prd_path in prds:
        issues_index = issues_dir / prd_path.stem / "README.md"
        if issues_index.is_file():
            artifacts.append(
                PlanningArtifacts(
                    prd_path=prd_path.resolve(),
                    issues_index=issues_index.resolve(),
                )
            )
    return artifacts


def find_flat_issue_artifacts(repo_root: Path) -> list[PlanningArtifacts]:
    issues_dir = repo_root / "issues"
    if not issues_dir.is_dir():
        return []
    artifacts: list[PlanningArtifacts] = []
    for issues_index in issues_dir.glob("*-issues.md"):
        prd_name = f"{issues_index.stem.removesuffix('-issues')}.md"
        prd_path = issues_index.with_name(prd_name)
        if not prd_path.is_file():
            continue
        artifacts.append(
            PlanningArtifacts(
                prd_path=prd_path.resolve(),
                issues_index=issues_index.resolve(),
            )
        )
    return artifacts


def artifact_mtime(artifacts: PlanningArtifacts) -> float:
    return max(artifacts.prd_path.stat().st_mtime, artifacts.issues_index.stat().st_mtime)


def normalize_start_issue(raw_start_issue: str) -> str | None:
    start_issue = raw_start_issue.strip()
    if not start_issue or start_issue.lower() in {"all", "*"}:
        return None
    return start_issue


def default_worktree_path(repo_root: Path, slug: str, *, parent: Path | None = None) -> Path:
    safe_slug = slug[:60].strip("-") or "devloop-work"
    return (parent or repo_root.parent) / f"{repo_root.name}-{safe_slug}-dev"


def artifact_slug(artifacts: PlanningArtifacts) -> str:
    if (
        artifacts.issues_index.name.casefold() == "readme.md"
        and artifacts.issues_index.parent.name == "issues"
    ):
        return artifacts.issues_index.parent.parent.name
    return artifacts.prd_path.stem


def git_repo_root(path: Path) -> Path:
    result = run_text(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if result.returncode != 0:
        raise RuntimeError(f"Could not find Git repository root from {path}: {result.stderr.strip()}")
    return Path(result.stdout.strip()).resolve()


def current_branch(repo_root: Path) -> str:
    result = run_text(["git", "branch", "--show-current"], cwd=repo_root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def run_git(args: Sequence[str], *, cwd: Path) -> None:
    command = ["git", *args]
    print(format_command(command))
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def run_text(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def ask_choice(prompt: str, allowed: set[str], *, default: str) -> str:
    while True:
        raw = read_prompt(f"{prompt} [{default}]: ").strip() or default
        if raw in allowed:
            return raw
        print(f"Expected one of: {', '.join(sorted(allowed))}", file=sys.stderr)


def ask_yes_no(prompt: str, *, default: bool) -> bool:
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


def ask_branch_name(prompt: str, *, default: str | None = None) -> str:
    raw = ask_required(prompt, default=default)
    branch_name = sanitize_branch_name(raw)
    if branch_name != raw:
        print(f"Using branch name: {branch_name}")
    return branch_name


def ask_path(prompt: str, *, default: Path | None = None) -> Path:
    while True:
        value = ask_required(prompt, default=str(default) if default else None)
        return Path(value).expanduser().resolve()


def ask_worktree_location(
    prompt: str,
    *,
    default: Path | None = None,
    default_parent: Path | None = None,
    remember_parent: bool = False,
) -> Path:
    parent_default = default_parent or (default.parent if default is not None else None)
    default_name = default.name if default is not None else None
    while True:
        parent = ask_path(f"{prompt} parent path", default=parent_default)
        name = ask_required(f"{prompt} folder name", default=default_name)
        name_path = Path(name)
        if name_path.is_absolute() or len(name_path.parts) != 1:
            print("Enter only the worktree folder name, not a full path.", file=sys.stderr)
            continue
        if remember_parent:
            save_last_worktree_parent(parent)
        return (parent / name).expanduser().resolve()


def ask_existing_file(prompt: str) -> Path:
    while True:
        path = ask_path(prompt)
        if path.is_file():
            return path
        print(f"File not found: {path}", file=sys.stderr)


def format_command(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([str(part) for part in command])
    return " ".join(shlex.quote(str(part)) for part in command)


if __name__ == "__main__":
    raise SystemExit(main())

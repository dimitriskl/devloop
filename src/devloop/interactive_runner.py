from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import catalog as catalog_module
from . import statusui
from .chat_loop import ChatCallbacks, ChatConfig, run_planning_chat
from .gitrefs import sanitize_branch_name
from .github_install import install_from_github
from .issue_pack import parse_issue_index, select_issues
from .lineeditor import LineEditor
from .self_improvement_wiki import DEFAULT_SELF_IMPROVEMENT_WIKI_PATH
from .statusui import Stage
from .templates import BundleContext
from .worktree import (
    branch_exists,
    build_worktree_add_command,
    resolve_existing_worktree,
)

PLAN_STATE_FILE = "devloop-plan.json"
TARGET_REPO_STATE_KEY = "target_repo"
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
        return run_handoff(bundle.root, repo_root, artifacts, selection, state_path)

    repo_root = choose_target_repo(args.repo)
    repo_root = apply_branch_strategy(repo_root)

    goal = args.goal.strip() if args.goal else ""
    collect_initial_message = not goal
    started_at = time.time()
    # Snapshot pre-existing PRD/issue pairs before the chat begins. `git worktree
    # add` (branch strategy 3) materializes old pairs with fresh checkout mtimes,
    # so the live probe must ignore anything in this snapshot unless its files are
    # modified past their snapshotted mtime.
    baseline = snapshot_artifacts(repo_root)

    found_catalog = catalog_module.discover(bundle.root)
    skill_paths = catalog_module.planning_skill_paths(selection, found_catalog)
    wiki_index = bundle.root / DEFAULT_SELF_IMPROVEMENT_WIKI_PATH / "index.md"
    initial_prompt = build_planning_prompt(
        repo_root=repo_root,
        bundle_root=bundle.root,
        goal=goal,
        skill_paths=skill_paths,
        wiki_index=wiki_index,
    )

    config = ChatConfig(
        codex=args.codex,
        repo_root=repo_root,
        bundle_root=bundle.root,
        sandbox=args.sandbox,
        approval_policy=args.approval_policy,
    )
    callbacks = ChatCallbacks(
        probe_artifacts=lambda: _first_or_none(find_new_artifacts(repo_root, started_at, baseline)),
        manual_artifacts=lambda: _manual_artifacts(),
        open_options=lambda: run_options_menu(bundle.root, selection, state_path),
        status_summary=lambda: _status_summary(repo_root, selection),
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
    return run_handoff(bundle.root, repo_root, artifacts, selection, state_path)


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
    default = load_last_target_repo()
    while True:
        raw = repo_arg
        if raw is None:
            if default is None:
                raw = ask_required("Target project root")
            else:
                raw = read_prompt(f"Target project root [{default}]: ").strip()
        candidate = (Path(raw).expanduser() if raw else default).resolve()
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
    raw = load_plan_state().get(TARGET_REPO_STATE_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None

    candidate = Path(raw).expanduser()
    if not candidate.is_dir():
        return None
    return candidate.resolve()


def save_last_target_repo(repo_root: Path) -> None:
    save_plan_state_value(TARGET_REPO_STATE_KEY, str(repo_root), "target project default")


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
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"Could not save {description}: {exc}", file=sys.stderr)


@dataclass
class HandoffParams:
    start_issue: str | None
    run_all: bool
    use_worktree: bool
    worktree_path: Path
    branch_name: str


def run_options_menu(bundle_root: Path, selection: "catalog_module.Selection", state_path: Path) -> None:
    found = catalog_module.discover(bundle_root)
    while True:
        print()
        print("Options")
        print(f"  1. Planning skills (current: {', '.join(selection.planning_skills)})")
        print("  2. Default agents & skills per role (coder / reviewer / qa)")
        print("  3. Add skill or agent from GitHub")
        print("  4. Back")
        choice = ask_choice("Select", {"1", "2", "3", "4"}, default="4")
        if choice == "4":
            catalog_module.save_selection(state_path, selection)
            return
        if choice == "1":
            edit_planning_skills(found, selection)
        elif choice == "2":
            edit_role_defaults(found, selection)
        elif choice == "3":
            url = ask_required("GitHub URL (optionally #subpath)")
            result = install_from_github(
                url,
                bundle_root,
                confirm=lambda message: ask_yes_no(f"{message}\nProceed?", default=False),
            )
            print(result.message)
            found = catalog_module.discover(bundle_root)


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
) -> list[str]:
    args = [
        "--prd",
        str(artifacts.prd_path),
        "--issues",
        str(artifacts.issues_index),
        "--self-improvement-wiki",
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
) -> int:
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
            "Press Enter to start development, /options to adjust, "
            "/reset-roles to clear role overrides, /quit to stop: "
        ).strip().lower()
        if raw == "":
            break
        if raw == "/quit":
            return 0
        if raw == "/options":
            adjust_handoff_params(params)
            continue
        if raw == "/reset-roles":
            selection.role_skills = {}
            selection.role_agents = {}
            catalog_module.save_selection(state_path, selection)
            print("Role overrides cleared; using embedded defaults.")
            continue
        print("Unrecognized input. Press Enter, or type /options, /reset-roles, or /quit.")

    preset_path = catalog_module.write_session_preset(
        bundle_root,
        selection,
        artifacts.prd_path.parent / "devloop.session.preset.json",
    )
    args = build_devloop_args(params, artifacts, preset_path)

    from .cli import main as devloop_main

    print()
    print("Starting Dev Loop development.")
    return devloop_main(args)


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
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "DevLoop" / PLAN_STATE_FILE

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "devloop" / PLAN_STATE_FILE

    return Path.home() / ".config" / "devloop" / PLAN_STATE_FILE


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
    wiki_index: Path,
) -> str:
    skills_block = "\n".join(f"- {path}" for path in skill_paths)
    return f"""You are running the Dev Loop interactive planning intake for this repository.

Repository root: {repo_root}
Dev Loop bundle root: {bundle_root}

Use these bundled Codex skill instructions:
{skills_block}

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
        if isinstance(details, dict) and details.get("status") == "Completed"
    )
    blocked = sorted(
        number
        for number, details in issues.items()
        if isinstance(details, dict) and details.get("status") == "Blocked"
    )
    in_progress = sorted(
        number
        for number, details in issues.items()
        if isinstance(details, dict) and str(details.get("status", "")).startswith("In Progress")
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
    prd_dir = repo_root / "prd"
    if not prd_dir.is_dir():
        return []

    candidates = find_prd_folder_artifacts(repo_root, prd_dir)
    candidates.extend(find_legacy_artifacts(repo_root, prd_dir))

    recent: list[PlanningArtifacts] = []
    older: list[PlanningArtifacts] = []
    for candidate in sorted(candidates, key=artifact_mtime, reverse=True):
        newest_mtime = artifact_mtime(candidate)
        if newest_mtime >= started_at - ARTIFACT_FRESHNESS_SLACK_SECONDS:
            recent.append(candidate)
        else:
            older.append(candidate)
    return recent or older[:3]


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


def find_legacy_artifacts(repo_root: Path, prd_dir: Path) -> list[PlanningArtifacts]:
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
    if artifacts.issues_index.parent.name == "issues":
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

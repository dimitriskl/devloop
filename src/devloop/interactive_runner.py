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
from typing import Sequence

from .templates import BundleContext

PLAN_STATE_FILE = "devloop-plan.json"


@dataclass(frozen=True)
class PlanningArtifacts:
    prd_path: Path
    issues_index: Path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    bundle = BundleContext.from_file(Path(__file__).resolve())
    repo_root = choose_target_repo(args.repo)
    repo_root = apply_branch_strategy(repo_root)

    goal = args.goal.strip() if args.goal else ""
    started_at = time.time()
    codex_result = run_codex_planning_session(
        codex=args.codex,
        repo_root=repo_root,
        bundle_root=bundle.root,
        sandbox=args.sandbox,
        approval_policy=args.approval_policy,
        goal=goal,
    )
    if codex_result != 0:
        print(f"Codex planning session exited with code {codex_result}.", file=sys.stderr)
        return codex_result

    artifacts = resolve_planning_artifacts(repo_root, started_at)
    print()
    print(f"PRD: {artifacts.prd_path}")
    print(f"Issue index: {artifacts.issues_index}")

    if not ask_yes_no("Continue to development now?", default=True):
        return 0

    command = build_devloop_command(bundle.root, repo_root, artifacts)
    print()
    print("Dev Loop command:")
    print(format_command(command))

    if not ask_yes_no("Start Dev Loop development with these parameters now?", default=True):
        return 0

    print()
    print("Starting Dev Loop.")
    return subprocess.run(command, cwd=bundle.root, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devloop-plan",
        description=(
            "Interactively plan a change with Codex, publish a PRD folder, "
            "then optionally start the Dev Loop implementation runner."
        ),
    )
    parser.add_argument("--repo", help="Target project checkout. Defaults to an interactive prompt.")
    parser.add_argument("--goal", help="Initial feature or fix description. If omitted, Codex asks for it interactively.")
    parser.add_argument("--codex", default="codex", help="Codex executable path or command name. Default: codex.")
    parser.add_argument("--sandbox", default="workspace-write", help="Codex sandbox mode. Default: workspace-write.")
    parser.add_argument(
        "--approval-policy",
        default="on-request",
        choices=["never", "on-request", "untrusted", "on-failure"],
        help="Codex approval policy for the planning session. Default: on-request.",
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
                raw = input(f"Target project root [{default}]: ").strip()
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
    try:
        state_path = plan_state_path()
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    raw = data.get("target_repo")
    if not isinstance(raw, str) or not raw.strip():
        return None

    candidate = Path(raw).expanduser()
    if not candidate.is_dir():
        return None
    return candidate.resolve()


def save_last_target_repo(repo_root: Path) -> None:
    state_path = plan_state_path()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"target_repo": str(repo_root)}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Could not save target project default: {exc}", file=sys.stderr)


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
    print("  2. New branch in this checkout")
    print("  3. New worktree")

    choice = ask_choice("Select 1, 2, or 3", {"1", "2", "3"}, default="1")
    if choice == "1":
        return repo_root

    if choice == "2":
        branch_name = ask_required("New branch name")
        run_git(["checkout", "-b", branch_name], cwd=repo_root)
        return repo_root

    worktree_path = ask_path("New worktree path")
    branch_name = ask_required("New worktree branch name")
    run_git(["worktree", "add", "-b", branch_name, str(worktree_path)], cwd=repo_root)
    return worktree_path.resolve()


def run_codex_planning_session(
    *,
    codex: str,
    repo_root: Path,
    bundle_root: Path,
    sandbox: str,
    approval_policy: str,
    goal: str,
) -> int:
    prompt = build_planning_prompt(repo_root=repo_root, bundle_root=bundle_root, goal=goal)
    command = [
        *codex_command_prefix(codex),
        "-C",
        str(repo_root),
        "--add-dir",
        str(bundle_root),
        "-s",
        sandbox,
        "-a",
        approval_policy,
        prompt,
    ]

    print()
    print("Starting Codex planning session.")
    print("Type the change request inside Codex; Codex input supports arrow navigation and Alt+V image paste when your installed CLI supports it.")
    print("Exit Codex after the PRD folder and issue pack are written.")
    print(format_command([*command[:-1], "<planning prompt>"]))
    return subprocess.run(command, cwd=repo_root, check=False).returncode


def build_planning_prompt(*, repo_root: Path, bundle_root: Path, goal: str) -> str:
    return f"""You are running the Dev Loop interactive planning intake for this repository.

Repository root: {repo_root}
Dev Loop bundle root: {bundle_root}

Use these bundled Codex skill instructions:
- {bundle_root / "skills" / "codex" / "grill-with-docs" / "SKILL.md"}
- {bundle_root / "skills" / "codex" / "domain-modeling" / "SKILL.md"}
- {bundle_root / "skills" / "codex" / "to-prd" / "SKILL.md"}
- {bundle_root / "skills" / "codex" / "to-issues" / "SKILL.md"}

Required workflow:
1. Use $grill-with-docs first. Interview the user until the requested change is sharp enough to build.
2. Use domain-modeling during the grill. Update glossary or ADR files only when the skill rules justify it.
3. After the user confirms the design, use $to-prd. Save the canonical PRD as {repo_root / "prd" / "<prd-name>" / "<prd-name>.md"}.
4. Then use $to-issues. Save the issue pack inside the same PRD folder at {repo_root / "prd" / "<prd-name>" / "issues" / "README.md"}.
5. Keep PRD-specific execution information inside {repo_root / "prd" / "<prd-name>"} unless a repository-wide glossary or ADR update is genuinely required.
6. The issue README must contain real Markdown links to numbered issue files.
7. Do not start implementation and do not run Dev Loop yourself from inside Codex.
8. Do not print a Dev Loop command or a long handoff message. When the artifacts are ready, ask the user to exit this Codex planning session so the wrapper can ask development parameters.
9. Before that final question, report only the exact PRD path and issue README path.

{initial_goal_block(goal)}
"""


def initial_goal_block(goal: str) -> str:
    if goal:
        return f"Initial user goal:\n{goal}"

    return (
        "No initial user goal was supplied on the command line.\n"
        "Start by asking the user to describe the feature or fix inside Codex. "
        "They may attach screenshots or photos with Alt+V if their Codex CLI supports it."
    )


def resolve_planning_artifacts(repo_root: Path, started_at: float) -> PlanningArtifacts:
    candidates = find_artifacts(repo_root, started_at)
    if candidates:
        if len(candidates) == 1:
            return candidates[0]

        print()
        print("Detected multiple PRD / issue-pack pairs:")
        for index, candidate in enumerate(candidates, start=1):
            print(f"  {index}. {candidate.prd_path.name} -> {candidate.issues_index}")
        choice = ask_choice("Select artifact pair", {str(i) for i in range(1, len(candidates) + 1)}, default="1")
        return candidates[int(choice) - 1]

    print()
    print("Could not detect a matching PRD folder and issue README pair.")
    prd_path = ask_existing_file("PRD path")
    issues_index = ask_existing_file("Issue README path")
    return PlanningArtifacts(prd_path=prd_path, issues_index=issues_index)


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
        if newest_mtime >= started_at - 5:
            recent.append(candidate)
        else:
            older.append(candidate)
    return recent or older[:3]


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


def build_devloop_command(bundle_root: Path, repo_root: Path, artifacts: PlanningArtifacts) -> list[str]:
    command = devloop_command_prefix(bundle_root)
    command.extend(["--prd", str(artifacts.prd_path), "--issues", str(artifacts.issues_index)])

    print()
    print("Development parameters")

    start_issue = input("Start issue [0001]: ").strip() or "0001"
    if start_issue:
        command.extend(["--start-issue", start_issue])

    if ask_yes_no("Run all pending issues?", default=True):
        command.append("--all")

    if ask_yes_no("Use a dedicated implementation worktree?", default=True):
        slug = artifact_slug(artifacts)
        worktree_path = ask_path(
            "Implementation worktree path",
            default=default_worktree_path(repo_root, slug),
        )
        branch_name = ask_required(
            "Implementation branch name",
            default=f"devloop/{slug}",
        )
        command.extend(["--create-worktree", "--worktree-path", str(worktree_path), "--branch-name", branch_name])
    else:
        command.append("--no-worktree")

    if not ask_yes_no("Use the Dev Loop self-improvement wiki during and after development?", default=True):
        command.append("--no-self-improvement-wiki")

    return command


def default_worktree_path(repo_root: Path, slug: str) -> Path:
    safe_slug = slug[:60].strip("-") or "devloop-work"
    return repo_root.parent / f"{repo_root.name}-{safe_slug}-dev"


def artifact_slug(artifacts: PlanningArtifacts) -> str:
    if artifacts.issues_index.parent.name == "issues":
        return artifacts.issues_index.parent.parent.name
    return artifacts.prd_path.stem


def devloop_command_prefix(bundle_root: Path) -> list[str]:
    if os.name == "nt":
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(bundle_root / "bin" / "devloop.ps1"),
        ]
    return [str(bundle_root / "bin" / "devloop.sh")]


def codex_command_prefix(codex: str) -> list[str]:
    resolved = shutil.which(codex) or codex
    if os.name != "nt":
        return [resolved]

    suffix = Path(resolved).suffix.lower()
    if suffix == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved]
    if suffix in {".cmd", ".bat"}:
        powershell_shim = Path(resolved).with_suffix(".ps1")
        if powershell_shim.is_file():
            return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(powershell_shim)]
        return [os.environ.get("ComSpec", "cmd.exe"), "/c", resolved]
    return [resolved]


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
        raw = input(f"{prompt} [{default}]: ").strip() or default
        if raw in allowed:
            return raw
        print(f"Expected one of: {', '.join(sorted(allowed))}", file=sys.stderr)


def ask_yes_no(prompt: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{suffix}]: ").strip().lower()
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
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        print("Value is required.", file=sys.stderr)


def ask_path(prompt: str, *, default: Path | None = None) -> Path:
    while True:
        value = ask_required(prompt, default=str(default) if default else None)
        return Path(value).expanduser().resolve()


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

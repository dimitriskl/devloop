from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .codex_runner import CodexRunner, RoleResult
from .issue_pack import Issue, find_repo_root, parse_issue_index, select_issues
from .self_improvement_wiki import (
    DEFAULT_SELF_IMPROVEMENT_WIKI_PATH,
    ensure_self_improvement_wiki,
    resolve_self_improvement_wiki_path,
    write_self_improvement_context,
)
from .state import LoopStateWriter, mark_issue_completed
from .subprocess_utils import run_captured_text
from .templates import BundleContext, load_preset
from .worktree import resolve_worktree


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

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

    if not issues_index.is_file():
        parser.error(f"Issue README/index file not found: {issues_index}")

    source_repo = find_repo_root(issues_index.parent)
    source_branch = git_current_branch(source_repo)
    source_issues = parse_issue_index(issues_index)

    if not source_issues:
        parser.error(f"No local issue links were found in {issues_index}")

    selected_source_issues = select_issues(
        source_issues,
        run_all=args.all,
        start_issue=args.start_issue,
    )

    if not selected_source_issues:
        print("No pending issues selected.")
        return 0

    pending_numbers = ", ".join(issue.number for issue in selected_source_issues)
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
    mapped_source_issues = [map_issue_to_worktree(issue, source_repo, repo_root) for issue in source_issues]
    issues = select_issues(
        mapped_source_issues,
        run_all=args.all,
        start_issue=args.start_issue,
    )

    if not issues:
        print("No pending issues selected in implementation worktree.")
        return 0

    report_mapped_selection(selected_source_issues, issues)

    bundle = BundleContext.from_file(Path(__file__).resolve())
    preset = load_preset(resolve_bundle_path(bundle.root, args.preset))
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

    state_writer.record_run_start(
        repo_root=repo_root,
        prd_path=prd_in_repo,
        issues=[issue.number for issue in issues],
        dry_run=args.dry_run,
    )
    print(f"Loop state: {state_writer.board_path}")

    overall_status = 0
    blocked_issues: dict[str, Issue] = {}
    for issue in issues:
        issue_result = run_issue(
            issue=issue,
            runner=runner,
            state_writer=state_writer,
            max_passes=args.max_passes,
        )

        if issue_result.status in {"BLOCKED", "FAIL"}:
            blocked_issues[issue.number] = issue
            overall_status = 2
            if not args.all:
                break
        else:
            blocked_issues.pop(issue.number, None)

    if (
        blocked_issues
        and not args.no_blocked_retry
        and args.blocked_retry_rounds > 0
        and not args.dry_run
    ):
        remaining_blocked = retry_blocked_issues(
            blocked_issues=list(blocked_issues.values()),
            runner=runner,
            state_writer=state_writer,
            max_passes=args.blocked_retry_max_passes,
            max_rounds=args.blocked_retry_rounds,
        )
        overall_status = 2 if remaining_blocked else 0
    elif blocked_issues and args.no_blocked_retry:
        print("Blocked issue retry skipped because --no-blocked-retry was set.", file=sys.stderr)

    if args.self_improvement_wiki:
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

            if memory_result.status == "PASS":
                print(f"Dev Loop self-improvement wiki updated: {memory_result.summary}")
            else:
                print(f"Dev Loop self-improvement wiki update {memory_result.status}: {memory_result.summary}", file=sys.stderr)

    if overall_status == 0:
        print("Dev loop finished.")
        offer_merge_followup(
            source_repo=source_repo,
            implementation_repo=repo_root,
            source_branch=source_branch,
            interactive=not args.non_interactive and not args.dry_run,
        )
    else:
        print("Dev loop finished with blocked or failed issues.", file=sys.stderr)

    return overall_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devloop",
        description="Run local PRD + issue-pack tasks through Codex coder, review, and QA gates.",
    )
    parser.add_argument("--prd", required=True, help="Path to the parent PRD Markdown file.")
    parser.add_argument("--issues", required=True, help="Path to the local issue README/index Markdown file.")
    parser.add_argument("--preset", default="presets/generic-minimal.json", help="Preset JSON path. Relative paths are resolved from the bundle root.")
    parser.add_argument("--all", action="store_true", help="Run every pending issue in dependency order.")
    parser.add_argument("--start-issue", help="Issue number or filename prefix to start from.")
    parser.add_argument("--max-passes", type=int, default=3, help="Maximum coder passes per issue.")
    parser.add_argument("--blocked-retry-rounds", type=int, default=3, help="After the normal run, retry blocked issues this many clean rounds. Default: 3.")
    parser.add_argument("--blocked-retry-max-passes", type=int, default=1, help="Maximum coder passes inside each clean blocked retry. Default: 1.")
    parser.add_argument("--no-blocked-retry", action="store_true", help="Do not retry blocked issues at the end of the run.")
    parser.add_argument("--dry-run", action="store_true", help="Render prompts and state without invoking Codex or modifying issues.")
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


def run_issue(
    issue: Issue,
    runner: CodexRunner,
    state_writer: LoopStateWriter,
    max_passes: int,
    initial_fix_list: list[str] | None = None,
    attempt_label: str | None = None,
    retry_round: int | None = None,
) -> RoleResult:
    fix_list = list(initial_fix_list or [])
    last_coder: RoleResult | None = None
    last_review: RoleResult | None = None
    last_qa: RoleResult | None = None

    state_writer.record_issue_start(issue, attempt_label=attempt_label, retry_round=retry_round)
    title = f"{issue.title} ({attempt_label})" if attempt_label else issue.title
    print(f"\n[{issue.number}] {title}")

    if runner.dry_run:
        runner.render_dry_run_prompts(issue)
        state_writer.record_issue_dry_run(issue)
        print(f"[{issue.number}] Dry run prompts rendered.")
        return RoleResult(status="PASS", summary="Dry run prompts rendered.")

    for pass_number in range(1, max_passes + 1):
        print(f"[{issue.number}] Pass {pass_number}: coder")
        last_coder = runner.run_role(
            role="coder",
            issue=issue,
            pass_number=pass_number,
            fix_list=fix_list,
            attempt_label=attempt_label,
        )
        state_writer.record_role_result(
            issue,
            "coder",
            pass_number,
            last_coder,
            attempt_label=attempt_label,
            retry_round=retry_round,
        )
        report_role_result(issue.number, "coder", last_coder)

        if last_coder.status != "PASS":
            state_writer.record_issue_blocked(
                issue,
                "coder",
                last_coder,
                attempt_label=attempt_label,
                retry_round=retry_round,
            )
            return last_coder

        print(f"[{issue.number}] Pass {pass_number}: reviewer")
        last_review = runner.run_role(
            role="reviewer",
            issue=issue,
            pass_number=pass_number,
            coder_result=last_coder,
            attempt_label=attempt_label,
        )
        state_writer.record_role_result(
            issue,
            "reviewer",
            pass_number,
            last_review,
            attempt_label=attempt_label,
            retry_round=retry_round,
        )
        report_role_result(issue.number, "reviewer", last_review)

        if last_review.status != "PASS":
            fix_list = last_review.fix_list or last_review.findings
            continue

        print(f"[{issue.number}] Pass {pass_number}: qa")
        last_qa = runner.run_role(
            role="qa",
            issue=issue,
            pass_number=pass_number,
            coder_result=last_coder,
            review_result=last_review,
            attempt_label=attempt_label,
        )
        state_writer.record_role_result(
            issue,
            "qa",
            pass_number,
            last_qa,
            attempt_label=attempt_label,
            retry_round=retry_round,
        )
        report_role_result(issue.number, "qa", last_qa)

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
        print(f"[{issue.number}] Completed.")
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
    report_role_result(issue.number, "max-passes", blocked)
    return blocked


def retry_blocked_issues(
    blocked_issues: list[Issue],
    runner: CodexRunner,
    state_writer: LoopStateWriter,
    max_passes: int,
    max_rounds: int,
) -> list[Issue]:
    remaining = list(blocked_issues)

    for retry_round in range(1, max_rounds + 1):
        if not remaining:
            break

        issue_numbers = ", ".join(issue.number for issue in remaining)
        print(f"\nBlocked retry round {retry_round}/{max_rounds}: {issue_numbers}")
        state_writer.record_blocked_retry_round_start(
            retry_round=retry_round,
            issues=[issue.number for issue in remaining],
        )

        next_remaining: list[Issue] = []
        for issue in remaining:
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
            )
            if issue_result.status in {"BLOCKED", "FAIL"}:
                next_remaining.append(issue)

        remaining = next_remaining

    if remaining:
        issue_numbers = ", ".join(issue.number for issue in remaining)
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


def report_role_result(issue_number: str, role: str, result: RoleResult) -> None:
    if result.status == "PASS":
        if result.summary:
            print(f"[{issue_number}] {role}: PASS - {result.summary}")
        else:
            print(f"[{issue_number}] {role}: PASS")
        return

    message = f"[{issue_number}] {role}: {result.status}"
    if result.summary:
        message = f"{message} - {result.summary}"
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
    )


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
        print(f"Selected issues in implementation worktree: {', '.join(mapped_numbers)}")


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


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))

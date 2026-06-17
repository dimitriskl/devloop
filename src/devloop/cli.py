from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .codex_runner import CodexRunner, RoleResult
from .issue_pack import Issue, find_repo_root, parse_issue_index, select_issues
from .state import LoopStateWriter, mark_issue_completed
from .templates import BundleContext, load_preset
from .worktree import resolve_worktree


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    prd_path = Path(args.prd).expanduser().resolve()
    issues_index = Path(args.issues).expanduser().resolve()

    if not prd_path.is_file():
        parser.error(f"PRD file not found: {prd_path}")

    if not issues_index.is_file():
        parser.error(f"Issue README/index file not found: {issues_index}")

    source_repo = find_repo_root(issues_index.parent)
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

    repo_root = worktree.repo_root
    prd_in_repo = map_path_to_worktree(prd_path, source_repo, repo_root)
    issues_index_in_repo = map_path_to_worktree(issues_index, source_repo, repo_root)
    issues = [map_issue_to_worktree(issue, source_repo, repo_root) for issue in selected_source_issues]

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
    )

    state_writer.record_run_start(
        repo_root=repo_root,
        prd_path=prd_in_repo,
        issues=[issue.number for issue in issues],
        dry_run=args.dry_run,
    )

    overall_status = 0
    for issue in issues:
        issue_result = run_issue(
            issue=issue,
            runner=runner,
            state_writer=state_writer,
            max_passes=args.max_passes,
        )

        if issue_result.status in {"BLOCKED", "FAIL"}:
            overall_status = 2
            if not args.all:
                break

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
    parser.add_argument("--dry-run", action="store_true", help="Render prompts and state without invoking Codex or modifying issues.")
    parser.add_argument("--codex", default="codex", help="Codex executable path or command name.")
    parser.add_argument("--sandbox", default="workspace-write", help="Codex sandbox mode. Default: workspace-write.")
    parser.add_argument("--approval-policy", default="never", choices=["never", "on-request", "untrusted", "on-failure"], help="Codex approval policy. Default: never.")
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
) -> RoleResult:
    fix_list: list[str] = []
    last_coder: RoleResult | None = None
    last_review: RoleResult | None = None
    last_qa: RoleResult | None = None

    state_writer.record_issue_start(issue)

    if runner.dry_run:
        runner.render_dry_run_prompts(issue)
        state_writer.record_issue_dry_run(issue)
        return RoleResult(status="PASS", summary="Dry run prompts rendered.")

    for pass_number in range(1, max_passes + 1):
        last_coder = runner.run_role(
            role="coder",
            issue=issue,
            pass_number=pass_number,
            fix_list=fix_list,
        )
        state_writer.record_role_result(issue, "coder", pass_number, last_coder)

        if last_coder.status != "PASS":
            state_writer.record_issue_blocked(issue, "coder", last_coder)
            return last_coder

        last_review = runner.run_role(
            role="reviewer",
            issue=issue,
            pass_number=pass_number,
            coder_result=last_coder,
        )
        state_writer.record_role_result(issue, "reviewer", pass_number, last_review)

        if last_review.status != "PASS":
            fix_list = last_review.fix_list or last_review.findings
            continue

        last_qa = runner.run_role(
            role="qa",
            issue=issue,
            pass_number=pass_number,
            coder_result=last_coder,
            review_result=last_review,
        )
        state_writer.record_role_result(issue, "qa", pass_number, last_qa)

        if last_qa.status != "PASS":
            fix_list = last_qa.fix_list or last_qa.findings
            continue

        mark_issue_completed(issue.path, last_coder, last_review, last_qa)
        state_writer.record_issue_completed(issue, last_coder, last_review, last_qa)
        return RoleResult(status="PASS", summary=f"Issue {issue.number} completed.")

    blocked = RoleResult(
        status="BLOCKED",
        summary=f"Issue {issue.number} reached max passes ({max_passes}).",
        fix_list=fix_list,
    )
    state_writer.record_issue_blocked(issue, "max-passes", blocked)
    return blocked


def resolve_bundle_path(bundle_root: Path, path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return (bundle_root / path).resolve()


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


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))



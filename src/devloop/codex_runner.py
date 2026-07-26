"""The portable role runner: prompts, durable logs, and backend dispatch.

The runner owns everything that is independent of the agent provider: prompt
construction, durable log-path resolution with confinement and compaction, and
the role result contract. It reaches a provider only through the Execution
Backend boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .issue_pack import Issue
from .portable_execution_backend import (
    ActivityCallback,
    ExecutionBackend,
    ExecutionBackendId,
    LogWriter,
    RefusalRecord,
    RunWideBlocker,
    RunWideBlockerPolicy,
    StepAttemptRequest,
    describe_refusals,
    extract_json_object,
    resolve_execution_backend,
)
from .portable_text import normalize_single_line_display_name
from .redaction import redact_persisted_evidence
from .self_improvement_wiki import DEFAULT_SELF_IMPROVEMENT_WIKI_PATH
from .statusui import Stage
from .step_configuration import STEP_GUIDANCE_PRECEDENCE, StepGuidance
from .subprocess_utils import EXECUTION_BUDGET_EXPIRY_RETURNCODE
from .templates import BundleContext, Preset, render_template

if TYPE_CHECKING:
    from .portable_workflow import ExecutionBudget, StepExecutionSettings

PORTABLE_LOG_MARKER = "portable-step"
PORTABLE_LOG_TOKEN_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
PORTABLE_STEP_INSTANCE_ID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
LOG_ATTEMPT_TOKEN_MAX_LENGTH = 24
LOG_FALLBACK_ATTEMPT_TOKEN_MAX_LENGTH = 16
LOG_FALLBACK_ROLE_TOKEN_MAX_LENGTH = 12
LOG_TOKEN_HASH_LENGTH = 8
MAX_PORTABLE_LOG_PATH_LENGTH = 259
PROMPT_LOG_SUFFIX = ".prompt.md"
STDOUT_LOG_SUFFIX = ".stdout.jsonl"
STDERR_LOG_SUFFIX = ".stderr.txt"
MESSAGE_LOG_SUFFIX = ".last-message.json"
LONGEST_ROLE_LOG_SUFFIX = MESSAGE_LOG_SUFFIX
ROLE_RESULT_SCHEMA_FILENAME = "role-result.schema.json"
SELF_IMPROVEMENT_LOG_PREFIX = "self-improvement-compiler"
EXECUTION_BUDGET_EXPIRATION_PATTERN = re.compile(
    r"Execution Budget (?:timeout|checkpoint deadline) "
    r"\([^)\r\n]+\) expired\."
)
# One sentence, reused by both failure summaries, so an operator reads the same
# promise about an unfinished attempt whichever Execution Backend ran it.
UNFINISHED_ATTEMPT_WORKSPACE_NOTE = (
    "Changes already written remain in the workspace. Rerun the unfinished "
    "issue to continue from them."
)
ROLE_SCHEMA_MISMATCH_SUMMARY = (
    "did not return valid JSON matching the role schema."
)
ROLE_STAGES = {
    "coder": Stage.DEVELOPMENT,
    "reviewer": Stage.REVIEW,
    "qa": Stage.QA,
}
DEVLOOP_RUN_GOAL = (
    "All selected issues from the issue pack must be developed, reviewed, "
    "and tested so the finished product has as few bugs and deficiencies as practical."
)


class RunWideBlockerError(RuntimeError):
    def __init__(self, blocker: RunWideBlocker) -> None:
        super().__init__(blocker.summary)
        self.blocker = blocker


def stage_for_role(role: str) -> Stage:
    try:
        return ROLE_STAGES[role]
    except KeyError as error:
        raise ValueError(f"Unsupported Dev Loop role: {role}") from error


def _role_activity_context(*, progress: str, pass_number: int) -> str:
    pass_label = f"p{pass_number}"
    return f"{progress} {pass_label}" if progress else pass_label


@dataclass
class RoleResult:
    status: str
    summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    fix_list: list[str] = field(default_factory=list)
    residual_risks: list[str] = field(default_factory=list)
    raw_message: str = ""

    @classmethod
    def from_message(
        cls,
        message: str,
        *,
        backend: ExecutionBackendId | None = None,
    ) -> "RoleResult":
        """Parse one attempt's role result, naming the backend that produced it.

        ``backend`` is what keeps a refusal summary honest in a mixed-backend
        Workflow: the operator is told which provider failed to return the role
        result, not whichever provider the runner was first written for.
        """
        data = extract_json_object(message)
        if not data:
            return cls(
                status="BLOCKED",
                summary=(
                    f"{_backend_subject(backend)} {ROLE_SCHEMA_MISMATCH_SUMMARY}"
                ),
                raw_message=message,
            )

        status = str(data.get("status", "BLOCKED")).upper()
        if status not in {"PASS", "FAIL", "BLOCKED"}:
            status = "BLOCKED"

        return cls(
            status=status,
            summary=str(data.get("summary", "")),
            changed_files=list_of_strings(data.get("changed_files")),
            verification_commands=list_of_strings(data.get("verification_commands")),
            findings=list_of_strings(data.get("findings")),
            fix_list=list_of_strings(data.get("fix_list")),
            residual_risks=list_of_strings(data.get("residual_risks")),
            raw_message=message,
        )


class CodexRunner:
    def __init__(
        self,
        bundle: BundleContext,
        repo_root: Path,
        prd_path: Path,
        issues_index: Path,
        preset: Preset,
        execution_backend: ExecutionBackend,
        dry_run: bool,
        use_self_improvement_wiki: bool,
    ) -> None:
        self.bundle = bundle
        self.repo_root = repo_root
        self.prd_path = prd_path
        self.issues_index = issues_index
        self.preset = preset
        self.execution_backend = execution_backend
        self.dry_run = dry_run
        self.use_self_improvement_wiki = use_self_improvement_wiki
        self.log_root = issues_index.parent / ".loop.logs"
        self.ensure_log_root()

    def ensure_log_root(self) -> None:
        self.log_root.mkdir(parents=True, exist_ok=True)

    def backend_for_step(
        self,
        execution_settings: StepExecutionSettings | None,
    ) -> ExecutionBackend:
        """Resolve the Execution Backend one Workflow Step attempt runs on.

        The backend the runner was constructed with stays authoritative for its
        own backend identity, so a Workflow Step that names it keeps the exact
        command-line configuration the run was started with. A Workflow Step
        naming another Execution Backend is dispatched to that backend's
        registered implementation, which is resolved only at that point.
        """
        if (
            execution_settings is None
            or execution_settings.backend is self.execution_backend.backend_id
        ):
            return self.execution_backend
        return resolve_execution_backend(execution_settings.backend)

    def write_log_text(
        self,
        path: Path,
        text: str,
        *,
        log_root: Path | None = None,
    ) -> None:
        """Write one durable attempt log inside the configured log root.

        This is the single point at which an attempt's text becomes durable, so
        it is where the Redaction Service masks detected secrets. Every Execution
        Backend writes through here, so both providers' logs are redacted on
        identical terms rather than each backend being trusted to remember.
        """
        configured_root = log_root or self.log_root
        configured_root.mkdir(parents=True, exist_ok=True)
        resolved_path = _confined_log_path(path, configured_root)
        resolved_path.write_text(redact_persisted_evidence(text), encoding="utf-8")

    def run_role(
        self,
        role: str,
        issue: Issue,
        pass_number: int,
        fix_list: list[str] | None = None,
        coder_result: RoleResult | None = None,
        review_result: RoleResult | None = None,
        attempt_label: str | None = None,
        progress: str = "",
        activity_callback: ActivityCallback | None = None,
        step_instance_id: str | None = None,
        step_display_name: str | None = None,
        step_attempt_id: str | None = None,
        prompt_session_id: str | None = None,
        rework_attempt_record: Mapping[str, Any] | None = None,
        role_adapter: str | None = None,
        execution_settings: StepExecutionSettings | None = None,
        execution_budget: ExecutionBudget | None = None,
        skill_paths: Iterable[str] | None = None,
        agent_paths: Iterable[str] | None = None,
        step_guidance: str | None = None,
    ) -> RoleResult:
        prompt = self.build_prompt(
            role=role,
            issue=issue,
            pass_number=pass_number,
            fix_list=fix_list or [],
            coder_result=coder_result,
            review_result=review_result,
            step_instance_id=step_instance_id,
            step_display_name=step_display_name,
            step_attempt_id=step_attempt_id,
            prompt_session_id=prompt_session_id,
            rework_attempt_record=rework_attempt_record,
            role_adapter=role_adapter,
            skill_paths=skill_paths,
            agent_paths=agent_paths,
            step_guidance=step_guidance,
            execution_budget=execution_budget,
        )

        logs = _attempt_log_paths(
            self.log_root,
            _role_attempt_log_prefix(
                log_root=self.log_root,
                issue=issue,
                role=role,
                pass_number=pass_number,
                attempt_label=attempt_label,
                step_instance_id=step_instance_id,
                step_display_name=step_display_name,
                step_attempt_id=step_attempt_id,
                prompt_session_id=prompt_session_id,
            ),
        )
        self.write_log_text(logs.prompt, prompt)

        backend = self.backend_for_step(execution_settings)
        result = backend.invoke(
            StepAttemptRequest(
                prompt=prompt,
                repo_root=self.repo_root,
                schema_path=self.bundle.schemas / ROLE_RESULT_SCHEMA_FILENAME,
                message_path=logs.message,
                stdout_path=logs.stdout,
                stderr_path=logs.stderr,
                write_log=self.write_log_text,
                execution_settings=execution_settings,
                execution_budget=execution_budget,
                activity_stage=stage_for_role(role_adapter or role),
                activity_context=_role_activity_context(
                    progress=progress,
                    pass_number=pass_number,
                ),
                activity_callback=activity_callback,
            )
        )
        process = result.process
        self.write_log_text(logs.stdout, process.stdout)
        self.write_log_text(logs.stderr, process.stderr)

        # Step Outcome precedence, in strict order. A Run-Wide Blocker is
        # evaluated first, ahead of every other signal including a Permission
        # Denial, because it is the only signal that says this attempt never got a
        # fair chance: the provider account or service refused the call, no Issue
        # caused it, and no other Issue could do better. Publishing any Step
        # Outcome for it would assert something about the Issue that the evidence
        # does not support, spend an Issue attempt budget on a provider that
        # cannot answer, and then spend every remaining Issue's budget the same
        # way — while pausing costs one rerun once the condition is repaired.
        #
        # A Permission Denial is evaluated immediately after, ahead of every
        # signal that could describe success, because a backend can deny the work
        # an attempt needed while still reporting no error, a success subtype, a
        # completed terminal reason and a zero exit code — and the agent may then
        # assert the denied work was done. That rule exists to stop a *success*
        # overruling a denial. A pause is not a success: it publishes no Step
        # Outcome, so denied work still cannot be reported as done, and the denial
        # stays in the Portable Activity Feed and the durable transcript for the
        # rerun that follows.
        if result.run_wide_blocker is not None:
            raise RunWideBlockerError(result.run_wide_blocker)

        if result.refusals:
            return RoleResult(
                status="BLOCKED",
                summary=role_permission_denial_summary(
                    backend=backend.backend_id,
                    refusals=result.refusals,
                    log_path=logs.stdout,
                ),
                raw_message=result.message,
            )

        # A backend that reported an error, or an ending other than completion,
        # is BLOCKED in its own words. The backend's judgement decides this
        # rather than the process exit code, because a provider was observed
        # exiting zero for a turn it did not carry out. An attempt whose
        # Execution Budget expired in the same window keeps the workspace promise
        # of the budget-expiry convention alongside those words.
        if result.failure_summary:
            return RoleResult(
                status="BLOCKED",
                summary=role_provider_failure_summary(
                    failure_summary=result.failure_summary,
                    returncode=process.returncode,
                    stderr=process.stderr,
                    stderr_path=logs.stderr,
                ),
                raw_message=result.message or process.stderr,
            )

        if process.returncode != 0:
            return RoleResult(
                status="BLOCKED",
                summary=role_execution_failure_summary(
                    backend=backend.backend_id,
                    returncode=process.returncode,
                    stderr=process.stderr,
                    stderr_path=logs.stderr,
                ),
                raw_message=process.stderr,
            )

        return RoleResult.from_message(result.message, backend=backend.backend_id)

    def render_dry_run_prompts(
        self,
        issue: Issue,
        workflow_steps: Iterable[tuple[Any, ...]] | None = None,
    ) -> None:
        steps = workflow_steps or (
            ("coder", "coder", "Development", "legacy-development"),
            ("reviewer", "reviewer", "Review", "legacy-review"),
            ("qa", "qa", "QA", "legacy-qa"),
        )
        for raw_step in steps:
            role, role_adapter, display_name, instance_id = raw_step[:4]
            skill_paths = raw_step[4] if len(raw_step) > 4 else None
            agent_paths = raw_step[5] if len(raw_step) > 5 else None
            step_guidance = raw_step[6] if len(raw_step) > 6 else None
            execution_budget = raw_step[7] if len(raw_step) > 7 else None
            prompt_session_id = f"dry-run-{instance_id}"
            prompt = self.build_prompt(
                role=role,
                issue=issue,
                pass_number=1,
                fix_list=[],
                step_instance_id=instance_id,
                step_display_name=display_name,
                step_attempt_id=f"dry-run-{instance_id}",
                prompt_session_id=prompt_session_id,
                role_adapter=role_adapter,
                skill_paths=skill_paths,
                agent_paths=agent_paths,
                step_guidance=step_guidance,
                execution_budget=execution_budget,
            )
            issue_slug = slugify_log_token(issue.number) or "issue"
            step_slug = slugify_log_token(display_name) or "step"
            instance_slug = slugify_log_token(instance_id) or "instance"
            role_slug = slugify_log_token(role) or "role"
            path = _confined_log_path(
                self.log_root
                / (
                    f"{issue_slug}-{step_slug}-{instance_slug}-{role_slug}"
                    "-dry-run.prompt.md"
                ),
                self.log_root,
            )
            self.write_log_text(path, prompt)
            print(f"[dry-run] Wrote {path}")

    def run_self_improvement_compiler(
        self,
        state_path: Path,
        board_path: Path,
        wiki_root: Path,
        max_lessons: int,
        compiler_repo_root: Path | None = None,
        run_context_path: Path | None = None,
    ) -> RoleResult:
        compiler_repo_root = compiler_repo_root or self.repo_root
        log_root = self.log_root if compiler_repo_root == self.repo_root else wiki_root.parent / ".compiler-runs"
        log_root.mkdir(parents=True, exist_ok=True)
        prompt = self.build_self_improvement_prompt(
            state_path=state_path,
            board_path=board_path,
            wiki_root=wiki_root,
            max_lessons=max_lessons,
            run_context_path=run_context_path,
            compiler_repo_root=compiler_repo_root,
        )

        logs = _attempt_log_paths(log_root, SELF_IMPROVEMENT_LOG_PREFIX)
        write_log = self._log_writer(log_root)
        write_log(logs.prompt, prompt)

        result = self.execution_backend.invoke(
            StepAttemptRequest(
                prompt=prompt,
                repo_root=compiler_repo_root,
                schema_path=self.bundle.schemas / ROLE_RESULT_SCHEMA_FILENAME,
                message_path=logs.message,
                stdout_path=logs.stdout,
                stderr_path=logs.stderr,
                write_log=write_log,
                activity_stage=Stage.QA,
                activity_context="self-improvement",
                # The compiler runs after every Issue has finished, so a
                # Run-Wide Blocker has nothing left to pause. It reports its own
                # outcome from its exit code and structured message instead.
                run_wide_blocker_policy=RunWideBlockerPolicy.IGNORE,
            )
        )
        process = result.process
        write_log(logs.stdout, process.stdout)
        write_log(logs.stderr, process.stderr)

        if process.returncode != 0:
            return RoleResult(
                status="BLOCKED",
                summary=(
                    "self-improvement compiler failed with exit code "
                    f"{process.returncode}. See {logs.stderr}."
                ),
                raw_message=process.stderr,
            )

        return RoleResult.from_message(
            result.message,
            backend=self.execution_backend.backend_id,
        )

    def _log_writer(self, log_root: Path) -> LogWriter:
        """Bind durable log writing, and its confinement, to one log root."""

        def write_log(path: Path, text: str) -> None:
            self.write_log_text(path, text, log_root=log_root)

        return write_log

    def build_prompt(
        self,
        role: str,
        issue: Issue,
        pass_number: int,
        fix_list: list[str],
        coder_result: RoleResult | None = None,
        review_result: RoleResult | None = None,
        step_instance_id: str | None = None,
        step_display_name: str | None = None,
        step_attempt_id: str | None = None,
        prompt_session_id: str | None = None,
        rework_attempt_record: Mapping[str, Any] | None = None,
        role_adapter: str | None = None,
        skill_paths: Iterable[str] | None = None,
        agent_paths: Iterable[str] | None = None,
        step_guidance: str | None = None,
        execution_budget: ExecutionBudget | None = None,
    ) -> str:
        role_config = self.preset.roles.get(role, {})
        execution_role = role_adapter or role
        normalized_step_display_name = normalize_single_line_display_name(
            step_display_name or role,
            field_name="Workflow step display name",
        )
        template_name = {
            "coder": "coder.md",
            "reviewer": "reviewer.md",
            "qa": "qa.md",
        }[execution_role]

        values = {
            "ROLE": role,
            "PASS_NUMBER": pass_number,
            "BUNDLE_ROOT": self.bundle.root,
            "REPO_ROOT": self.repo_root,
            "PRD_PATH": self.prd_path,
            "ISSUES_INDEX": self.issues_index,
            "ISSUE_NUMBER": issue.number,
            "ISSUE_TITLE": issue.title,
            "ISSUE_PATH": issue.path,
            "STEP_INSTANCE_ID": step_instance_id or "Not applicable",
            "STEP_DISPLAY_NAME": normalized_step_display_name,
            "STEP_ATTEMPT_ID": step_attempt_id or "Not applicable",
            "PROMPT_SESSION_ID": prompt_session_id or "Not applicable",
            "REWORK_ATTEMPT_RECORD": json.dumps(
                rework_attempt_record,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            "REQUIRED_DOCS": self.preset.required_docs,
            "RUN_GOAL": DEVLOOP_RUN_GOAL,
            "EXECUTION_BUDGET": execution_budget_prompt_guidance(
                execution_budget
            ),
            "BUNDLE_MEMORY_DOCS": self.bundle_memory_docs(),
            "SKILL_PATHS": (
                tuple(skill_paths)
                if skill_paths is not None
                else role_config.get("skills", [])
            ),
            "AGENT_PATHS": (
                tuple(agent_paths)
                if agent_paths is not None
                else role_config.get("agents", [])
            ),
            "STEP_GUIDANCE": (
                StepGuidance(step_guidance).text
                if step_guidance
                else "No additional Step Guidance."
            ),
            "STEP_GUIDANCE_PRECEDENCE": STEP_GUIDANCE_PRECEDENCE,
            "FIX_LIST": fix_list or ["None"],
            "CODER_RESULT": json.dumps(result_to_dict(coder_result), indent=2),
            "REVIEW_RESULT": json.dumps(result_to_dict(review_result), indent=2),
            "TIMESTAMP": datetime.now().isoformat(timespec="seconds"),
        }
        return render_template(self.bundle.prompts / template_name, values)

    def bundle_memory_docs(self) -> list[Path | str]:
        if not self.use_self_improvement_wiki:
            return ["Disabled for this run."]
        return [self.bundle.root / DEFAULT_SELF_IMPROVEMENT_WIKI_PATH / "index.md"]

    def build_self_improvement_prompt(
        self,
        state_path: Path,
        board_path: Path,
        wiki_root: Path,
        max_lessons: int,
        run_context_path: Path | None = None,
        compiler_repo_root: Path | None = None,
    ) -> str:
        compiler_repo_root = compiler_repo_root or self.repo_root
        values = {
            "BUNDLE_ROOT": self.bundle.root,
            "REPO_ROOT": self.repo_root,
            "COMPILER_REPO_ROOT": compiler_repo_root,
            "PRD_PATH": self.prd_path,
            "ISSUES_INDEX": self.issues_index,
            "LOOP_STATE_PATH": state_path,
            "LOOP_BOARD_PATH": board_path,
            "LOOP_LOG_ROOT": self.log_root,
            "RUN_CONTEXT_PATH": run_context_path or "None",
            "SELF_IMPROVEMENT_WIKI_ROOT": wiki_root,
            "SELF_IMPROVEMENT_WIKI_SCHEMA": wiki_root.parent / "SCHEMA.md",
            "SELF_IMPROVEMENT_WIKI_INDEX": wiki_root / "index.md",
            "MAX_LESSONS": max_lessons,
            "TIMESTAMP": datetime.now().isoformat(timespec="seconds"),
        }
        return render_template(self.bundle.prompts / "self-improvement.md", values)


def result_to_dict(result: RoleResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        "status": result.status,
        "summary": result.summary,
        "changed_files": result.changed_files,
        "verification_commands": result.verification_commands,
        "findings": result.findings,
        "fix_list": result.fix_list,
        "residual_risks": result.residual_risks,
    }


def execution_budget_prompt_guidance(
    execution_budget: ExecutionBudget | None,
) -> str:
    if execution_budget is None:
        return (
            "No explicit Execution Budget was supplied. Still finish with the "
            "Required Final Response promptly."
        )
    return (
        f"- Hard timeout: {execution_budget.timeout_seconds:g} seconds from "
        "attempt start.\n"
        "- Inactivity checkpoint: "
        f"{execution_budget.checkpoint_seconds:g} seconds without backend "
        "activity.\n\n"
        "The runner enforces both limits. Plan the work and focused "
        "verification so you stop repository activity and return the Required "
        "Final Response before the hard timeout. Do not start an optional or "
        "broad verification command when the remaining time may be "
        "insufficient; record any unrun gate in `residual_risks`."
    )


def role_execution_failure_summary(
    *,
    backend: ExecutionBackendId | None = None,
    returncode: int,
    stderr: str,
    stderr_path: Path,
) -> str:
    """Summarize an attempt that failed without words of its own.

    The Execution Backend is named rather than assumed, so a Claude-backed
    Workflow Step never reports a Codex failure and the operator troubleshoots the
    provider that actually ran.
    """
    subject = _backend_subject(backend)
    expiration = execution_budget_expiration(returncode=returncode, stderr=stderr)
    if expiration is not None:
        return (
            f"{expiration} {subject} did not return a final role result "
            f"before termination. {UNFINISHED_ATTEMPT_WORKSPACE_NOTE} "
            f"See {stderr_path}."
        )
    return f"{subject} failed with exit code {returncode}. See {stderr_path}."


def role_provider_failure_summary(
    *,
    failure_summary: str,
    returncode: int,
    stderr: str,
    stderr_path: Path,
) -> str:
    """Summarize an attempt that failed in its own words, budget expiry included.

    A provider can report a failure of its own in the same window in which the
    Execution Budget expires, and its words then remain the summary because they
    are the most specific thing known about the attempt. They are not allowed to
    cost the operator what the budget-expiry convention guarantees: that the
    repository changes already written remain in the workspace and that rerunning
    the unfinished Issue continues from them. A terminated attempt therefore
    carries that promise whichever signal produced its summary.
    """
    expiration = execution_budget_expiration(returncode=returncode, stderr=stderr)
    if expiration is None:
        return failure_summary
    return (
        f"{expiration} {_as_sentence(failure_summary)} "
        f"{UNFINISHED_ATTEMPT_WORKSPACE_NOTE} See {stderr_path}."
    )


def execution_budget_expiration(*, returncode: int, stderr: str) -> str | None:
    """The Execution Budget annotation of an attempt terminated on expiry.

    The expiry exit status and the stderr annotation have to agree before an
    attempt counts as terminated by its budget, because a provider can produce
    either one while failing on its own terms.
    """
    if returncode != EXECUTION_BUDGET_EXPIRY_RETURNCODE:
        return None
    expiration = EXECUTION_BUDGET_EXPIRATION_PATTERN.search(stderr)
    return None if expiration is None else expiration.group(0)


def _as_sentence(text: str) -> str:
    """Punctuate a provider's own words so they compose into one summary."""
    stripped = text.strip()
    if not stripped or stripped[-1] in ".!?":
        return stripped
    return f"{stripped}."


def role_permission_denial_summary(
    *,
    backend: ExecutionBackendId | None = None,
    refusals: Iterable[RefusalRecord],
    log_path: Path,
) -> str:
    """Summarize an attempt whose Step Outcome is BLOCKED by a Permission Denial.

    The denied tools and their count are named because they are the diagnosis,
    and the durable log is referenced because the full denial record — including
    the exact tool input that was refused — stays there rather than in the
    Workflow Run's state.
    """
    denials = tuple(refusals)
    return (
        f"{_backend_subject(backend)} was denied {describe_refusals(denials)} "
        "during this attempt, so nothing it reported as verified can be trusted. "
        f"{UNFINISHED_ATTEMPT_WORKSPACE_NOTE} See {log_path}."
    )


def _backend_subject(backend: ExecutionBackendId | None) -> str:
    """Name the Execution Backend a summary is about, or the boundary itself."""
    if backend is None:
        return "The Execution Backend"
    return f"The {backend.display_name} Backend"


def list_of_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def slugify_log_token(value: str | None) -> str:
    if not value:
        return ""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    # Truncation can expose the separator that was previously followed by
    # characters outside the length limit, so normalize the boundary again.
    return slug[:48].strip("-")


def compact_log_identity_token(
    value: str,
    max_length: int = LOG_ATTEMPT_TOKEN_MAX_LENGTH,
) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if slug and len(slug) <= max_length:
        return slug

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:LOG_TOKEN_HASH_LENGTH]
    if max_length <= LOG_TOKEN_HASH_LENGTH:
        return digest[:max_length]

    prefix_length = max_length - LOG_TOKEN_HASH_LENGTH - 1
    prefix = slug[:prefix_length].rstrip("-")
    return f"{prefix}-{digest}" if prefix else digest


def _role_attempt_log_prefix(
    *,
    log_root: Path,
    issue: Issue,
    role: str,
    pass_number: int,
    attempt_label: str | None,
    step_instance_id: str | None,
    step_display_name: str | None,
    step_attempt_id: str | None,
    prompt_session_id: str | None,
) -> str:
    """Compose the readable durable-log filename prefix for one role attempt."""
    prefix_parts = [slugify_log_token(issue.number) or "issue"]
    attempt_identity = step_attempt_id or prompt_session_id or str(uuid.uuid4())
    attempt_slug = compact_log_identity_token(attempt_identity)
    prefix_parts.append(f"attempt-{attempt_slug or uuid.uuid4()}")
    attempt_label_slug = slugify_log_token(attempt_label)
    if attempt_label_slug:
        prefix_parts.append(attempt_label_slug)
    if step_instance_id:
        prefix_parts.extend(
            [
                PORTABLE_LOG_MARKER,
                slugify_log_token(step_display_name) or "step",
                slugify_log_token(step_instance_id) or "instance",
            ]
        )
    prefix_parts.extend(
        [slugify_log_token(role) or "role", f"pass{pass_number}"]
    )
    return _fit_role_log_prefix(
        log_root=log_root,
        readable_prefix="-".join(prefix_parts),
        issue_slug=prefix_parts[0],
        attempt_identity=attempt_identity,
        step_instance_id=step_instance_id,
        role=role,
        pass_number=pass_number,
    )


def _fit_role_log_prefix(
    *,
    log_root: Path,
    readable_prefix: str,
    issue_slug: str,
    attempt_identity: str,
    step_instance_id: str | None,
    role: str,
    pass_number: int,
) -> str:
    if _role_log_prefix_fits(log_root, readable_prefix):
        return readable_prefix

    prefix_parts = [
        issue_slug,
        (
            "attempt-"
            + compact_log_identity_token(
                attempt_identity,
                LOG_FALLBACK_ATTEMPT_TOKEN_MAX_LENGTH,
            )
        ),
    ]
    if step_instance_id:
        prefix_parts.extend(
            [
                PORTABLE_LOG_MARKER,
                "step",
                slugify_log_token(step_instance_id) or "instance",
            ]
        )
    prefix_parts.extend(
        [
            compact_log_identity_token(role, LOG_FALLBACK_ROLE_TOKEN_MAX_LENGTH),
            f"pass{pass_number}",
        ]
    )
    compact_prefix = "-".join(prefix_parts)
    if _role_log_prefix_fits(log_root, compact_prefix):
        return compact_prefix

    raise OSError(
        "Dev Loop's issue-local log path is too long for portable Windows "
        f"writes even after filename compaction: {log_root}. "
        "Choose a shorter implementation worktree path."
    )


def _role_log_prefix_fits(log_root: Path, prefix: str) -> bool:
    longest_path = (log_root / f"{prefix}{LONGEST_ROLE_LOG_SUFFIX}").resolve()
    return len(str(longest_path)) <= MAX_PORTABLE_LOG_PATH_LENGTH


def _confined_log_path(path: Path, log_root: Path) -> Path:
    resolved_root = log_root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(
            f"Refusing to write a log outside the configured log root: {path}"
        ) from error
    return resolved_path


@dataclass(frozen=True)
class AttemptLogPaths:
    """The four durable log locations one agent attempt writes."""

    prompt: Path
    stdout: Path
    stderr: Path
    message: Path


def _attempt_log_paths(log_root: Path, prefix: str) -> AttemptLogPaths:
    return AttemptLogPaths(
        prompt=_confined_log_path(log_root / f"{prefix}{PROMPT_LOG_SUFFIX}", log_root),
        stdout=_confined_log_path(log_root / f"{prefix}{STDOUT_LOG_SUFFIX}", log_root),
        stderr=_confined_log_path(log_root / f"{prefix}{STDERR_LOG_SUFFIX}", log_root),
        message=_confined_log_path(
            log_root / f"{prefix}{MESSAGE_LOG_SUFFIX}",
            log_root,
        ),
    )

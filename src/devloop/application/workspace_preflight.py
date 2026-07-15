from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from devloop.domain.identifiers import WorkflowRunId
from devloop.domain.workspace import (
    WORKSPACE_PERMISSION_PROFILE_SCHEMA,
    WORKSPACE_PROBE_VERSION,
    WorkspacePermissionProfile,
    WorkspaceProbeId,
    WorkspaceProbeResult,
    WorkspaceProbeStatus,
)
from devloop.execution.app_server import (
    AppServerApprovalPolicy,
    AppServerApprovalRequest,
    AppServerApprovalsReviewer,
    AppServerClient,
    AppServerCommandApprovalDecision,
    AppServerPermissionProfile,
    AppServerReasoningEffort,
    AppServerTurnResult,
    AppServerTurnStatus,
)
from devloop.infrastructure.codex import resolve_codex_executable
from devloop.infrastructure.git import run_git
from devloop.infrastructure.windows_acl import (
    current_windows_user_sid,
    is_safe_windows_acl_grant,
)

WORKSPACE_PROBE_DIRECTORY = ".permission-probes"
WORKSPACE_PERMISSION_PROFILE = ":workspace"
REAL_WORKSPACE_PROBE_TIMEOUT_SECONDS = 180.0
REAL_WORKSPACE_PROBE_MODEL = "gpt-5.6-sol"
_PROBE_CONTENT = "devloop-workspace-probe-v1\n"

GitProbe = Callable[[Path, tuple[str, ...]], None]


class WorkspacePreflightError(RuntimeError):
    pass


class WorkspacePreflight(Protocol):
    def probe(
        self,
        workspace: Path,
        run_id: WorkflowRunId,
    ) -> WorkspacePermissionProfile: ...


class LocalWorkspacePreflight:
    """Prove parent-process workspace behavior without acting as an agent backend."""

    def __init__(self, *, git_probe: GitProbe | None = None) -> None:
        self._git_probe = git_probe or _default_git_probe

    def probe(
        self,
        workspace: Path,
        run_id: WorkflowRunId,
    ) -> WorkspacePermissionProfile:
        root = _exact_workspace_root(workspace)
        probe_root = _probe_root(root, run_id, suffix="parent")
        try:
            results = _run_parent_probes(root, probe_root, self._git_probe)
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkspacePreflightError(
                "Workspace permission preflight failed before development."
            ) from error
        finally:
            _cleanup_probe_root(probe_root)
        return WorkspacePermissionProfile(
            WORKSPACE_PERMISSION_PROFILE_SCHEMA,
            run_id,
            str(root),
            WORKSPACE_PERMISSION_PROFILE,
            WORKSPACE_PROBE_VERSION,
            False,
            sys.platform.startswith("win"),
            results,
        )


class RealAppServerWorkspacePreflight:
    """Prove the selected root through the only executable backend and parent process."""

    def __init__(self, *, git_probe: GitProbe | None = None) -> None:
        self._git_probe = git_probe or _default_git_probe
        self._parent = LocalWorkspacePreflight(git_probe=self._git_probe)

    def probe(
        self,
        workspace: Path,
        run_id: WorkflowRunId,
    ) -> WorkspacePermissionProfile:
        root = _exact_workspace_root(workspace)
        parent_profile = self._parent.probe(root, run_id)
        probe_root = _probe_root(root, run_id, suffix="app-server")
        approval_requests: list[AppServerApprovalRequest] = []
        try:
            probe_root.parent.mkdir(parents=True, exist_ok=True)
            output, handoff_used = self._run_real_probe(
                root,
                probe_root,
                approval_requests,
            )
            _validate_real_probe_output(output)
        except Exception as error:
            raise WorkspacePreflightError(
                "The real App Server cannot sustain work in the selected workspace."
            ) from error
        finally:
            _cleanup_probe_root(probe_root)
        replacements = {
            WorkspaceProbeId.NESTED_WRITE: "App Server created the exact nested probe file.",
            WorkspaceProbeId.NESTED_ENUMERATION: (
                "The parent process enumerated the App Server-created directory."
            ),
            WorkspaceProbeId.PARENT_HASH: (
                "The parent process read and hashed the App Server-created file."
            ),
            WorkspaceProbeId.TEST_EXECUTION: "The App Server executed the bounded Python probe.",
            WorkspaceProbeId.GIT_INSPECTION: (
                "Git inspection completed without warnings or incomplete state."
            ),
        }
        results = tuple(
            WorkspaceProbeResult(
                result.probe_id,
                result.status,
                replacements.get(result.probe_id, result.evidence),
            )
            for result in parent_profile.results
        )
        results += (
            WorkspaceProbeResult(
                WorkspaceProbeId.APPROVAL_FRAMING,
                WorkspaceProbeStatus.PASSED,
                (
                    "Every probe approval was parsed and limited to the selected workspace."
                    if approval_requests
                    else "The selected permission profile required no broad approval."
                ),
            ),
            WorkspaceProbeResult(
                WorkspaceProbeId.WINDOWS_ACL_HANDOFF,
                (
                    WorkspaceProbeStatus.PASSED
                    if handoff_used
                    else WorkspaceProbeStatus.NOT_REQUIRED
                ),
                (
                    "The exact non-recursive Windows handoff restored parent access."
                    if handoff_used
                    else "The installed backend required no Windows ACL handoff."
                ),
            ),
        )
        return WorkspacePermissionProfile(
            WORKSPACE_PERMISSION_PROFILE_SCHEMA,
            run_id,
            str(root),
            WORKSPACE_PERMISSION_PROFILE,
            WORKSPACE_PROBE_VERSION,
            True,
            handoff_used,
            results,
        )

    def _run_real_probe(
        self,
        root: Path,
        probe_root: Path,
        approval_requests: list[AppServerApprovalRequest],
    ) -> tuple[dict[str, object], bool]:
        sid = current_windows_user_sid() if sys.platform.startswith("win") else None

        def approval(request: AppServerApprovalRequest) -> str | None:
            approval_requests.append(request)
            if sid is None or not is_safe_windows_acl_grant(request.action, root, sid):
                return None
            return AppServerCommandApprovalDecision.ACCEPT.value

        relative_probe = probe_root.relative_to(root).as_posix()
        prompt = _real_probe_prompt(relative_probe)
        with AppServerClient(
            str(resolve_codex_executable()),
            experimental_api=True,
            process_cwd=root,
            approval_handler=approval,
            environment_overrides={
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        ) as client:
            client.initialize()
            client.start_thread(
                root,
                model=REAL_WORKSPACE_PROBE_MODEL,
                reasoning_effort=AppServerReasoningEffort.XHIGH,
                developer_instructions=(
                    "Validate only the selected full execution profile; do not start a turn."
                ),
                runtime_workspace_roots=(root,),
                ephemeral=True,
            )
            thread = client.start_thread(
                root,
                model=REAL_WORKSPACE_PROBE_MODEL,
                reasoning_effort=AppServerReasoningEffort.LOW,
                developer_instructions=(
                    "Perform only the bounded workspace capability probe. Do not inspect secrets, "
                    "change source files, commit, publish, or access paths outside the supplied "
                    "root."
                ),
                approval_policy=AppServerApprovalPolicy.ON_REQUEST,
                approvals_reviewer=AppServerApprovalsReviewer.USER,
                permission_profile=AppServerPermissionProfile.WORKSPACE,
                runtime_workspace_roots=(root,),
                ephemeral=True,
            )
            result = client.run_turn(
                thread.thread_id,
                prompt,
                output_schema=_REAL_PROBE_OUTPUT_SCHEMA,
                timeout_seconds=REAL_WORKSPACE_PROBE_TIMEOUT_SECONDS,
            )
            output = _real_probe_output(result)
            _validate_real_probe_output(output)
            try:
                _verify_agent_probe(root, probe_root, self._git_probe)
            except (OSError, subprocess.SubprocessError, WorkspacePreflightError):
                if sid is None:
                    raise
                handoff = client.run_turn(
                    thread.thread_id,
                    _windows_handoff_prompt(relative_probe, sid),
                    output_schema=_REAL_PROBE_OUTPUT_SCHEMA,
                    timeout_seconds=REAL_WORKSPACE_PROBE_TIMEOUT_SECONDS,
                )
                output = _real_probe_output(handoff)
                _validate_real_probe_output(output)
                _verify_agent_probe(root, probe_root, self._git_probe)
                return output, True
        return output, False


def _real_probe_output(result: AppServerTurnResult) -> dict[str, object]:
    if result.status is not AppServerTurnStatus.COMPLETED:
        raise WorkspacePreflightError("The real workspace probe did not complete.")
    try:
        value = json.loads(result.message)
    except json.JSONDecodeError as error:
        raise WorkspacePreflightError(
            "The real workspace probe output is invalid."
        ) from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WorkspacePreflightError("The real workspace probe output is invalid.")
    return cast(dict[str, object], value)


_REAL_PROBE_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["completed", "checks"],
    "properties": {
        "completed": {"type": "boolean"},
        "checks": {
            "type": "array",
            "minItems": 4,
            "maxItems": 12,
            "items": {"type": "string", "maxLength": 200},
        },
    },
}


def _run_parent_probes(
    workspace: Path,
    probe_root: Path,
    git_probe: GitProbe,
) -> tuple[WorkspaceProbeResult, ...]:
    tuple(workspace.iterdir())
    nested = probe_root / "nested"
    nested.mkdir(parents=True, exist_ok=False)
    probe_file = nested / "probe.txt"
    probe_file.write_text(_PROBE_CONTENT, encoding="utf-8", newline="\n")
    if probe_file.read_text(encoding="utf-8") != _PROBE_CONTENT:
        raise WorkspacePreflightError("Workspace probe bytes changed after writing.")
    if probe_file.name not in {path.name for path in nested.iterdir()}:
        raise WorkspacePreflightError("Workspace probe file cannot be enumerated.")
    digest = hashlib.sha256(probe_file.read_bytes()).hexdigest()
    if digest != hashlib.sha256(_PROBE_CONTENT.encode("utf-8")).hexdigest():
        raise WorkspacePreflightError("Workspace probe file cannot be hashed reliably.")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "assert Path('nested/probe.txt').read_text(encoding='utf-8') == "
                f"{_PROBE_CONTENT!r}"
            ),
        ],
        cwd=probe_root,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=10.0,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        raise WorkspacePreflightError("Focused workspace test execution failed.")
    git_probe(workspace, ("status", "--porcelain=v1", "--untracked-files=all"))
    return (
        WorkspaceProbeResult(
            WorkspaceProbeId.ROOT_READ,
            WorkspaceProbeStatus.PASSED,
            "The parent process read and enumerated the selected root.",
        ),
        WorkspaceProbeResult(
            WorkspaceProbeId.NESTED_WRITE,
            WorkspaceProbeStatus.PASSED,
            "The parent process created a nested probe file.",
        ),
        WorkspaceProbeResult(
            WorkspaceProbeId.NESTED_ENUMERATION,
            WorkspaceProbeStatus.PASSED,
            "The parent process enumerated the nested probe directory.",
        ),
        WorkspaceProbeResult(
            WorkspaceProbeId.PARENT_HASH,
            WorkspaceProbeStatus.PASSED,
            "The parent process read and hashed the nested probe file.",
        ),
        WorkspaceProbeResult(
            WorkspaceProbeId.TEST_EXECUTION,
            WorkspaceProbeStatus.PASSED,
            "A bounded Python check read the probe from the selected root.",
        ),
        WorkspaceProbeResult(
            WorkspaceProbeId.GIT_INSPECTION,
            WorkspaceProbeStatus.PASSED,
            "Git inspection completed without warnings.",
        ),
    )


def _verify_agent_probe(workspace: Path, probe_root: Path, git_probe: GitProbe) -> None:
    probe_file = probe_root / "nested" / "probe.txt"
    if probe_file.read_text(encoding="utf-8") != _PROBE_CONTENT:
        raise WorkspacePreflightError("Parent process cannot read the App Server probe file.")
    if probe_file.name not in {path.name for path in probe_file.parent.iterdir()}:
        raise WorkspacePreflightError("Parent process cannot enumerate the App Server probe file.")
    hashlib.sha256(probe_file.read_bytes()).hexdigest()
    git_probe(workspace, ("status", "--porcelain=v1", "--untracked-files=all"))


def _validate_real_probe_output(value: dict[str, object]) -> None:
    if set(value) != {"completed", "checks"} or value.get("completed") is not True:
        raise WorkspacePreflightError("The real workspace probe reported incomplete checks.")
    checks = value.get("checks")
    if not isinstance(checks, list) or len(checks) < 4 or not all(
        isinstance(item, str) and item for item in checks
    ):
        raise WorkspacePreflightError("The real workspace probe evidence is incomplete.")


def _real_probe_prompt(relative_probe: str) -> str:
    return (
        f"Within the selected workspace only, create {relative_probe}/nested/probe.txt with exact "
        f"UTF-8 content {_PROBE_CONTENT!r}. Read it, enumerate its parent directory, run a tiny "
        "Python assertion that reads it, and run git status --porcelain=v1 --untracked-files=all. "
        "Do not edit any other file. Return completed=true and short check names only."
    )


def _windows_handoff_prompt(relative_probe: str, sid: str) -> str:
    return (
        "The parent process could not read the probe. Grant access only to the existing paths "
        f"{relative_probe}, {relative_probe}/nested, and {relative_probe}/nested/probe.txt with "
        f"separate exact non-recursive icacls grants to *{sid}:(F). Never use /T, wildcards, "
        "a parent path, or another path. Re-read and enumerate the probe, then return "
        "completed=true and short check names only."
    )


def _exact_workspace_root(workspace: Path) -> Path:
    expanded = workspace.expanduser().absolute()
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as error:
        raise WorkspacePreflightError("The selected workspace is unavailable.") from error
    if expanded != resolved or workspace.is_symlink() or not resolved.is_dir():
        raise WorkspacePreflightError(
            "The selected workspace must be its exact canonical directory."
        )
    return resolved


def _probe_root(workspace: Path, run_id: WorkflowRunId, *, suffix: str) -> Path:
    return (
        workspace
        / ".devloop"
        / "runs"
        / WORKSPACE_PROBE_DIRECTORY
        / f"{run_id.value}-{suffix}"
    )


def _cleanup_probe_root(probe_root: Path) -> None:
    try:
        if probe_root.exists():
            shutil.rmtree(probe_root)
        parent = probe_root.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as error:
        raise WorkspacePreflightError("Workspace probe artifacts could not be removed.") from error


def _default_git_probe(workspace: Path, arguments: tuple[str, ...]) -> None:
    run_git(workspace, arguments, fail_on_stderr=True)

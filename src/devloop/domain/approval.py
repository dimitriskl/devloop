from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath

from devloop.domain.doctor import redact_diagnostic

APPROVAL_POLICY_SCHEMA = "devloop.approval-policy/v1"
APPROVAL_DECISION_SCHEMA = "devloop.approval-decision/v1"
STANDARD_APPROVAL_POLICY_VERSION = "1.0.0"

_SHELL_CONTROL = re.compile(r"(?:&&|\|\||[;|<>]|\$\(|`|\r|\n)")
_EXECUTABLE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|\.\.?[\\/])")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//)")
_POWERSHELL_PROVIDER = re.compile(r"^(?:[^:\s]+::|[A-Za-z][A-Za-z0-9+.-]*:(?![\\/]))")
_READ_ONLY_GIT = frozenset({"diff", "log", "show", "status", "rev-parse", "ls-files"})
_READ_COMMANDS = frozenset({"rg", "sed", "head", "tail", "wc", "findstr", "type", "cat"})
_PYTHON_NAMES = frozenset({"python", "python3", "py"})


class CommandFamily(str, Enum):
    GIT_INSPECTION = "GIT_INSPECTION"
    WORKSPACE_READ = "WORKSPACE_READ"
    FOCUSED_TEST = "FOCUSED_TEST"
    WINDOWS_ACL_HANDOFF = "WINDOWS_ACL_HANDOFF"
    FILE_CHANGE = "FILE_CHANGE"
    PERMISSIONS = "PERMISSIONS"
    OTHER = "OTHER"
    AMBIGUOUS = "AMBIGUOUS"


class PathBoundary(str, Enum):
    WORKSPACE = "WORKSPACE"
    OUTSIDE_WORKSPACE = "OUTSIDE_WORKSPACE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ApprovalClassification(str, Enum):
    USER_DECISION = "USER_DECISION"
    UNSUPPORTED = "UNSUPPORTED"


class ApprovalDecisionScope(str, Enum):
    ONCE = "ONCE"
    SESSION = "SESSION"
    DENIED = "DENIED"
    RUN_ABORTED = "RUN_ABORTED"


@dataclass(frozen=True)
class ApprovalPolicy:
    schema: str
    version: str
    component_id: str
    command_families: tuple[CommandFamily, ...]
    path_boundary: PathBoundary
    decision_options: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema != APPROVAL_POLICY_SCHEMA:
            raise ValueError("Unsupported approval policy schema.")
        if not self.version or not self.component_id:
            raise ValueError("Approval policy provenance is incomplete.")
        if len(set(self.command_families)) != len(self.command_families):
            raise ValueError("Approval policy command families must be unique.")
        if len(set(self.decision_options)) != len(self.decision_options):
            raise ValueError("Approval policy decision options must be unique.")

    @property
    def policy_hash(self) -> str:
        return _canonical_hash(
            {
                "schema": self.schema,
                "version": self.version,
                "component_id": self.component_id,
                "command_families": [item.value for item in self.command_families],
                "path_boundary": self.path_boundary.value,
                "decision_options": list(self.decision_options),
            }
        )

    @classmethod
    def standard(cls, component_id: str) -> ApprovalPolicy:
        return cls(
            APPROVAL_POLICY_SCHEMA,
            STANDARD_APPROVAL_POLICY_VERSION,
            component_id,
            (
                CommandFamily.GIT_INSPECTION,
                CommandFamily.WORKSPACE_READ,
                CommandFamily.FOCUSED_TEST,
                CommandFamily.WINDOWS_ACL_HANDOFF,
                CommandFamily.FILE_CHANGE,
                CommandFamily.PERMISSIONS,
                CommandFamily.OTHER,
                CommandFamily.AMBIGUOUS,
            ),
            PathBoundary.WORKSPACE,
            ("accept", "acceptForSession", "decline", "cancel"),
        )

    @classmethod
    def read_only(
        cls,
        component_id: str,
        *,
        focused_tests: bool = False,
    ) -> ApprovalPolicy:
        families = [
            CommandFamily.GIT_INSPECTION,
            CommandFamily.WORKSPACE_READ,
        ]
        if focused_tests:
            families.append(CommandFamily.FOCUSED_TEST)
        families.extend((CommandFamily.OTHER, CommandFamily.AMBIGUOUS))
        return cls(
            APPROVAL_POLICY_SCHEMA,
            STANDARD_APPROVAL_POLICY_VERSION,
            component_id,
            tuple(families),
            PathBoundary.WORKSPACE,
            ("accept", "acceptForSession", "decline", "cancel"),
        )


@dataclass(frozen=True)
class ClassifiedApproval:
    policy: ApprovalPolicy
    family: CommandFamily
    boundary: PathBoundary
    classification: ApprovalClassification
    parsed_action: str
    reason: str
    command_hash: str
    auto_decision: str | None = None


def classify_command(
    command: str,
    cwd: Path,
    workspace: Path,
    policy: ApprovalPolicy,
) -> ClassifiedApproval:
    text = command.strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not text or _SHELL_CONTROL.search(text):
        return _classification(
            policy,
            CommandFamily.AMBIGUOUS,
            PathBoundary.UNKNOWN,
            "Compound or ambiguous shell request",
            "Shell operators or substitutions require an explicit user decision.",
            digest,
        )
    try:
        tokens = [_unquote(item) for item in shlex.split(text, posix=False)]
    except ValueError:
        return _classification(
            policy,
            CommandFamily.AMBIGUOUS,
            PathBoundary.UNKNOWN,
            "Unparseable shell request",
            "The command could not be parsed and requires an explicit user decision.",
            digest,
        )
    if not tokens or _EXECUTABLE_PATH.match(tokens[0]):
        return _classification(
            policy,
            CommandFamily.AMBIGUOUS,
            PathBoundary.UNKNOWN,
            "Executable path request",
            "Executable paths fail closed and require an explicit user decision.",
            digest,
        )
    boundary = _path_boundary(tokens[1:], cwd, workspace)
    executable = Path(tokens[0]).name.lower()
    if executable == "git" and len(tokens) > 1 and tokens[1].lower() in _READ_ONLY_GIT:
        family = CommandFamily.GIT_INSPECTION
        action = f"Read-only Git {tokens[1].lower()}"
    elif executable in _READ_COMMANDS:
        family = CommandFamily.WORKSPACE_READ
        action = f"Bounded workspace read with {executable}"
    elif executable in _PYTHON_NAMES and _is_focused_test(tokens):
        family = CommandFamily.FOCUSED_TEST
        action = "Run focused Python verification"
    elif executable in {"icacls", "icacls.exe"}:
        family = CommandFamily.WINDOWS_ACL_HANDOFF
        action = "Grant a non-recursive Windows workspace ACL"
    else:
        family = CommandFamily.OTHER
        action = f"Run {executable or 'an unknown executable'}"
    reason = "The request is inside the selected workspace and still requires your decision."
    if boundary is PathBoundary.OUTSIDE_WORKSPACE:
        reason = "The request names a path outside the selected workspace; approve only explicitly."
    elif boundary is PathBoundary.UNKNOWN:
        reason = "The request path boundary is unknown and requires an explicit user decision."
    classification = _classification(policy, family, boundary, action, reason, digest)
    if family is CommandFamily.WINDOWS_ACL_HANDOFF and (
        any(item.casefold() == "/t" for item in tokens)
        or boundary is not PathBoundary.WORKSPACE
    ):
        return ClassifiedApproval(
            policy,
            family,
            boundary,
            ApprovalClassification.UNSUPPORTED,
            action,
            "Recursive or out-of-workspace ACL grants are rejected by the locked policy.",
            digest,
        )
    return classification


def classify_non_command(
    *,
    family: CommandFamily,
    target: str | None,
    workspace: Path,
    policy: ApprovalPolicy,
) -> ClassifiedApproval:
    boundary = PathBoundary.UNKNOWN
    if target is None:
        boundary = PathBoundary.NOT_APPLICABLE
    else:
        boundary = _resolved_boundary(Path(target), workspace)
    return _classification(
        policy,
        family,
        boundary,
        family.value.replace("_", " ").title(),
        "Codex requested an explicit user decision under the locked component policy.",
        hashlib.sha256((target or "").encode("utf-8")).hexdigest(),
    )


def decision_evidence(
    *,
    component_id: str,
    issue_id: str | None,
    attempt_id: str | None,
    request_id: int | str,
    request_kind: str,
    classification: ClassifiedApproval,
    selected_decision: str,
    supported_decisions: tuple[str, ...],
) -> Mapping[str, object]:
    if selected_decision not in supported_decisions:
        raise ValueError("The approval decision was not advertised by the backend.")
    return {
        "schema": APPROVAL_DECISION_SCHEMA,
        "component_id": component_id,
        "issue_id": issue_id,
        "attempt_id": attempt_id,
        "request_id": request_id,
        "request_kind": request_kind,
        "policy_version": classification.policy.version,
        "policy_hash": classification.policy.policy_hash,
        "command_family": classification.family.value,
        "workspace_boundary": classification.boundary.value,
        "classification": classification.classification.value,
        "parsed_action": redact_diagnostic(classification.parsed_action, limit=500),
        "policy_reason": redact_diagnostic(classification.reason, limit=1000),
        "command_hash": classification.command_hash,
        "selected_decision": selected_decision,
        "decision_scope": _decision_scope(selected_decision).value,
        "supported_decisions": list(supported_decisions),
    }


def approval_policy_payload(policy: ApprovalPolicy) -> Mapping[str, object]:
    return {
        "schema": policy.schema,
        "version": policy.version,
        "component_id": policy.component_id,
        "command_families": [item.value for item in policy.command_families],
        "path_boundary": policy.path_boundary.value,
        "decision_options": list(policy.decision_options),
        "policy_hash": policy.policy_hash,
    }


def approval_policy_from_payload(payload: Mapping[str, object]) -> ApprovalPolicy:
    families = payload.get("command_families")
    decisions = payload.get("decision_options")
    if not isinstance(families, list) or not all(isinstance(item, str) for item in families):
        raise ValueError("Approval policy command families are invalid.")
    if not isinstance(decisions, list) or not all(isinstance(item, str) for item in decisions):
        raise ValueError("Approval policy decision options are invalid.")
    policy = ApprovalPolicy(
        str(payload.get("schema", "")),
        str(payload.get("version", "")),
        str(payload.get("component_id", "")),
        tuple(CommandFamily(item) for item in families),
        PathBoundary(str(payload.get("path_boundary", ""))),
        tuple(decisions),
    )
    expected_hash = payload.get("policy_hash")
    if expected_hash is not None and expected_hash != policy.policy_hash:
        raise ValueError("Approval policy hash does not match its content.")
    return policy


def locked_approval_policy(
    policies: Iterable[ApprovalPolicy],
    component_id: str,
    fallback: ApprovalPolicy,
) -> ApprovalPolicy:
    matches = tuple(item for item in policies if item.component_id == component_id)
    if len(matches) > 1:
        raise ValueError("Workflow Run has multiple approval policies for one component.")
    return fallback if not matches else matches[0]


def _classification(
    policy: ApprovalPolicy,
    family: CommandFamily,
    boundary: PathBoundary,
    action: str,
    reason: str,
    command_hash: str,
) -> ClassifiedApproval:
    if family not in policy.command_families:
        return ClassifiedApproval(
            policy,
            family,
            boundary,
            ApprovalClassification.UNSUPPORTED,
            action,
            "The locked component policy does not permit this command family.",
            command_hash,
        )
    if (
        policy.path_boundary is PathBoundary.WORKSPACE
        and boundary is PathBoundary.OUTSIDE_WORKSPACE
    ):
        return ClassifiedApproval(
            policy,
            family,
            boundary,
            ApprovalClassification.UNSUPPORTED,
            action,
            "The locked component policy does not permit out-of-workspace access.",
            command_hash,
        )
    return ClassifiedApproval(
        policy,
        family,
        boundary,
        ApprovalClassification.USER_DECISION,
        action,
        reason,
        command_hash,
    )


def _is_focused_test(tokens: list[str]) -> bool:
    lowered = [item.lower() for item in tokens]
    return (
        len(lowered) >= 3
        and lowered[1:3] == ["-m", "pytest"]
        and "--collect-only" not in lowered
    )


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _path_boundary(tokens: list[str], cwd: Path, workspace: Path) -> PathBoundary:
    candidates = [
        candidate
        for item in tokens
        if (candidate := _path_token(item)) is not None
    ]
    if not candidates:
        return _resolved_boundary(cwd, workspace)
    boundaries = {_token_boundary(item, cwd, workspace) for item in candidates}
    if PathBoundary.OUTSIDE_WORKSPACE in boundaries:
        return PathBoundary.OUTSIDE_WORKSPACE
    if PathBoundary.UNKNOWN in boundaries:
        return PathBoundary.UNKNOWN
    return PathBoundary.WORKSPACE


def _looks_like_path(value: str) -> bool:
    if value.startswith("-") or value.startswith("/") and value.lower().startswith("/grant"):
        return False
    return (
        value in {".", ".."}
        or _POWERSHELL_PROVIDER.match(value) is not None
        or any(separator in value for separator in ("/", "\\"))
    )


def _path_token(value: str) -> str | None:
    if value.startswith("-") and "=" in value:
        _, candidate = value.split("=", 1)
        return candidate if _looks_like_path(candidate) else None
    return value if _looks_like_path(value) else None


def _token_boundary(value: str, cwd: Path, workspace: Path) -> PathBoundary:
    if _POWERSHELL_PROVIDER.match(value) is not None and not _WINDOWS_ABSOLUTE.match(value):
        return PathBoundary.UNKNOWN
    if _WINDOWS_ABSOLUTE.match(value):
        candidate = PureWindowsPath(value)
        root = PureWindowsPath(str(workspace))
        if ".." in candidate.parts:
            return PathBoundary.UNKNOWN
        if candidate.drive.casefold() != root.drive.casefold():
            return PathBoundary.OUTSIDE_WORKSPACE
        try:
            candidate.relative_to(root)
        except ValueError:
            return PathBoundary.OUTSIDE_WORKSPACE
        return PathBoundary.WORKSPACE
    path = Path(value)
    return _resolved_boundary(path if path.is_absolute() else cwd / path, workspace)


def _resolved_boundary(candidate: Path, workspace: Path) -> PathBoundary:
    try:
        candidate.resolve(strict=False).relative_to(workspace.resolve(strict=True))
    except ValueError:
        return PathBoundary.OUTSIDE_WORKSPACE
    except OSError:
        return PathBoundary.UNKNOWN
    return PathBoundary.WORKSPACE


def _decision_scope(decision: str) -> ApprovalDecisionScope:
    scopes = {
        "accept": ApprovalDecisionScope.ONCE,
        "acceptForSession": ApprovalDecisionScope.SESSION,
        "decline": ApprovalDecisionScope.DENIED,
        "cancel": ApprovalDecisionScope.RUN_ABORTED,
    }
    try:
        return scopes[decision]
    except KeyError:
        raise ValueError("Unsupported approval decision.") from None


def _canonical_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

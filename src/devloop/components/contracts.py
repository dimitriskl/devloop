from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from devloop.domain.approval import ApprovalPolicy
from devloop.domain.execution import ExecutionProfile
from devloop.domain.identifiers import DataContractId, StepComponentId

COMPONENT_MANIFEST_SCHEMA = "devloop.step-component/v1"
_SEMANTIC_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_DISTRIBUTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class PortDirection(str, Enum):
    INPUT = "INPUT"
    OUTPUT = "OUTPUT"


class StepExecutionPolicy(str, Enum):
    ANALYSIS_DRAFT_ONLY = "ANALYSIS_DRAFT_ONLY"
    EXPLICIT_GIT = "EXPLICIT_GIT"
    WORKSPACE_WRITE = "WORKSPACE_WRITE"
    READ_ONLY = "READ_ONLY"
    VERIFICATION_ONLY = "VERIFICATION_ONLY"
    LOCAL_FINALIZATION = "LOCAL_FINALIZATION"


@dataclass(frozen=True)
class ComponentPort:
    name: str
    contract_id: DataContractId
    direction: PortDirection
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("Component port names must be nonempty identifiers.")


@dataclass(frozen=True)
class ComponentManifest:
    schema: str
    component_id: StepComponentId
    version: str
    distribution: str
    package_hash: str
    execution_policy: StepExecutionPolicy
    ports: tuple[ComponentPort, ...]
    approval_policy: ApprovalPolicy | None = None
    execution_profiles: tuple[ExecutionProfile, ...] = ()
    default_execution_profile: str | None = None


class StepComponentRunner(Protocol):
    @property
    def component_id(self) -> StepComponentId: ...


class ComponentRegistryError(RuntimeError):
    pass


class ComponentRegistry:
    def __init__(self) -> None:
        self._entries: dict[StepComponentId, tuple[ComponentManifest, StepComponentRunner]] = {}

    def register(self, manifest: ComponentManifest, runner: StepComponentRunner) -> None:
        _validate_manifest(manifest)
        if manifest.component_id != runner.component_id:
            raise ComponentRegistryError("Component manifest and runner identities differ.")
        if manifest.component_id in self._entries:
            raise ComponentRegistryError(f"Duplicate component: {manifest.component_id}.")
        self._entries[manifest.component_id] = (manifest, runner)

    def resolve(
        self,
        component_id: StepComponentId,
    ) -> tuple[ComponentManifest, StepComponentRunner]:
        try:
            return self._entries[component_id]
        except KeyError:
            raise ComponentRegistryError(f"Component is not installed: {component_id}.") from None

    @property
    def manifests(self) -> tuple[ComponentManifest, ...]:
        return tuple(
            self._entries[key][0] for key in sorted(self._entries, key=lambda item: item.value)
        )


def _validate_manifest(manifest: ComponentManifest) -> None:
    if manifest.schema != COMPONENT_MANIFEST_SCHEMA:
        raise ComponentRegistryError("Unsupported component manifest schema.")
    if _SEMANTIC_VERSION.fullmatch(manifest.version) is None:
        raise ComponentRegistryError("Component manifest version must use semantic versioning.")
    if _DISTRIBUTION.fullmatch(manifest.distribution) is None:
        raise ComponentRegistryError("Component manifest distribution is invalid.")
    if _SHA256.fullmatch(manifest.package_hash) is None:
        raise ComponentRegistryError("Component manifest package hash must be SHA-256.")
    if (
        manifest.approval_policy is not None
        and manifest.approval_policy.component_id != manifest.component_id.value
    ):
        raise ComponentRegistryError("Approval policy belongs to a different component.")
    profile_ids = [item.profile_id.value for item in manifest.execution_profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise ComponentRegistryError("Component execution profiles must be unique.")
    if any(
        item.component_id != manifest.component_id.value
        for item in manifest.execution_profiles
    ):
        raise ComponentRegistryError("Execution profile belongs to a different component.")
    if manifest.default_execution_profile is not None:
        if manifest.default_execution_profile not in profile_ids:
            raise ComponentRegistryError("Default execution profile is not installed.")
    port_keys = [(port.direction, port.name) for port in manifest.ports]
    if len(port_keys) != len(set(port_keys)):
        raise ComponentRegistryError("Component manifest contains duplicate ports.")


def package_source_hash(package_root: Path) -> str:
    root = package_root.resolve()
    files = tuple(
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".json", ".py"}
    )
    if not files:
        raise ComponentRegistryError("Component package contains no hashable source files.")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

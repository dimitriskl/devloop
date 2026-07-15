from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from devloop.application.config import ApplicationConfig
from devloop.execution.compatibility import (
    STANDARD_WORKFLOW_BACKEND_CONTRACT,
    CompatibilityCache,
    CompatibilityCacheKey,
    CompatibilityError,
    CompatibilityFinding,
    CompatibilityFindingSeverity,
    InstalledBackendProfile,
    InstalledSchemaProfiler,
)
from devloop.infrastructure.codex import run_codex
from devloop.infrastructure.git import GitOperationError, run_git

BACKEND_PROBE_VERSION = "devloop.backend-preflight/v1"
SCHEMA_GENERATION_TIMEOUT_SECONDS = 15.0
COMPATIBILITY_CACHE_DIRECTORY = "backend-compatibility"
STANDARD_PERMISSION_PROFILE = ":read-only|:workspace"

SchemaCommand = Callable[[Path, list[str], Path, float], bool]
PlatformProbe = Callable[[], bool]


class InstalledBackendCompatibilityService:
    """Generate, reduce, and exactly cache the installed App Server contract."""

    def __init__(
        self,
        config: ApplicationConfig,
        *,
        schema_command: SchemaCommand | None = None,
        platform_probe: PlatformProbe | None = None,
    ) -> None:
        self._config = config
        self._schema_command = schema_command or _run_schema_command
        self._platform_probe = platform_probe or self._run_platform_probe
        self._profiler = InstalledSchemaProfiler(STANDARD_WORKFLOW_BACKEND_CONTRACT)
        self._cache = CompatibilityCache(
            config.paths.user_data / COMPATIBILITY_CACHE_DIRECTORY
        )

    def profile(self, executable: Path, codex_version: str) -> InstalledBackendProfile:
        key = self.cache_key(codex_version)
        cached = self._cache.load(key)
        if cached is not None:
            return cached
        with tempfile.TemporaryDirectory(prefix="devloop-app-server-schema-") as temporary:
            root = Path(temporary)
            stable = root / "stable"
            experimental = root / "experimental"
            self._generate(executable, stable, experimental)
            profile = self._profiler.profile(
                stable,
                experimental,
                codex_version=codex_version,
            )
        if profile.compatible:
            try:
                platform_verified = self._platform_probe()
            except (GitOperationError, OSError, subprocess.SubprocessError, ValueError):
                platform_verified = False
            if not platform_verified:
                return replace(
                    profile,
                    findings=(
                        *profile.findings,
                        CompatibilityFinding(
                            "platform-preflight-failed",
                            CompatibilityFindingSeverity.BLOCKING,
                            (
                                "The real App Server could not sustain the bounded workspace "
                                "capability probe."
                            ),
                            (
                                "Verify the selected model, filesystem permissions, Git, and "
                                "Windows ACL handoff, then run the doctor again."
                            ),
                        ),
                    ),
                )
            profile = replace(profile, platform_preflight_verified=True)
        self._cache.save(key, profile)
        return profile

    def cache_key(self, codex_version: str) -> CompatibilityCacheKey:
        return CompatibilityCacheKey(
            codex_version=codex_version,
            operating_system=sys.platform,
            filesystem=_filesystem_identity(self._config.repository),
            permission_profile=STANDARD_PERMISSION_PROFILE,
            probe_version=BACKEND_PROBE_VERSION,
            workflow_contract_hash=STANDARD_WORKFLOW_BACKEND_CONTRACT.contract_hash,
        )

    def _generate(self, executable: Path, stable: Path, experimental: Path) -> None:
        commands = (
            (stable, ["app-server", "generate-json-schema", "--out", str(stable)]),
            (
                experimental,
                [
                    "app-server",
                    "generate-json-schema",
                    "--out",
                    str(experimental),
                    "--experimental",
                ],
            ),
        )
        for output, arguments in commands:
            output.mkdir(parents=True, exist_ok=False)
            if not self._schema_command(
                executable,
                arguments,
                self._config.repository,
                SCHEMA_GENERATION_TIMEOUT_SECONDS,
            ):
                raise CompatibilityError(
                    "The installed Codex CLI could not generate its App Server schema."
                )

    def _run_platform_probe(self) -> bool:
        from devloop.application.workspace_preflight import (
            RealAppServerWorkspacePreflight,
            WorkspacePreflightError,
        )
        from devloop.domain.identifiers import WorkflowRunId

        try:
            with tempfile.TemporaryDirectory(
                prefix=".devloop-doctor-platform-",
                dir=self._config.repository,
            ) as temporary:
                repository = Path(temporary).resolve(strict=True)
                run_git(repository, ("init", "--quiet"), fail_on_stderr=True)
                profile = RealAppServerWorkspacePreflight().probe(
                    repository,
                    WorkflowRunId("run-20000101t000000-000000000000"),
                )
                return profile.ready and profile.real_backend_verified
        except WorkspacePreflightError:
            return False


def _run_schema_command(
    executable: Path,
    arguments: list[str],
    cwd: Path,
    timeout_seconds: float,
) -> bool:
    try:
        completed = run_codex(
            executable,
            arguments,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _filesystem_identity(path: Path) -> str:
    """Return a stable, non-sensitive volume identity for exact probe caching."""

    try:
        stat = path.stat()
    except OSError:
        return f"unavailable:{path.anchor.casefold()}"
    device = getattr(stat, "st_dev", None)
    if isinstance(device, int):
        return f"device:{device}"
    return f"volume:{os.path.splitdrive(str(path))[0].casefold()}"

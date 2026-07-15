from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from devloop.application.backend_compatibility import BACKEND_PROBE_VERSION
from devloop.components.development import DEVELOPMENT_MODEL
from devloop.domain.workspace import WORKSPACE_PROBE_VERSION
from devloop.execution.compatibility import STANDARD_WORKFLOW_BACKEND_CONTRACT
from devloop.verification.evidence import (
    EvidenceIdentity,
    EvidenceManifest,
    GateResult,
    GateStatus,
    VerificationTier,
    manifest_payload,
)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codexcli-gate")
    parser.add_argument("--tier", choices=[item.value for item in VerificationTier], required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--status", choices=[item.value for item in GateStatus], required=True)
    parser.add_argument("--duration-ms", type=int, required=True)
    parser.add_argument("--result-log", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument("--model", default=DEVELOPMENT_MODEL)
    parser.add_argument("--reasoning-effort", default="xhigh")
    args = parser.parse_args(arguments)
    root = args.repo.resolve(strict=True)
    output = _inside(root, args.output)
    result_log = _inside(root, args.result_log)
    if not result_log.is_file():
        raise SystemExit("Verification result log does not exist inside the workspace.")
    identity = build_identity(
        root,
        artifacts=tuple(_inside(root, item) for item in args.artifact),
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    manifest = EvidenceManifest.create(
        VerificationTier(args.tier),
        identity,
        (
            GateResult(
                args.gate_id,
                GateStatus(args.status),
                args.duration_ms,
                result_log.relative_to(root).as_posix(),
            ),
        ),
        commit=_command(root, "git", "rev-parse", "HEAD"),
        codex_version=_command(root, "codex", "--version"),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest_payload(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"RECORDED evidence tier={manifest.tier.value} "
        f"status={args.status} key={manifest.evidence_key}"
    )
    return 0


def build_identity(
    root: Path,
    *,
    artifacts: tuple[Path, ...],
    model: str,
    reasoning_effort: str,
) -> EvidenceIdentity:
    status = _command(
        root,
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        allow_empty=True,
    )
    if status:
        raise ValueError("Verification evidence requires a clean tracked implementation.")
    probe_hash = hashlib.sha256(
        f"{BACKEND_PROBE_VERSION}\0{WORKSPACE_PROBE_VERSION}".encode("utf-8")
    ).hexdigest()
    artifact_hashes = tuple(
        f"{item.name}:{hashlib.sha256(item.read_bytes()).hexdigest()}"
        for item in sorted(artifacts)
    )
    platform_id = "-".join(
        (
            platform.system().lower(),
            platform.machine().lower(),
            f"python{sys.version_info.major}.{sys.version_info.minor}",
        )
    )
    return EvidenceIdentity(
        _git_identity_hash(root),
        _git_identity_hash(root, prefixes=("src/devloop/components/",)),
        _git_identity_hash(root, prefixes=("src/devloop/workflows/",)),
        STANDARD_WORKFLOW_BACKEND_CONTRACT.contract_hash,
        probe_hash,
        model,
        reasoning_effort,
        platform_id,
        artifact_hashes,
    )


def _git_identity_hash(root: Path, *, prefixes: tuple[str, ...] = ()) -> str:
    output = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    entries: list[bytes] = []
    for item in output.split(b"\0"):
        if not item:
            continue
        _, separator, encoded_path = item.partition(b"\t")
        if not separator:
            raise ValueError("Git returned an invalid index entry.")
        path = encoded_path.decode("utf-8")
        if not prefixes or any(path.startswith(prefix) for prefix in prefixes):
            entries.append(item)
    if not entries:
        raise ValueError("Verification identity cannot find required tracked sources.")
    digest = hashlib.sha256()
    for item in sorted(entries):
        digest.update(item)
        digest.update(b"\0")
    return digest.hexdigest()


def _inside(root: Path, value: Path) -> Path:
    candidate = value if value.is_absolute() else root / value
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise SystemExit("Verification evidence paths must stay inside the workspace.") from None
    return resolved


def _command(root: Path, *command: str, allow_empty: bool = False) -> str:
    completed = subprocess.run(
        list(command),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if not value and not allow_empty:
        raise ValueError(f"Verification provenance command returned no value: {command[0]}.")
    return value


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from devloop.verification.evidence import (
    EvidenceManifest,
    VerificationTier,
    manifest_from_payload,
)

RELEASE_EVIDENCE_SCHEMA = "devloop.release-evidence/v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--allow-pending-notes", action="store_true")
    args = parser.parse_args()
    root = args.repo.resolve(strict=True)
    evidence_root = root / ".release-evidence"
    windows = _load_manifest(evidence_root / "windows-release.json")
    linux = _load_manifest(evidence_root / "linux-release.json")
    _validate_platform_pair(root, windows, linux)
    demonstration = _demonstration(evidence_root / "demonstration.json", root)
    notes = (root / "docs" / "release-notes-v0.1.0.md").read_text(encoding="utf-8")
    if not args.allow_pending_notes and "PENDING" in notes:
        raise SystemExit("Release notes still contain PENDING required evidence.")
    payload = {
        "schema": RELEASE_EVIDENCE_SCHEMA,
        "commit": windows.commit,
        "implementation_hash": windows.identity.implementation_hash,
        "component_hash": windows.identity.component_hash,
        "workflow_hash": windows.identity.workflow_hash,
        "protocol_contract_hash": windows.identity.protocol_contract_hash,
        "probe_hash": windows.identity.probe_hash,
        "model": windows.identity.model,
        "reasoning_effort": windows.identity.reasoning_effort,
        "artifact_hashes": list(windows.identity.artifact_hashes),
        "platforms": [
            _platform_payload("windows", windows),
            _platform_payload("linux", linux),
        ],
        "demonstration": demonstration,
        "release_ready": "PENDING" not in notes,
    }
    output = evidence_root / "release-evidence.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"PASS combined release evidence {output.relative_to(root).as_posix()}")
    return 0


def _load_manifest(path: Path) -> EvidenceManifest:
    if not path.is_file():
        raise SystemExit(f"Required operator evidence is missing: {path.as_posix()}.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"Operator evidence is invalid: {path.as_posix()}.")
    manifest = manifest_from_payload(value)
    if manifest.tier is not VerificationTier.RELEASE or not manifest.passed:
        raise SystemExit(f"Required release gate did not pass: {path.as_posix()}.")
    return manifest


def _validate_platform_pair(
    root: Path,
    windows: EvidenceManifest,
    linux: EvidenceManifest,
) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if windows.commit != linux.commit or windows.commit != head:
        raise SystemExit("Windows and Linux evidence must use the current release commit.")
    fields = (
        "implementation_hash",
        "component_hash",
        "workflow_hash",
        "protocol_contract_hash",
        "probe_hash",
        "model",
        "reasoning_effort",
        "artifact_hashes",
    )
    if any(getattr(windows.identity, field) != getattr(linux.identity, field) for field in fields):
        raise SystemExit("Windows and Linux evidence identities do not describe one release.")
    if not windows.identity.platform.startswith("windows-"):
        raise SystemExit("Windows release evidence has the wrong platform identity.")
    if not linux.identity.platform.startswith("linux-"):
        raise SystemExit("Linux release evidence has the wrong platform identity.")
    actual = {
        f"{path.name}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        for path in (root / "dist").glob("devloop_codexcli-0.1.0*")
        if path.is_file()
    }
    if actual != set(windows.identity.artifact_hashes):
        raise SystemExit("Release artifact bytes do not match the platform evidence.")


def _demonstration(path: Path, root: Path) -> dict[str, object]:
    if not path.is_file():
        raise SystemExit("Demonstration evidence is missing.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "devloop.demonstration/v1":
        raise SystemExit("Demonstration evidence is invalid.")
    recording_value = value.get("recording")
    digest = value.get("sha256")
    if not isinstance(recording_value, str) or not isinstance(digest, str):
        raise SystemExit("Demonstration evidence is incomplete.")
    recording = (root / recording_value).resolve(strict=True)
    try:
        recording.relative_to(root)
    except ValueError:
        raise SystemExit("Demonstration recording must stay inside the workspace.") from None
    if hashlib.sha256(recording.read_bytes()).hexdigest() != digest:
        raise SystemExit("Demonstration recording hash does not match its evidence.")
    return value


def _platform_payload(name: str, manifest: EvidenceManifest) -> dict[str, object]:
    return {
        "name": name,
        "platform": manifest.identity.platform,
        "codex_version": manifest.codex_version,
        "evidence_key": manifest.evidence_key,
        "duration_ms": sum(item.duration_ms for item in manifest.results),
        "results": [
            {
                "gate_id": item.gate_id,
                "status": item.status.value,
                "duration_ms": item.duration_ms,
                "result_log": item.result_log,
            }
            for item in manifest.results
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import tarfile
import zipfile
from pathlib import Path


def _members(path: Path) -> tuple[str, ...]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return tuple(archive.namelist())
    with tarfile.open(path, "r:gz") as archive:
        return tuple(member.name for member in archive.getmembers())


def _read_wheel(path: Path, suffix: str) -> str:
    with zipfile.ZipFile(path) as archive:
        member = next(name for name in archive.namelist() if name.endswith(suffix))
        return archive.read(member).decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    wheels = tuple(args.dist.glob("devloop_codexcli-0.1.0-*.whl"))
    sdists = tuple(args.dist.glob("devloop_codexcli-0.1.0.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit("Expected exactly one v0.1.0 wheel and one v0.1.0 sdist.")
    expected = {wheels[0], sdists[0]}
    release_archives = {
        *args.dist.glob("devloop_codexcli-*.whl"),
        *args.dist.glob("devloop_codexcli-*.tar.gz"),
    }
    unexpected = sorted(path.name for path in release_archives - expected)
    if unexpected:
        raise SystemExit(f"Unexpected distribution archives: {', '.join(unexpected)}.")
    wheel = wheels[0]
    metadata = _read_wheel(wheel, ".dist-info/METADATA")
    entry_points = _read_wheel(wheel, ".dist-info/entry_points.txt")
    if "Version: 0.1.0" not in metadata:
        raise SystemExit("Wheel metadata does not identify v0.1.0.")
    forbidden_metadata = ("devloop-plan", "install more from GitHub")
    if any(token in metadata for token in forbidden_metadata):
        raise SystemExit("Wheel metadata contains legacy bundle instructions.")
    if "codexcli = devloop.entrypoint:main" not in entry_points:
        raise SystemExit("Wheel does not expose the codexcli command.")
    if "codexcli-gate = devloop.verification.cli:main" not in entry_points:
        raise SystemExit("Wheel does not expose the versioned verification tiers.")
    finalization_entry_point = (
        "workspace-finalization = devloop.components.finalization:finalization_component"
    )
    if finalization_entry_point not in entry_points:
        raise SystemExit("Wheel does not register workspace finalization.")
    forbidden = ("docs/adr/", "docs/prd/", "/docs/adr/", "/docs/prd/")
    allowed_root_runtime_files = {
        "devloop/__init__.py",
        "devloop/entrypoint.py",
        "devloop/version.py",
    }
    for artifact in (wheel, sdists[0]):
        members = _members(artifact)
        if any(token in name.replace("\\", "/") for name in members for token in forbidden):
            raise SystemExit(f"{artifact.name} contains local ADR or PRD content.")
        root_runtime_files = {
            "/".join(parts[-2:])
            for name in members
            if len(parts := name.replace("\\", "/").split("/")) >= 2
            and parts[-2] == "devloop"
            and parts[-1].endswith(".py")
        }
        unexpected_runtime_files = sorted(
            root_runtime_files - allowed_root_runtime_files
        )
        if unexpected_runtime_files:
            raise SystemExit(
                f"{artifact.name} contains deferred legacy runtime files: "
                f"{', '.join(unexpected_runtime_files)}."
            )
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        print(f"PASS {artifact.name} sha256={digest} files={len(members)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

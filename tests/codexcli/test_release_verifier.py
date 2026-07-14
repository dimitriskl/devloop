from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
VERIFY_RELEASE = REPOSITORY_ROOT / "install" / "verify-release.py"


def _write_valid_release_artifacts(
    dist: Path,
    *,
    extra_runtime_file: str | None = None,
    metadata_suffix: str = "",
) -> None:
    wheel = dist / "devloop_codexcli-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "devloop_codexcli-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: devloop-codexcli\nVersion: 0.1.0\n"
            f"{metadata_suffix}",
        )
        archive.writestr(
            "devloop_codexcli-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\n"
            "codexcli = devloop.entrypoint:main\n\n"
            "[devloop.step_components]\n"
            "workspace-finalization = "
            "devloop.components.finalization:finalization_component\n",
        )
        if extra_runtime_file is not None:
            archive.writestr(f"devloop/{extra_runtime_file}", "")
    sdist = dist / "devloop_codexcli-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        content = b"Metadata-Version: 2.4\nName: devloop-codexcli\nVersion: 0.1.0\n"
        member = tarfile.TarInfo("devloop_codexcli-0.1.0/PKG-INFO")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
        if extra_runtime_file is not None:
            runtime_member = tarfile.TarInfo(
                f"devloop_codexcli-0.1.0/src/devloop/{extra_runtime_file}"
            )
            runtime_member.size = 0
            archive.addfile(runtime_member, io.BytesIO())


def test_release_verifier_rejects_unexpected_distribution_archives(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_valid_release_artifacts(dist)
    (dist / "devloop_codexcli-0.0.1-py3-none-any.whl").write_bytes(b"stale")

    result = subprocess.run(
        [sys.executable, str(VERIFY_RELEASE), "--dist", str(dist)],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Unexpected distribution archives" in result.stderr


def test_release_verifier_rejects_any_legacy_root_runtime_module(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_valid_release_artifacts(dist, extra_runtime_file="state.py")

    result = subprocess.run(
        [sys.executable, str(VERIFY_RELEASE), "--dist", str(dist)],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "deferred legacy runtime files: devloop/state.py" in result.stderr


def test_release_verifier_rejects_legacy_bundle_metadata(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_valid_release_artifacts(
        dist,
        metadata_suffix="\nRun devloop-plan and install more from GitHub.\n",
    )

    result = subprocess.run(
        [sys.executable, str(VERIFY_RELEASE), "--dist", str(dist)],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "legacy bundle instructions" in result.stderr

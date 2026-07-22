from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install" / "devloop.sh"
INSTALL_PS1 = ROOT / "install" / "devloop.ps1"


class BundleInstallerScriptTests(unittest.TestCase):
    def test_unix_installer_has_valid_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(INSTALL_SH)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unix_installer_help_exits_zero(self) -> None:
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Install or update the portable Dev Loop bundle.", result.stdout)

    def test_unix_installer_installs_local_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_dir = Path(raw) / "bundle"
            bin_dir = Path(raw) / "bin"
            env = os.environ.copy()
            env.update(
                {
                    "DEVLOOP_INSTALL_DIR": str(install_dir),
                    "DEVLOOP_BIN_DIR": str(bin_dir),
                    "DEVLOOP_REPO_URL": f"file://{ROOT.as_posix()}",
                    "DEVLOOP_REF": "HEAD",
                }
            )
            result = subprocess.run(
                ["bash", str(INSTALL_SH), "--no-skills"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
                cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue((install_dir / "bin" / "devloop.sh").is_file())
            self.assertTrue((bin_dir / "devloop").is_symlink())
            self.assertTrue((bin_dir / "devloop-plan").is_symlink())

    def test_unix_installer_updates_existing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_dir = Path(raw) / "bundle"
            bin_dir = Path(raw) / "bin"
            env = os.environ.copy()
            env.update(
                {
                    "DEVLOOP_INSTALL_DIR": str(install_dir),
                    "DEVLOOP_BIN_DIR": str(bin_dir),
                    "DEVLOOP_REPO_URL": f"file://{ROOT.as_posix()}",
                    "DEVLOOP_REF": "HEAD",
                }
            )
            first = subprocess.run(
                ["bash", str(INSTALL_SH), "--no-skills", "--no-bin-links"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
                cwd=ROOT,
            )
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)

            marker = install_dir / ".install-marker"
            marker.write_text("updated", encoding="utf-8")

            second = subprocess.run(
                ["bash", str(INSTALL_SH), "--no-skills", "--no-bin-links"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
                cwd=ROOT,
            )
            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
            self.assertFalse(marker.exists(), "update should replace the checkout")
            self.assertIn("Updating existing install", second.stdout)

    def test_unix_installer_requires_install_dir_when_non_interactive(self) -> None:
        env = os.environ.copy()
        env.pop("DEVLOOP_INSTALL_DIR", None)
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--no-skills", "--no-bin-links"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Install directory is required", result.stderr)

    def test_unix_installer_rejects_non_git_install_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_dir = Path(raw) / "bundle"
            install_dir.mkdir()
            (install_dir / "README.md").write_text("not a git checkout", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "DEVLOOP_INSTALL_DIR": str(install_dir),
                    "DEVLOOP_BIN_DIR": str(Path(raw) / "bin"),
                    "DEVLOOP_REPO_URL": f"file://{ROOT.as_posix()}",
                    "DEVLOOP_REF": "HEAD",
                }
            )
            result = subprocess.run(
                ["bash", str(INSTALL_SH), "--no-skills", "--no-bin-links"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
                cwd=ROOT,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a git checkout", result.stderr)


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is required")
class BundleInstallerPowerShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.powershell = "pwsh" if shutil.which("pwsh") else "powershell"

    def test_windows_installer_help_exits_zero(self) -> None:
        result = subprocess.run(
            [self.powershell, "-NoProfile", "-File", str(INSTALL_PS1), "-Help"],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Install or update the portable Dev Loop bundle.", result.stdout)


@unittest.skipUnless(
    os.name == "nt" and (shutil.which("pwsh") or shutil.which("powershell")),
    "Windows PowerShell is required",
)
class BundleInstallerWindowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.powershell = "pwsh" if shutil.which("pwsh") else "powershell"

    def _run_installer(
        self,
        install_dir: str,
        fake_git: str,
        *,
        ref: str = "main",
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as raw:
            test_root = Path(raw)
            fake_bin = test_root / "bin"
            fake_bin.mkdir()
            (fake_bin / "git.cmd").write_text(fake_git, encoding="utf-8")
            marker = test_root / "git-invocations.log"

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["GIT_MARKER"] = str(marker)
            result = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-File",
                    str(INSTALL_PS1),
                    "-InstallDir",
                    install_dir,
                    "-RepoUrl",
                    "https://github.com/example/devloop.git",
                    "-Ref",
                    ref,
                    "-NoSkills",
                    "-NoBinLinks",
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=ROOT,
                env=env,
            )
            invocations = (
                marker.read_text(encoding="utf-8").splitlines()
                if marker.exists()
                else []
            )
            return result, invocations

    def _find_unavailable_drive_root(self) -> str:
        command = """
$root = 68..90 |
    ForEach-Object { '{0}:\\' -f [char]$_ } |
    Where-Object { -not (Test-Path -LiteralPath $_ -PathType Container -ErrorAction SilentlyContinue) } |
    Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($root)) { exit 1 }
Write-Output $root
"""
        result = subprocess.run(
            [self.powershell, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
        )
        if result.returncode != 0:
            self.skipTest("No unavailable Windows drive letter is available for this test")
        return result.stdout.strip()

    def test_windows_installer_rejects_unavailable_drive_before_git(self) -> None:
        drive_root = self._find_unavailable_drive_root()
        install_dir = f"{drive_root}devloop"
        result, invocations = self._run_installer(
            install_dir,
            """@echo off
echo invoked>>"%GIT_MARKER%"
exit /b 99
""",
        )

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(invocations, [])
        self.assertIn(f"Install drive '{drive_root}' is not available", output)
        self.assertIn(install_dir, output)

    def test_windows_installer_rejects_parent_that_cannot_be_created(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            blocked_parent = Path(raw) / "parent-file"
            blocked_parent.write_text("not a directory", encoding="utf-8")
            install_dir = str(blocked_parent / "bundle")
            result, invocations = self._run_installer(
                install_dir,
                """@echo off
echo invoked>>"%GIT_MARKER%"
exit /b 99
""",
            )

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(invocations, [])
        self.assertIn("cannot be created or written", output)
        self.assertIn(str(blocked_parent), output)

    def test_windows_installer_does_not_retry_filesystem_clone_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_dir = str(Path(raw) / "bundle")
            result, invocations = self._run_installer(
                install_dir,
                """@echo off
echo invoked>>"%GIT_MARKER%"
if /I "%~4"=="--branch" mkdir "%~7"
echo fatal: simulated permission failure 1>&2
exit /b 128
""",
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(invocations), 1)
            self.assertEqual(output.count("fatal: simulated permission failure"), 1)
            self.assertIn(install_dir, output)
            self.assertFalse(Path(install_dir).exists())

    def test_windows_installer_clones_available_destination_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_dir = str(Path(raw) / "bundle")
            result, invocations = self._run_installer(
                install_dir,
                r"""@echo off
echo %*>>"%GIT_MARKER%"
mkdir "%~7"
mkdir "%~7\.git"
exit /b 0
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(len(invocations), 1)

    def test_windows_installer_preserves_existing_update_flow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_dir = Path(raw) / "bundle"
            (install_dir / ".git").mkdir(parents=True)
            result, invocations = self._run_installer(
                str(install_dir),
                """@echo off
echo %*>>"%GIT_MARKER%"
exit /b 0
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(len(invocations), 3)
        self.assertIn("Updating existing install", result.stdout)

    def test_windows_installer_keeps_ref_fallback_for_missing_remote_ref(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_dir = str(Path(raw) / "bundle")
            result, invocations = self._run_installer(
                install_dir,
                r"""@echo off
echo %*>>"%GIT_MARKER%"
if /I "%~1"=="clone" (
    if /I "%~4"=="--branch" (
        echo warning: Could not find remote branch missing-ref to clone. 1>&2
        echo fatal: Remote branch missing-ref not found in upstream origin 1>&2
        exit /b 128
    )
    mkdir "%~5"
    mkdir "%~5\.git"
    exit /b 0
)
exit /b 0
""",
                ref="missing-ref",
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(len(invocations), 3)


if __name__ == "__main__":
    unittest.main()

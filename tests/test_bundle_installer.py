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
                    "DEVLOOP_TESTING": "1",
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
                    "DEVLOOP_TESTING": "1",
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
                    "DEVLOOP_TESTING": "1",
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


class PortableRuntimePackagingTests(unittest.TestCase):
    def test_runtime_lock_pins_textual_and_every_transitive_dependency(self) -> None:
        lock = (ROOT / "requirements-portable.lock").read_text(encoding="utf-8")

        self.assertIn("textual==8.2.8", lock)
        self.assertNotIn(">=", lock)
        self.assertEqual(
            len([line for line in lock.splitlines() if line and not line.startswith("#")]),
            10,
        )

    def test_launchers_use_only_the_bundle_runtime_and_do_not_print_a_logo(self) -> None:
        launchers = {
            "powershell-runner": (ROOT / "bin" / "devloop.ps1").read_text(
                encoding="utf-8"
            ),
            "powershell-planner": (ROOT / "bin" / "devloop-plan.ps1").read_text(
                encoding="utf-8"
            ),
            "shell-runner": (ROOT / "bin" / "devloop.sh").read_text(
                encoding="utf-8"
            ),
            "shell-planner": (ROOT / "bin" / "devloop-plan.sh").read_text(
                encoding="utf-8"
            ),
        }

        for launcher in launchers.values():
            self.assertIn(".venv", launcher)
            self.assertNotIn("devloop.logo", launcher)

        for name, launcher in launchers.items():
            with self.subTest(name=name):
                self.assertIn("DEVLOOP_UI_MODE", launcher)

        for launcher in (
            launchers["powershell-runner"],
            launchers["powershell-planner"],
        ):
            self.assertIn("[Console]::IsInputRedirected", launcher)
            self.assertIn("[Console]::IsOutputRedirected", launcher)

        for launcher in (
            launchers["shell-runner"],
            launchers["shell-planner"],
        ):
            self.assertIn("-t 0", launcher)
            self.assertIn("-t 1", launcher)

    def test_installers_stage_and_validate_a_replacement_runtime(self) -> None:
        installers = (
            INSTALL_PS1.read_text(encoding="utf-8"),
            INSTALL_SH.read_text(encoding="utf-8"),
        )

        for installer in installers:
            self.assertIn(".venv.next", installer)
            self.assertIn("requirements-portable.lock", installer)
            self.assertIn("textual.__version__", installer)


if __name__ == "__main__":
    unittest.main()

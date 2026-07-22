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
UNINSTALL_SH = ROOT / "install" / "uninstall-devloop.sh"
UNINSTALL_PS1 = ROOT / "install" / "uninstall-devloop.ps1"
DEVELOPMENT_SETUP_SH = ROOT / "install" / "setup-development.sh"
DEVELOPMENT_SETUP_PS1 = ROOT / "install" / "setup-development.ps1"


@unittest.skipIf(os.name == "nt", "Unix installer behavior requires a POSIX host")
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
            [
                "bash",
                str(INSTALL_SH),
                "--bin-dir",
                "ignored",
                "--no-bin-links",
                "--help",
            ],
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
            self.assertFalse(bin_dir.exists())

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
                ["bash", str(INSTALL_SH), "--no-skills"],
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
                ["bash", str(INSTALL_SH), "--no-skills"],
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
            ["bash", str(INSTALL_SH), "--no-skills"],
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
                ["bash", str(INSTALL_SH), "--no-skills"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
                cwd=ROOT,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a git checkout", result.stderr)

    def test_unix_wrapper_bootstraps_a_missing_local_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw) / "bundle"
            wrapper = bundle / "bin" / "devloop-plan.sh"
            wrapper.parent.mkdir(parents=True)
            shutil.copy2(ROOT / "bin" / "devloop-plan.sh", wrapper)
            setup = bundle / "install" / "setup-development.sh"
            setup.parent.mkdir(parents=True)
            setup.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
bundle="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$bundle/.venv/bin"
printf '#!/usr/bin/env bash\\nprintf "fake-python-started\\n"\\n' > "$bundle/.venv/bin/python"
chmod +x "$bundle/.venv/bin/python"
touch "$bundle/install/bootstrap-ran"
""",
                encoding="utf-8",
            )

            result = subprocess.run(
                ["bash", str(wrapper), "--help"],
                capture_output=True,
                text=True,
                check=False,
                cwd=bundle,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue((bundle / "install" / "bootstrap-ran").is_file())
            self.assertIn("preparing the checkout-local runtime", result.stdout)
            self.assertIn("fake-python-started", result.stdout)

    def test_unix_uninstaller_removes_managed_artifacts_but_keeps_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_dir = root / "development-checkout"
            bin_dir = root / "bin"
            runtime = install_dir / ".venv"
            runtime.mkdir(parents=True)
            (runtime / "runtime.txt").write_text("installed", encoding="utf-8")
            (install_dir / "keep-source.txt").write_text("source", encoding="utf-8")
            bin_dir.mkdir()
            (bin_dir / "devloop").symlink_to(install_dir / "bin" / "devloop.sh")
            (bin_dir / "devloop-plan").symlink_to(
                install_dir / "bin" / "devloop-plan.sh"
            )
            unrelated = bin_dir / "keep"
            unrelated.write_text("keep", encoding="utf-8")

            result = subprocess.run(
                [
                    "bash",
                    str(UNINSTALL_SH),
                    "--dir",
                    str(install_dir),
                    "--bin-dir",
                    str(bin_dir),
                    "--keep-skills",
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=ROOT,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse(runtime.exists())
            self.assertFalse((bin_dir / "devloop").exists())
            self.assertFalse((bin_dir / "devloop-plan").exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue((install_dir / "keep-source.txt").exists())


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is required")
class BundleInstallerPowerShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.powershell = (
            "powershell"
            if os.name == "nt" and shutil.which("powershell")
            else "pwsh"
        )
        self.powershell_command = [
            self.powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
        ]

    def test_windows_installer_help_exits_zero(self) -> None:
        result = subprocess.run(
            [
                *self.powershell_command,
                "-File",
                str(INSTALL_PS1),
                "-BinDir",
                "ignored",
                "-NoBinLinks",
                "-Help",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Install or update the portable Dev Loop bundle.", result.stdout)

    def test_windows_development_setup_help_exits_zero(self) -> None:
        result = subprocess.run(
            [
                *self.powershell_command,
                "-File",
                str(DEVELOPMENT_SETUP_PS1),
                "-Help",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prepare this development checkout", result.stdout)

    def test_windows_wrapper_bootstraps_a_missing_local_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw) / "bundle"
            wrapper = bundle / "bin" / "devloop-plan.ps1"
            wrapper.parent.mkdir(parents=True)
            shutil.copy2(ROOT / "bin" / "devloop-plan.ps1", wrapper)
            setup = bundle / "install" / "setup-development.ps1"
            setup.parent.mkdir(parents=True)
            setup.write_text(
                "\n".join(
                    (
                        "$runtime = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\\Scripts'",
                        "New-Item -ItemType Directory -Force -Path $runtime | Out-Null",
                        "Copy-Item -LiteralPath $env:ComSpec -Destination (Join-Path $runtime 'python.exe')",
                        "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'bootstrap-ran') -Value 'yes'",
                    )
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [*self.powershell_command, "-File", str(wrapper), "-Help"],
                capture_output=True,
                text=True,
                check=False,
                cwd=bundle,
            )

            self.assertTrue((bundle / "install" / "bootstrap-ran").is_file())
            self.assertTrue((bundle / ".venv" / "Scripts" / "python.exe").is_file())
            self.assertIn("preparing the checkout-local runtime", result.stdout)
            self.assertNotIn("runtime and bootstrap script are missing", result.stderr)

    def test_windows_uninstaller_removes_managed_artifacts_but_keeps_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_dir = root / "development-checkout"
            bin_dir = root / "bin"
            runtime = install_dir / ".venv"
            runtime.mkdir(parents=True)
            (runtime / "runtime.txt").write_text("installed", encoding="utf-8")
            (install_dir / "keep-source.txt").write_text("source", encoding="utf-8")
            bin_dir.mkdir()
            (bin_dir / "devloop.cmd").write_text(
                '@echo off\npowershell -File "C:\\devloop\\bin\\devloop.ps1" %*\n',
                encoding="ascii",
            )
            (bin_dir / "devloop-plan.cmd").write_text(
                '@echo off\npowershell -File "C:\\devloop\\bin\\devloop-plan.ps1" %*\n',
                encoding="ascii",
            )
            unrelated = bin_dir / "keep.cmd"
            unrelated.write_text("@echo off\necho keep\n", encoding="ascii")
            env = os.environ.copy()
            env["DEVLOOP_TESTING"] = "1"

            result = subprocess.run(
                [
                    *self.powershell_command,
                    "-File",
                    str(UNINSTALL_PS1),
                    "-InstallDir",
                    str(install_dir),
                    "-BinDir",
                    str(bin_dir),
                    "-KeepSkills",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
                cwd=ROOT,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse(runtime.exists())
            self.assertFalse((bin_dir / "devloop.cmd").exists())
            self.assertFalse((bin_dir / "devloop-plan.cmd").exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue((install_dir / "keep-source.txt").exists())

    def test_windows_uninstaller_removes_only_unchanged_installed_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_dir = root / "development-checkout"
            install_dir.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            skills_dir = root / "skills"
            agents_dir = root / "agents"
            matching_skill = skills_dir / "implement" / "SKILL.md"
            matching_skill.parent.mkdir(parents=True)
            shutil.copy2(ROOT / "skills" / "codex" / "implement" / "SKILL.md", matching_skill)
            modified_skill = skills_dir / "tdd" / "SKILL.md"
            modified_skill.parent.mkdir(parents=True)
            modified_skill.write_text("personal changes", encoding="utf-8")
            unrelated_empty_directory = skills_dir / "personal-empty-skill"
            unrelated_empty_directory.mkdir(parents=True)
            matching_agent = agents_dir / "senior-code-reviewer.md"
            matching_agent.parent.mkdir(parents=True)
            shutil.copy2(
                ROOT / "agents" / "codex" / "senior-code-reviewer.md",
                matching_agent,
            )
            env = os.environ.copy()
            env["DEVLOOP_TESTING"] = "1"

            result = subprocess.run(
                [
                    *self.powershell_command,
                    "-File",
                    str(UNINSTALL_PS1),
                    "-InstallDir",
                    str(install_dir),
                    "-BinDir",
                    str(bin_dir),
                    "-CodexSkillsPath",
                    str(skills_dir),
                    "-CodexAgentsPath",
                    str(agents_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
                cwd=ROOT,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse(matching_skill.exists())
            self.assertFalse(matching_agent.exists())
            self.assertTrue(modified_skill.exists())
            self.assertTrue(unrelated_empty_directory.exists())
            self.assertIn("Kept modified capability", result.stdout)

    def test_windows_uninstaller_refuses_a_filesystem_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = subprocess.run(
                [
                    *self.powershell_command,
                    "-File",
                    str(UNINSTALL_PS1),
                    "-InstallDir",
                    Path(raw).anchor,
                    "-BinDir",
                    str(Path(raw) / "bin"),
                    "-KeepSkills",
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=ROOT,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to use filesystem root", result.stderr)


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
            self.assertIn("setup-development", launcher)

        for name, launcher in launchers.items():
            with self.subTest(name=name):
                self.assertIn("DEVLOOP_UI_MODE", launcher)

        for launcher in (
            launchers["powershell-runner"],
            launchers["powershell-planner"],
        ):
            self.assertIn("[Console]::IsInputRedirected", launcher)
            self.assertIn("[Console]::IsOutputRedirected", launcher)
            self.assertIn("& $developmentSetup", launcher)

        for launcher in (
            launchers["shell-runner"],
            launchers["shell-planner"],
        ):
            self.assertIn("-t 0", launcher)
            self.assertIn("-t 1", launcher)
            self.assertIn('bash "$DEVELOPMENT_SETUP"', launcher)

    def test_installers_stage_and_validate_a_replacement_runtime(self) -> None:
        installers = (
            INSTALL_PS1.read_text(encoding="utf-8"),
            INSTALL_SH.read_text(encoding="utf-8"),
        )

        for installer in installers:
            self.assertIn(".venv.next", installer)
            self.assertIn("requirements-portable.lock", installer)
            self.assertIn("textual.__version__", installer)

    def test_installers_never_create_command_shortcuts_or_modify_path(self) -> None:
        powershell_installer = INSTALL_PS1.read_text(encoding="utf-8")
        shell_installer = INSTALL_SH.read_text(encoding="utf-8")

        self.assertNotIn("SetEnvironmentVariable('Path'", powershell_installer)
        self.assertNotIn(".cmd", powershell_installer)
        self.assertNotIn("Install-CommandLaunchers", powershell_installer)

        self.assertNotIn("DEVLOOP_BIN_DIR", shell_installer)
        self.assertNotIn("ln -sf", shell_installer)
        self.assertNotIn("LINK_COMMANDS", shell_installer)
        self.assertNotIn("Linking commands", shell_installer)

    def test_development_setup_is_local_only_and_uninstall_is_available(self) -> None:
        setup_scripts = (
            DEVELOPMENT_SETUP_PS1.read_text(encoding="utf-8"),
            DEVELOPMENT_SETUP_SH.read_text(encoding="utf-8"),
        )
        uninstallers = (
            UNINSTALL_PS1.read_text(encoding="utf-8"),
            UNINSTALL_SH.read_text(encoding="utf-8"),
        )

        for setup in setup_scripts:
            self.assertIn(".venv.next", setup)
            self.assertIn("requirements-portable.lock", setup)
            self.assertIn("textual.__version__", setup)
            self.assertNotIn("install-skills", setup)
            self.assertNotIn("devloop-plan.cmd", setup)
            self.assertNotIn("ln -sf", setup)

        for uninstaller in uninstallers:
            self.assertIn("Source checkout preserved", uninstaller)
            self.assertIn("devloop-plan", uninstaller)
            self.assertIn(".venv.previous", uninstaller)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from devloop.portable_session_catalog import PortableSessionCatalog


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install" / "devloop.ps1"
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required")
class PortableSideBySideInstallTests(unittest.TestCase):
    def test_versioned_bootstrap_lock_rejects_live_owner_and_wrong_transaction(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            install_root = root / "locked bundle"
            transaction = ROOT / "install" / "bootstrap" / "transaction.py"
            holder_code = (
                "import os,subprocess,sys,time; "
                "r=subprocess.run([sys.executable,sys.argv[1],'begin',sys.argv[2],"
                "'install','--owner-pid',str(os.getpid()),'--protocol','2'],"
                "capture_output=True,text=True); print(r.stdout.strip(),flush=True); "
                "raise SystemExit(r.returncode) if r.returncode else time.sleep(60)"
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(transaction), str(install_root)],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert holder.stdout is not None
            transaction_id = holder.stdout.readline().strip()
            self.assertRegex(transaction_id, r"^[0-9a-f-]{36}$")
            blocked = subprocess.run(
                [
                    sys.executable,
                    str(transaction),
                    "begin",
                    str(install_root),
                    "install",
                    "--protocol",
                    "2",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("healthy owner", blocked.stderr)
            wrong = subprocess.run(
                [
                    sys.executable,
                    str(transaction),
                    "recover",
                    str(install_root),
                    str(uuid.uuid4()),
                    "--protocol",
                    "2",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(wrong.returncode, 0)
            self.assertIn("transaction identity does not match", wrong.stderr)
            orphan = root / f".locked bundle.candidate-{transaction_id}"
            orphan.mkdir()
            (orphan / "owned-only.txt").write_text("orphan", encoding="utf-8")
            holder.terminate()
            holder.wait(timeout=10)
            holder.communicate(timeout=1)
            for _ in range(50):
                if holder.poll() is not None:
                    break
                time.sleep(0.02)
            recovered = subprocess.run(
                [
                    sys.executable,
                    str(transaction),
                    "begin",
                    str(install_root),
                    "install",
                    "--protocol",
                    "2",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertFalse(orphan.exists())
            new_id = recovered.stdout.strip()
            aborted = subprocess.run(
                [
                    sys.executable,
                    str(transaction),
                    "abort",
                    str(install_root),
                    new_id,
                    "--protocol",
                    "2",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(aborted.returncode, 0, aborted.stderr)

    def test_committed_layout_reconciles_only_exact_completed_pending_publication(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "pending bundle"
            environment = _environment(root)
            installed = _run_installer(
                INSTALLER, install_root, remote, "previous", environment
            )
            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            layout = json.loads(
                (install_root / "bootstrap" / "layout.json").read_text(encoding="utf-8")
            )
            pending_path = install_root / "bootstrap" / "layout.pending.json"
            pending = {
                **layout,
                "backups_ready": sorted(layout["legacy_backups"]),
                "published": sorted(layout["assets"]),
            }
            pending_path.write_text(json.dumps(pending), encoding="utf-8")
            transaction = install_root / "bootstrap" / "transaction.py"
            begun = subprocess.run(
                [
                    sys.executable,
                    str(transaction),
                    "begin",
                    str(install_root),
                    "install",
                    "--protocol",
                    "2",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(begun.returncode, 0, begun.stderr)
            self.assertFalse(pending_path.exists())
            transaction_id = begun.stdout.strip()
            subprocess.run(
                [
                    sys.executable,
                    str(transaction),
                    "abort",
                    str(install_root),
                    transaction_id,
                    "--protocol",
                    "2",
                ],
                cwd=ROOT,
                check=True,
            )
            pending["published"] = pending["published"][:-1]
            pending_path.write_text(json.dumps(pending), encoding="utf-8")
            rejected = subprocess.run(
                [
                    sys.executable,
                    str(transaction),
                    "begin",
                    str(install_root),
                    "install",
                    "--protocol",
                    "2",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("does not match committed layout", rejected.stderr)
            self.assertTrue(pending_path.exists())

    def test_pointer_commit_requires_the_exact_prepared_baseline_and_transaction_id(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "cas bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            second = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
            interrupted_environment = environment.copy()
            interrupted_environment["DEVLOOP_TEST_INTERRUPT_AFTER_PHASE"] = "adopted"
            interrupted = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "third",
                interrupted_environment,
            )
            self.assertEqual(interrupted.returncode, 91, interrupted.stderr or interrupted.stdout)
            pointer_path = install_root / "bootstrap" / "current.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["current"], pointer["previous"] = pointer["previous"], pointer["current"]
            pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
            journal = json.loads(
                (install_root / "bootstrap" / "install-transaction.json").read_text(
                    encoding="utf-8"
                )
            )
            wrong = subprocess.run(
                [
                    sys.executable,
                    str(install_root / "bootstrap" / "transaction.py"),
                    "commit",
                    str(install_root),
                    str(uuid.uuid4()),
                    "--protocol",
                    "2",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(wrong.returncode, 0)
            self.assertIn("transaction identity does not match", wrong.stderr)
            retry = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "third",
                environment,
            )
            self.assertNotEqual(retry.returncode, 0)
            self.assertIn("authoritative pointer changed", retry.stderr)
            self.assertTrue(
                (install_root / "releases" / str(journal["commit"])).is_dir(),
                "failed CAS must retain the prepared release for inspection",
            )

    def test_uninstall_resumes_after_every_durable_action_boundary(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            template = root / "uninstall template"
            environment = _environment(root)
            installed = _run_installer(INSTALLER, template, remote, "previous", environment)
            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            victim = root / "victim.bin"
            victim.write_bytes(b"outside-install\x00\xff")

            probe = root / "uninstall probe"
            shutil.copytree(template, probe)
            transaction = probe / "bootstrap" / "transaction.py"
            transaction_id = _begin_transaction(transaction, probe, "uninstall")
            interrupted_environment = environment.copy()
            interrupted_environment["DEVLOOP_TEST_INTERRUPT_UNINSTALL"] = (
                "before:000-remove_release"
            )
            interrupted = _run_transaction_uninstall(
                transaction, probe, transaction_id, interrupted_environment
            )
            self.assertEqual(interrupted.returncode, 95, interrupted.stderr)
            staging = root / f".uninstall probe.uninstall-{transaction_id}"
            journal = json.loads((staging / "journal.json").read_text(encoding="utf-8"))
            action_ids = tuple(str(action["id"]) for action in journal["actions"])
            self.assertGreaterEqual(len(action_ids), 16)

            selected_boundary = os.environ.get("DEVLOOP_TEST_ONLY_UNINSTALL_BOUNDARY")
            for number, action_id in enumerate(action_ids):
                for position in ("before", "after"):
                    if selected_boundary and selected_boundary != f"{position}:{action_id}":
                        continue
                    with self.subTest(action=action_id, position=position):
                        install_root = root / f"uninstall-{number}-{position}"
                        shutil.copytree(template, install_root)
                        transaction = install_root / "bootstrap" / "transaction.py"
                        current_id = _begin_transaction(
                            transaction, install_root, "uninstall"
                        )
                        crash_environment = environment.copy()
                        crash_environment["DEVLOOP_TEST_INTERRUPT_UNINSTALL"] = (
                            f"{position}:{action_id}"
                        )
                        crashed = _run_transaction_uninstall(
                            transaction, install_root, current_id, crash_environment
                        )
                        self.assertEqual(crashed.returncode, 95, crashed.stderr)
                        external = (
                            root
                            / f".{install_root.name}.uninstall-{current_id}"
                            / "transaction.py"
                        )
                        retry = _run_transaction_uninstall(
                            external, install_root, current_id, environment
                        )
                        self.assertEqual(retry.returncode, 0, retry.stderr)
                        self.assertFalse((install_root / "releases").exists())
                        self.assertFalse(
                            (root / f".{install_root.name}.uninstall-{current_id}").exists()
                        )
                        self.assertEqual(victim.read_bytes(), b"outside-install\x00\xff")

    def test_candidate_creation_and_clone_crashes_reconcile_owned_orphans(self) -> None:
        for boundary in ("candidate_created", "candidate_cloned"):
            with self.subTest(boundary=boundary), _workspace_temporary_directory() as directory:
                root = Path(directory)
                remote = _release_repository(root)
                install_root = root / "orphan bundle"
                environment = _environment(root)
                interrupted_environment = environment.copy()
                interrupted_environment["DEVLOOP_TEST_INTERRUPT_CANDIDATE_BOUNDARY"] = boundary
                interrupted = _run_installer(
                    INSTALLER,
                    install_root,
                    remote,
                    "previous",
                    interrupted_environment,
                )
                self.assertEqual(
                    interrupted.returncode, 92, interrupted.stderr or interrupted.stdout
                )
                self.assertEqual(
                    len(tuple(root.glob(".orphan bundle.candidate-*"))),
                    1,
                    "the crash fixture must expose one owned orphan",
                )
                recovered = _run_installer(
                    INSTALLER, install_root, remote, "previous", environment
                )
                self.assertEqual(recovered.returncode, 0, recovered.stderr or recovered.stdout)
                self.assertFalse(tuple(root.glob(".orphan bundle.candidate-*")))
                self.assertFalse(tuple((install_root / "bootstrap").glob("candidate-*.json")))

    def test_new_bootstrap_migrates_the_committed_v1_pointer_fixture_on_rollback(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "legacy pointer bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            second = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
            pointer_path = install_root / "bootstrap" / "current.json"
            state = json.loads(pointer_path.read_text(encoding="utf-8"))
            current = state["current"]
            previous = state["previous"]
            self.assertIsNotNone(previous)
            pointer_path.write_text(
                json.dumps({"version": 1, **current}), encoding="utf-8"
            )
            (install_root / "bootstrap" / "previous.json").write_text(
                json.dumps({"version": 1, **previous}), encoding="utf-8"
            )
            for pointer in (current, previous):
                manifest = install_root / "bootstrap" / f"release-{pointer['commit']}.json"
                manifest.write_text(
                    json.dumps({"version": 1, **pointer}), encoding="utf-8"
                )
            rolled_back = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
                rollback=True,
            )
            self.assertEqual(
                rolled_back.returncode, 0, rolled_back.stderr or rolled_back.stdout
            )
            migrated = json.loads(pointer_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["version"], 2)
            self.assertEqual(migrated["current"], previous)
            self.assertEqual(migrated["previous"], current)
            self.assertFalse((install_root / "bootstrap" / "previous.json").exists())

    def test_exact_604_bootstrap_updates_to_v2_then_old_release_rolls_back_and_updates(
        self,
    ) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            old_remote = root / "release-604"
            cloned = subprocess.run(
                ["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(old_remote)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr)
            old_snapshot = "6048556f0f279cb54f4d1afa00f764227049eb8f"
            _git(old_remote, "checkout", "--detach", old_snapshot)
            _git(old_remote, "config", "user.email", "devloop@example.invalid")
            _git(old_remote, "config", "user.name", "Dev Loop Tests")
            (old_remote / "src" / "textual.py").write_text(
                '__version__ = "8.2.8"\n', encoding="utf-8"
            )
            _git(old_remote, "add", "src/textual.py")
            _git(old_remote, "commit", "-m", "604 runtime fixture")
            old_commit = _git(old_remote, "rev-parse", "HEAD")
            remote = _release_repository(root)
            candidate_commit = _git(remote, "rev-parse", "candidate^{commit}")
            install_root = root / "cross-version bundle"
            environment = _environment(root)

            installed_v1 = _run_installer(
                old_remote / "install" / "devloop.ps1",
                install_root,
                old_remote,
                old_commit,
                environment,
                timeout=300,
            )
            self.assertEqual(installed_v1.returncode, 0, installed_v1.stderr or installed_v1.stdout)
            legacy_layout = json.loads(
                (install_root / "bootstrap" / "layout.json").read_text(encoding="utf-8")
            )
            self.assertEqual(legacy_layout["version"], 1)
            self.assertEqual(_pointer(install_root)["commit"], old_commit)
            self.assertEqual(
                (install_root / "bootstrap" / "transaction.py").read_bytes(),
                (old_remote / "install" / "bootstrap" / "transaction.py").read_bytes(),
            )

            updated = _run_installer(
                INSTALLER, install_root, remote, "candidate", environment
            )
            self.assertEqual(updated.returncode, 0, updated.stderr or updated.stdout)
            self.assertEqual(_pointer(install_root)["commit"], candidate_commit)
            migrated_layout = json.loads(
                (install_root / "bootstrap" / "layout.json").read_text(encoding="utf-8")
            )
            self.assertEqual(migrated_layout["version"], 2)

            stable_driver = install_root / "install" / "devloop.ps1"
            rolled_back = _run_installer(
                stable_driver,
                install_root,
                remote,
                "candidate",
                environment,
                rollback=True,
            )
            self.assertEqual(
                rolled_back.returncode, 0, rolled_back.stderr or rolled_back.stdout
            )
            self.assertEqual(_pointer(install_root)["commit"], old_commit)

            updated_from_old_release = _run_installer(
                stable_driver, install_root, remote, "candidate", environment
            )
            self.assertEqual(
                updated_from_old_release.returncode,
                0,
                updated_from_old_release.stderr or updated_from_old_release.stdout,
            )
            self.assertEqual(_pointer(install_root)["commit"], candidate_commit)

    def test_release_driver_contains_no_pointer_manifest_or_journal_codec(self) -> None:
        windows = (ROOT / "install" / "devloop.ps1").read_text(encoding="utf-8")
        posix = (ROOT / "install" / "devloop.sh").read_text(encoding="utf-8")
        for driver in (windows, posix):
            self.assertNotIn("Write-DurableJson", driver)
            self.assertNotIn("previous_pointer", driver)
            self.assertNotIn("tracked_fingerprint", driver)
            self.assertNotIn("release-<commit>", driver)
            self.assertIn("--protocol", driver)
            self.assertIn("bootstrap/transaction.py", driver.replace("\\", "/"))

    @unittest.skipUnless(GIT_BASH.is_file(), "Git Bash is required")
    def test_posix_fresh_install_and_installed_update_use_the_same_layout(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "posix bundle"
            environment = _environment(root)
            environment["HOME"] = str(root / "home")
            environment["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{environment['PATH']}"
            first = _run_posix_installer(
                ROOT / "install" / "devloop.sh",
                install_root,
                remote,
                "previous",
                environment,
            )
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            old_pointer = _pointer(install_root)
            same = _run_posix_installer(
                install_root / "install" / "devloop.sh",
                install_root,
                remote,
                "previous",
                environment,
            )
            self.assertEqual(same.returncode, 0, same.stderr or same.stdout)
            self.assertEqual(len(tuple((install_root / "releases").iterdir())), 1)
            second = _run_posix_installer(
                install_root / "install" / "devloop.sh",
                install_root,
                remote,
                "candidate",
                environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
            self.assertNotEqual(_pointer(install_root)["commit"], old_pointer["commit"])
            self.assertTrue((install_root / old_pointer["release_path"]).is_dir())
            rollback = _run_posix_installer(
                install_root / "install" / "devloop.sh",
                install_root,
                remote,
                "candidate",
                environment,
                rollback=True,
            )
            self.assertEqual(rollback.returncode, 0, rollback.stderr or rollback.stdout)
            self.assertEqual(_pointer(install_root), old_pointer)
            roll_forward = _run_posix_installer(
                install_root / "install" / "devloop.sh",
                install_root,
                remote,
                "candidate",
                environment,
                rollback=True,
            )
            self.assertEqual(roll_forward.returncode, 0, roll_forward.stderr or roll_forward.stdout)
            uninstall = subprocess.run(
                [
                    str(GIT_BASH),
                    _git_bash_path(install_root / "install" / "uninstall-devloop.sh"),
                    "--dir",
                    _git_bash_path(install_root),
                    "--keep-skills",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=90,
            )
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr or uninstall.stdout)
            self.assertFalse((install_root / "releases").exists())

    def test_fresh_install_and_installed_update_switch_one_atomic_pointer(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "installed bundle with spaces"
            environment = _environment(root)

            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            first_pointer = _pointer(install_root)
            first_release = install_root / first_pointer["release_path"]
            self.assertTrue(first_release.is_dir())
            self.assertEqual(_git(first_release, "rev-parse", "HEAD"), first_pointer["commit"])
            stable_updater = install_root / "install" / "devloop.ps1"
            stable_updater_bytes = stable_updater.read_bytes()
            same = _run_installer(stable_updater, install_root, remote, "previous", environment)
            self.assertEqual(same.returncode, 0, same.stderr or same.stdout)
            self.assertEqual(len(tuple((install_root / "releases").iterdir())), 1)

            second = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )

            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
            second_pointer = _pointer(install_root)
            self.assertNotEqual(second_pointer["commit"], first_pointer["commit"])
            self.assertTrue(first_release.is_dir(), "the prior release is rollback evidence")
            self.assertEqual(
                (install_root / second_pointer["release_path"] / "release-marker").read_text(
                    encoding="utf-8"
                ),
                "candidate\n",
            )
            third = _run_installer(stable_updater, install_root, remote, "third", environment)
            self.assertEqual(third.returncode, 0, third.stderr or third.stdout)
            self.assertIn("Staging through changed release driver", third.stdout)
            self.assertEqual(stable_updater.read_bytes(), stable_updater_bytes)
            third_pointer = _pointer(install_root)
            current_release = install_root / third_pointer["release_path"]
            self.assertEqual(
                (current_release / "release-marker").read_text(encoding="utf-8"),
                "third\n",
            )
            rollback = _run_installer(
                stable_updater,
                install_root,
                remote,
                "third",
                environment,
                rollback=True,
            )
            self.assertEqual(rollback.returncode, 0, rollback.stderr or rollback.stdout)
            self.assertEqual(_pointer(install_root), second_pointer)
            roll_forward = _run_installer(
                stable_updater,
                install_root,
                remote,
                "third",
                environment,
                rollback=True,
            )
            self.assertEqual(roll_forward.returncode, 0, roll_forward.stderr or roll_forward.stdout)
            self.assertEqual(_pointer(install_root), third_pointer)

    def test_legacy_dirty_checkout_is_preserved_and_managed_entrypoints_are_restorable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "legacy"
            subprocess.run(
                ["git", "clone", str(remote), str(install_root)], check=True, capture_output=True
            )
            tracked = install_root / "release-marker"
            tracked.write_bytes(b"operator tracked edit\r\n\x00\xff")
            custom_launcher = install_root / "bin" / "devloop.ps1"
            custom_launcher.write_bytes(b"operator launcher\r\n")
            untracked = install_root / "operator-note.bin"
            untracked.write_bytes(b"untracked\x00\xfe")
            environment = _environment(root)

            result = _run_installer(INSTALLER, install_root, remote, "candidate", environment)

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(tracked.read_bytes(), b"operator tracked edit\r\n\x00\xff")
            self.assertEqual(untracked.read_bytes(), b"untracked\x00\xfe")
            self.assertEqual(
                (install_root / "bootstrap" / "legacy-assets" / "bin" / "devloop.ps1").read_bytes(),
                b"operator launcher\r\n",
            )
            uninstall = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(install_root / "install" / "uninstall-devloop.ps1"),
                    "-InstallDir",
                    str(install_root),
                    "-KeepSkills",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr or uninstall.stdout)
            self.assertFalse((install_root / "releases").exists())
            self.assertEqual(custom_launcher.read_bytes(), b"operator launcher\r\n")
            self.assertEqual(tracked.read_bytes(), b"operator tracked edit\r\n\x00\xff")
            self.assertEqual(untracked.read_bytes(), b"untracked\x00\xfe")

    def test_interrupted_update_recovers_each_durable_phase(self) -> None:
        for phase in (
            "journaled",
            "owned",
            "manifested",
            "prepared",
            "candidate_moved",
            "release_ready",
            "adopted",
            "pointer_swapped",
            "switched",
            "committed",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                remote = _release_repository(root)
                install_root = root / "bundle"
                environment = _environment(root)
                first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
                self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
                old_pointer = _pointer(install_root)
                interrupted_environment = environment.copy()
                interrupted_environment["DEVLOOP_TEST_INTERRUPT_AFTER_PHASE"] = phase

                interrupted = _run_installer(
                    install_root / "install" / "devloop.ps1",
                    install_root,
                    remote,
                    "candidate",
                    interrupted_environment,
                )
                self.assertEqual(
                    interrupted.returncode,
                    91,
                    interrupted.stderr or interrupted.stdout,
                )
                if phase in {
                    "journaled",
                    "owned",
                    "manifested",
                    "prepared",
                    "candidate_moved",
                    "release_ready",
                    "adopted",
                }:
                    self.assertEqual(_pointer(install_root), old_pointer)
                self.assertTrue(
                    (install_root / old_pointer["release_path"]).is_dir(),
                    "the previous current release remains present",
                )

                recovered = _run_installer(
                    install_root / "install" / "devloop.ps1",
                    install_root,
                    remote,
                    "candidate",
                    environment,
                )
                self.assertEqual(recovered.returncode, 0, recovered.stderr or recovered.stdout)
                current_release = install_root / _pointer(install_root)["release_path"]
                self.assertEqual(
                    (current_release / "release-marker").read_text(encoding="utf-8"),
                    "candidate\n",
                )
                self.assertFalse((install_root / "bootstrap" / "install-transaction.json").exists())
                self.assertFalse(tuple(install_root.parent.glob(".bundle.candidate-*")))
                self.assertFalse(tuple((install_root / "bootstrap").glob("candidate-*.json")))

    def test_corrupt_current_pointer_fails_closed_without_touching_victim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            victim = root / "victim"
            victim.mkdir()
            victim_file = victim / "keep.bin"
            victim_file.write_bytes(b"victim\x00\xff")
            pointer_path = install_root / "bootstrap" / "current.json"
            pointer_state = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer_state["current"]["release_path"] = "../victim"
            pointer_path.write_text(json.dumps(pointer_state), encoding="utf-8")

            result = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-canonical release path", result.stderr)
            self.assertEqual(victim_file.read_bytes(), b"victim\x00\xff")

    def test_candidate_mutation_fails_before_pointer_switch(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            old_pointer = _pointer(install_root)
            raced = environment.copy()
            raced["DEVLOOP_TEST_MUTATE_CANDIDATE_AFTER_VALIDATION"] = "1"

            result = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                raced,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release has tracked changes", result.stderr)
            self.assertEqual(_pointer(install_root), old_pointer)

    def test_concurrent_edit_to_previous_release_is_retained_after_switch(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            old_pointer = _pointer(install_root)
            raced = environment.copy()
            raced["DEVLOOP_TEST_MUTATE_CURRENT_AFTER_FINAL_CHECK"] = "1"

            result = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                raced,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(
                (install_root / old_pointer["release_path"] / "operator-race.bin").read_bytes(),
                b"race\x00\xff",
            )

    def test_capability_failure_is_postcommit_and_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            environment["DEVLOOP_TEST_CAPABILITY_FAILURE"] = "1"

            result = _run_installer(
                INSTALLER,
                install_root,
                remote,
                "candidate",
                environment,
                no_skills=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn("capability installation warning", result.stdout + result.stderr)
            current_release = install_root / _pointer(install_root)["release_path"]
            self.assertEqual(
                (current_release / "release-marker").read_text(encoding="utf-8"),
                "candidate\n",
            )

    def test_uninstall_commits_core_before_nonfatal_capability_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            capability_source = ROOT / "skills" / "codex" / "implement" / "SKILL.md"
            bundled_capability = remote / "skills" / "codex" / "implement" / "SKILL.md"
            bundled_capability.parent.mkdir(parents=True)
            shutil.copy2(capability_source, bundled_capability)
            _git(remote, "add", "skills")
            _git(remote, "commit", "-m", "capability fixture")
            _git(remote, "tag", "capabilities")
            install_root = root / "bundle"
            environment = _environment(root)
            installed = _run_installer(INSTALLER, install_root, remote, "capabilities", environment)
            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            release = install_root / _pointer(install_root)["release_path"]
            source = next(
                path for path in (release / "skills" / "codex").rglob("*") if path.is_file()
            )
            skills = root / "capabilities"
            matching = skills / source.relative_to(release / "skills" / "codex")
            matching.parent.mkdir(parents=True)
            shutil.copy2(source, matching)
            modified = skills / "personal" / "SKILL.md"
            modified.parent.mkdir(parents=True)
            modified.write_text("personal changes\n", encoding="utf-8")
            failed_cleanup_environment = environment.copy()
            failed_cleanup_environment["DEVLOOP_TEST_CAPABILITY_FAILURE"] = "1"

            uninstall = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(install_root / "install" / "uninstall-devloop.ps1"),
                    "-InstallDir",
                    str(install_root),
                    "-CodexSkillsPath",
                    str(skills),
                    "-CodexAgentsPath",
                    str(root / "agents"),
                ],
                cwd=ROOT,
                env=failed_cleanup_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

            self.assertEqual(uninstall.returncode, 0, uninstall.stderr or uninstall.stdout)
            self.assertFalse((install_root / "releases").exists())
            self.assertFalse(matching.exists())
            self.assertTrue(modified.exists())
            self.assertIn("capability cleanup warning", uninstall.stderr)

    def test_power_loss_after_core_uninstall_leaves_external_cleanup_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            capability_source = ROOT / "skills" / "codex" / "implement" / "SKILL.md"
            bundled_capability = remote / "skills" / "codex" / "implement" / "SKILL.md"
            bundled_capability.parent.mkdir(parents=True)
            shutil.copy2(capability_source, bundled_capability)
            _git(remote, "add", "skills")
            _git(remote, "commit", "-m", "capability fixture")
            _git(remote, "tag", "capabilities")
            install_root = root / "bundle"
            environment = _environment(root)
            installed = _run_installer(INSTALLER, install_root, remote, "capabilities", environment)
            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            release = install_root / _pointer(install_root)["release_path"]
            source = next(
                path for path in (release / "skills" / "codex").rglob("*") if path.is_file()
            )
            skills = root / "capabilities"
            matching = skills / source.relative_to(release / "skills" / "codex")
            matching.parent.mkdir(parents=True)
            shutil.copy2(source, matching)
            interrupted_environment = environment.copy()
            interrupted_environment["DEVLOOP_TEST_INTERRUPT_AFTER_CORE_UNINSTALL"] = "1"

            uninstall = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(install_root / "install" / "uninstall-devloop.ps1"),
                    "-InstallDir",
                    str(install_root),
                    "-CodexSkillsPath",
                    str(skills),
                    "-CodexAgentsPath",
                    str(root / "agents"),
                ],
                cwd=ROOT,
                env=interrupted_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

            self.assertEqual(uninstall.returncode, 95, uninstall.stderr or uninstall.stdout)
            self.assertFalse((install_root / "releases").exists())
            self.assertTrue(matching.exists())
            self.assertTrue(tuple(root.glob(".bundle.uninstall-*")))

    def test_failed_adoption_keeps_old_pointer_and_retry_records_one_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            old_pointer = _pointer(install_root)
            target = root / "adoption-target"
            target.mkdir()
            configuration = Path(environment["APPDATA"]) / "DevLoop" / "devloop-plan.json"
            configuration.parent.mkdir(parents=True)
            configuration.write_text(
                json.dumps({"target_repo": str(target), "target_repo_confirmed": True}),
                encoding="utf-8",
            )

            failed = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )

            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(_pointer(install_root), old_pointer)
            catalog = PortableSessionCatalog(
                Path(environment["LOCALAPPDATA"])
                / "DevLoop"
                / "state"
                / "portable-sessions.sqlite3"
            )
            self.assertEqual(catalog.list_adoption_receipts(), ())
            _git(target, "init")
            _git(target, "config", "user.email", "devloop@example.invalid")
            _git(target, "config", "user.name", "Dev Loop Tests")
            (target / "README.md").write_text("# target\n", encoding="utf-8")
            _git(target, "add", "README.md")
            _git(target, "commit", "-m", "target")
            recovered = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr or recovered.stdout)
            self.assertEqual(len(catalog.list_adoption_receipts()), 1)

    def test_tampered_journal_candidate_path_is_retained_without_victim_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            interrupted_environment = environment.copy()
            interrupted_environment["DEVLOOP_TEST_INTERRUPT_AFTER_PHASE"] = "prepared"
            interrupted = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                interrupted_environment,
            )
            self.assertEqual(interrupted.returncode, 91)
            victim = root / "victim"
            victim.mkdir()
            victim_file = victim / "keep.bin"
            victim_file.write_bytes(b"keep\x00\xff")
            journal_path = install_root / "bootstrap" / "install-transaction.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["candidate_path"] = str(victim)
            journal_path.write_text(json.dumps(journal), encoding="utf-8")

            result = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("candidate path is not owned", result.stderr)
            self.assertEqual(victim_file.read_bytes(), b"keep\x00\xff")
            self.assertTrue(journal_path.is_file())

    @unittest.skipUnless(GIT_BASH.is_file(), "Git Bash is required")
    def test_posix_recovery_rejects_same_prefix_victim_without_ownership_evidence(self) -> None:
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            environment["HOME"] = str(root / "home")
            environment["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{environment['PATH']}"
            first = _run_posix_installer(
                ROOT / "install" / "devloop.sh", install_root, remote, "previous", environment
            )
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            interrupted_environment = environment.copy()
            interrupted_environment["DEVLOOP_TEST_INTERRUPT_AFTER_PHASE"] = "prepared"
            interrupted = _run_posix_installer(
                install_root / "install" / "devloop.sh",
                install_root,
                remote,
                "candidate",
                interrupted_environment,
            )
            self.assertEqual(interrupted.returncode, 91, interrupted.stderr or interrupted.stdout)
            victim = root / ".bundle.candidate-123-456"
            victim.mkdir()
            victim_file = victim / "keep.bin"
            victim_file.write_bytes(b"keep\x00\xff")
            journal_path = install_root / "bootstrap" / "install-transaction.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["candidate_path"] = str(victim)
            journal_path.write_text(json.dumps(journal), encoding="utf-8")

            recovered = _run_posix_installer(
                install_root / "install" / "devloop.sh",
                install_root,
                remote,
                "candidate",
                environment,
            )

            self.assertNotEqual(recovered.returncode, 0)
            self.assertIn("ownership evidence does not match", recovered.stderr)
            self.assertEqual(victim_file.read_bytes(), b"keep\x00\xff")

    def test_bootstrap_publication_recovers_every_asset_boundary(self) -> None:
        asset_paths = (
            "bootstrap/dispatch.ps1",
            "bootstrap/dispatch.sh",
            "bootstrap/verify.py",
            "bootstrap/transaction.py",
            "bin/devloop.ps1",
            "bin/devloop-plan.ps1",
            "bin/devloop.sh",
            "bin/devloop-plan.sh",
            "install/devloop.ps1",
            "install/uninstall-devloop.ps1",
            "install/devloop.sh",
            "install/uninstall-devloop.sh",
        )
        with _workspace_temporary_directory() as directory:
            root = Path(directory)
            remote = root / "bootstrap-fixture"
            shutil.copytree(ROOT / "install" / "bootstrap", remote / "install" / "bootstrap")
            shutil.copy2(ROOT / "portable-release.json", remote / "portable-release.json")
            _git(remote, "init")
            _git(remote, "config", "user.email", "devloop@example.invalid")
            _git(remote, "config", "user.name", "Dev Loop Tests")
            _git(remote, "add", ".")
            _git(remote, "commit", "-m", "bootstrap fixture")
            commit = _git(remote, "rev-parse", "HEAD")
            environment = _environment(root)
            for number, asset in enumerate(asset_paths):
                with self.subTest(asset=asset):
                    install_root = root / f"bundle-{number}"
                    transaction = ROOT / "install" / "bootstrap" / "transaction.py"
                    begun = subprocess.run(
                        [
                            sys.executable,
                            str(transaction),
                            "begin",
                            str(install_root),
                            "install",
                            "--protocol",
                            "2",
                        ],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(begun.returncode, 0, begun.stderr)
                    transaction_id = begun.stdout.strip()
                    candidate = root / f".bundle-{number}.candidate-{transaction_id}"
                    shutil.copytree(remote, candidate)
                    _git(candidate, "checkout", "--force", commit)
                    runtime = candidate / ".venv"
                    runtime.mkdir()
                    (runtime / ".devloop-test-runtime").write_text(commit, encoding="utf-8")
                    interrupted_environment = os.environ.copy()
                    interrupted_environment.update(
                        {
                            "DEVLOOP_TESTING": "1",
                            "DEVLOOP_TEST_INTERRUPT_BOOTSTRAP_AFTER_ASSET": asset,
                            "PYTHONDONTWRITEBYTECODE": "1",
                        }
                    )
                    transaction = candidate / "install" / "bootstrap" / "transaction.py"
                    interrupted = subprocess.run(
                        [
                            sys.executable,
                            str(transaction),
                            "publish",
                            str(install_root),
                            str(candidate),
                            transaction_id,
                            "--protocol",
                            "2",
                        ],
                        cwd=ROOT,
                        env=interrupted_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(interrupted.returncode, 94, interrupted.stderr)
                    recovery_environment = environment.copy()
                    recovery_environment["PYTHONDONTWRITEBYTECODE"] = "1"
                    recovered = subprocess.run(
                        [
                            sys.executable,
                            str(transaction),
                            "publish",
                            str(install_root),
                            str(candidate),
                            transaction_id,
                            "--protocol",
                            "2",
                        ],
                        cwd=ROOT,
                        env=recovery_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    prepared = subprocess.run(
                        [
                            sys.executable,
                            str(install_root / "bootstrap" / "transaction.py"),
                            "prepare",
                            str(install_root),
                            str(candidate),
                            commit,
                            transaction_id,
                            "--protocol",
                            "2",
                        ],
                        cwd=ROOT,
                        env=recovery_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(prepared.returncode, 0, prepared.stderr)
                    committed = subprocess.run(
                        [
                            sys.executable,
                            str(install_root / "bootstrap" / "transaction.py"),
                            "commit",
                            str(install_root),
                            transaction_id,
                            "--protocol",
                            "2",
                        ],
                        cwd=ROOT,
                        env=recovery_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(committed.returncode, 0, committed.stderr)
                    self.assertFalse((install_root / "bootstrap" / "layout.pending.json").exists())

    def test_failed_prepared_validation_retains_candidate_and_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            tampered = environment.copy()
            tampered["DEVLOOP_TEST_MUTATE_PREPARED_CANDIDATE"] = "1"
            result = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                tampered,
            )
            self.assertNotEqual(result.returncode, 0)
            journal_path = install_root / "bootstrap" / "install-transaction.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertTrue(Path(journal["candidate_path"]).is_dir())

    def test_bootstrap_and_transaction_compatibility_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            installed = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            old_pointer = _pointer(install_root)
            manifest_path = install_root / "bootstrap" / f"release-{old_pointer['commit']}.json"
            original_manifest = manifest_path.read_text(encoding="utf-8")
            incompatible = json.loads(original_manifest)
            incompatible["bootstrap_protocol_min"] = 3
            incompatible["bootstrap_protocol_max"] = 3
            manifest_path.write_text(json.dumps(incompatible), encoding="utf-8")
            verify_result = subprocess.run(
                [
                    sys.executable,
                    str(install_root / "bootstrap" / "verify.py"),
                    str(install_root),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(verify_result.returncode, 0)
            self.assertIn("incompatible with bootstrap", verify_result.stderr)
            manifest_path.write_text(original_manifest, encoding="utf-8")

            interrupted_environment = environment.copy()
            interrupted_environment["DEVLOOP_TEST_INTERRUPT_AFTER_PHASE"] = "prepared"
            interrupted = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                interrupted_environment,
            )
            self.assertEqual(interrupted.returncode, 91, interrupted.stderr or interrupted.stdout)
            journal_path = install_root / "bootstrap" / "install-transaction.json"
            legacy_journal = json.loads(journal_path.read_text(encoding="utf-8"))
            legacy_journal["version"] = 1
            legacy_journal.pop("transaction_id")
            legacy_journal.pop("previous_previous_pointer")
            journal_path.write_text(json.dumps(legacy_journal), encoding="utf-8")

            recovered = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )

            self.assertNotEqual(recovered.returncode, 0)
            self.assertIn("transaction identity is invalid", recovered.stderr)
            self.assertEqual(_pointer(install_root), old_pointer)

    def test_uninstall_rejects_tampered_retained_release_before_deleting_any(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            old_pointer = _pointer(install_root)
            second = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
            old_release = install_root / old_pointer["release_path"]
            (old_release / "release-marker").write_text("tampered\n", encoding="utf-8")
            releases_before = {path.name for path in (install_root / "releases").iterdir()}
            uninstall = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(install_root / "install" / "uninstall-devloop.ps1"),
                    "-InstallDir",
                    str(install_root),
                    "-KeepSkills",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotEqual(uninstall.returncode, 0)
            self.assertEqual(
                {path.name for path in (install_root / "releases").iterdir()},
                releases_before,
            )

    def test_uninstall_rejects_layout_escape_before_deleting_any_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            installed = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            victim = root / "victim.bin"
            victim.write_bytes(b"keep\x00\xff")
            releases_before = {path.name for path in (install_root / "releases").iterdir()}
            layout_path = install_root / "bootstrap" / "layout.json"
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            layout["assets"]["../victim.bin"] = "0" * 64
            layout_path.write_text(json.dumps(layout), encoding="utf-8")

            uninstall = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(install_root / "install" / "uninstall-devloop.ps1"),
                    "-InstallDir",
                    str(install_root),
                    "-KeepSkills",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

            self.assertNotEqual(uninstall.returncode, 0)
            self.assertEqual(victim.read_bytes(), b"keep\x00\xff")
            self.assertEqual(
                {path.name for path in (install_root / "releases").iterdir()}, releases_before
            )

    def test_rollback_exchanges_current_and_previous_in_one_pointer_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "bundle"
            environment = _environment(root)
            first = _run_installer(INSTALLER, install_root, remote, "previous", environment)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            first_pointer = _pointer(install_root)
            second = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)

            rollback = _run_installer(
                install_root / "install" / "devloop.ps1",
                install_root,
                remote,
                "candidate",
                environment,
                rollback=True,
            )

            self.assertEqual(rollback.returncode, 0, rollback.stderr or rollback.stdout)
            state = json.loads(
                (install_root / "bootstrap" / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["version"], 2)
            self.assertEqual(state["current"], first_pointer)
            self.assertFalse((install_root / "bootstrap" / "previous.json").exists())

    def test_interrupted_legacy_backup_is_not_overwritten_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _release_repository(root)
            install_root = root / "legacy"
            subprocess.run(
                ["git", "clone", str(remote), str(install_root)],
                check=True,
                capture_output=True,
            )
            launcher = install_root / "bin" / "devloop.ps1"
            launcher.write_bytes(b"original operator launcher\r\n\x00")
            environment = _environment(root)
            interrupted_environment = environment.copy()
            interrupted_environment["DEVLOOP_TEST_INTERRUPT_BOOTSTRAP_AFTER_BACKUP"] = "1"
            interrupted = _run_installer(
                INSTALLER,
                install_root,
                remote,
                "candidate",
                interrupted_environment,
            )
            self.assertEqual(interrupted.returncode, 93)
            retry = _run_installer(INSTALLER, install_root, remote, "candidate", environment)
            self.assertEqual(retry.returncode, 0, retry.stderr or retry.stdout)
            self.assertEqual(
                (install_root / "bootstrap" / "legacy-assets" / "bin" / "devloop.ps1").read_bytes(),
                b"original operator launcher\r\n\x00",
            )


def _pointer(install_root: Path) -> dict[str, object]:
    value = json.loads((install_root / "bootstrap" / "current.json").read_text(encoding="utf-8"))
    return value["current"] if value.get("version") == 2 else value


def _begin_transaction(transaction: Path, install_root: Path, operation: str) -> str:
    result = subprocess.run(
        [
            sys.executable,
            str(transaction),
            "begin",
            str(install_root),
            operation,
            "--protocol",
            "2",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _run_transaction_uninstall(
    transaction: Path,
    install_root: Path,
    transaction_id: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(transaction),
            "uninstall",
            str(install_root),
            transaction_id,
            "--protocol",
            "2",
            "--keep-capabilities",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _workspace_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    temporary_root = ROOT / "tmp"
    temporary_root.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=temporary_root)


def _run_installer(
    script: Path,
    install_root: Path,
    remote: Path,
    ref: str,
    environment: dict[str, str],
    *,
    no_skills: bool = True,
    rollback: bool = False,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "pwsh",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-InstallDir",
        str(install_root),
        "-RepoUrl",
        remote.as_uri(),
        "-Ref",
        ref,
    ]
    if no_skills:
        arguments.append("-NoSkills")
    if rollback:
        arguments.append("-Rollback")
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _run_posix_installer(
    script: Path,
    install_root: Path,
    remote: Path,
    ref: str,
    environment: dict[str, str],
    *,
    rollback: bool = False,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        str(GIT_BASH),
        _git_bash_path(script),
        "--dir",
        _git_bash_path(install_root),
        "--repo",
        remote.as_uri(),
        "--ref",
        ref,
        "--no-skills",
    ]
    if rollback:
        arguments.append("--rollback")
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=90,
    )


def _release_repository(root: Path) -> Path:
    remote = root / "remote"
    remote.mkdir()
    for name in ("src", "bin", "install"):
        shutil.copytree(ROOT / name, remote / name)
    for name in (".gitignore", "portable-release.json", "requirements-portable.lock"):
        shutil.copy2(ROOT / name, remote / name)
    (remote / "src" / "textual.py").write_text('__version__ = "8.2.8"\n', encoding="utf-8")
    (remote / "release-marker").write_text("previous\n", encoding="utf-8")
    _git(remote, "init")
    _git(remote, "config", "user.email", "devloop@example.invalid")
    _git(remote, "config", "user.name", "Dev Loop Tests")
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "previous")
    _git(remote, "tag", "previous")
    (remote / "release-marker").write_text("candidate\n", encoding="utf-8")
    installer = remote / "install" / "devloop.ps1"
    installer.write_text(
        installer.read_text(encoding="utf-8").replace(
            'Write-InstallLog "Staging ref $Ref outside the stable bootstrap"',
            'Write-InstallLog "Staging through changed release driver: $Ref"',
        ),
        encoding="utf-8",
    )
    dispatch = remote / "install" / "bootstrap" / "dispatch.ps1"
    dispatch.write_text(
        dispatch.read_text(encoding="utf-8")
        + "# candidate bootstrap must not replace stable bootstrap\n",
        encoding="utf-8",
    )
    _git(remote, "add", "release-marker")
    _git(remote, "add", "install/devloop.ps1", "install/bootstrap/dispatch.ps1")
    _git(remote, "commit", "-m", "candidate")
    _git(remote, "tag", "candidate")
    (remote / "release-marker").write_text("third\n", encoding="utf-8")
    _git(remote, "add", "release-marker")
    _git(remote, "commit", "-m", "third")
    _git(remote, "tag", "third")
    return remote


def _environment(root: Path) -> dict[str, str]:
    tools = root / "tools"
    tools.mkdir()
    (tools / "python.cmd").write_text(f'@echo off\r\n"{sys.executable}" %*\r\n', encoding="ascii")
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tools}{os.pathsep}{environment['PATH']}",
            "DEVLOOP_TESTING": "1",
            "LOCALAPPDATA": str(root / "state"),
            "APPDATA": str(root / "configuration"),
        }
    )
    return environment


def _git_bash_path(path: Path) -> str:
    drive = path.drive.rstrip(":").lower()
    suffix = path.as_posix()[len(path.drive) :]
    return f"/{drive}{suffix}"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()

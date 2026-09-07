from __future__ import annotations

import errno
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import unittest
import uuid
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from portable_test_support import (
    fresh_fixture_directory,
    isolated_environment,
    require_fixture_path,
)

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "install/bootstrap"
sys.path.insert(0, str(BOOTSTRAP))
try:
    import transaction
finally:
    sys.path.remove(str(BOOTSTRAP))

TRANSACTION_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
COMMIT = "a" * 40


class TransactionRecoveryRepairs(unittest.TestCase):
    """Production function seams only; never execute installers or real deletion."""

    def setUp(self) -> None:
        self.root = fresh_fixture_directory()
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.guards.enter_context(mock.patch.dict(
            os.environ, isolated_environment(self.root / "environment"), clear=True,
        ))
        self.write_json(self.root / "test-identity.json", {
            "test": self.id(), "scope": "guarded production function seams; retained fixture",
        })
        for target, name in (
            (subprocess, "run"), (subprocess, "Popen"), (os, "system"), (os, "_exit"),
            (shutil, "rmtree"),
        ):
            self.guards.enter_context(mock.patch.object(
                target, name, side_effect=AssertionError(f"blocked production seam: {name}"),
            ))
        self.install = self.root / "bundle"
        self.bootstrap = self.install / "bootstrap"
        self.bootstrap.mkdir(parents=True)
        (self.install / "releases").mkdir()
        self.candidate = self.root / f".bundle.candidate-{TRANSACTION_ID}"
        self.candidate.mkdir()
        self.staging = transaction._uninstall_staging(self.install, TRANSACTION_ID)
        self.pointer = {
            "commit": COMMIT, "release_path": f"releases/{COMMIT}",
            "tracked_fingerprint": "A" * 64, "runtime_fingerprint": "B" * 64,
        }

    def remove_fixture(self, path: Path, **_: Any) -> None:
        """Model a removed directory by retaining its exact tree at a fresh fixture path."""
        path = require_fixture_path(Path(path))
        self.assertNotEqual(path, self.root)
        self.assertTrue(path.is_relative_to(self.root))
        for child in path.rglob("*"):
            require_fixture_path(child)
        retained = require_fixture_path(
            self.root / ("retained-tree-" + uuid.uuid4().hex), allow_existing=False,
        )
        path.rename(retained)

    @staticmethod
    def write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def lock(self, operation: str = "uninstall") -> dict[str, Any]:
        value = {
            "version": 1, "transaction_id": TRANSACTION_ID,
            "owner_pid": os.getpid(), "owner_start": "original-start",
            "owner_host": socket.gethostname(), "operation": operation,
            "candidate_name": self.candidate.name,
        }
        self.write_json(transaction._lock_path(self.install) / "owner.json", value)
        return value

    def template(self) -> None:
        for source, _ in transaction.ASSETS:
            path = self.candidate / "install/bootstrap" / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((BOOTSTRAP / source).read_bytes())

    def layout(self) -> None:
        self.template()
        transaction._initialize_bootstrap(self.install, self.candidate)

    def pointer_state(self, current: dict[str, Any], previous: dict[str, Any] | None) -> None:
        self.write_json(self.bootstrap / "current.json", {
            "version": 2, "current": current, "previous": previous,
        })

    def journal(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        self.staging.mkdir(exist_ok=True)
        value = {
            "version": 1, "transaction_id": TRANSACTION_ID, "install_root": str(self.install),
            "layout_hash": transaction._sha256(self.bootstrap / "layout.json"),
            "plan_hash": transaction._uninstall_plan_hash(actions),
            "status": "executing", "actions": actions,
        }
        transaction._publish_uninstall_evidence(
            self.install, self.staging, value,
            release_inventories={Path(action["path"]): transaction._content_inventory(
                Path(action["path"]),
            ) for action in actions if action["kind"] == "remove_release"},
        )
        self.write_json(self.staging / "journal.json", value)
        return value

    @staticmethod
    def action(kind: str, index: int = 0, **values: Any) -> dict[str, Any]:
        return {"id": f"{index:03d}-{kind}", "kind": kind, "state": "pending", **values}

    def test_unknown_and_remote_owner_are_never_reclaimed(self) -> None:
        lock = self.lock()
        for identity in (
            transaction.ProcessIdentity(transaction.ProcessState.UNKNOWN),
            transaction.ProcessIdentity(transaction.ProcessState.ALIVE, "original-start"),
            transaction.ProcessIdentity(transaction.ProcessState.ALIVE, "alive"),
        ):
            with mock.patch.object(transaction, "_inspect_process", return_value=identity):
                self.assertTrue(transaction._lock_owner_alive(lock))
        lock["owner_host"] = "another-host"
        with mock.patch.object(transaction, "_inspect_process") as inspect:
            self.assertTrue(transaction._lock_owner_alive(lock))
            inspect.assert_not_called()

    def test_confirmed_dead_and_reused_pid_are_distinct_from_unknown(self) -> None:
        lock = self.lock()
        for identity in (
            transaction.ProcessIdentity(transaction.ProcessState.DEAD),
            transaction.ProcessIdentity(transaction.ProcessState.ALIVE, "later-start"),
        ):
            with mock.patch.object(transaction, "_inspect_process", return_value=identity):
                self.assertFalse(transaction._lock_owner_alive(lock))

    def test_posix_permission_denied_is_unknown_but_esrch_is_dead(self) -> None:
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(
            Path, "is_file", return_value=False,
        ):
            for code, expected in (
                (errno.EPERM, transaction.ProcessState.UNKNOWN),
                (errno.ESRCH, transaction.ProcessState.DEAD),
            ):
                with mock.patch.object(os, "kill", side_effect=OSError(code, "fixture")):
                    self.assertEqual(transaction._inspect_process(999999).state, expected)

    def test_kernel_guard_blocks_a_second_handle_without_removing_owner(self) -> None:
        self.lock()
        with transaction._lock_guard(self.install):
            with self.assertRaisesRegex(RuntimeError, "concurrently"):
                with transaction._lock_guard(self.install):
                    self.fail("second handle acquired exclusive guard")
        self.assertEqual(transaction._read_lock(self.install)["transaction_id"], TRANSACTION_ID)

    def test_reclamation_rejects_a_replacement_lock(self) -> None:
        original = self.lock()
        replacement = {**original, "transaction_id": OTHER_ID,
                       "candidate_name": f".bundle.candidate-{OTHER_ID}"}
        self.write_json(transaction._lock_path(self.install) / "owner.json", replacement)
        with transaction._lock_guard(self.install):
            with self.assertRaisesRegex(RuntimeError, "changed during reclamation"):
                transaction._remove_lock(self.install, original)
        self.assertEqual(transaction._read_lock(self.install), replacement)

    def interrupt_publication(self) -> None:
        real_replace = os.replace
        target = self.bootstrap / "dispatch.ps1"

        def replace(source: Any, destination: Any) -> None:
            self.assertTrue(Path(source).resolve().is_relative_to(self.root))
            self.assertTrue(Path(destination).resolve().is_relative_to(self.root))
            real_replace(source, destination)
            if Path(destination) == target:
                raise RuntimeError("fixture publication interruption")

        with mock.patch.object(os, "replace", side_effect=replace):
            with self.assertRaisesRegex(RuntimeError, "fixture publication interruption"):
                transaction._initialize_bootstrap(self.install, self.candidate)

    def test_fresh_publication_resumes_asset_replaced_before_checkpoint(self) -> None:
        self.template()
        self.interrupt_publication()
        transaction._initialize_bootstrap(self.install, self.candidate)
        layout = json.loads((self.bootstrap / "layout.json").read_text())
        self.assertEqual(layout["legacy_backups"], {})
        self.assertFalse((self.bootstrap / "layout.pending.json").exists())

    def test_file_flush_accepts_existing_file_and_preserves_exact_bytes(self) -> None:
        target = self.root / "publication.bin"
        original = b"Existing publication bytes\x00\xff"
        target.write_bytes(original)
        transaction._flush_file(target)
        self.assertEqual(target.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "Windows read-only attribute and flush-handle contract")
    def test_readonly_legacy_backup_refuses_before_ready_or_publication(self) -> None:
        self.template()
        original = self.bootstrap / "dispatch.ps1"
        original_bytes = b"Read-only legacy asset\x00\xff"
        original.write_bytes(original_bytes)
        original.chmod(stat.S_IREAD)
        with self.assertRaises(PermissionError):
            transaction._initialize_bootstrap(self.install, self.candidate)
        pending = json.loads((self.bootstrap / "layout.pending.json").read_text())
        self.assertEqual(pending["backups_ready"], [])
        self.assertEqual(pending["published"], [])
        self.assertFalse((self.bootstrap / "layout.json").exists())
        backup = self.bootstrap / "legacy-assets/bootstrap/dispatch.ps1"
        staged = backup.with_name(pending["backup_staging"]["bootstrap/dispatch.ps1"])
        self.assertFalse(backup.exists())
        for path in (original, staged):
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertTrue(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)

    def test_original_backup_survives_interrupted_publication(self) -> None:
        self.template()
        original = b"original operator entrypoint\x00\xff"
        (self.bootstrap / "dispatch.ps1").write_bytes(original)
        self.interrupt_publication()
        transaction._initialize_bootstrap(self.install, self.candidate)
        self.assertEqual((self.bootstrap / "legacy-assets/bootstrap/dispatch.ps1").read_bytes(),
                         original)

    def test_partial_backup_copy_resumes_from_original_without_partial_final_backup(self) -> None:
        self.template()
        original = self.bootstrap / "dispatch.ps1"
        original.write_bytes(b"exact original launcher\x00\xff")
        backup = self.bootstrap / "legacy-assets/bootstrap/dispatch.ps1"
        copy = shutil.copy2

        def partial_copy(source: Path, target: Path) -> Any:
            if ".backup-" in Path(target).name:
                Path(target).write_bytes(b"partial")
                raise RuntimeError("fixture interruption inside backup copy")
            return copy(source, target)

        with mock.patch.object(shutil, "copy2", side_effect=partial_copy):
            with self.assertRaisesRegex(RuntimeError, "inside backup copy"):
                transaction._initialize_bootstrap(self.install, self.candidate)
        pending = json.loads((self.bootstrap / "layout.pending.json").read_text())
        staged = backup.with_name(pending["backup_staging"]["bootstrap/dispatch.ps1"])
        self.assertEqual(staged.read_bytes(), b"partial")
        self.assertFalse(backup.exists())
        self.assertEqual(original.read_bytes(), b"exact original launcher\x00\xff")
        transaction._initialize_bootstrap(self.install, self.candidate)
        self.assertEqual(backup.read_bytes(), b"exact original launcher\x00\xff")
        self.assertFalse(staged.exists())

    def test_old_pending_layout_without_staging_map_keeps_original_backup_evidence(self) -> None:
        self.template()
        (self.bootstrap / "dispatch.ps1").write_bytes(b"old pending original")
        self.interrupt_publication()
        pending_path = self.bootstrap / "layout.pending.json"
        pending = json.loads(pending_path.read_text())
        pending.pop("backup_staging")
        self.write_json(pending_path, pending)
        transaction._initialize_bootstrap(self.install, self.candidate)
        self.assertEqual(
            (self.bootstrap / "legacy-assets/bootstrap/dispatch.ps1").read_bytes(),
            b"old pending original",
        )

    def test_unrecorded_backup_staging_is_not_adopted_or_removed_on_retry(self) -> None:
        self.template()
        (self.bootstrap / "dispatch.ps1").write_bytes(b"original")
        atomic_json = transaction._atomic_json

        def interrupt_before_ownership(path: Path, value: Any) -> None:
            if value.get("backup_staging"):
                raise RuntimeError("fixture before staging ownership checkpoint")
            atomic_json(path, value)

        with mock.patch.object(transaction, "_atomic_json", side_effect=interrupt_before_ownership):
            with self.assertRaisesRegex(RuntimeError, "before staging ownership checkpoint"):
                transaction._initialize_bootstrap(self.install, self.candidate)
        backup_parent = self.bootstrap / "legacy-assets/bootstrap"
        unowned = list(backup_parent.glob("*.backup-*"))
        self.assertEqual(len(unowned), 1)
        unowned[0].write_bytes(b"operator content in unowned staging")
        transaction._initialize_bootstrap(self.install, self.candidate)
        self.assertEqual(unowned[0].read_bytes(), b"operator content in unowned staging")
        self.assertEqual((backup_parent / "dispatch.ps1").read_bytes(), b"original")

    def test_backup_publication_and_checkpoint_interruptions_preserve_exact_original(self) -> None:
        self.template()
        for position in ("before-link", "after-link", "before-checkpoint", "after-checkpoint"):
            with self.subTest(position=position):
                install = self.root / position
                install.mkdir()
                target = install / "bin/devloop.sh"
                target.parent.mkdir()
                target.write_bytes(b"original bytes")
                backup = install / "bootstrap/legacy-assets/bin/devloop.sh"
                link, atomic_json = os.link, transaction._atomic_json

                def publish(
                    source: Any, destination: Any, position: str = position, link: Any = link,
                ) -> None:
                    if position == "before-link":
                        raise RuntimeError("fixture backup boundary")
                    link(source, destination)
                    if position == "after-link":
                        raise RuntimeError("fixture backup boundary")

                def checkpoint(
                    path: Path, value: Any, position: str = position,
                    atomic_json: Any = atomic_json,
                ) -> None:
                    at_backup = bool(value.get("backups_ready"))
                    if at_backup and position == "before-checkpoint":
                        raise RuntimeError("fixture backup boundary")
                    atomic_json(path, value)
                    if at_backup and position == "after-checkpoint":
                        raise RuntimeError("fixture backup boundary")

                with mock.patch.object(os, "link", side_effect=publish), mock.patch.object(
                    transaction, "_atomic_json", side_effect=checkpoint,
                ):
                    with self.assertRaisesRegex(RuntimeError, "fixture backup boundary"):
                        transaction._initialize_bootstrap(install, self.candidate)
                self.assertEqual(target.read_bytes(), b"original bytes")
                transaction._initialize_bootstrap(install, self.candidate)
                self.assertEqual(backup.read_bytes(), b"original bytes")
                self.assertEqual(list(backup.parent.glob("*.backup-*")), [])

    def test_backup_publication_does_not_overwrite_new_operator_final_file(self) -> None:
        self.template()
        (self.bootstrap / "dispatch.ps1").write_bytes(b"original")
        backup = self.bootstrap / "legacy-assets/bootstrap/dispatch.ps1"
        link = os.link

        def create_operator_backup(source: Any, destination: Any) -> None:
            Path(destination).write_bytes(b"operator backup appeared during copy")
            link(source, destination)

        with mock.patch.object(os, "link", side_effect=create_operator_backup):
            with self.assertRaises(FileExistsError):
                transaction._initialize_bootstrap(self.install, self.candidate)
        with self.assertRaisesRegex(RuntimeError, "legacy backup was modified"):
            transaction._initialize_bootstrap(self.install, self.candidate)
        self.assertEqual(backup.read_bytes(), b"operator backup appeared during copy")

    def test_unsupported_backup_link_preserves_original_and_bound_staging(self) -> None:
        self.template()
        original = self.bootstrap / "dispatch.ps1"
        original.write_bytes(b"original")
        backup = self.bootstrap / "legacy-assets/bootstrap/dispatch.ps1"
        with mock.patch.object(os, "link", side_effect=OSError(errno.EPERM, "unsupported")):
            with self.assertRaisesRegex(RuntimeError, "original and owned staging were preserved"):
                transaction._initialize_bootstrap(self.install, self.candidate)
        pending = json.loads((self.bootstrap / "layout.pending.json").read_text())
        self.assertEqual(pending["backups_ready"], [])
        self.assertEqual(original.read_bytes(), b"original")
        self.assertFalse(backup.exists())
        staged = backup.with_name(pending["backup_staging"]["bootstrap/dispatch.ps1"])
        self.assertEqual(staged.read_bytes(), b"original")

    def test_asset_data_is_flushed_before_publication_checkpoint(self) -> None:
        self.template()
        flushed: list[Path] = []
        flush, atomic_json = transaction._flush_file, transaction._atomic_json

        def record_flush(path: Path) -> None:
            flush(path)
            flushed.append(path)

        def checkpoint(path: Path, value: Any) -> None:
            for relative in value.get("published", []):
                self.assertIn(self.install / relative, flushed)
            atomic_json(path, value)

        with mock.patch.object(transaction, "_flush_file", side_effect=record_flush):
            with mock.patch.object(transaction, "_atomic_json", side_effect=checkpoint):
                transaction._initialize_bootstrap(self.install, self.candidate)
        self.assertTrue(any(".publish-" in path.name for path in flushed))

    def test_legacy_migration_resumes_with_original_backup_evidence(self) -> None:
        self.template()
        hashes: dict[str, str] = {}
        for _, relative in transaction.ASSETS:
            target = self.install / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("legacy:" + relative)
            hashes[relative] = transaction._sha256(target)
        backup = self.bootstrap / "legacy-assets/bin/devloop.sh"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"original checkout launcher")
        self.write_json(self.bootstrap / "layout.json", {
            "version": 1, "assets": hashes, "legacy_backups": ["bin/devloop.sh"],
        })
        self.interrupt_publication()
        transaction._reconcile_committed_pending_layout(self.install)
        transaction._initialize_bootstrap(self.install, self.candidate)
        self.assertEqual(backup.read_bytes(), b"original checkout launcher")
        self.assertEqual(json.loads((self.bootstrap / "layout.json").read_text())["version"], 2)

    def test_changed_backup_is_rejected_even_when_checkpoint_says_ready(self) -> None:
        self.template()
        (self.bootstrap / "dispatch.ps1").write_bytes(b"original")
        self.interrupt_publication()
        backup = self.bootstrap / "legacy-assets/bootstrap/dispatch.ps1"
        backup.write_bytes(b"operator changed backup")
        with self.assertRaisesRegex(RuntimeError, "legacy backup was modified"):
            transaction._initialize_bootstrap(self.install, self.candidate)
        self.assertEqual(backup.read_bytes(), b"operator changed backup")

    def test_fresh_activation_accepts_exact_pointer_only(self) -> None:
        journal = {"previous_pointer": None, "previous_previous_pointer": None}
        transaction._assert_pointer_baseline(self.install, journal, self.pointer)
        self.pointer_state(self.pointer, None)
        transaction._assert_pointer_baseline(self.install, journal, self.pointer)
        for current, previous in (
            ({**self.pointer, "runtime_fingerprint": "C" * 64}, None),
            (self.pointer, self.pointer),
        ):
            self.pointer_state(current, previous)
            with self.assertRaisesRegex(RuntimeError, "pointer changed"):
                transaction._assert_pointer_baseline(self.install, journal, self.pointer)

    def test_fresh_commit_recovers_crash_between_pointer_swap_and_checkpoint(self) -> None:
        self.lock("install")
        journal = {
            "version": 2, "transaction_id": TRANSACTION_ID, "install_root": str(self.install),
            "candidate_path": str(self.candidate), **self.pointer,
            "previous_pointer": None, "previous_previous_pointer": None,
            "phase": "release_ready",
        }
        self.write_json(self.bootstrap / "install-transaction.json", journal)
        self.write_json(transaction._ownership_path(self.install, TRANSACTION_ID), {
            "version": 1, "transaction_id": TRANSACTION_ID,
            "candidate_name": self.candidate.name, "commit": COMMIT,
        })

        def interrupt(phase: str) -> None:
            if phase == "pointer_swapped":
                raise RuntimeError("fixture pointer interruption")

        with mock.patch.object(transaction, "_validate_journal_release"), mock.patch.object(
            transaction, "verify",
        ), mock.patch.object(transaction, "_release_lock"):
            with mock.patch.object(transaction, "_interrupt", side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, "fixture pointer interruption"):
                    transaction.commit(self.install, TRANSACTION_ID)
            saved = transaction._journal(self.install)
            assert saved is not None
            self.assertEqual(saved["phase"], "adopted")
            transaction.commit(self.install, TRANSACTION_ID)
        self.assertEqual(transaction.read_pointer_state(self.install).current, self.pointer)
        self.assertFalse((self.bootstrap / "install-transaction.json").exists())

    def test_retained_candidate_uses_original_runtime_and_preserves_both_releases(self) -> None:
        self.layout()
        self.lock("rollback")
        release = self.install / self.pointer["release_path"]
        release.mkdir()
        (release / "payload").write_bytes(b"retained")
        previous = {**self.pointer, "commit": "b" * 40, "release_path": f"releases/{'b' * 40}"}
        old_release = self.install / previous["release_path"]
        old_release.mkdir()
        (old_release / "payload").write_bytes(b"previous")
        self.pointer_state(self.pointer, previous)
        with mock.patch.object(transaction, "verify_pointer"), mock.patch.object(
            transaction, "_release_lock",
        ):
            transaction.rollback(self.install, TRANSACTION_ID)
        self.lock("install")
        fresh = {**self.pointer, "runtime_fingerprint": "C" * 64}
        with mock.patch.object(transaction, "verify_release"), mock.patch.object(
            transaction, "verify_pointer",
        ), mock.patch.object(transaction, "_verify_git_ownership"), mock.patch.object(
            transaction, "_release_evidence",
            side_effect=lambda path, _: fresh if path == self.candidate else self.pointer,
        ), mock.patch.object(shutil, "rmtree", side_effect=self.remove_fixture):
            self.assertEqual(transaction.prepare(
                self.install, self.candidate, COMMIT, TRANSACTION_ID,
            ), release)
        journal = transaction._journal(self.install)
        assert journal is not None
        self.assertEqual(journal["runtime_fingerprint"], self.pointer["runtime_fingerprint"])
        self.assertEqual(journal["phase"], "release_ready")
        with mock.patch.object(transaction, "_validate_journal_release", return_value=release):
            with mock.patch.object(transaction, "verify"), mock.patch.object(
                transaction, "_release_lock",
            ):
                transaction.commit(self.install, TRANSACTION_ID)
        state = transaction.read_pointer_state(self.install)
        self.assertEqual(state.current, self.pointer)
        self.assertEqual(state.previous, previous)
        self.assertEqual((release / "payload").read_bytes(), b"retained")
        self.assertEqual((old_release / "payload").read_bytes(), b"previous")

    def test_rehashed_descendant_action_cannot_escape_exact_allowlist(self) -> None:
        self.layout()
        target = self.install / "operator-config.json"
        target.write_text("operator data")
        action = self.action("remove_asset", path=str(self.install / "bin/devloop.sh"))
        journal = self.journal([action])
        action["path"] = str(target)
        journal["plan_hash"] = transaction._uninstall_plan_hash(journal["actions"])
        self.write_json(self.staging / "journal.json", journal)
        with self.assertRaisesRegex(RuntimeError, "canonical allowlist"):
            transaction._read_uninstall_journal(self.install, TRANSACTION_ID)
        self.assertEqual(target.read_text(), "operator data")

    def test_resumed_uninstall_rejects_modified_stable_asset(self) -> None:
        self.layout()
        target = self.install / "bin/devloop.sh"
        self.journal([self.action("remove_asset", path=str(target))])
        target.write_text("modified after preflight")
        with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
            transaction._read_uninstall_journal(self.install, TRANSACTION_ID)
        self.assertEqual(target.read_text(), "modified after preflight")

    def test_all_action_kinds_reject_noncanonical_owned_descendants(self) -> None:
        for kind, fields in (
            ("remove_release", {"path": str(self.install / "releases/operator-project")}),
            ("remove_manifest", {"path": str(self.bootstrap / "operator.json")}),
            ("remove_pointer", {"path": str(self.bootstrap / "operator.json")}),
            ("remove_metadata", {"path": str(self.bootstrap / "operator.json")}),
            ("restore_legacy", {"source": str(self.bootstrap / "legacy-assets/bin/devloop.sh"),
                                "target": str(self.install / "operator.json")}),
            ("stage_capability", {"source": str(self.install / "releases/operator/skills/codex"),
                                  "staged": str(self.staging / "skills/codex"),
                                  "destination": str(self.root / "capabilities")}),
            ("cleanup_capability", {"staged": str(self.staging / "operator"),
                                    "destination": str(self.root / "capabilities")}),
        ):
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                transaction._validate_action_paths(
                    self.install, self.staging, self.action(kind, **fields), 0,
                )

    def test_resumed_action_rejects_replaced_ancestor_before_access(self) -> None:
        original = Path.lstat
        replaced = self.install / "bin"

        def lstat(path: Path) -> Any:
            if path == replaced:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return original(path)

        with mock.patch.object(Path, "lstat", lstat):
            with self.assertRaisesRegex(RuntimeError, "symbolic link rejected"):
                transaction._validate_action_paths(self.install, self.staging,
                                                  self.action("remove_asset", path=str(
                                                      replaced / "devloop.sh",
                                                  )), 0)

    def test_partial_release_inventory_accepts_missing_files_and_rejects_new_data(self) -> None:
        self.layout()
        release = self.install / self.pointer["release_path"]
        release.mkdir()
        (release / "tracked.py").write_bytes(b"original tracked file")
        runtime = release / ".venv"
        runtime.mkdir()
        (runtime / "runtime.bin").write_bytes(b"original runtime")
        action = self.action("remove_release", path=str(release))
        journal = self.journal([action])
        action["state"] = "before"
        self.write_json(self.staging / "journal.json", journal)
        (release / "tracked.py").unlink()
        transaction._read_uninstall_journal(self.install, TRANSACTION_ID)
        for target in (release / "operator-notes", runtime / "runtime.bin"):
            original = target.read_bytes() if target.exists() else None
            target.write_bytes(b"operator data after interruption")
            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                transaction._read_uninstall_journal(self.install, TRANSACTION_ID)
            if original is None:
                target.unlink()
            else:
                target.write_bytes(original)

    def test_late_release_changes_are_rejected_before_any_release_removal(self) -> None:
        self.layout()
        self.lock()
        release = self.install / self.pointer["release_path"]
        release.mkdir()
        (release / "tracked.py").write_bytes(b"owned tracked bytes")
        (release / ".venv").mkdir()
        (release / ".venv/runtime.bin").write_bytes(b"owned runtime bytes")
        original = transaction._content_inventory(release)
        plan = transaction.UninstallPlan(
            (release,), (), (), (), (), ((release, original),),
        )
        copy = shutil.copy2
        for relative in ("operator-note.ignored", "tracked.py", ".venv/runtime.bin"):
            with self.subTest(relative=relative):
                changed = release / relative
                saved = changed.read_bytes() if changed.exists() else None

                def change_after_preflight(
                    source: Any, destination: Any, changed: Path = changed,
                ) -> Any:
                    changed.write_bytes(b"late operator content")
                    return copy(source, destination)

                with mock.patch.object(transaction, "_uninstall_plan", return_value=plan):
                    with mock.patch.object(shutil, "copy2", side_effect=change_after_preflight):
                        with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                            transaction.uninstall(self.install, TRANSACTION_ID, None, None, True)
                self.assertEqual(changed.read_bytes(), b"late operator content")
                self.assertTrue(release.is_dir())
                self.assertFalse((self.staging / "journal.json").exists())
                self.remove_fixture(self.staging)
                if saved is None:
                    changed.unlink()
                else:
                    changed.write_bytes(saved)
                self.assertEqual(transaction._content_inventory(release), original)

    def test_release_inventory_publication_requires_original_preflight_evidence(self) -> None:
        self.layout()
        release = self.install / self.pointer["release_path"]
        release.mkdir()
        self.staging.mkdir()
        with self.assertRaisesRegex(RuntimeError, "ownership-bound preflight inventory"):
            transaction._publish_uninstall_evidence(self.install, self.staging, {
                "actions": [self.action("remove_release", path=str(release))],
            })

    def test_preflight_inventory_cannot_recapture_changes_during_release_verification(self) -> None:
        self.layout()
        release = self.install / self.pointer["release_path"]
        release.mkdir()
        tracked = release / "tracked.py"
        tracked.write_bytes(b"original")

        def verify_then_change(install: Path, commit: str) -> Path:
            self.assertEqual((install, commit), (self.install, COMMIT))
            tracked.write_bytes(b"changed after manifest verification")
            return release

        with mock.patch.object(transaction, "read_pointer_state", return_value=SimpleNamespace(
            current=self.pointer, previous=None,
        )), mock.patch.object(transaction, "verify_pointer"), mock.patch.object(
            transaction, "verify_release", side_effect=verify_then_change,
        ), mock.patch.object(transaction, "_verify_git_ownership"):
            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                transaction._uninstall_plan(self.install)
        self.assertEqual(tracked.read_bytes(), b"changed after manifest verification")

    def test_restore_retry_requires_original_backup_and_unchanged_target(self) -> None:
        self.template()
        target = self.install / "bin/devloop.sh"
        target.parent.mkdir()
        target.write_bytes(b"original launcher")
        transaction._initialize_bootstrap(self.install, self.candidate)
        source = self.bootstrap / "legacy-assets/bin/devloop.sh"
        action = self.action("restore_legacy", source=str(source), target=str(target))
        journal = self.journal([action])
        action["state"] = "before"
        self.write_json(self.staging / "journal.json", journal)
        target.write_bytes(b"operator changed stable target")
        with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
            transaction._read_uninstall_journal(self.install, TRANSACTION_ID)
        target.unlink()
        transaction._read_uninstall_journal(self.install, TRANSACTION_ID)
        os.replace(source, target)
        transaction._read_uninstall_journal(self.install, TRANSACTION_ID)
        self.assertEqual(target.read_bytes(), b"original launcher")

    def test_missing_original_uninstall_evidence_fails_closed(self) -> None:
        self.layout()
        self.journal([self.action("remove_asset", path=str(self.install / "bin/devloop.sh"))])
        (self.staging / transaction.UNINSTALL_EVIDENCE_NAME).unlink()
        with self.assertRaisesRegex(RuntimeError, "original content evidence is missing"):
            transaction._read_uninstall_journal(self.install, TRANSACTION_ID)

    def test_uninstall_revalidates_content_after_the_before_checkpoint(self) -> None:
        self.layout()
        self.lock()
        target = self.install / "bin/devloop.sh"
        plan = transaction.UninstallPlan((), (), (target,), (), ())

        def change_after_checkpoint(_: str, position: str) -> None:
            if position == "before":
                target.write_bytes(b"operator change at deletion boundary")

        with mock.patch.object(transaction, "_uninstall_plan", return_value=plan):
            with mock.patch.object(transaction, "_uninstall_interrupt",
                                   side_effect=change_after_checkpoint):
                with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                    transaction.uninstall(self.install, TRANSACTION_ID, None, None, True)
        self.assertEqual(target.read_bytes(), b"operator change at deletion boundary")
        self.assertTrue((self.staging / "uninstall-devloop.ps1").is_file())
        self.assertTrue((self.staging / "uninstall-devloop.sh").is_file())

    def test_both_public_recovery_probes_find_external_code_after_core_removal(self) -> None:
        self.layout()
        self.lock()
        self.journal([])
        for name in ("transaction.py", "verify.py"):
            shutil.copy2(self.bootstrap / name, self.staging / name)
        ps = (BOOTSTRAP / "install/uninstall-devloop.ps1").read_text()
        bash = (BOOTSTRAP / "install/uninstall-devloop.sh").read_text()
        ps_probe = ps.split("$recoveryProbe = @'\n", 1)[1].split("\n'@", 1)[0]
        bash_probe = bash.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        self.assertEqual(ps_probe, bash_probe)
        (self.bootstrap / "verify.py").unlink()
        (self.bootstrap / "transaction.py").unlink()
        for base in (self.install / "install", self.staging):
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["probe", str(base)]), redirect_stdout(output):
                exec(compile(ps_probe, "public-uninstall-recovery-probe", "exec"), {})
            self.assertEqual(output.getvalue().strip(), str(self.staging))
        (self.staging / "transaction.py").write_bytes(b"changed recovery executable")
        with mock.patch.object(sys, "argv", ["probe", str(self.staging)]):
            with self.assertRaisesRegex(RuntimeError, "executable was modified"):
                exec(compile(ps_probe, "public-uninstall-recovery-probe", "exec"), {})

    def test_public_resume_reuses_bound_journal_after_pointer_and_layout_removal(self) -> None:
        self.layout()
        self.journal([])
        (self.bootstrap / "layout.json").unlink()
        with mock.patch.object(transaction, "begin", return_value=TRANSACTION_ID) as begin:
            with mock.patch.object(transaction, "uninstall") as uninstall:
                transaction.resume_uninstall(self.staging, 123)
        begin.assert_called_once_with(
            self.install, "uninstall", 123, recovery_transaction_id=TRANSACTION_ID,
        )
        uninstall.assert_called_once_with(self.install, TRANSACTION_ID, None, None, True)

    def test_public_resume_checks_preservation_and_destinations_before_acquiring_lock(self) -> None:
        self.layout()
        source = self.install / self.pointer["release_path"] / "skills/codex"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_bytes(b"owned skill")
        destination = self.root / "installed-skills"
        self.journal([
            self.action("stage_capability", source=str(source),
                        staged=str(self.staging / "skills/codex"), destination=str(destination)),
            self.action("cleanup_capability", 1, staged=str(self.staging / "skills/codex"),
                        destination=str(destination)),
        ])
        before = (self.staging / "journal.json").read_bytes()
        for options in (
            {"keep_capabilities": True}, {"skills_destination": self.root / "different"},
            {"agents_destination": self.root / "agents"},
            {"install_root": self.root / "different-install"},
            {"bin_directory": self.root / "bin"},
        ):
            with self.subTest(options=options), mock.patch.object(transaction, "begin") as begin:
                with self.assertRaisesRegex(RuntimeError, "uninstall retry"):
                    transaction.resume_uninstall(self.staging, **options)
                begin.assert_not_called()
                self.assertEqual((self.staging / "journal.json").read_bytes(), before)
        with mock.patch.object(transaction, "begin", return_value=TRANSACTION_ID) as begin:
            with mock.patch.object(transaction, "uninstall") as uninstall:
                transaction.resume_uninstall(
                    self.staging, skills_destination=destination, install_root=self.install,
                )
        begin.assert_called_once()
        uninstall.assert_called_once_with(self.install, TRANSACTION_ID, None, None, True)
        self.assertEqual((self.staging / "journal.json").read_bytes(), before)

    def test_public_resume_accepts_keep_option_for_original_preservation_plan(self) -> None:
        self.layout()
        self.journal([])
        with mock.patch.object(transaction, "begin", return_value=TRANSACTION_ID):
            with mock.patch.object(transaction, "uninstall") as uninstall:
                transaction.resume_uninstall(self.staging, keep_capabilities=True)
        uninstall.assert_called_once_with(self.install, TRANSACTION_ID, None, None, True)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "install" / "bootstrap"
sys.path.insert(0, str(BOOTSTRAP))
try:
    import transaction
    import verify
finally:
    sys.path.remove(str(BOOTSTRAP))

TRANSACTION_ID = "33333333-3333-4333-8333-333333333333"


class ReleaseContentSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        # Never honor TMP/TEMP redirected into a source checkout.
        self.temporary_parent = (
            Path(os.environ["LOCALAPPDATA"]) / "Temp"
            if os.name == "nt" else Path("/tmp")
        ).resolve(strict=True)
        self.root = Path(tempfile.mkdtemp(
            prefix="devloop-release-content-safety-", dir=self.temporary_parent,
        )).resolve(strict=True)
        self.validate_root()
        self.addCleanup(self.cleanup_fixture)
        self.git_executable = (
            Path(r"C:\Program Files\Git\cmd\git.exe")
            if os.name == "nt" else Path(shutil.which("git") or "/nonexistent-git")
        )
        self.assertTrue(self.git_executable.is_file(), "real Git is required for these tests")
        self.empty_config = self.root / "empty-config"
        self.empty_config.write_bytes(b"")
        self.empty_directory = self.root / "empty-git-support"
        self.empty_directory.mkdir()
        self.git_environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        self.git_environment.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": str(self.empty_config),
            "GIT_CONFIG_GLOBAL": str(self.empty_config),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_AUTHOR_NAME": "Release safety fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Release safety fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        })
        self.real_run = subprocess.run
        self.install = self.root / "bundle"
        self.bootstrap = self.install / "bootstrap"
        self.bootstrap.mkdir(parents=True)
        (self.install / "releases").mkdir()
        self.candidate = self.root / f".bundle.candidate-{TRANSACTION_ID}"
        self.candidate.mkdir()
        self.release = self.candidate
        (self.candidate / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
        (self.candidate / "portable-release.json").write_bytes(
            (ROOT / "portable-release.json").read_bytes()
        )
        (self.candidate / "payload.bin").write_bytes(b"tracked payload\x00\xff")
        for source, _ in transaction.ASSETS:
            target = self.candidate / "install" / "bootstrap" / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((BOOTSTRAP / source).read_bytes())
        self.git("init", "--quiet")
        self.git("add", "--", ".gitignore", "portable-release.json", "payload.bin", "install")
        self.git("commit", "--quiet", "--no-gpg-sign", "-m", "Release safety fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.release = self.install / "releases" / self.commit
        self.validate_root()
        self.candidate.rename(self.release)
        runtime = self.release / ".venv"
        runtime.mkdir()
        (runtime / "runtime.bin").write_bytes(b"isolated runtime fixture\x00\xff")
        self.write_manifest()
        self.run_guard = mock.patch.object(subprocess, "run", side_effect=self.readonly_git)
        self.run_guard.start()
        self.addCleanup(self.run_guard.stop)

    def validate_root(self) -> None:
        self.assertEqual(self.root.resolve(strict=True), self.root)
        self.assertEqual(self.root.parent, self.temporary_parent)
        self.assertTrue(self.root.name.startswith("devloop-release-content-safety-"))
        self.assertFalse(self.root.is_symlink())

    def cleanup_fixture(self) -> None:
        self.validate_root()

        def writable_remove(function: object, path: str, _: object) -> None:
            target = Path(path)
            self.assertTrue(target.resolve(strict=True).is_relative_to(self.root))
            os.chmod(target, stat.S_IWRITE)
            function(path)  # type: ignore[operator]

        shutil.rmtree(self.root, onerror=writable_remove)

    def git(self, *arguments: str) -> str:
        options = list(arguments[:3]) if arguments[:1] == ("--no-optional-locks",) else []
        if options:
            self.assertEqual(options, ["--no-optional-locks", "-c", "diff.autoRefreshIndex=false"])
            arguments = arguments[3:]
        allowed = {
            ("init", "--quiet"),
            ("add", "--", ".gitignore", "portable-release.json", "payload.bin", "install"),
            ("commit", "--quiet", "--no-gpg-sign", "-m", "Release safety fixture"),
            ("rev-parse", "HEAD"),
            ("diff", "--quiet", "--"),
            ("diff", "--cached", "--quiet", "--"),
            ("ls-files", "--stage"),
            ("ls-files", "--others", "--exclude-standard"),
            ("ls-files", "--others", "--directory", "-z"),
            ("update-ref", "refs/heads/operator", self.commit if hasattr(self, "commit") else ""),
            ("show-ref", "--verify", "refs/heads/operator"),
            ("worktree", "add", "--quiet", "--detach", "--no-checkout",
             str(self.root / "operator-worktree"), self.commit if hasattr(self, "commit") else ""),
        }
        self.assertIn(arguments, allowed, "unreviewed Git command blocked")
        self.validate_root()
        self.assertTrue(self.release.resolve(strict=True).is_relative_to(self.root))
        result = self.real_run(
            [str(self.git_executable),
             "-c", f"core.hooksPath={self.empty_directory}",
             "-c", f"init.templateDir={self.empty_directory}",
             "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
             "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false",
             "-C", str(self.release), *options, *arguments],
            env=self.git_environment, cwd=self.root, capture_output=True,
            text=True, encoding="utf-8", check=False, timeout=15,
        )
        if result.returncode:
            raise RuntimeError(f"fixture Git validation failed: {result.stderr}")
        return result.stdout

    def readonly_git(
        self, arguments: list[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        self.assertEqual(arguments[:3], ["git", "-C", str(self.release)])
        self.assertEqual(arguments[3], "--no-optional-locks")
        self.assertIn(arguments[6], {"rev-parse", "diff", "ls-files"})
        before = (self.release / ".git" / "index").read_bytes()
        output = self.git(*arguments[3:])
        self.assertEqual(
            hashlib.sha256((self.release / ".git" / "index").read_bytes()).hexdigest(),
            hashlib.sha256(before).hexdigest(), arguments,
        )
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")

    def write_manifest(self) -> None:
        index = "\n".join(sorted(self.git("ls-files", "--stage").splitlines()))
        self.pointer: dict[str, object] = {
            "commit": self.commit,
            "release_path": f"releases/{self.commit}",
            "tracked_fingerprint": hashlib.sha256(index.encode()).hexdigest().upper(),
            "runtime_fingerprint": verify._directory_fingerprint(self.release / ".venv"),
        }
        metadata = json.loads((self.release / "portable-release.json").read_text())
        manifest = {
            "version": 2, **self.pointer,
            **{key: metadata[key] for key in transaction.COMPATIBILITY_FIELDS},
        }
        (self.bootstrap / f"release-{self.commit}.json").write_text(json.dumps(manifest))
        (self.bootstrap / "current.json").write_text(json.dumps({
            "version": 2, "current": self.pointer, "previous": None,
        }))

    def snapshot(self) -> dict[str, bytes | None]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in self.root.rglob("*")
        }

    def write_lock(self, operation: str) -> None:
        lock = self.root / ".bundle.install-lock"
        lock.mkdir()
        (lock / "owner.json").write_text(json.dumps({
            "version": 1,
            "transaction_id": TRANSACTION_ID,
            "owner_pid": os.getpid(),
            "owner_start": "fixture",
            "owner_host": "fixture",
            "operation": operation,
            "candidate_name": self.candidate.name,
        }))

    def write_layout(self) -> None:
        assets: dict[str, str] = {}
        for source, relative in transaction.ASSETS:
            destination = self.install / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = (BOOTSTRAP / source).read_bytes()
            destination.write_bytes(content)
            assets[relative] = hashlib.sha256(content).hexdigest().upper()
        (self.bootstrap / "layout.json").write_text(json.dumps({
            "version": 2, "assets": assets, "legacy_backups": {},
        }))

    @contextmanager
    def reject_mutations(self) -> Iterator[None]:
        original_open = io.open
        original_builtin_open = builtins.open

        def readonly_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(flag in mode for flag in "wax+"):
                raise AssertionError(f"filesystem write blocked: {file}")
            return original_open(file, mode, *args, **kwargs)

        def readonly_builtin_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(flag in mode for flag in "wax+"):
                raise AssertionError(f"filesystem write blocked: {file}")
            return original_builtin_open(file, mode, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(io, "open", side_effect=readonly_open))
            stack.enter_context(mock.patch.object(
                builtins, "open", side_effect=readonly_builtin_open,
            ))
            mutations = ("open", "mkdir", "rmdir", "unlink", "remove", "rename", "replace", "chmod")
            for name in mutations:
                stack.enter_context(mock.patch.object(
                    os, name, side_effect=AssertionError(f"filesystem {name} blocked"),
                ))
            stack.enter_context(mock.patch.object(
                shutil, "rmtree", side_effect=AssertionError("recursive removal blocked"),
            ))
            yield

    def test_ignored_log_blocks_public_release_verification_without_mutation(self) -> None:
        log = self.release / ".loop.logs" / "operator.log"
        log.parent.mkdir()
        log.write_bytes(b"operator log must survive\x00\xff")
        self.assertEqual(self.git("ls-files", "--others", "--exclude-standard"), "")
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "untracked content"):
                verify.verify_release(self.install, self.commit)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_ignored_config_blocks_uninstall_before_any_mutation(self) -> None:
        self.write_layout()
        self.write_lock("uninstall")
        (self.release / ".env.local").write_bytes(b"fixture setting, not a secret\x00\xff")
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "untracked content"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)
            self.assertFalse((self.root / f".bundle.uninstall-{TRANSACTION_ID}").exists())

    def test_git_operator_notes_block_uninstall_before_any_mutation(self) -> None:
        self.write_layout()
        self.write_lock("uninstall")
        (self.release / ".git" / "operator-notes").write_bytes(b"operator data\x00\xff")
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "Git ownership"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)
            self.assertFalse((self.root / f".bundle.uninstall-{TRANSACTION_ID}").exists())

    def stage_candidate(self) -> None:
        self.write_layout()
        self.write_lock("install")
        (self.bootstrap / "current.json").unlink()
        (self.bootstrap / f"release-{self.commit}.json").unlink()
        self.release.rename(self.candidate)
        self.release = self.candidate

    def prepare_owned_release(self) -> None:
        self.stage_candidate()
        prepared = transaction.prepare(self.install, self.candidate, self.commit, TRANSACTION_ID)
        self.release = prepared
        transaction.commit(self.install, TRANSACTION_ID)

    def test_pre_activation_git_ownership_allows_unchanged_release_removal(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        before_release = {
            path.relative_to(self.release).as_posix(): path.read_bytes() if path.is_file() else None
            for path in self.release.rglob("*")
        }

        class RemovalReached(Exception):
            pass

        def capture_removal(path: Path, **kwargs: object) -> None:
            self.assertEqual(path, self.release)
            raise RemovalReached

        # Stop at the destructive system seam: no release/installer is executed or removed.
        with mock.patch.object(shutil, "rmtree", side_effect=capture_removal):
            with self.assertRaises(RemovalReached):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        self.assertEqual({
            path.relative_to(self.release).as_posix(): path.read_bytes() if path.is_file() else None
            for path in self.release.rglob("*")
        }, before_release)

    def test_owned_git_admin_additions_block_verification_and_uninstall(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        for relative in ("operator-notes", "refs/heads/operator", "worktrees/operator/gitdir"):
            with self.subTest(relative=relative):
                target = self.release / ".git" / relative
                created = []
                parent = target.parent
                while not parent.exists():
                    created.append(parent)
                    parent = parent.parent
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((self.commit + "\n").encode())
                before = self.snapshot()
                try:
                    with self.reject_mutations():
                        with self.assertRaisesRegex(RuntimeError, "Git ownership"):
                            verify.verify(self.install)
                        with self.assertRaisesRegex(RuntimeError, "Git ownership"):
                            transaction.uninstall_layout(self.install, TRANSACTION_ID)
                finally:
                    self.assertEqual(self.snapshot(), before)
                    target.unlink()
                    for directory in created:
                        directory.rmdir()

    def test_normal_git_reads_preserve_owned_admin_bytes_with_optional_locks_enabled(self) -> None:
        self.git_environment.pop("GIT_OPTIONAL_LOCKS")
        self.prepare_owned_release()
        payload = self.release / "payload.bin"
        status = payload.stat()
        os.utime(payload, ns=(status.st_atime_ns, status.st_mtime_ns + 2_000_000_000))
        before = self.snapshot()
        with self.reject_mutations():
            for _ in range(3):
                self.assertEqual(verify.verify(self.install), self.release)
        self.assertEqual(self.snapshot(), before)

    def test_real_operator_branch_and_linked_worktree_survive_uninstall_refusal(self) -> None:
        self.prepare_owned_release()
        self.git("update-ref", "refs/heads/operator", self.commit)
        self.assertEqual(
            self.git("show-ref", "--verify", "refs/heads/operator").strip(),
            f"{self.commit} refs/heads/operator",
        )
        worktree = self.root / "operator-worktree"
        self.git("worktree", "add", "--quiet", "--detach", "--no-checkout",
                 str(worktree), self.commit)
        self.assertTrue((worktree / ".git").is_file())
        self.assertTrue((self.release / ".git" / "worktrees" / worktree.name / "gitdir").is_file())
        self.write_lock("uninstall")
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "Git ownership"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_git_ownership_receipt_requires_an_exact_integer_schema_version(self) -> None:
        self.prepare_owned_release()
        receipt = self.bootstrap / f"git-ownership-{self.commit}.json"
        value = json.loads(receipt.read_text())
        for version in (True, 1.0):
            with self.subTest(version=version):
                receipt.write_text(json.dumps({**value, "version": version}))
                before = self.snapshot()
                try:
                    with self.reject_mutations(), self.assertRaisesRegex(
                        RuntimeError, "Git ownership evidence has an unsupported schema",
                    ):
                        verify.verify(self.install)
                finally:
                    self.assertEqual(self.snapshot(), before)

    def test_empty_git_admin_directory_and_modified_config_are_not_owned(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        config = self.release / ".git" / "config"
        original = config.read_bytes()
        empty = self.release / ".git" / "operator-empty"
        for kind in ("empty-directory", "modified-config"):
            with self.subTest(kind=kind):
                if kind == "empty-directory":
                    empty.mkdir()
                else:
                    config.write_bytes(original + b"\n# operator-owned setting\n")
                before = self.snapshot()
                try:
                    with self.reject_mutations(), self.assertRaisesRegex(
                        RuntimeError, "Git ownership",
                    ):
                        transaction.uninstall_layout(self.install, TRANSACTION_ID)
                finally:
                    self.assertEqual(self.snapshot(), before)
                    if kind == "empty-directory":
                        empty.rmdir()
                    else:
                        config.write_bytes(original)

    def test_git_admin_root_reparse_point_is_rejected_before_uninstall_mutation(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        before = self.snapshot()
        original_lstat = Path.lstat

        def reparse_lstat(path: Path) -> os.stat_result | SimpleNamespace:
            status = original_lstat(path)
            if path == self.release / ".git":
                return SimpleNamespace(
                    st_mode=status.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return status

        try:
            with mock.patch.object(Path, "lstat", autospec=True, side_effect=reparse_lstat):
                with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "Git ownership"):
                    transaction.uninstall_layout(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_uninstall_retry_rejects_new_git_data_before_any_further_mutation(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        with mock.patch.object(shutil, "rmtree", side_effect=RuntimeError("removal intercepted")):
            with self.assertRaisesRegex(RuntimeError, "removal intercepted"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        (self.release / ".git" / "operator-notes").write_bytes(b"added after interrupted preflight")
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "Git ownership"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_uninstall_retry_continues_after_only_owned_git_file_was_removed(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        receipt = self.bootstrap / f"git-ownership-{self.commit}.json"
        original_receipt = receipt.read_bytes()
        removed = self.release / ".git" / "HEAD"

        def partial_removal(path: Path, **kwargs: object) -> None:
            self.assertEqual(path, self.release)
            self.validate_root()
            self.assertTrue(removed.resolve(strict=True).is_relative_to(self.root))
            removed.unlink()
            raise OSError("interrupted after owned HEAD removal")

        with mock.patch.object(shutil, "rmtree", side_effect=partial_removal):
            with self.assertRaisesRegex(OSError, "interrupted after owned HEAD removal"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        self.assertFalse(removed.exists())
        before_retry = self.snapshot()

        class RemovalReached(Exception):
            pass

        def capture_retry(path: Path, **kwargs: object) -> None:
            self.assertEqual(path, self.release)
            raise RemovalReached

        with mock.patch.object(shutil, "rmtree", side_effect=capture_retry):
            with self.assertRaises(RemovalReached):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        self.assertEqual(receipt.read_bytes(), original_receipt)
        self.assertEqual(self.snapshot(), before_retry)

    def test_partial_uninstall_retry_preserves_new_and_changed_git_survivors(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        removed = self.release / ".git" / "HEAD"

        def partial_removal(path: Path, **kwargs: object) -> None:
            self.assertEqual(path, self.release)
            self.validate_root()
            self.assertTrue(removed.resolve(strict=True).is_relative_to(self.root))
            removed.unlink()
            raise OSError("owned HEAD removed")

        with mock.patch.object(shutil, "rmtree", side_effect=partial_removal):
            with self.assertRaisesRegex(OSError, "owned HEAD removed"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        config = self.release / ".git" / "config"
        config_bytes = config.read_bytes()
        for kind in ("new-file", "new-directory", "changed-file", "changed-kind"):
            with self.subTest(kind=kind):
                added = self.release / ".git" / "operator-data"
                if kind == "new-file":
                    added.write_bytes(b"new operator-owned data")
                elif kind == "new-directory":
                    added.mkdir()
                elif kind == "changed-file":
                    config.write_bytes(config_bytes + b"\n# operator changes\n")
                else:
                    self.validate_root()
                    self.assertTrue(config.resolve(strict=True).is_relative_to(self.root))
                    config.unlink()
                    config.mkdir()
                before = self.snapshot()
                try:
                    with self.reject_mutations(), self.assertRaisesRegex(
                        RuntimeError, "Git ownership survivors changed",
                    ):
                        transaction.uninstall_layout(self.install, TRANSACTION_ID)
                finally:
                    self.assertEqual(self.snapshot(), before)
                    self.validate_root()
                    if kind == "new-file":
                        added.unlink()
                    elif kind == "new-directory":
                        added.rmdir()
                    elif kind == "changed-file":
                        config.write_bytes(config_bytes)
                    else:
                        config.rmdir()
                        config.write_bytes(config_bytes)

    def test_proof_backed_retry_accepts_missing_git_without_running_git(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        with mock.patch.object(shutil, "rmtree", side_effect=RuntimeError("removal intercepted")):
            with self.assertRaisesRegex(RuntimeError, "removal intercepted"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        git_root = self.release / ".git"
        self.validate_root()
        self.assertTrue(git_root.resolve(strict=True).is_relative_to(self.root))
        retained_git = self.root / "retained-git-fixture"
        git_root.rename(retained_git)
        before = self.snapshot()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("Git must not run")):
            with mock.patch.object(shutil, "rmtree", side_effect=RuntimeError("retry reached")):
                with self.assertRaisesRegex(RuntimeError, "retry reached"):
                    transaction.uninstall_layout(self.install, TRANSACTION_ID)
        self.assertEqual(self.snapshot(), before)

    def test_pending_release_action_cannot_use_partial_git_proof(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        with mock.patch.object(shutil, "rmtree", side_effect=RuntimeError("removal intercepted")):
            with self.assertRaisesRegex(RuntimeError, "removal intercepted"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        staging = self.root / f".bundle.uninstall-{TRANSACTION_ID}"
        journal_path = staging / "journal.json"
        journal = json.loads(journal_path.read_text())
        release_action = next(action for action in journal["actions"]
                              if action["kind"] == "remove_release")
        release_action["state"] = "pending"
        journal_path.write_text(json.dumps(journal))
        removed = self.release / ".git" / "HEAD"
        self.validate_root()
        self.assertTrue(removed.resolve(strict=True).is_relative_to(self.root))
        removed.unlink()
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(
                RuntimeError, "Git ownership survivors changed",
            ):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_old_uninstall_without_entry_proof_requires_exact_original_git_inventory(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        with mock.patch.object(shutil, "rmtree", side_effect=RuntimeError("removal intercepted")):
            with self.assertRaisesRegex(RuntimeError, "removal intercepted"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        proof = (
            self.root / f".bundle.uninstall-{TRANSACTION_ID}" / f"git-entries-{self.commit}.json"
        )
        self.validate_root()
        self.assertTrue(proof.resolve(strict=True).is_relative_to(self.root))
        proof.unlink()
        receipt = self.bootstrap / f"git-ownership-{self.commit}.json"
        receipt_bytes = receipt.read_bytes()
        with mock.patch.object(shutil, "rmtree", side_effect=RuntimeError("intact retry reached")):
            with self.assertRaisesRegex(RuntimeError, "intact retry reached"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        self.assertTrue(proof.is_file())
        proof.unlink()
        removed = self.release / ".git" / "HEAD"
        self.assertTrue(removed.resolve(strict=True).is_relative_to(self.root))
        removed.unlink()
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(
                RuntimeError, "proof missing; preserve and restore owned data",
            ):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(receipt.read_bytes(), receipt_bytes)

    def test_interrupted_git_proof_publication_retries_from_original_ownership(self) -> None:
        self.assert_interrupted_git_proof_retry(after_publication=False)

    def test_published_git_proof_retries_before_release_action_checkpoint(self) -> None:
        self.assert_interrupted_git_proof_retry(after_publication=True)

    def assert_interrupted_git_proof_retry(self, *, after_publication: bool) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        receipt = self.bootstrap / f"git-ownership-{self.commit}.json"
        receipt_bytes = receipt.read_bytes()
        proof = (
            self.root / f".bundle.uninstall-{TRANSACTION_ID}" / f"git-entries-{self.commit}.json"
        )
        original_replace = os.replace

        def interrupt_proof(source: Path, destination: Path) -> None:
            if Path(destination) == proof:
                if after_publication:
                    original_replace(source, destination)
                raise OSError("proof publication interrupted")
            original_replace(source, destination)

        with mock.patch.object(os, "replace", side_effect=interrupt_proof):
            with self.assertRaisesRegex(OSError, "proof publication interrupted"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        self.assertEqual(proof.exists(), after_publication)
        journal = json.loads((proof.parent / "journal.json").read_text())
        self.assertTrue(all(action["state"] == "pending" for action in journal["actions"]))
        before_release = {path.relative_to(self.release): path.read_bytes()
                          for path in self.release.rglob("*") if path.is_file()}
        with mock.patch.object(shutil, "rmtree", side_effect=RuntimeError("retry reached")):
            with self.assertRaisesRegex(RuntimeError, "retry reached"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        self.assertTrue(proof.is_file())
        self.assertEqual(receipt.read_bytes(), receipt_bytes)
        self.assertEqual({path.relative_to(self.release): path.read_bytes()
                          for path in self.release.rglob("*") if path.is_file()}, before_release)

    def test_uninstall_retry_rejects_reparse_entry_proof_before_mutation(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        with mock.patch.object(shutil, "rmtree", side_effect=RuntimeError("removal intercepted")):
            with self.assertRaisesRegex(RuntimeError, "removal intercepted"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        proof = (
            self.root / f".bundle.uninstall-{TRANSACTION_ID}" / f"git-entries-{self.commit}.json"
        )
        self.assertTrue(proof.is_file())
        before = self.snapshot()
        original_lstat = Path.lstat

        original_exists = Path.exists
        for target, dangling in ((proof, False), (proof.parent, False), (self.root, False),
                                 (proof, True)):
            with self.subTest(target=target.name, dangling=dangling):
                def reparse_lstat(
                    path: Path, *, expected: Path = target, is_dangling: bool = dangling,
                ) -> os.stat_result | SimpleNamespace:
                    status = original_lstat(path)
                    if path == expected:
                        return SimpleNamespace(
                            st_mode=stat.S_IFLNK if is_dangling else status.st_mode,
                            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                        )
                    return status

                def entry_exists(
                    path: Path, *, expected: Path = target, is_dangling: bool = dangling,
                ) -> bool:
                    return False if path == expected and is_dangling else original_exists(path)

                try:
                    with mock.patch.object(Path, "lstat", autospec=True, side_effect=reparse_lstat):
                        with mock.patch.object(
                            Path, "exists", autospec=True, side_effect=entry_exists,
                        ):
                            with self.reject_mutations(), self.assertRaisesRegex(
                                RuntimeError, "Git ownership",
                            ):
                                transaction.uninstall_layout(self.install, TRANSACTION_ID)
                finally:
                    self.assertEqual(self.snapshot(), before)

    def test_uninstall_retry_rejects_corrupt_entry_proof_without_recapturing(self) -> None:
        self.prepare_owned_release()
        self.write_lock("uninstall")
        with mock.patch.object(shutil, "rmtree", side_effect=RuntimeError("removal intercepted")):
            with self.assertRaisesRegex(RuntimeError, "removal intercepted"):
                transaction.uninstall_layout(self.install, TRANSACTION_ID)
        proof = (
            self.root / f".bundle.uninstall-{TRANSACTION_ID}" / f"git-entries-{self.commit}.json"
        )
        original = json.loads(proof.read_text())
        entries = original["entries"]
        variants: tuple[dict[str, object], ...] = (
            {"version": True}, {"version": 1.0}, {"extra": "unowned"},
            {"transaction_id": "44444444-4444-4444-8444-444444444444"},
            {"install_root": str(self.root)}, {"commit": "a" * 40},
            {"git_fingerprint": "A" * 64}, {"entries": {}},
            {"entries": entries + [entries[0]]}, {"entries": list(reversed(entries))},
            {"entries": [["../HEAD", "file", "a" * 64]]},
            {"entries": [["HEAD", "file", True]]},
            {"entries": [["HEAD", "file", "A" * 64]]},
            {"entries": [["HEAD", "directory", "not-empty"]]},
            {"entries": [["HEAD", "symlink", ""]]},
            {"entries": [["missing-parent/HEAD", "file", "a" * 64]]},
            {"entries": entries[1:]},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                proof.write_text(json.dumps({**original, **variant}))
                before = self.snapshot()
                try:
                    with self.reject_mutations(), self.assertRaisesRegex(
                        RuntimeError, "Git ownership",
                    ):
                        transaction.uninstall_layout(self.install, TRANSACTION_ID)
                finally:
                    self.assertEqual(self.snapshot(), before)

    def test_journaled_recovery_does_not_recapture_modified_git_ownership(self) -> None:
        self.stage_candidate()
        real_replace = os.replace

        def interrupt_journal(source: Path, destination: Path) -> None:
            real_replace(source, destination)
            if Path(destination).name == "install-transaction.json":
                raise RuntimeError("journal publication intercepted")

        with mock.patch.object(os, "replace", side_effect=interrupt_journal):
            with self.assertRaisesRegex(RuntimeError, "journal publication intercepted"):
                transaction.prepare(self.install, self.candidate, self.commit, TRANSACTION_ID)
        (self.candidate / ".git" / "operator-notes").write_bytes(b"after durable journal")
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "Git ownership"):
                transaction.recover(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_legacy_duplicate_recovery_preserves_unproven_git_admin_before_mutation(self) -> None:
        self.write_lock("install")
        canonical = self.release
        shutil.copytree(canonical, self.candidate)
        (self.candidate / ".git" / "operator-notes").write_bytes(b"operator data must survive")
        (self.bootstrap / "install-transaction.json").write_text(json.dumps({
            "version": 2,
            "transaction_id": TRANSACTION_ID,
            "install_root": str(self.install),
            "candidate_path": str(self.candidate),
            **self.pointer,
            "previous_pointer": None,
            "previous_previous_pointer": None,
            "phase": "prepared",
        }))
        (self.bootstrap / f"candidate-{TRANSACTION_ID}.json").write_text(json.dumps({
            "version": 1, "transaction_id": TRANSACTION_ID,
            "candidate_name": self.candidate.name, "commit": self.commit,
        }))
        before = self.snapshot()

        def routed_git(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.release = Path(arguments[2])
            self.assertIn(self.release, (self.candidate, canonical))
            return self.readonly_git(arguments, **kwargs)

        try:
            with mock.patch.object(subprocess, "run", side_effect=routed_git):
                with self.reject_mutations(), self.assertRaisesRegex(
                    RuntimeError, "Git ownership evidence is missing",
                ):
                    transaction.recover(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_duplicate_recovery_accepts_original_prepare_time_git_ownership(self) -> None:
        self.prepare_owned_release()
        self.write_lock("install")
        canonical = self.release
        shutil.copytree(canonical, self.candidate)
        (self.bootstrap / "install-transaction.json").write_text(json.dumps({
            "version": 2, "transaction_id": TRANSACTION_ID,
            "install_root": str(self.install), "candidate_path": str(self.candidate),
            **self.pointer, "previous_pointer": None, "previous_previous_pointer": None,
            "phase": "prepared",
        }))
        (self.bootstrap / f"candidate-{TRANSACTION_ID}.json").write_text(json.dumps({
            "version": 1, "transaction_id": TRANSACTION_ID,
            "candidate_name": self.candidate.name, "commit": self.commit,
        }))
        before = self.snapshot()

        def routed_git(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.release = Path(arguments[2])
            self.assertIn(self.release, (self.candidate, canonical))
            return self.readonly_git(arguments, **kwargs)

        class RemovalReached(Exception):
            pass

        def capture_removal(path: Path, **kwargs: object) -> None:
            self.assertEqual(path, self.candidate)
            raise RemovalReached

        with mock.patch.object(subprocess, "run", side_effect=routed_git):
            with self.reject_mutations():
                with mock.patch.object(shutil, "rmtree", side_effect=capture_removal):
                    with self.assertRaises(RemovalReached):
                        transaction.recover(self.install, TRANSACTION_ID)
        self.assertEqual(self.snapshot(), before)

    def test_receipt_survives_crash_before_journal_and_is_not_replaced_on_retry(self) -> None:
        self.stage_candidate()
        real_replace = os.replace

        def interrupt_after_receipt(source: Path, destination: Path) -> None:
            real_replace(source, destination)
            if Path(destination).name == f"git-ownership-{self.commit}.json":
                raise RuntimeError("receipt publication intercepted")

        with mock.patch.object(os, "replace", side_effect=interrupt_after_receipt):
            with self.assertRaisesRegex(RuntimeError, "receipt publication intercepted"):
                transaction.prepare(self.install, self.candidate, self.commit, TRANSACTION_ID)
        self.assertFalse((self.bootstrap / "install-transaction.json").exists())
        (self.candidate / ".git" / "operator-notes").write_bytes(b"after durable receipt")
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "Git ownership"):
                transaction.prepare(self.install, self.candidate, self.commit, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_unchanged_receipt_survives_journaled_recovery_and_commit(self) -> None:
        self.stage_candidate()
        real_replace = os.replace

        def interrupt_journal(source: Path, destination: Path) -> None:
            real_replace(source, destination)
            if Path(destination).name == "install-transaction.json":
                raise RuntimeError("journal publication intercepted")

        with mock.patch.object(os, "replace", side_effect=interrupt_journal):
            with self.assertRaisesRegex(RuntimeError, "journal publication intercepted"):
                transaction.prepare(self.install, self.candidate, self.commit, TRANSACTION_ID)
        receipt = self.bootstrap / f"git-ownership-{self.commit}.json"
        before = receipt.read_bytes()
        original_readonly_git = self.readonly_git

        def moved_git(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            # Only adapt the external Git seam to the reviewed atomic candidate move.
            self.release = Path(arguments[2])
            self.assertIn(self.release, (self.candidate, self.install / "releases" / self.commit))
            return original_readonly_git(arguments, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=moved_git):
            action, release = transaction.recover(self.install, TRANSACTION_ID)
        self.assertEqual((action, release), ("NEEDS_ADOPTION", self.release))
        transaction.commit(self.install, TRANSACTION_ID)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse((self.bootstrap / "install-transaction.json").exists())
        self.assertEqual(verify.verify(self.install), self.release)

    def test_legacy_manifests_remain_runnable_but_uninstall_cannot_invent_ownership(self) -> None:
        self.write_layout()
        self.write_lock("uninstall")
        original_manifest = (self.bootstrap / f"release-{self.commit}.json").read_bytes()
        original_pointer = (self.bootstrap / "current.json").read_bytes()
        for version in (1, 2):
            with self.subTest(version=version):
                if version == 1:
                    legacy = json.dumps({"version": 1, **self.pointer})
                    (self.bootstrap / f"release-{self.commit}.json").write_text(legacy)
                    (self.bootstrap / "current.json").write_text(legacy)
                else:
                    (self.bootstrap / f"release-{self.commit}.json").write_bytes(original_manifest)
                    (self.bootstrap / "current.json").write_bytes(original_pointer)
                before = self.snapshot()
                with self.reject_mutations():
                    self.assertEqual(verify.verify(self.install), self.release)
                    with self.assertRaisesRegex(RuntimeError, "Git ownership evidence is missing"):
                        transaction.uninstall_layout(self.install, TRANSACTION_ID)
                self.assertEqual(self.snapshot(), before)

    def test_ignored_state_blocks_prepare_before_any_mutation(self) -> None:
        self.write_layout()
        self.write_lock("install")
        self.validate_root()
        (self.bootstrap / "current.json").unlink()
        self.release.rename(self.candidate)
        self.release = self.candidate
        (self.candidate / "README.loop.state.json").write_bytes(b"operator state\x00\xff")
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "untracked content"):
                transaction.prepare(self.install, self.candidate, self.commit, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_empty_unmanaged_directories_block_verification(self) -> None:
        for relative in (".loop.logs", "operator-empty", "install/bootstrap/empty", ".venv.backup"):
            with self.subTest(directory=relative):
                directory = self.release / relative
                directory.mkdir()
                before = self.snapshot()
                try:
                    inventory = self.git("ls-files", "--others", "--directory", "-z").split("\0")
                    self.assertIn(relative + "/", inventory)
                    with self.reject_mutations(), self.assertRaisesRegex(
                        RuntimeError, "untracked content",
                    ):
                        verify.verify_release(self.install, self.commit)
                finally:
                    self.assertEqual(self.snapshot(), before)
                    self.validate_root()
                    directory.rmdir()

    def test_ignored_project_files_block_pointer_verification(self) -> None:
        paths = (
            "README.loop.md", "README.loop.state.json", ".env", ".env.operator",
            ".devloop/runs/operator/state.json", "install/bootstrap/.loop.logs/operator.log",
            ".venv.backup/operator.txt",
        )
        for relative in paths:
            with self.subTest(file=relative):
                target = self.release / relative
                created_directories = []
                parent = target.parent
                while not parent.exists():
                    created_directories.append(parent)
                    parent = parent.parent
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"operator data must survive\x00\xff")
                before = self.snapshot()
                try:
                    with self.reject_mutations(), self.assertRaisesRegex(
                        RuntimeError, "untracked content",
                    ):
                        verify.verify(self.install)
                finally:
                    self.assertEqual(self.snapshot(), before)
                    self.validate_root()
                    target.unlink()
                    for directory in created_directories:
                        directory.rmdir()

    def test_clean_tracked_payload_and_fingerprinted_runtime_remain_valid(self) -> None:
        self.assertEqual(self.git("ls-files", "--others", "--directory", "-z"), ".venv/\0")
        before = self.snapshot()
        with self.reject_mutations():
            self.assertEqual(verify.verify_release(self.install, self.commit), self.release)
            self.assertEqual(verify.verify(self.install), self.release)
        self.assertEqual(self.snapshot(), before)

    def test_added_runtime_file_still_requires_matching_fingerprint(self) -> None:
        (self.release / ".venv" / "operator.log").write_bytes(b"unrecorded runtime content")
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(
                RuntimeError, "runtime fingerprint mismatch",
            ):
                verify.verify_release(self.install, self.commit)
        finally:
            self.assertEqual(self.snapshot(), before)

    def test_nul_inventory_preserves_whitespace_and_unicode_names(self) -> None:
        relative = "operator ü file.log"
        (self.release / relative).write_bytes(b"operator data must survive")
        inventory = self.git("ls-files", "--others", "--directory", "-z").split("\0")
        self.assertIn(relative, inventory)
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, "untracked content"):
                verify.verify_release(self.install, self.commit)
        finally:
            self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()

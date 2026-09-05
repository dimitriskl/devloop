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
             "-C", str(self.release), *arguments],
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
        self.assertIn(arguments[3], {"rev-parse", "diff", "ls-files"})
        return subprocess.CompletedProcess(arguments, 0, stdout=self.git(*arguments[3:]), stderr="")

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

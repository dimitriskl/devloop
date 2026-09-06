from __future__ import annotations

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
from unittest import mock

BOOTSTRAP = Path(__file__).resolve().parents[1] / "install" / "bootstrap"
sys.path.insert(0, str(BOOTSTRAP))
try:
    import transaction
finally:
    sys.path.remove(str(BOOTSTRAP))

TRANSACTION_ID = "11111111-1111-4111-8111-111111111111"
OTHER_TRANSACTION_ID = "22222222-2222-4222-8222-222222222222"
COMMIT = "a" * 40
DURABLE_PHASES = (
    "journaled", "owned", "manifested", "prepared", "release_ready", "adopted",
    "switched", "committed",
)


class CandidateSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        # Ignore redirected TMP/TEMP values that could point into a source checkout.
        temporary_parent = (
            Path(os.environ["LOCALAPPDATA"]) / "Temp"
            if os.name == "nt"
            else Path("/tmp")
        ).resolve(strict=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="devloop-candidate-safety-", dir=temporary_parent
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.assertEqual(self.root.parent, temporary_parent)
        self.install = self.root / "bundle"
        self.bootstrap = self.install / "bootstrap"
        self.bootstrap.mkdir(parents=True)
        (self.install / "releases").mkdir()
        self.candidate = self.root / f".bundle.candidate-{TRANSACTION_ID}"
        self.candidate.mkdir()
        (self.candidate / "payload.bin").write_bytes(b"candidate\x00\xff")
        self.source = self.root / "source-checkout"
        self.source.mkdir()
        (self.source / "source.bin").write_bytes(b"source\x00\xff")
        lock = self.root / ".bundle.install-lock"
        lock.mkdir()
        self.write_json(
            lock / "owner.json",
            {
                "version": 1,
                "transaction_id": TRANSACTION_ID,
                "owner_pid": os.getpid(),
                "owner_start": "fixture",
                "owner_host": "fixture",
                "operation": "install",
                "candidate_name": self.candidate.name,
            },
        )
        self.subprocess_guard = mock.patch.object(
            subprocess, "run", side_effect=AssertionError("external subprocess invocation blocked")
        )
        self.run = self.subprocess_guard.start()
        self.addCleanup(self.subprocess_guard.stop)
        self.evidence: dict[str, object] = {
            "commit": COMMIT,
            "release_path": f"releases/{COMMIT}",
            "tracked_fingerprint": "A" * 64,
            "runtime_fingerprint": "B" * 64,
        }

    @staticmethod
    def write_json(path: Path, value: dict[str, object]) -> None:
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    def write_journal(self, candidate: object, phase: str = "journaled") -> None:
        self.write_json(
            self.bootstrap / "install-transaction.json",
            {
                "version": 2,
                "transaction_id": TRANSACTION_ID,
                "install_root": str(self.install),
                "candidate_path": candidate,
                **self.evidence,
                "previous_pointer": None,
                "previous_previous_pointer": None,
                "phase": phase,
            },
        )

    def write_ownership(self, candidate_name: str) -> None:
        self.write_json(
            self.bootstrap / f"candidate-{TRANSACTION_ID}.json",
            {
                "version": 1,
                "transaction_id": TRANSACTION_ID,
                "candidate_name": candidate_name,
                "commit": COMMIT,
            },
        )

    def snapshot(self) -> dict[str, bytes | None]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in self.root.rglob("*")
        }

    @contextmanager
    def reject_mutations(self) -> Iterator[None]:
        original_open = io.open
        original_os_open = os.open

        def readonly_open(file: object, mode: str = "r", *args: object, **kwargs: object):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError(f"filesystem write blocked: {file}")
            return original_open(file, mode, *args, **kwargs)

        def readonly_os_open(path: object, flags: int, *args: object, **kwargs: object):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise AssertionError(f"filesystem write blocked: {path}")
            return original_os_open(path, flags, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(io, "open", side_effect=readonly_open))
            stack.enter_context(mock.patch.object(os, "open", side_effect=readonly_os_open))
            for name in ("mkdir", "rmdir", "unlink", "remove", "rename", "replace", "chmod"):
                stack.enter_context(
                    mock.patch.object(
                        os, name, side_effect=AssertionError(f"filesystem {name} blocked")
                    )
                )
            yield

    def assert_recovery_rejected(self, message: str = "candidate path") -> None:
        before = self.snapshot()
        try:
            with self.reject_mutations(), self.assertRaisesRegex(RuntimeError, message):
                transaction.recover(self.install, TRANSACTION_ID)
        finally:
            self.assertEqual(self.snapshot(), before)
            self.run.assert_not_called()

    def test_journaled_source_candidate_is_rejected_before_git_or_mutation(self) -> None:
        self.write_journal(str(self.source))
        self.assert_recovery_rejected()

    def test_every_durable_phase_rejects_paths_outside_exact_transaction_scope(self) -> None:
        other_candidate = self.root / f".bundle.candidate-{OTHER_TRANSACTION_ID}"
        other_candidate.mkdir()
        (other_candidate / "keep.bin").write_bytes(b"other transaction\x00\xff")
        invalid_paths: tuple[object, ...] = (
            str(self.source),
            str(self.install),
            str(Path(self.root.anchor)),
            str(other_candidate),
            str(self.root / ".bundle.candidate-123-456"),
            str(self.source / self.candidate.name),
            self.candidate.name,
            str(self.root / "missing-parent" / ".." / self.candidate.name),
            str(self.root) + os.sep + "." + os.sep + self.candidate.name,
            str(self.candidate) + os.sep,
            None,
        )
        for phase in DURABLE_PHASES:
            for candidate_path in invalid_paths:
                with self.subTest(phase=phase, candidate_path=candidate_path):
                    self.write_journal(candidate_path, phase)
                    self.write_ownership(
                        Path(candidate_path).name
                        if isinstance(candidate_path, str)
                        else self.candidate.name
                    )
                    self.assert_recovery_rejected()

    def test_prepare_and_publish_reject_other_transaction_before_evidence_or_writes(self) -> None:
        other_candidate = self.root / f".bundle.candidate-{OTHER_TRANSACTION_ID}"
        other_candidate.mkdir()
        for command in (transaction.prepare, transaction.publish):
            with self.subTest(command=command.__name__):
                before = self.snapshot()
                arguments = (COMMIT, TRANSACTION_ID) if command is transaction.prepare else (
                    TRANSACTION_ID,
                )
                try:
                    with self.reject_mutations(), self.assertRaisesRegex(
                        RuntimeError, "candidate path"
                    ):
                        command(self.install, other_candidate, *arguments)
                finally:
                    self.assertEqual(self.snapshot(), before)
                    self.run.assert_not_called()

    def test_candidate_and_ancestor_reparse_points_are_rejected_in_every_phase(self) -> None:
        original_lstat = Path.lstat
        for phase in DURABLE_PHASES:
            for reparse_path in (self.candidate, self.root):
                with self.subTest(phase=phase, reparse_path=reparse_path):
                    self.write_journal(str(self.candidate), phase)
                    self.write_ownership(self.candidate.name)

                    def reparse_lstat(
                        path: Path, *args: object, reparse_target: Path = reparse_path,
                        **kwargs: object,
                    ):
                        result = original_lstat(path, *args, **kwargs)
                        if path == reparse_target:
                            return SimpleNamespace(
                                st_mode=result.st_mode,
                                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                            )
                        return result

                    with mock.patch.object(Path, "lstat", autospec=True, side_effect=reparse_lstat):
                        self.assert_recovery_rejected()

    def seed_recoverable_journal(self, phase: str) -> Path:
        metadata = json.loads((BOOTSTRAP.parents[1] / "portable-release.json").read_text())
        self.write_json(self.candidate / "portable-release.json", metadata)
        runtime = self.candidate / ".venv"
        runtime.mkdir()
        runtime_bytes = b"isolated runtime fixture"
        (runtime / "runtime.bin").write_bytes(runtime_bytes)
        index = f"100644 {'1' * 40} 0\tpayload.bin\n100644 {'2' * 40} 0\tportable-release.json"
        runtime_entry = f"runtime.bin={hashlib.sha256(runtime_bytes).hexdigest().upper()}"
        self.evidence = {
            "commit": COMMIT,
            "release_path": f"releases/{COMMIT}",
            "tracked_fingerprint": hashlib.sha256(index.encode()).hexdigest().upper(),
            "runtime_fingerprint": hashlib.sha256(runtime_entry.encode()).hexdigest().upper(),
        }
        self.write_journal(str(self.candidate), phase)
        if phase != "journaled":
            self.write_ownership(self.candidate.name)
        if phase not in {"journaled", "owned"}:
            compatibility = {
                key: value for key, value in metadata.items() if key.endswith(("_min", "_max"))
            }
            self.write_json(
                self.bootstrap / f"release-{COMMIT}.json",
                {"version": 2, **self.evidence, **compatibility},
            )
        release = self.install / "releases" / COMMIT

        def git_response(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            self.assertEqual(arguments[:2], ["git", "-C"])
            self.assertIn(Path(arguments[2]), (self.candidate, release))
            self.assertEqual(arguments[3], "--no-optional-locks")
            self.assertEqual(arguments[4:6], ["-c", "diff.autoRefreshIndex=false"])
            responses = {
                ("rev-parse", "HEAD"): COMMIT,
                ("diff", "--quiet", "--"): "",
                ("diff", "--cached", "--quiet", "--"): "",
                ("ls-files", "--others", "--directory", "-z"): "",
                ("ls-files", "--stage"): index,
            }
            return subprocess.CompletedProcess(
                arguments, 0, stdout=responses[tuple(arguments[6:])], stderr=""
            )

        self.run.side_effect = git_response
        return release

    def assert_valid_recovery(self, phase: str, *, moved: bool = False) -> None:
        release = self.seed_recoverable_journal(phase)
        candidate_bytes = {
            path.relative_to(self.candidate): path.read_bytes()
            for path in self.candidate.rglob("*") if path.is_file()
        }
        source_before = (self.source / "source.bin").read_bytes()
        if moved:
            self.candidate.rename(release)
        if phase in {"switched", "committed"}:
            self.write_json(
                self.bootstrap / "current.json",
                {"version": 2, "current": self.evidence, "previous": None},
            )
        action, recovered_release = transaction.recover(self.install, TRANSACTION_ID)
        expected_action = (
            "COMPLETE" if phase in {"switched", "committed"}
            else "READY_TO_SWITCH" if phase == "adopted" else "NEEDS_ADOPTION"
        )
        self.assertEqual((action, recovered_release), (expected_action, release))
        self.assertFalse(self.candidate.exists())
        self.assertEqual(
            {path.relative_to(release): path.read_bytes()
             for path in release.rglob("*") if path.is_file()},
            candidate_bytes,
        )
        self.assertEqual((self.source / "source.bin").read_bytes(), source_before)
        self.assertGreater(self.run.call_count, 0)

    def test_journaled_candidate_recovers_without_preexisting_ownership_receipt(self) -> None:
        self.assert_valid_recovery("journaled")

    def test_owned_candidate_recovers(self) -> None:
        self.assert_valid_recovery("owned")

    def test_manifested_candidate_recovers(self) -> None:
        self.assert_valid_recovery("manifested")

    def test_prepared_candidate_recovers(self) -> None:
        self.assert_valid_recovery("prepared")

    def test_prepared_journal_recovers_after_candidate_has_moved(self) -> None:
        self.assert_valid_recovery("prepared", moved=True)

    def test_prepared_journal_recovers_with_candidate_and_canonical_release(self) -> None:
        release = self.seed_recoverable_journal("prepared")
        git_root = self.candidate / ".git"
        git_root.mkdir()
        head = (COMMIT + "\n").encode()
        (git_root / "HEAD").write_bytes(head)
        inventory = [["HEAD", "file", hashlib.sha256(head).hexdigest()]]
        encoded = json.dumps(inventory, ensure_ascii=True, separators=(",", ":")).encode()
        self.write_json(self.bootstrap / f"git-ownership-{COMMIT}.json", {
            "version": 1, "commit": COMMIT,
            "git_fingerprint": hashlib.sha256(encoded).hexdigest().upper(),
        })
        shutil.copytree(self.candidate, release)
        release_bytes = {
            path.relative_to(release): path.read_bytes()
            for path in release.rglob("*") if path.is_file()
        }
        source_bytes = (self.source / "source.bin").read_bytes()
        preserved = self.root / "preserved-duplicate-candidate"

        def retain_candidate(path: Path, **kwargs: object) -> None:
            self.assertEqual(path, self.candidate)
            self.assertEqual(path.resolve(strict=True).parent, self.root)
            path.rename(preserved)

        with mock.patch.object(shutil, "rmtree", side_effect=retain_candidate):
            self.assertEqual(
                transaction.recover(self.install, TRANSACTION_ID), ("NEEDS_ADOPTION", release)
            )
        self.assertFalse(self.candidate.exists())
        self.assertEqual(
            {path.relative_to(preserved): path.read_bytes()
             for path in preserved.rglob("*") if path.is_file()},
            release_bytes,
        )
        self.assertEqual(
            {path.relative_to(release): path.read_bytes()
             for path in release.rglob("*") if path.is_file()},
            release_bytes,
        )
        self.assertEqual((self.source / "source.bin").read_bytes(), source_bytes)

    def test_release_ready_recovers_with_missing_moved_candidate(self) -> None:
        self.assert_valid_recovery("release_ready", moved=True)

    def test_adopted_release_recovers_with_missing_moved_candidate(self) -> None:
        self.assert_valid_recovery("adopted", moved=True)

    def test_switched_release_finishes_with_missing_moved_candidate(self) -> None:
        self.assert_valid_recovery("switched", moved=True)

    def test_committed_release_finishes_with_missing_moved_candidate(self) -> None:
        self.assert_valid_recovery("committed", moved=True)

    def test_later_phases_require_receipt_before_evidence_or_mutation(self) -> None:
        for phase in DURABLE_PHASES[1:]:
            with self.subTest(phase=phase):
                self.write_journal(str(self.candidate), phase)
                before = self.snapshot()
                try:
                    with self.reject_mutations(), self.assertRaises(FileNotFoundError):
                        transaction.recover(self.install, TRANSACTION_ID)
                finally:
                    self.assertEqual(self.snapshot(), before)
                    self.run.assert_not_called()

    def test_later_phases_reject_mismatched_ownership_before_evidence_or_mutation(self) -> None:
        for phase in DURABLE_PHASES[1:]:
            with self.subTest(phase=phase):
                self.write_journal(str(self.candidate), phase)
                self.write_ownership(f".bundle.candidate-{OTHER_TRANSACTION_ID}")
                self.assert_recovery_rejected("ownership evidence does not match")

    def test_existing_candidate_must_be_a_plain_directory_in_every_phase(self) -> None:
        self.candidate.rename(self.root / "saved-candidate-fixture")
        self.candidate.write_bytes(b"not a directory\x00\xff")
        for phase in DURABLE_PHASES:
            with self.subTest(phase=phase):
                self.write_journal(str(self.candidate), phase)
                self.write_ownership(self.candidate.name)
                self.assert_recovery_rejected()


if __name__ == "__main__":
    unittest.main()

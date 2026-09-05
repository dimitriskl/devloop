"""Validate inert historical data without importing it or launching installers."""

from __future__ import annotations

import ast
import hashlib
import json
import unittest
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "portable_bootstrap_604"
HISTORICAL_COMMIT = "6048556f0f279cb54f4d1afa00f764227049eb8f"
HISTORICAL_PATHS = {
    "install/bootstrap/bin/devloop-plan.ps1",
    "install/bootstrap/bin/devloop-plan.sh",
    "install/bootstrap/bin/devloop.ps1",
    "install/bootstrap/bin/devloop.sh",
    "install/bootstrap/dispatch.ps1",
    "install/bootstrap/dispatch.sh",
    "install/bootstrap/install/devloop.ps1",
    "install/bootstrap/install/devloop.sh",
    "install/bootstrap/install/uninstall-devloop.ps1",
    "install/bootstrap/install/uninstall-devloop.sh",
    "install/bootstrap/transaction.py",
    "install/bootstrap/verify.py",
    "install/devloop.ps1",
    "install/devloop.sh",
    "install/uninstall-devloop.ps1",
    "install/uninstall-devloop.sh",
    "portable-release.json",
    "src/devloop/portable_release.py",
}
# Independent historical diff targets, author log line 974 (not newly generated hashes).
RECORDED_BLOB_PREFIXES = {
    "install/bootstrap/install/devloop.ps1": "ff54387",
    "install/bootstrap/transaction.py": "796ebec",
    "install/devloop.ps1": "dfe87df",
    "install/devloop.sh": "021dd03",
}


class HistoricalBootstrapFixtureTests(unittest.TestCase):
    def test_frozen_overlay_preserves_inventory_bytes_and_historical_contract(self) -> None:
        manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
        sources = json.loads((FIXTURE / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["fixture_format"], 1)
        self.assertEqual(manifest["scope"], "historical-bootstrap-overlay")
        self.assertEqual(manifest["historical_commit"], HISTORICAL_COMMIT)
        self.assertIs(manifest["historical_commit_available"], False)
        self.assertIs(manifest["full_historical_tree"], False)
        self.assertEqual(manifest["source_encoding"], "utf-8")
        self.assertEqual(manifest["source_newlines"], "LF")
        self.assertEqual(set(sources), HISTORICAL_PATHS)
        self.assertEqual(set(manifest["files"]), HISTORICAL_PATHS)
        self.assertEqual(
            {path.name for path in FIXTURE.iterdir()},
            {"README.md", "manifest.json", "sources.json"},
            "Historical code must remain JSON data, not executable fixture files.",
        )
        for name, source in sources.items():
            with self.subTest(path=name):
                self.assertIsInstance(source, str)
                self.assertNotIn("\r", source)
                self.assertFalse(source.startswith("\ufeff"))
                raw = source.encode("utf-8")
                record = manifest["files"][name]
                self.assertEqual(len(raw), record["bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), record["sha256"])
                blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                self.assertEqual(blob, record["git_blob_sha1"])
                if name in RECORDED_BLOB_PREFIXES:
                    self.assertTrue(blob.startswith(RECORDED_BLOB_PREFIXES[name]))
        self.assertEqual(
            json.loads(sources["portable-release.json"]),
            {
                "product": "Portable Dev Loop",
                "generation": "v3",
                "version": "0.3.1",
                "codexcli": "separate product",
            },
            "The historical metadata predates the protocol-v2 compatibility fields.",
        )
        pointer_fields = {
            "version", "commit", "release_path", "tracked_fingerprint", "runtime_fingerprint"
        }
        for name in ("install/bootstrap/verify.py", "install/bootstrap/transaction.py"):
            with self.subTest(historical_schema=name):
                tree = ast.parse(sources[name], filename=name)
                assignment = next(
                    node
                    for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "POINTER_FIELDS"
                        for target in node.targets
                    )
                )
                self.assertEqual(ast.literal_eval(assignment.value), pointer_fields)
        self.assertIn('pointer["version"] != 1', sources["install/bootstrap/verify.py"])
        self.assertIn('layout.get("version") != 1', sources["install/bootstrap/transaction.py"])


if __name__ == "__main__":
    unittest.main()

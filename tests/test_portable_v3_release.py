from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import devloop
from devloop import cli, interactive_runner
from devloop.logo import render_logo
from devloop.portable_release import (
    PortableReleasePreparationError,
    prepare_portable_user_state,
    validate_portable_runtime,
)
from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_version import (
    PORTABLE_GENERATION,
    PORTABLE_PRODUCT_NAME,
    PORTABLE_VERSION,
)


ROOT = Path(__file__).resolve().parents[1]


class PortableVersionConsistencyTests(unittest.TestCase):
    def test_every_portable_version_surface_identifies_v3_release_031(self) -> None:
        self.assertEqual(PORTABLE_PRODUCT_NAME, "Portable Dev Loop")
        self.assertEqual(PORTABLE_GENERATION, "v3")
        self.assertEqual(PORTABLE_VERSION, "0.3.1")
        self.assertEqual(devloop.__version__, "0.2.1", "CodexCLI remains separate")
        self.assertIn("v0.3.1", render_logo(ROOT))
        for parser in (cli.build_parser(), interactive_runner.build_parser()):
            output = StringIO()
            with self.subTest(program=parser.prog), redirect_stdout(output):
                with self.assertRaisesRegex(SystemExit, "0"):
                    parser.parse_args(["--version"])
            self.assertEqual(
                output.getvalue().strip(),
                f"{parser.prog} - Portable Dev Loop v3 0.3.1",
            )
        metadata = json.loads((ROOT / "portable-release.json").read_text(encoding="utf-8"))
        self.assertEqual(
            metadata,
            {
                "product": "Portable Dev Loop",
                "generation": "v3",
                "version": "0.3.1",
                "codexcli": "separate product",
                "bootstrap_protocol_min": 2,
                "bootstrap_protocol_max": 2,
                "pointer_schema_min": 2,
                "pointer_schema_max": 2,
                "manifest_schema_min": 2,
                "manifest_schema_max": 2,
                "transaction_schema_min": 2,
                "transaction_schema_max": 2,
            },
        )

    def test_portable_runtime_dependencies_are_pinned_without_system_services(self) -> None:
        lines = {
            line.strip()
            for line in (ROOT / "requirements-portable.lock")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.startswith("#")
        }
        self.assertIn("textual==8.2.8", lines)
        self.assertTrue(all("==" in line for line in lines))
        self.assertFalse(
            {"psutil", "sqlalchemy", "redis", "grpcio"}
            & {line.partition("==")[0].lower() for line in lines}
        )


class PortableReleaseDocumentationTests(unittest.TestCase):
    def test_docs_cover_release_behavior_and_side_by_side_layout(self) -> None:
        guide = (ROOT / "docs" / "portable-v3-user-guide.md").read_text(encoding="utf-8")
        troubleshooting = (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
        release_notes = (ROOT / "docs" / "release-notes-portable-v0.3.1.md").read_text(
            encoding="utf-8"
        )
        layout = (ROOT / "docs" / "portable-v3-install-layout.md").read_text(encoding="utf-8")
        for term in (
            "Sessions",
            "saved projects",
            "worktree",
            "Pause",
            "Force Stop",
            "History",
            "Relink",
            "Plain Mode",
            "0.2.1",
            "Uninstall",
            "side-by-side",
        ):
            self.assertIn(term.lower(), guide.lower())
        for term in ("pointer", "journal", "symbolic link", "reparse point"):
            self.assertIn(term.lower(), troubleshooting.lower())
        self.assertIn("Portable Dev Loop v3 0.3.1", release_notes)
        self.assertIn("CodexCLI 0.2.1", release_notes)
        for term in (
            "bootstrap/current.json",
            "releases/<commit>",
            "legacy-assets",
            "atomic",
            "fingerprint",
            "No detached helper",
        ):
            self.assertIn(term.lower(), layout.lower())


class PortableReleasePreparationTests(unittest.TestCase):
    def test_runtime_validation_rejects_mismatched_release_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "portable-release.json").write_text(
                json.dumps(
                    {
                        "product": "Portable Dev Loop",
                        "generation": "v3",
                        "version": "9.9.9",
                        "codexcli": "separate product",
                    }
                ),
                encoding="utf-8",
            )
            (root / "requirements-portable.lock").write_text("textual==8.2.8\n", encoding="utf-8")
            with self.assertRaisesRegex(PortableReleasePreparationError, "release metadata"):
                validate_portable_runtime(bundle_root=root)

    def test_fresh_state_is_passive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "state" / "catalog.sqlite3"
            result = prepare_portable_user_state(
                catalog_path=catalog_path,
                configuration_path=root / "missing.json",
            )
            self.assertEqual(result.catalog_path, catalog_path.resolve())
            self.assertIsNone(result.adoption_report)
            catalog = PortableSessionCatalog(catalog_path)
            self.assertEqual(catalog.list_saved_projects(), ())
            self.assertEqual(catalog.list_sessions(), ())
            self.assertEqual(catalog.list_adoption_receipts(), ())


class PortableInstallerStaticContractTests(unittest.TestCase):
    def test_stable_bootstrap_and_transactions_use_only_foreground_standard_tools(self) -> None:
        paths = tuple((ROOT / "install" / "bootstrap").rglob("*")) + (
            ROOT / "install" / "devloop.ps1",
            ROOT / "install" / "devloop.sh",
        )
        content = "\n".join(
            path.read_text(encoding="utf-8", errors="replace") for path in paths if path.is_file()
        ).lower()
        self.assertNotIn("start-process", content)
        self.assertNotIn("nohup", content)
        self.assertNotIn("& disown", content)
        self.assertNotIn("daemon", content)
        self.assertIn("current.json", content)
        self.assertIn("runtime_fingerprint", content)
        self.assertIn("tracked_fingerprint", content)

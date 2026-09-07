"""Source/data checks for lane selection; no installer or historical code execution."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import portable_test_support as support
import pytest
import run_portable_operator_gate as gate
from conftest import pytest_collection_modifyitems

from devloop.portable_session_catalog import CATALOG_FILENAME, portable_session_catalog_path


class PortableTestLaneTests(unittest.TestCase):
    def test_unittest_cannot_bypass_operator_opt_in(self) -> None:
        with mock.patch.object(support, "_operator_enabled", False):
            with self.assertRaisesRegex(RuntimeError, "operator-only"):
                support.OperatorInstallTestCase.setUpClass()

    def test_selected_operator_tests_fail_before_setup_without_opt_in(self) -> None:
        config = mock.Mock()
        config.getoption.return_value = False
        item = mock.Mock()
        item.get_closest_marker.return_value = object()
        with self.assertRaisesRegex(pytest.UsageError, "1 operator-only"):
            pytest_collection_modifyitems(config, [item])

    def test_safe_selection_is_allowed_without_operator_opt_in(self) -> None:
        config = mock.Mock()
        config.getoption.return_value = False
        item = mock.Mock()
        item.get_closest_marker.return_value = None
        pytest_collection_modifyitems(config, [item])

    def test_operator_selection_requires_explicit_cli_option(self) -> None:
        config = mock.Mock()
        config.getoption.return_value = True
        item = mock.Mock()
        item.get_closest_marker.return_value = object()
        pytest_collection_modifyitems(config, [item])
        config.getoption.assert_called_with("--run-operator-install")

    def test_unapproved_materialization_stops_before_reading_or_writing(self) -> None:
        with mock.patch.object(support, "_operator_enabled", False):
            with mock.patch.object(Path, "read_bytes") as read:
                with self.assertRaisesRegex(RuntimeError, "operator-only"):
                    support.create_source_repository(support.ROOT / ".tmp-not-created")
            read.assert_not_called()


class PortableTestPathTests(unittest.TestCase):
    def test_safe_fixture_is_fresh_validated_and_retained_without_cleanup(self) -> None:
        with mock.patch.object(support, "_operator_enabled", False):
            with mock.patch("shutil.rmtree") as remove:
                first = support.fresh_fixture_directory()
                second = support.fresh_fixture_directory()
        self.assertNotEqual(first, second)
        for path in (first, second):
            self.assertEqual(path.parent, support.session_root())
            self.assertEqual(support.require_fixture_path(path), path)
            self.assertTrue(path.is_dir())
        remove.assert_not_called()

    def test_root_and_external_paths_are_rejected(self) -> None:
        for path in (support.ROOT, support.ROOT.parent / "victim", Path(support.ROOT.anchor)):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "below the workspace"):
                    support.validate_workspace_path(path)

    def test_existing_basetemp_is_rejected_without_cleanup(self) -> None:
        path = support.ROOT / "tests"
        with mock.patch("shutil.rmtree") as remove:
            with self.assertRaisesRegex(ValueError, "fresh, nonexistent"):
                support.validate_workspace_path(path)
        remove.assert_not_called()
        self.assertTrue(path.is_dir())

    def test_reparse_ancestor_is_rejected(self) -> None:
        path = support.ROOT / "tests" / "not-created"
        metadata = mock.Mock(st_mode=0o040755, st_file_attributes=0x400)
        with mock.patch.object(Path, "lstat", return_value=metadata):
            with self.assertRaisesRegex(ValueError, "alias/reparse"):
                support.validate_workspace_path(path)

    def test_fresh_workspace_basetemp_is_accepted(self) -> None:
        path = support.session_root() / "explicit-unused-basetemp"
        self.assertEqual(support.validate_workspace_path(path), path)
        self.assertFalse(path.exists())

    def test_environment_is_child_scoped_and_clears_repository_and_profile_overrides(self) -> None:
        supplied = {
            "PATH": "supplied-path", "GIT_DIR": "victim", "git_work_tree": "victim",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "victim-hooks", "BASH_ENV": "victim-shell-profile",
            "PYTHONPATH": "victim-python", "PYTHONSTARTUP": "victim-startup",
            "DEVLOOP_INSTALL_DIR": "victim", "DEVLOOP_TEST_INTERRUPT_AFTER_PHASE": "staged",
            "CODEX_HOME": "real-profile", "HOME": "real-home", "USERPROFILE": "real-profile",
            "XDG_STATE_HOME": str(support.ROOT.parent / "operator-state-do-not-touch"),
            "LOCALAPPDATA": str(support.ROOT.parent / "operator-local-data-do-not-touch"),
            "CODEX_SKILLS_PATH": "real-skills", "CODEX_AGENTS_PATH": "real-agents",
        }
        original = supplied.copy()
        with mock.patch.object(Path, "mkdir"):
            isolated = support.isolated_environment(support.session_root() / "env-probe", supplied)
        self.assertEqual(supplied, original)
        for key in ("GIT_DIR", "git_work_tree", "BASH_ENV", "PYTHONPATH", "PYTHONSTARTUP",
                    "DEVLOOP_INSTALL_DIR", "DEVLOOP_TEST_INTERRUPT_AFTER_PHASE",
                    "CODEX_SKILLS_PATH", "CODEX_AGENTS_PATH"):
            self.assertNotIn(key, isolated)
        self.assertEqual(isolated["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(isolated["GIT_CONFIG_SYSTEM"], os.devnull)
        self.assertEqual(isolated["GIT_CONFIG_KEY_0"], "core.hooksPath")
        self.assertEqual(isolated["GIT_CONFIG_VALUE_1"], "false")
        self.assertEqual(isolated["GIT_CONFIG_VALUE_2"], "false")
        for key in ("HOME", "USERPROFILE", "CODEX_HOME", "TMP", "TEMP", "TMPDIR", "APPDATA",
                    "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                    "XDG_CACHE_HOME", "GIT_TEMPLATE_DIR"):
            self.assertTrue(Path(isolated[key]).is_relative_to(support.session_root()))
        for platform, suffix in (("posix", Path("devloop") / CATALOG_FILENAME),
                                 ("nt", Path("DevLoop/state") / CATALOG_FILENAME)):
            selected = portable_session_catalog_path(
                environment=isolated, platform=platform, home=Path(isolated["HOME"]),
            )
            self.assertEqual(selected, support.session_root() / "env-probe" / "state" / suffix)
        self.assertEqual(supplied, original)


class HistoricalOverlayConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(
            (support.HISTORICAL_FIXTURE / "manifest.json").read_text(encoding="utf-8")
        )
        self.sources = json.loads(
            (support.HISTORICAL_FIXTURE / "sources.json").read_text(encoding="utf-8")
        )

    def read_fixture(self, path: Path, *args: object, **kwargs: object) -> str:
        return json.dumps(self.manifest if path.name == "manifest.json" else self.sources)

    def test_validates_all_eighteen_inert_files_without_materializing_or_launching(self) -> None:
        with mock.patch("subprocess.run") as run, mock.patch.object(Path, "write_bytes") as write:
            overlay = support.historical_overlay()
        self.assertEqual(set(overlay), support.HISTORICAL_PATHS)
        self.assertEqual(len(overlay), 18)
        run.assert_not_called()
        write.assert_not_called()

    def test_missing_or_additional_inventory_is_rejected(self) -> None:
        for action in ("missing", "extra"):
            with self.subTest(action=action):
                sources = self.sources.copy()
                if action == "missing":
                    sources.pop("install/devloop.ps1")
                else:
                    sources["../victim.py"] = "do not write"
                def read(path: Path, sources: dict = sources, **kwargs: object) -> str:
                    return json.dumps(self.manifest if path.name == "manifest.json" else sources)

                with mock.patch.object(Path, "read_text", autospec=True, side_effect=read):
                    with self.assertRaisesRegex(ValueError, "exact 18-file"):
                        support.historical_overlay()

    def test_corrupt_hash_newlines_bom_and_full_tree_claim_are_rejected(self) -> None:
        name = "install/devloop.ps1"
        original = self.sources[name]
        for changed in (original + "# changed\n", original.replace("\n", "\r\n"),
                        "\ufeff" + original):
            with self.subTest(changed=changed[:25]):
                self.sources[name] = changed
                with mock.patch.object(
                    Path, "read_text", autospec=True, side_effect=self.read_fixture,
                ):
                    with self.assertRaises(ValueError):
                        support.historical_overlay()
        self.sources[name] = original
        self.manifest["full_historical_tree"] = True
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=self.read_fixture):
            with self.assertRaisesRegex(ValueError, "identity"):
                support.historical_overlay()


class CurrentSourceInventoryTests(unittest.TestCase):
    def test_copies_current_bytes_from_explicit_git_inventory_without_launching_git(self) -> None:
        destination = support.session_root() / "source-copy-probe"
        name = "src/devloop/version.py"
        supplied = subprocess.CompletedProcess([], 0, stdout=name.encode() + b"\0", stderr=b"")
        with mock.patch.object(support, "_operator_enabled", True):
            with mock.patch("subprocess.run", return_value=supplied) as run:
                inventory = support.copy_current_source(destination, (name,))
        self.assertEqual((destination / name).read_bytes(), (support.ROOT / name).read_bytes())
        self.assertEqual(set(inventory), {name})
        command = run.call_args.args[0]
        self.assertIn("ls-files", command)
        self.assertIn("--others", command)
        self.assertIn("--exclude-standard", command)
        self.assertNotIn("clone", command)
        self.assertEqual(run.call_count, 1)

    def test_invalid_inventory_fails_before_any_source_is_written(self) -> None:
        destination = support.session_root() / "invalid-source-probe"
        supplied = subprocess.CompletedProcess([], 0, stdout=b"../victim.py\0", stderr=b"")
        with mock.patch.object(support, "_operator_enabled", True):
            with mock.patch("subprocess.run", return_value=supplied):
                with self.assertRaisesRegex(ValueError, "Invalid current-source path"):
                    support.copy_current_source(destination)
        self.assertFalse(destination.exists())

    def test_git_checks_actual_reported_scope_before_a_mutation(self) -> None:
        fixture = support.session_root() / "git-scope-probe"
        (fixture / ".git").mkdir(parents=True)
        escaped = subprocess.CompletedProcess([], 0, stdout="/outside/victim\n", stderr="")
        with mock.patch.object(support, "_operator_enabled", True):
            with mock.patch("subprocess.run", return_value=escaped) as run:
                with self.assertRaisesRegex(ValueError, "escapes the private fixture"):
                    support.git(fixture, "checkout", "--force", "HEAD")
        self.assertEqual(run.call_count, 1)
        self.assertIn("rev-parse", run.call_args.args[0])
        self.assertNotIn("checkout", run.call_args.args[0])


class OperatorEvidenceTests(unittest.TestCase):
    @staticmethod
    def junit(cases: list[tuple[str, str | None]]) -> str:
        suite = ET.Element("testsuite", {
            "tests": str(len(cases)), "failures": "0", "errors": "0", "skipped": "0",
        })
        for identity, outcome in cases:
            classname, name = identity.split("::")
            case = ET.SubElement(suite, "testcase", classname=classname, name=name)
            if outcome:
                ET.SubElement(case, outcome, message="inert refusal")
                counter = {"failure": "failures", "error": "errors", "skipped": "skipped"}[outcome]
                suite.set(counter, str(int(suite.attrib[counter]) + 1))
        return ET.tostring(suite, encoding="unicode")

    def test_exact_declared_matrices_match_current_source_test_methods(self) -> None:
        expected = {
            "tests/test_bundle_installer.py": {
                "BundleInstallerPowerShellTests": set(gate.WINDOWS_BUNDLE_METHODS),
                "BundleInstallerScriptTests": set(gate.LINUX_BUNDLE_METHODS),
            },
            "tests/test_portable_side_by_side_install.py": {
                "PortableSideBySideInstallTests": set(gate.SIDE_BY_SIDE_METHODS),
            },
        }
        for name, classes in expected.items():
            tree = ast.parse((support.ROOT / name).read_text(encoding="utf-8"))
            found = {
                node.name: {method.name for method in node.body
                            if isinstance(method, ast.FunctionDef)
                            and method.name.startswith("test_")}
                for node in tree.body if isinstance(node, ast.ClassDef) and node.name in classes
            }
            self.assertEqual(found, classes)
        self.assertEqual(len(gate.matrix_nodeids(gate.Matrix.WINDOWS)), 33)
        self.assertEqual(len(gate.matrix_nodeids(gate.Matrix.LINUX_BUNDLE)), 8)

    def test_complete_matrix_and_unchanged_reviewed_bytes_pass(self) -> None:
        for matrix in gate.Matrix:
            identities = {gate.junit_identity(nodeid) for nodeid in gate.matrix_nodeids(matrix)}
            xml = self.junit([(name, None) for name in sorted(identities)])
            inventory = {"source.py": "a" * 64, "config.toml": "b" * 64}
            result = gate.classify_gate(xml, identities, 0, inventory, inventory, inventory)
            self.assertEqual(result["status"], gate.GateStatus.PASSED.value)
            self.assertEqual(result["missing_tests"], [])

    def test_missing_deselected_unexpected_and_duplicate_cases_never_pass(self) -> None:
        expected = {"tests.fixture.Matrix::first", "tests.fixture.Matrix::second"}
        cases = [(name, None) for name in sorted(expected)]
        for selected in (cases[:1], [], cases + [cases[0]],
                         cases + [("tests.fixture.Matrix::unreviewed", None)]):
            with self.subTest(selected=selected):
                result = gate.classify_junit(self.junit(selected), expected, 0)
                self.assertEqual(result["status"], gate.GateStatus.FAILED.value)
                self.assertTrue(result["missing_tests"] or result["unexpected_tests"]
                                or result["duplicate_tests"])

    def test_skips_failures_errors_and_collection_refusal_never_pass(self) -> None:
        identity = "tests.fixture.Matrix::required"
        for outcome in ("skipped", "failure", "error"):
            with self.subTest(outcome=outcome):
                result = gate.classify_junit(self.junit([(identity, outcome)]), {identity}, 0)
                self.assertEqual(result["status"], gate.GateStatus.FAILED.value)
                self.assertEqual(result["not_passed_tests"], [{
                    "identity": identity, "outcome": outcome, "reason": "inert refusal",
                }])
        for exit_code in (1, 2, 3, 4, 5):
            result = gate.classify_junit(self.junit([(identity, None)]), {identity}, exit_code)
            self.assertEqual(result["status"], gate.GateStatus.FAILED.value)

    def test_source_config_or_historical_drift_and_inventory_changes_never_pass(self) -> None:
        identity = "tests.fixture.Matrix::required"
        xml = self.junit([(identity, None)])
        reviewed = {"source.py": "a" * 64, "pyproject.toml": "b" * 64,
                    "tests/fixtures/historical.json": "c" * 64}
        variants = [dict(reviewed, added="d" * 64), {"source.py": "a" * 64}]
        variants.extend({**reviewed, name: "e" * 64} for name in reviewed)
        for changed in variants:
            for before, after in ((changed, changed), (reviewed, changed)):
                with self.subTest(before=before, after=after):
                    result = gate.classify_gate(xml, {identity}, 0, reviewed, before, after)
                    self.assertEqual(result["status"], gate.GateStatus.FAILED.value)
                    differences = result["inventory_differences"]
                    self.assertTrue(any(paths for difference in differences.values()
                                        for paths in difference.values()))

    def test_malformed_or_inconsistent_junit_is_rejected(self) -> None:
        for xml in ("<broken", "<not-junit />", "<testsuites />",
                    '<testsuite tests="-1" failures="0" errors="0" skipped="0" />',
                    '<testsuite tests="1" failures="0" errors="0" skipped="0" />'):
            with self.subTest(xml=xml), self.assertRaises((ValueError, ET.ParseError)):
                gate.classify_junit(xml, {"required::test"}, 0)

    def test_manifest_requires_complete_inputs_and_exact_test_identity_contract(self) -> None:
        root = support.fresh_fixture_directory()
        path = root / "manifest.json"
        inventory = {name: "a" * 64 for name in gate.REQUIRED_GATE_FILES}
        value = {"format": gate.MANIFEST_FORMAT, "inventory_sha256": inventory,
                 "matrices": gate.matrix_contract()}
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(gate.read_reviewed_manifest(path)[0], inventory)
        variants = [
            {**value, "format": True}, {**value, "inventory_sha256": {}},
            {**value, "matrices": {gate.Matrix.WINDOWS.value: []}},
            {**value, "inventory_sha256": {**inventory, "pyproject.toml": "not-a-hash"}},
        ]
        for changed in variants:
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                gate.read_reviewed_manifest(path)

    def test_launcher_refuses_changed_reviewed_inputs_before_starting_pytest(self) -> None:
        evidence = support.session_root() / "operator-refusal-evidence"
        reviewed = {"source.py": "a" * 64}
        with mock.patch.object(sys, "argv", [
            "gate", "--matrix", gate.Matrix.WINDOWS.value, "--acknowledge-operator-install",
            "--reviewed-manifest", str(evidence / "not-read.json"), "--evidence-dir", str(evidence),
        ]), mock.patch.object(gate, "configure_session"), mock.patch.object(
            gate, "read_reviewed_manifest", return_value=(reviewed, b"{}"),
        ), mock.patch.object(gate, "capture_gate_inventory", return_value={
            "source.py": "b" * 64,
        }), mock.patch.object(subprocess, "run") as run:
            self.assertEqual(gate.main(), 2)
        run.assert_not_called()
        report = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], gate.GateStatus.BLOCKED.value)
        self.assertEqual(report["inventory_differences"]["reviewed_to_before"]["changed"],
                         ["source.py"])

    def test_launcher_records_postrun_source_drift_as_failure_with_inert_child(self) -> None:
        evidence = support.session_root() / "operator-drift-evidence"
        before = {"source.py": "a" * 64}
        after = {"source.py": "b" * 64}
        nodeids = gate.matrix_nodeids(gate.Matrix.WINDOWS)
        xml = self.junit([(gate.junit_identity(nodeid), None) for nodeid in nodeids])

        def inert_child(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[-len(nodeids):], nodeids)
            (evidence / "junit.xml").write_text(xml, encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch.object(sys, "argv", [
            "gate", "--matrix", gate.Matrix.WINDOWS.value, "--acknowledge-operator-install",
            "--reviewed-manifest", str(evidence / "not-read.json"), "--evidence-dir", str(evidence),
        ]), mock.patch.object(gate, "configure_session"), mock.patch.object(
            gate, "read_reviewed_manifest", return_value=(before, b"{}"),
        ), mock.patch.object(gate, "capture_gate_inventory", side_effect=[before, after]), \
                mock.patch.object(gate, "missing_prerequisites", return_value=[]), \
                mock.patch.object(subprocess, "run", side_effect=inert_child) as run:
            self.assertEqual(gate.main(), 1)
        self.assertEqual(run.call_count, 1)
        report = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], gate.GateStatus.FAILED.value)
        self.assertEqual(report["inventory_sha256_before"], before)
        self.assertEqual(report["inventory_sha256_after"], after)
        self.assertEqual(report["missing_tests"], [])

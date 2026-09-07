"""Operator-terminal installer fixtures bound to reviewed bytes and exact test identities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping
from enum import Enum
from pathlib import Path

from portable_test_support import (
    ROOT,
    SESSION_MARKER,
    SOURCE_PATHS,
    configure_session,
    isolated_environment,
    read_current_source,
    validate_workspace_path,
)

MANIFEST_FORMAT = 1
GATE_PATHS = (*SOURCE_PATHS, "tests", "pyproject.toml", "AGENTS.md", "docs/portable-test-lanes.md")
REQUIRED_GATE_FILES = frozenset({
    "pyproject.toml", "AGENTS.md", "docs/portable-test-lanes.md", "tests/conftest.py",
    "tests/run_portable_operator_gate.py", "tests/portable_test_support.py",
    "tests/test_bundle_installer.py", "tests/test_portable_side_by_side_install.py",
    "tests/test_portable_test_safety.py", "tests/fixtures/portable_bootstrap_604/manifest.json",
    "tests/fixtures/portable_bootstrap_604/sources.json", ".gitignore", "portable-release.json",
    "requirements-portable.lock", "install/bootstrap/transaction.py", "install/bootstrap/verify.py",
})


class Matrix(str, Enum):
    WINDOWS = "windows-fixtures"
    LINUX_BUNDLE = "linux-bundle-fixtures"


class GateStatus(str, Enum):
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    PASSED = "PASSED"


# An explicit reviewed contract; never discover the expected set from pytest's selection.
WINDOWS_BUNDLE_METHODS = (
    "test_windows_installer_help_exits_zero",
    "test_windows_development_setup_help_exits_zero",
    "test_windows_wrapper_bootstraps_a_missing_local_runtime",
    "test_windows_uninstaller_removes_managed_artifacts_but_keeps_checkout",
    "test_windows_uninstaller_removes_only_unchanged_installed_capabilities",
    "test_windows_uninstaller_refuses_a_filesystem_root",
)
SIDE_BY_SIDE_METHODS = (
    "test_versioned_bootstrap_lock_rejects_live_owner_and_wrong_transaction",
    "test_committed_layout_reconciles_only_exact_completed_pending_publication",
    "test_pointer_commit_requires_the_exact_prepared_baseline_and_transaction_id",
    "test_uninstall_resumes_after_every_durable_action_boundary",
    "test_candidate_creation_and_clone_crashes_reconcile_owned_orphans",
    "test_new_bootstrap_migrates_the_committed_v1_pointer_fixture_on_rollback",
    "test_604_overlay_with_current_support_updates_v2_rolls_back_and_reuses_candidate",
    "test_posix_fresh_install_and_installed_update_use_the_same_layout",
    "test_fresh_install_and_installed_update_switch_one_atomic_pointer",
    "test_legacy_dirty_checkout_is_preserved_and_managed_entrypoints_are_restorable",
    "test_interrupted_update_recovers_each_durable_phase",
    "test_corrupt_current_pointer_fails_closed_without_touching_victim",
    "test_candidate_mutation_fails_before_pointer_switch",
    "test_concurrent_edit_to_previous_release_is_retained_after_switch",
    "test_capability_failure_is_postcommit_and_nonfatal",
    "test_uninstall_commits_core_before_nonfatal_capability_cleanup",
    "test_power_loss_after_core_uninstall_leaves_external_cleanup_plan",
    "test_failed_adoption_keeps_old_pointer_and_retry_records_one_receipt",
    "test_tampered_journal_candidate_path_is_retained_without_victim_access",
    "test_posix_recovery_rejects_same_prefix_victim_without_ownership_evidence",
    "test_bootstrap_publication_recovers_every_asset_boundary",
    "test_failed_prepared_validation_retains_candidate_and_journal",
    "test_bootstrap_and_transaction_compatibility_fail_closed",
    "test_uninstall_rejects_tampered_retained_release_before_deleting_any",
    "test_uninstall_rejects_layout_escape_before_deleting_any_release",
    "test_rollback_exchanges_current_and_previous_in_one_pointer_file",
    "test_interrupted_legacy_backup_is_not_overwritten_on_retry",
)
LINUX_BUNDLE_METHODS = (
    "test_unix_installer_has_valid_shell_syntax",
    "test_unix_installer_help_exits_zero",
    "test_unix_installer_installs_local_bundle",
    "test_unix_installer_updates_existing_checkout",
    "test_unix_installer_requires_install_dir_when_non_interactive",
    "test_unix_installer_rejects_non_git_install_dir",
    "test_unix_wrapper_bootstraps_a_missing_local_runtime",
    "test_unix_uninstaller_removes_managed_artifacts_but_keeps_checkout",
)


def matrix_nodeids(matrix: Matrix) -> list[str]:
    groups = (
        ("tests/test_bundle_installer.py::BundleInstallerPowerShellTests", WINDOWS_BUNDLE_METHODS),
        ("tests/test_portable_side_by_side_install.py::PortableSideBySideInstallTests",
         SIDE_BY_SIDE_METHODS),
    ) if matrix is Matrix.WINDOWS else (
        ("tests/test_bundle_installer.py::BundleInstallerScriptTests", LINUX_BUNDLE_METHODS),
    )
    return sorted(f"{owner}::{method}" for owner, methods in groups for method in methods)


def matrix_contract() -> dict[str, list[str]]:
    return {matrix.value: matrix_nodeids(matrix) for matrix in Matrix}


def junit_identity(nodeid: str) -> str:
    module, owner, method = nodeid.split("::")
    return f"{module.removesuffix('.py').replace('/', '.')}.{owner}::{method}"


def capture_gate_inventory() -> dict[str, str]:
    sources = read_current_source(GATE_PATHS)
    missing = REQUIRED_GATE_FILES - sources.keys()
    if missing:
        raise ValueError(f"Required gate inputs are missing: {sorted(missing)}")
    return {name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()}


def read_reviewed_manifest(path: Path) -> tuple[dict[str, str], bytes]:
    raw = validate_workspace_path(path, allow_existing=True).read_bytes()
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"format", "inventory_sha256", "matrices"}
            or type(value["format"]) is not int or value["format"] != MANIFEST_FORMAT
            or value["matrices"] != matrix_contract()):
        raise ValueError("Reviewed manifest must bind the exact supported matrix contract")
    inventory = value["inventory_sha256"]
    if not isinstance(inventory, dict) or not REQUIRED_GATE_FILES.issubset(inventory):
        raise ValueError("Reviewed manifest is missing required source/config/historical inputs")
    for name, digest in inventory.items():
        if (not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            raise ValueError("Reviewed manifest contains an invalid SHA-256 entry")
        validate_workspace_path(ROOT / name, allow_existing=True)
    return inventory, raw


def inventory_difference(
    expected: Mapping[str, str], actual: Mapping[str, str],
) -> dict[str, list[str]]:
    return {
        "missing": sorted(expected.keys() - actual.keys()),
        "added": sorted(actual.keys() - expected.keys()),
        "changed": sorted(name for name in expected.keys() & actual.keys()
                          if expected[name] != actual[name]),
    }


def classify_junit(xml: str, expected: set[str], pytest_exit_code: int) -> dict[str, object]:
    root = ET.fromstring(xml)
    suites = [root] if root.tag == "testsuite" else list(root) if root.tag == "testsuites" else []
    if not suites or any(suite.tag != "testsuite" for suite in suites):
        raise ValueError("Expected a nonempty JUnit testsuite collection")
    counts = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    identities: list[str] = []
    not_passed: list[dict[str, str]] = []
    for suite in suites:
        cases = suite.findall("testcase")
        for name in counts:
            count = int(suite.attrib[name])
            if count < 0:
                raise ValueError("JUnit counts cannot be negative")
            counts[name] += count
        if int(suite.attrib["tests"]) != len(cases):
            raise ValueError("JUnit test count disagrees with testcase records")
        for case in cases:
            identity = case.get("classname", "") + "::" + case.get("name", "")
            identities.append(identity)
            for tag in ("skipped", "error", "failure"):
                element = case.find(tag)
                if element is not None:
                    not_passed.append({"identity": identity, "outcome": tag,
                                       "reason": element.get("message", "") or element.text or ""})
    actual = set(identities)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    duplicates = sorted(name for name, count in Counter(identities).items() if count != 1)
    passed = (pytest_exit_code == 0 and bool(expected) and not missing and not unexpected
              and not duplicates and not not_passed and counts["tests"] == len(expected)
              and all(counts[name] == 0 for name in ("failures", "errors", "skipped")))
    return {
        "status": GateStatus.PASSED.value if passed else GateStatus.FAILED.value,
        "counts": counts, "expected_tests": sorted(expected), "actual_tests": sorted(identities),
        "missing_tests": missing, "unexpected_tests": unexpected, "duplicate_tests": duplicates,
        "not_passed_tests": not_passed, "pytest_exit_code": pytest_exit_code,
    }


def classify_gate(
    xml: str, expected: set[str], pytest_exit_code: int,
    reviewed: Mapping[str, str], before: Mapping[str, str], after: Mapping[str, str],
) -> dict[str, object]:
    result = classify_junit(xml, expected, pytest_exit_code)
    differences = {
        "reviewed_to_before": inventory_difference(reviewed, before),
        "before_to_after": inventory_difference(before, after),
    }
    result["inventory_differences"] = differences
    if any(paths for difference in differences.values() for paths in difference.values()):
        result["status"] = GateStatus.FAILED.value
    return result


def missing_prerequisites(matrix: Matrix) -> list[str]:
    names = ("git", "pwsh", "powershell") if matrix is Matrix.WINDOWS else ("git", "bash")
    missing = [name for name in names if not shutil.which(name)]
    if matrix is Matrix.WINDOWS:
        if os.name != "nt":
            missing.append("native Windows host")
        if not Path(r"C:\Program Files\Git\bin\bash.exe").is_file():
            missing.append("Git Bash at the path required by the fixture suite")
    elif not sys.platform.startswith("linux"):
        missing.append("native Linux host")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", choices=[matrix.value for matrix in Matrix], required=True)
    parser.add_argument("--acknowledge-operator-install", action="store_true", required=True)
    parser.add_argument("--reviewed-manifest", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path)
    arguments = parser.parse_args()
    matrix = Matrix(arguments.matrix)
    evidence = validate_workspace_path(arguments.evidence_dir or ROOT / (
        ".tmp-portable-operator-" + uuid.uuid4().hex
    ))
    evidence.mkdir()
    (evidence / SESSION_MARKER).write_text('{"purpose":"operator gate launcher"}\n')
    configure_session(evidence, operator_enabled=False)
    environment = isolated_environment(evidence / "environment")
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    command = [
        sys.executable, "-B", "-m", "pytest", "-q", "-c", str(ROOT / "pyproject.toml"),
        "--run-operator-install", "-m", "operator_install",
        "--junitxml=" + str(evidence / "junit.xml"), *matrix_nodeids(matrix),
    ]
    report: dict[str, object] = {
        "matrix": matrix.value, "status": GateStatus.BLOCKED.value,
        "scope": "disposable current-source installer fixtures with DEVLOOP_TESTING=1",
        "runtime": "Textual version stub; no pinned dependency installation",
        "historical": "18-file bootstrap overlay with current-checkout support; full commit absent",
        "native_linux_side_by_side_gate": "not established by Git Bash or Unix bundle tests",
        "command": command, "reviewed_manifest": str(arguments.reviewed_manifest),
    }
    log = evidence / "operator.log"
    log.write_text("Operator fixtures have not started.\n", encoding="utf-8")
    exit_code = 2
    started = False
    try:
        reviewed, manifest_raw = read_reviewed_manifest(arguments.reviewed_manifest)
        report["reviewed_manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
        (evidence / "reviewed-manifest.json").write_bytes(manifest_raw)
        before = capture_gate_inventory()
        report["inventory_sha256_before"] = before
        report["inventory_differences"] = {"reviewed_to_before": inventory_difference(
            reviewed, before,
        )}
        if any(inventory_difference(reviewed, before).values()):
            raise ValueError("Current gate inputs differ from the reviewed manifest")
        missing = missing_prerequisites(matrix)
        report["missing_prerequisites"] = missing
        if missing:
            log.write_text("Missing prerequisites: " + ", ".join(missing) + "\n", encoding="utf-8")
        else:
            started = True
            try:
                with log.open("w", encoding="utf-8") as output:
                    completed = subprocess.run(
                        command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT, check=False,
                    )
            finally:
                after = capture_gate_inventory()
                report["inventory_sha256_after"] = after
                report["inventory_differences"] = {
                    "reviewed_to_before": inventory_difference(reviewed, before),
                    "before_to_after": inventory_difference(before, after),
                }
            report.update(classify_gate(
                (evidence / "junit.xml").read_text(encoding="utf-8"),
                {junit_identity(nodeid) for nodeid in matrix_nodeids(matrix)}, completed.returncode,
                reviewed, before, after,
            ))
            exit_code = 0 if report["status"] == GateStatus.PASSED.value else 1
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
        report["error"] = str(error)
        report["status"] = GateStatus.FAILED.value if started else GateStatus.BLOCKED.value
        exit_code = 1 if started else 2
    (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{report['status']}: {evidence / 'result.json'}")
    print(f"Installer log: {log}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

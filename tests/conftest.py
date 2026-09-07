"""Select safe tests by default and reserve fresh workspace temporary paths."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from portable_test_support import ROOT, SESSION_MARKER, configure_session, validate_workspace_path


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-operator-install", action="store_true", default=False,
        help="Operator-terminal opt-in for disposable installer/rollback/uninstaller fixtures",
    )


def pytest_configure(config: pytest.Config) -> None:
    requested = config.option.basetemp
    try:
        explicit = validate_workspace_path(Path(requested)) if requested else None
        root = Path(tempfile.mkdtemp(prefix=".tmp-test-session-", dir=ROOT))
        validate_workspace_path(root, allow_existing=True)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    operator_enabled = bool(config.getoption("--run-operator-install"))
    marker = {
        "purpose": "fresh disposable pytest session; retained for evidence",
        "operator_install": operator_enabled,
        "basetemp": str(explicit or root / "pytest"),
    }
    (root / SESSION_MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    configure_session(root, operator_enabled=operator_enabled, basetemp=explicit)
    # Never point pytest at a reused directory: its tmp_path factory recursively clears basetemp.
    config.option.basetemp = str(explicit or root / "pytest")
    temporary = root / "temporary"
    temporary.mkdir()
    saved = {name: os.environ.get(name) for name in ("TMP", "TEMP", "TMPDIR")}
    saved_tempdir = tempfile.tempdir
    for name in saved:
        os.environ[name] = str(temporary)
    tempfile.tempdir = str(temporary)

    def restore() -> None:
        tempfile.tempdir = saved_tempdir
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    config.add_cleanup(restore)
    config._portable_test_session = root  # type: ignore[attr-defined]


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    operator_items = [item for item in items if item.get_closest_marker("operator_install")]
    if operator_items and not config.getoption("--run-operator-install"):
        raise pytest.UsageError(
            f"{len(operator_items)} operator-only installation tests were selected. "
            'Agent lane: -m "not integration and not operator_install". '
            "Operator terminal only: --run-operator-install -m operator_install. "
            "Deselection is not completed installation evidence."
        )


def pytest_report_header(config: pytest.Config) -> str:
    root = config._portable_test_session  # type: ignore[attr-defined]
    return f"Disposable test session (retained): {root}"


def pytest_terminal_summary(terminalreporter: object, config: pytest.Config) -> None:
    root = config._portable_test_session  # type: ignore[attr-defined]
    terminalreporter.write_line(f"Portable test evidence retained at: {root}")  # type: ignore[attr-defined]
    if not config.getoption("--run-operator-install"):
        terminalreporter.write_line(  # type: ignore[attr-defined]
            "Operator installation gates were not run; their required evidence remains open."
        )

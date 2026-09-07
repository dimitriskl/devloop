from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)


def _legacy_catalog(
    root: Path,
    operation: PortableWorkflowOperation = PortableWorkflowOperation.PLANNING,
) -> tuple[PortableSessionCatalog, Path]:
    checkout = root / "checkout"
    checkout.mkdir()
    arguments = ("--repo", str(checkout))
    if operation is PortableWorkflowOperation.DELIVERY:
        prd = checkout / "feature.md"
        issues = checkout / "issues" / "README.md"
        issues.parent.mkdir()
        prd.write_text("# Feature\n", encoding="utf-8")
        issues.write_text("# Issues\n", encoding="utf-8")
        arguments = ("--prd", str(prd), "--issues", str(issues), "--dry-run")
    catalog = PortableSessionCatalog(root / "sessions.sqlite3")
    catalog.create_session(PortableSessionLaunch("legacy", checkout, operation, arguments))
    # Older catalog records predate the optional trusted argument-base field.
    with closing(sqlite3.connect(catalog.path)) as connection:
        row = connection.execute("SELECT arguments_json FROM sessions").fetchone()
        settings = json.loads(row[0])
        del settings["argument_base"]
        connection.execute("UPDATE sessions SET arguments_json = ?", (json.dumps(settings),))
        connection.commit()
    return PortableSessionCatalog(catalog.path), checkout


@pytest.mark.parametrize("operation", tuple(PortableWorkflowOperation))
@pytest.mark.parametrize("checkout_remains", (False, True))
def test_startup_retains_unavailable_legacy_records_without_launching(
    tmp_path: Path, operation: PortableWorkflowOperation, checkout_remains: bool
) -> None:
    catalog, checkout = _legacy_catalog(tmp_path, operation)
    if not checkout_remains:
        checkout.rename(tmp_path / "moved")
    catalog.discover_resume_candidates(lambda _checkout: ())
    original = catalog.get_session("legacy")
    assert original.status is PortableSessionStatus.UNAVAILABLE
    assert original.launch_settings.argument_base is None
    launcher = Mock(side_effect=AssertionError("Startup must not launch a worker"))

    supervisor = PortableSessionSupervisor(catalog=catalog, worker_launcher=launcher)
    try:
        snapshot, = supervisor.list_sessions()
        assert snapshot.session_id == original.session_id
        assert snapshot.checkout == original.checkout
        assert snapshot.status is PortableSessionStatus.UNAVAILABLE
        assert snapshot.unavailable_from_status is original.unavailable_from_status
        with pytest.raises(ValueError, match="unavailable.*Relink"):
            supervisor.resume_session("legacy")
        assert catalog.get_session("legacy") == original
        assert catalog.get_worktree_lease(checkout) is None
        with closing(sqlite3.connect(catalog.path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM execution_claims").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM execution_requests").fetchone()[0] == 0
        launcher.assert_not_called()
        supervisor.forget_session("legacy")
        assert supervisor.list_sessions() == ()
        assert (checkout if checkout_remains else tmp_path / "moved").is_dir()
    finally:
        supervisor.shutdown()


def test_peer_refresh_retains_unavailable_record_and_relink_restores_explicit_resume(
    tmp_path: Path,
) -> None:
    catalog, checkout = _legacy_catalog(tmp_path)
    git_environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    subprocess.run(
        ["git", "init", "--quiet", str(checkout)],
        check=True,
        capture_output=True,
        env=git_environment,
    )
    launcher = Mock(side_effect=RuntimeError("Intercepted worker launch"))
    supervisor = PortableSessionSupervisor(catalog=catalog, worker_launcher=launcher)
    try:
        moved_checkout = tmp_path / "moved"
        checkout.rename(moved_checkout)
        catalog.discover_resume_candidates(lambda _checkout: ())
        snapshot, = supervisor.list_sessions()
        assert snapshot.status is PortableSessionStatus.UNAVAILABLE
        with pytest.raises(ValueError, match="unavailable.*Relink"):
            supervisor.resume_session("legacy")
        launcher.assert_not_called()

        restored = supervisor.relink_session("legacy", moved_checkout)
        assert restored.status is PortableSessionStatus.READY
        assert restored.checkout == moved_checkout.resolve()
        with pytest.raises(RuntimeError, match="Intercepted worker launch"):
            supervisor.resume_session("legacy")
        launcher.assert_called_once()
        assert launcher.call_args.args[0].checkout == moved_checkout.resolve()
    finally:
        supervisor.shutdown()


def test_resume_checks_current_catalog_availability_before_reconstructing_launch(
    tmp_path: Path,
) -> None:
    catalog, checkout = _legacy_catalog(tmp_path)
    launcher = Mock(side_effect=AssertionError("Unavailable session must not launch"))
    supervisor = PortableSessionSupervisor(catalog=catalog, worker_launcher=launcher)
    try:
        checkout.rename(tmp_path / "moved")
        catalog.discover_resume_candidates(lambda _checkout: ())
        # Resume must use the durable row even before the next display refresh.
        with pytest.raises(ValueError, match="unavailable.*Relink"):
            supervisor.resume_session("legacy")
        launcher.assert_not_called()
    finally:
        supervisor.shutdown()


def test_unavailable_legacy_session_opens_in_the_application_with_recovery_actions(
    tmp_path: Path,
) -> None:
    from textual.widgets import OptionList, Static

    from devloop.portable_runtime import PortableRuntimeBridge
    from devloop.portable_ui.app import PortableApplicationShell

    catalog, checkout = _legacy_catalog(tmp_path, PortableWorkflowOperation.DELIVERY)
    checkout.rename(tmp_path / "moved")
    candidates = catalog.discover_resume_candidates(lambda _checkout: ())
    launcher = Mock(side_effect=AssertionError("UI startup must not launch a worker"))
    supervisor = PortableSessionSupervisor(
        catalog=catalog, resume_candidates=candidates, worker_launcher=launcher
    )

    async def exercise() -> None:
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                "new", tmp_path, PortableWorkflowOperation.PLANNING, ()
            ),
        )
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            assert str(app.query_one("#portable-header", Static).content) == "Dev Loop > Sessions"
            menu = app.query_one("#portable-navigation", OptionList)
            session_option = next(
                index
                for index in range(menu.option_count)
                if "UNAVAILABLE" in str(menu.get_option_at_index(index).prompt)
            )
            menu.highlighted = session_option
            await pilot.press("enter")
            await pilot.pause()
            labels = tuple(
                str(menu.get_option_at_index(index).prompt) for index in range(menu.option_count)
            )
            assert "Relink" in labels
            assert "Forget (metadata only)" in labels
            assert not any("Resume" in label for label in labels)
            launcher.assert_not_called()

    try:
        asyncio.run(exercise())
    finally:
        supervisor.shutdown()

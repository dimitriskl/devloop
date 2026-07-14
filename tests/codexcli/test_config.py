from __future__ import annotations

import threading
from pathlib import Path

import pytest

import devloop.application.doctor as doctor_module
from devloop.application.config import ApplicationConfig
from devloop.domain.doctor import DoctorCheckId, DoctorCheckStatus
from devloop.execution.app_server import (
    AppServerHandshake,
    AppServerStatus,
    AuthenticationReadiness,
)


def test_configuration_resolves_windows_storage_without_creating_it(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    config = ApplicationConfig.resolve(
        repository,
        platform="win32",
        environment={
            "APPDATA": str(tmp_path / "roaming"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        },
        home=tmp_path / "home",
    )

    assert config.repository == repository.resolve()
    assert config.paths.run_root == repository.resolve() / ".devloop" / "runs"
    assert config.paths.user_config == tmp_path / "roaming" / "codexcli"
    assert config.paths.user_data == tmp_path / "local" / "codexcli"
    assert not repository.exists()


@pytest.mark.parametrize("invalid_root", ["", "relative-root"])
@pytest.mark.parametrize(
    (
        "platform",
        "config_variable",
        "data_variable",
        "config_fallback",
        "data_fallback",
    ),
    [
        (
            "win32",
            "APPDATA",
            "LOCALAPPDATA",
            Path("AppData") / "Roaming",
            Path("AppData") / "Local",
        ),
        (
            "linux",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            Path(".config"),
            Path(".local") / "share",
        ),
    ],
)
def test_configuration_uses_home_fallback_for_invalid_platform_storage_roots(
    tmp_path: Path,
    invalid_root: str,
    platform: str,
    config_variable: str,
    data_variable: str,
    config_fallback: Path,
    data_fallback: Path,
) -> None:
    home = tmp_path / "home"

    config = ApplicationConfig.resolve(
        tmp_path / "repo",
        platform=platform,
        environment={
            config_variable: invalid_root,
            data_variable: invalid_root,
        },
        home=home,
    )

    assert config.paths.user_config == home / config_fallback / "codexcli"
    assert config.paths.user_data == home / data_fallback / "codexcli"


def test_doctor_probes_nonexistent_storage_without_creating_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ApplicationConfig.resolve(
        tmp_path,
        platform="win32",
        environment={
            "APPDATA": str(tmp_path / "roaming"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        },
        home=tmp_path / "home",
    )

    def reject_temporary_directory(*args: object, **kwargs: object) -> None:
        raise AssertionError("Storage probes must not create temporary directories.")

    monkeypatch.setattr(doctor_module.tempfile, "TemporaryDirectory", reject_temporary_directory)
    monkeypatch.setattr(doctor_module, "_which", lambda name: None)
    monkeypatch.setattr(doctor_module, "_resolve_codex", lambda: None)
    monkeypatch.setattr(doctor_module, "_terminal_capabilities", lambda: (True, True))

    report = doctor_module.collect_doctor_report(config)
    checks = {check.check_id: check for check in report.checks}

    assert checks[DoctorCheckId.STORAGE].status is DoctorCheckStatus.PASS
    assert not (tmp_path / ".devloop").exists()
    assert not (tmp_path / "roaming").exists()
    assert not (tmp_path / "local").exists()


def test_doctor_fails_closed_when_account_read_returns_a_malformed_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MalformedAccountClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> MalformedAccountClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def probe(self) -> AppServerStatus:
            return AppServerStatus(
                AppServerHandshake("windows", "windows"),
                AuthenticationReadiness.from_result(
                    {"account": {}, "requiresOpenaiAuth": True}
                ),
            )

    monkeypatch.setattr(doctor_module, "AppServerClient", MalformedAccountClient)

    status = doctor_module._probe_app_server(Path("codex"), timeout_seconds=1.0)
    authentication = doctor_module.check_authentication(status)

    assert status is None
    assert authentication.status is DoctorCheckStatus.FAIL


def test_doctor_reports_a_blocked_storage_probe_without_hanging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_probe = threading.Event()
    report_ready = threading.Event()
    reports = []
    config = ApplicationConfig.resolve(
        tmp_path,
        platform="win32",
        environment={
            "APPDATA": str(tmp_path / "roaming"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        },
        home=tmp_path / "home",
    )

    class BlockingProbe:
        def __enter__(self) -> BlockingProbe:
            release_probe.wait()
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(doctor_module.tempfile, "TemporaryFile", lambda **kwargs: BlockingProbe())
    monkeypatch.setattr(doctor_module, "STORAGE_PROBE_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(doctor_module, "_which", lambda name: None)
    monkeypatch.setattr(doctor_module, "_resolve_codex", lambda: None)
    monkeypatch.setattr(doctor_module, "_terminal_capabilities", lambda: (True, True))

    def collect_report() -> None:
        reports.append(doctor_module.collect_doctor_report(config))
        report_ready.set()

    worker = threading.Thread(target=collect_report)
    worker.start()
    completed_within_bound = report_ready.wait(timeout=1.0)
    release_probe.set()
    worker.join(timeout=1.0)

    assert completed_within_bound
    checks = {check.check_id: check for check in reports[0].checks}
    assert checks[DoctorCheckId.STORAGE].status is DoctorCheckStatus.FAIL

from __future__ import annotations

import os
from pathlib import Path

import pytest

from devloop.application.config import ApplicationConfig
from devloop.application.doctor import collect_doctor_report
from devloop.domain.doctor import DoctorCheckId, DoctorCheckStatus


@pytest.mark.integration
def test_real_app_server_is_initialized_and_authenticated() -> None:
    if os.environ.get("DEVLOOP_REAL_APP_SERVER") != "1":
        pytest.skip("Set DEVLOOP_REAL_APP_SERVER=1 to run the real App Server gate.")

    report = collect_doctor_report(ApplicationConfig.resolve(Path.cwd()))
    checks = {check.check_id: check for check in report.checks}

    assert checks[DoctorCheckId.APP_SERVER].status is DoctorCheckStatus.PASS
    assert checks[DoctorCheckId.BACKEND_COMPATIBILITY].status is DoctorCheckStatus.PASS
    assert checks[DoctorCheckId.AUTHENTICATION].status is DoctorCheckStatus.PASS

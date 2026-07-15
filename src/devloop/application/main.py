from __future__ import annotations

from collections.abc import Sequence

from devloop.application.cli import CliCommand, parse_arguments
from devloop.application.commands import launcher_command_registry
from devloop.application.config import ApplicationConfig
from devloop.application.doctor import collect_doctor_report, print_doctor_report


def run_application(arguments: Sequence[str] | None = None) -> int:
    parsed = parse_arguments(arguments)
    config = ApplicationConfig.resolve(parsed.repository)
    report = collect_doctor_report(config)

    if parsed.command is CliCommand.DOCTOR:
        print_doctor_report(report)
        return int(report.exit_code)

    if not report.ready:
        print_doctor_report(report)
        return int(report.exit_code)

    from devloop.ui.app import run_launcher

    run_launcher(config, launcher_command_registry())
    return 0

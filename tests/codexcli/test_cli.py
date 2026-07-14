from __future__ import annotations

from importlib.metadata import distribution
from pathlib import Path

from devloop.application.cli import CliCommand, parse_arguments


def test_parse_run_command_uses_the_requested_repository() -> None:
    arguments = parse_arguments(["run", "--repo", "example"])

    assert arguments.command is CliCommand.RUN
    assert arguments.repository == Path("example")


def test_project_metadata_exposes_the_isolated_codexcli_command() -> None:
    installed = distribution("devloop-codexcli")
    scripts = {
        entry.name: entry.value
        for entry in installed.entry_points
        if entry.group == "console_scripts"
    }

    assert installed.version == "0.1.0"
    assert installed.metadata["Requires-Python"] == ">=3.10"
    assert scripts["codexcli"] == "devloop.entrypoint:main"
    assert "textual<8.3,>=8.2.8" in (installed.requires or [])

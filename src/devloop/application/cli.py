from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CliCommand(str, Enum):
    """Commands exposed by the new console application."""

    DOCTOR = "doctor"
    RUN = "run"


@dataclass(frozen=True)
class CliArguments:
    command: CliCommand
    repository: Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codexcli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in CliCommand:
        command_parser = subparsers.add_parser(command.value)
        command_parser.add_argument("--repo", type=Path, default=Path.cwd())

    return parser


def parse_arguments(arguments: Sequence[str] | None = None) -> CliArguments:
    namespace = _build_parser().parse_args(arguments)
    return CliArguments(
        command=CliCommand(namespace.command),
        repository=namespace.repo,
    )

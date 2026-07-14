from __future__ import annotations

import pytest

from devloop.application.commands import launcher_command_registry
from devloop.domain.commands import CommandScope, SlashCommand, SlashCommandRegistry
from devloop.domain.identifiers import SlashCommandId


def test_slash_command_id_rejects_display_syntax_and_whitespace() -> None:
    with pytest.raises(ValueError, match="Slash Command ID"):
        SlashCommandId("/bad command")


def test_registry_filters_commands_by_prefix_and_active_scope() -> None:
    registry = SlashCommandRegistry(
        [
            SlashCommand(
                command_id=SlashCommandId("resume"),
                title="Resume run",
                description="Choose an unfinished Workflow Run.",
                scope=CommandScope.GLOBAL,
            ),
            SlashCommand(
                command_id=SlashCommandId("retry"),
                title="Retry step",
                description="Retry the current Workflow Step.",
                scope=CommandScope.STEP,
            ),
        ]
    )

    matches = registry.matching("/re", active_scopes={CommandScope.GLOBAL})

    assert [str(command.command_id) for command in matches] == ["resume"]


def test_launcher_registers_explicit_blocked_retry_and_failed_reset_commands() -> None:
    registry = launcher_command_registry()

    matches = registry.matching(
        "/re",
        active_scopes={CommandScope.GLOBAL, CommandScope.WORKFLOW, CommandScope.STEP},
    )

    assert [command.command_id.value for command in matches] == [
        "request-changes",
        "reset",
        "resume",
        "retry",
    ]


def test_standard_commands_have_typed_contextual_scope() -> None:
    registry = launcher_command_registry()

    global_commands = registry.matching("/", active_scopes={CommandScope.GLOBAL})
    workflow_commands = registry.matching(
        "/",
        active_scopes={CommandScope.GLOBAL, CommandScope.WORKFLOW},
    )

    assert {item.command_id.value for item in global_commands} >= {
        "language",
        "options",
        "resume",
        "status",
    }
    assert {item.command_id.value for item in workflow_commands} >= {
        "cancel",
        "issues",
        "pause",
        "runs",
    }
    issues = next(item for item in workflow_commands if item.command_id.value == "issues")
    assert issues.scope is CommandScope.WORKFLOW

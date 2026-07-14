from __future__ import annotations

from collections.abc import Iterable, Set
from dataclasses import dataclass
from enum import Enum

from devloop.domain.identifiers import SlashCommandId


class CommandScope(str, Enum):
    GLOBAL = "global"
    WORKFLOW = "workflow"
    STEP = "step"


@dataclass(frozen=True)
class SlashCommand:
    command_id: SlashCommandId
    title: str
    description: str
    scope: CommandScope


class SlashCommandRegistry:
    """Validated registry and query interface for contextual Slash Commands."""

    def __init__(self, commands: Iterable[SlashCommand] = ()) -> None:
        registered: dict[SlashCommandId, SlashCommand] = {}
        for command in commands:
            if command.command_id in registered:
                raise ValueError(f"Duplicate Slash Command ID: {command.command_id}")
            registered[command.command_id] = command
        self._commands = registered

    def matching(
        self,
        query: str,
        *,
        active_scopes: Set[CommandScope],
    ) -> tuple[SlashCommand, ...]:
        prefix = query[1:] if query.startswith("/") else query
        normalized_prefix = prefix.casefold()
        return tuple(
            command
            for command in sorted(self._commands.values(), key=lambda item: item.command_id)
            if command.scope in active_scopes
            and command.command_id.value.startswith(normalized_prefix)
        )

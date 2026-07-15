from __future__ import annotations

from devloop.domain.commands import CommandScope, SlashCommand, SlashCommandRegistry
from devloop.domain.identifiers import SlashCommandId


def launcher_command_registry() -> SlashCommandRegistry:
    return SlashCommandRegistry(
        (
            SlashCommand(
                SlashCommandId("resume"),
                "Resume run",
                "Choose an unfinished Workflow Run.",
                CommandScope.GLOBAL,
            ),
            SlashCommand(
                SlashCommandId("options"),
                "Options",
                "Configure component capabilities.",
                CommandScope.GLOBAL,
            ),
            SlashCommand(
                SlashCommandId("status"),
                "Status",
                "Inspect the typed active Workflow status.",
                CommandScope.GLOBAL,
            ),
            SlashCommand(
                SlashCommandId("profile"),
                "Execution profile",
                "Show or select a component model, reasoning, and budget profile.",
                CommandScope.GLOBAL,
            ),
            SlashCommand(
                SlashCommandId("language"),
                "Language",
                "Choose the content language for new user-authored text.",
                CommandScope.GLOBAL,
            ),
            SlashCommand(
                SlashCommandId("runs"),
                "Runs",
                "Inspect Workflow Runs for the current project.",
                CommandScope.WORKFLOW,
            ),
            SlashCommand(
                SlashCommandId("issues"),
                "Issue Board",
                "Inspect the read-only Issue Board.",
                CommandScope.WORKFLOW,
            ),
            SlashCommand(
                SlashCommandId("pause"),
                "Pause run",
                "Persist the current run and require explicit resume.",
                CommandScope.WORKFLOW,
            ),
            SlashCommand(
                SlashCommandId("cancel"),
                "Cancel run",
                "Permanently cancel the active run after confirmation.",
                CommandScope.WORKFLOW,
            ),
            SlashCommand(
                SlashCommandId("retry"),
                "Retry blocked Issue",
                "Explicitly retry one blocked Issue by ID.",
                CommandScope.WORKFLOW,
            ),
            SlashCommand(
                SlashCommandId("reset"),
                "Reset failed Issue",
                "Explicitly reset one failed Issue by ID.",
                CommandScope.WORKFLOW,
            ),
            SlashCommand(
                SlashCommandId("finalize"),
                "Finalize workspace",
                "Create the local Handoff Summary and leave the workspace intact.",
                CommandScope.STEP,
            ),
            SlashCommand(
                SlashCommandId("accept"),
                "Accept analysis",
                "Validate and publish the current PRD Package.",
                CommandScope.STEP,
            ),
            SlashCommand(
                SlashCommandId("request-changes"),
                "Request changes",
                "Continue the analysis thread with requested changes.",
                CommandScope.STEP,
            ),
        )
    )

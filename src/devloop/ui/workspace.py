from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static

from devloop.components.workspace import WorkspaceProposal
from devloop.domain.development import WorkspaceChoice


class WorkspaceChoiceSelected(Message):
    def __init__(self, choice: WorkspaceChoice) -> None:
        super().__init__()
        self.choice = choice


class WorkspacePreparationView(Vertical):
    DEFAULT_CSS = """
    WorkspacePreparationView { height: 1fr; display: none; padding: 1 2; }
    WorkspacePreparationView #workspace-title { height: 2; }
    WorkspacePreparationView .workspace-option { height: auto; padding: 1; }
    WorkspacePreparationView #workspace-actions { height: 3; align-horizontal: right; }
    """

    def compose(self) -> ComposeResult:
        self._busy = False
        yield Static("Workspace preparation", id="workspace-title")
        yield Static(id="workspace-current", classes="workspace-option")
        yield Static(id="workspace-dedicated", classes="workspace-option")
        with Horizontal(id="workspace-actions"):
            yield Button("Cancel", id="workspace-cancel")
            yield Button("Current checkout", id="workspace-current-choice")
            yield Button("Dedicated worktree", id="workspace-dedicated-choice", variant="primary")

    def show_proposal(self, proposal: WorkspaceProposal) -> None:
        self.display = True
        self.query_one("#workspace-current", Static).update(
            f"Current checkout\nPath: {proposal.current_path}\nBase: {proposal.base_commit}"
        )
        self.query_one("#workspace-dedicated", Static).update(
            "Dedicated worktree\n"
            f"Path: {proposal.dedicated_path}\n"
            f"Branch: {proposal.dedicated_branch}\n"
            f"Base: {proposal.base_commit}"
        )

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button in self.query(Button):
            button.disabled = busy

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {
            "workspace-cancel": WorkspaceChoice.CANCEL,
            "workspace-current-choice": WorkspaceChoice.CURRENT_CHECKOUT,
            "workspace-dedicated-choice": WorkspaceChoice.DEDICATED_WORKTREE,
        }
        button_id = event.button.id
        if button_id is None:
            return
        choice = choices.get(button_id)
        if choice is not None:
            self.post_message(WorkspaceChoiceSelected(choice))

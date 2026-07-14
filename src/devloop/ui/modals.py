from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from devloop.application.capabilities import (
    CapabilityOptionsSession,
    CapabilityProfileSet,
    CapabilitySelection,
)
from devloop.domain.capabilities import CapabilityDescriptor
from devloop.domain.identifiers import CapabilityId, StepComponentId
from devloop.domain.language import LanguageTag
from devloop.domain.operations import (
    ApprovalDecision,
    ApprovalRequest,
    StopAction,
    StopRequest,
)

MODAL_CSS = """
    ApprovalModal, StopModal, CancelRunConfirmationModal,
    CapabilityOptionsModal, LanguageModal {
        align: center middle;
        background: $background 60%;
    }
    ApprovalModal > Vertical, StopModal > Vertical,
    CancelRunConfirmationModal > Vertical, CapabilityOptionsModal > Vertical,
    LanguageModal > Vertical {
        width: 72;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    .modal-actions { height: 3; align-horizontal: right; }
"""


class ApprovalModal(ModalScreen[ApprovalDecision | None]):
    DEFAULT_CSS = MODAL_CSS

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        issue = "-" if self.request.issue_id is None else self.request.issue_id.value
        target = self.request.target or "-"
        reason = self.request.reason or "Not supplied by the backend."
        with Vertical():
            yield Label("Codex approval required")
            yield Static(
                f"Step: {self.request.step.value}\nIssue: {issue}\n"
                f"Action: {self.request.action}\nTarget: {target}\nReason: {reason}"
            )
            with Horizontal(classes="modal-actions"):
                for decision in self.request.supported_decisions:
                    yield Button(
                        _decision_label(decision),
                        id=f"approval-{decision.value}",
                        variant="primary" if decision is ApprovalDecision.ACCEPT else "default",
                    )

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        prefix = "approval-"
        button_id = event.button.id
        if button_id is None or not button_id.startswith(prefix):
            return
        self.dismiss(ApprovalDecision(button_id.removeprefix(prefix)))


class StopModal(ModalScreen[StopAction]):
    DEFAULT_CSS = MODAL_CSS

    def __init__(self, request: StopRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        step = "-" if self.request.step is None else self.request.step.value
        issue = "-" if self.request.issue_id is None else self.request.issue_id.value
        with Vertical():
            yield Label("Stop or continue?")
            yield Static(
                f"Step: {step}\nIssue: {issue}\n"
                "Stopping never merges, pushes, deletes a branch, or removes a worktree."
            )
            with Horizontal(classes="modal-actions"):
                for action in self.request.supported_actions:
                    yield Button(
                        action.value.replace("_", " ").title(),
                        id=f"stop-{action.value.lower()}",
                        variant="error" if action is StopAction.CANCEL_RUN else "default",
                    )

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id is None or not button_id.startswith("stop-"):
            return
        self.dismiss(StopAction(button_id.removeprefix("stop-").upper()))


class CancelRunConfirmationModal(ModalScreen[bool]):
    DEFAULT_CSS = MODAL_CSS

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Confirm permanent cancellation")
            yield Static(
                "The run becomes terminal. The workspace and all Git topology remain intact."
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Keep run", id="cancel-no")
                yield Button("Cancel run", id="cancel-yes", variant="error")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id in {"cancel-no", "cancel-yes"}:
            self.dismiss(event.button.id == "cancel-yes")


class CapabilityOptionsModal(ModalScreen[CapabilityProfileSet | None]):
    DEFAULT_CSS = MODAL_CSS + """
    #capability-options { height: 1fr; min-height: 12; }
    #capability-search { margin: 1 0; }
    #capability-help { height: 2; }
    """

    def __init__(self, session: CapabilityOptionsSession) -> None:
        super().__init__()
        self._session = session
        self._options: dict[str, tuple[StepComponentId, CapabilityId]] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Step Capability Profiles")
            yield Input(
                placeholder="Search installed Skills and Agent References",
                id="capability-search",
            )
            yield OptionList(id="capability-options")
            yield Static(
                "Required capabilities are locked. Changes are saved only by Apply.",
                id="capability-help",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Reset", id="capability-reset")
                yield Button("Cancel", id="capability-cancel")
                yield Button("Apply", id="capability-apply", variant="primary")

    def on_mount(self) -> None:
        self._rebuild("")
        self.query_one("#capability-search", Input).focus()

    @on(Input.Changed, "#capability-search")
    def search(self, event: Input.Changed) -> None:
        self._rebuild(event.value)

    @on(OptionList.OptionSelected, "#capability-options")
    def toggle(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        selection = None if option_id is None else self._options.get(option_id)
        if selection is None:
            return
        try:
            self._session.toggle(*selection)
        except ValueError as error:
            self.query_one("#capability-help", Static).update(str(error))
        self._rebuild(self.query_one("#capability-search", Input).value)

    @on(Button.Pressed)
    def action(self, event: Button.Pressed) -> None:
        if event.button.id == "capability-reset":
            self._session.reset()
            self._rebuild(self.query_one("#capability-search", Input).value)
        elif event.button.id == "capability-cancel":
            self._session.cancel()
            self.dismiss(None)
        elif event.button.id == "capability-apply":
            self.dismiss(self._session.apply())

    def _rebuild(self, query: str) -> None:
        descriptors = self._session.search(query)
        profiles = self._session.current.profiles
        option_list = self.query_one("#capability-options", OptionList)
        option_list.clear_options()
        self._options.clear()
        options: list[Option] = []
        for profile in profiles:
            for descriptor in descriptors:
                option_id = f"{profile.component_id.value}|{descriptor.capability_id.value}"
                self._options[option_id] = (profile.component_id, descriptor.capability_id)
                options.append(
                    Option(
                        _capability_label(profile, descriptor),
                        id=option_id,
                    )
                )
        option_list.add_options(options)


class LanguageModal(ModalScreen[LanguageTag | None]):
    DEFAULT_CSS = MODAL_CSS

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Content language")
            yield Input(placeholder="Language tag, for example el-GR or zh-Hans", id="language-tag")
            yield Static("Machine identifiers remain stable English tokens.", id="language-help")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="language-cancel")
                yield Button("Apply", id="language-apply", variant="primary")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "language-cancel":
            self.dismiss(None)
            return
        if event.button.id != "language-apply":
            return
        try:
            language = LanguageTag(self.query_one("#language-tag", Input).value.strip())
        except ValueError as error:
            self.query_one("#language-help", Static).update(str(error))
            return
        self.dismiss(language)


def _decision_label(decision: ApprovalDecision) -> str:
    labels = {
        ApprovalDecision.ACCEPT: "Accept",
        ApprovalDecision.ACCEPT_FOR_SESSION: "Accept for session",
        ApprovalDecision.DECLINE: "Decline",
        ApprovalDecision.CANCEL: "Cancel request",
    }
    return labels[decision]


def _capability_label(
    profile: CapabilitySelection,
    descriptor: CapabilityDescriptor,
) -> str:
    if descriptor.capability_id in profile.required:
        marker = "[required, locked]"
    elif descriptor.capability_id in profile.selected:
        marker = "[selected]"
    elif descriptor.capability_id in profile.defaults:
        marker = "[default, replaced]"
    else:
        marker = "[available]"
    return (
        f"{profile.component_id.value} | {descriptor.kind.value} | "
        f"{descriptor.title} {marker}"
    )

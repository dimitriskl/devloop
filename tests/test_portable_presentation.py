from __future__ import annotations

import io
import unittest
from threading import Event, Thread, get_ident

from devloop.portable_presentation import (
    PORTABLE_UI_MODE_ENVIRONMENT_VARIABLE,
    PortableActivity,
    PortableActivityFeed,
    PortableActivityStatus,
    PortableListItem,
    PortableUiMode,
    PortableViewModel,
    requested_portable_ui_mode,
    select_portable_ui_mode,
)
from devloop.portable_runtime import (
    PortableRunContext,
    PortableRoutedStream,
    PortableRuntimeBridge,
    PortableRuntimeEventKind,
    PortableRuntimeStopped,
)
from devloop.portable_runtime import portable_runtime_session
from devloop.terminal_editor import TerminalEditor
from devloop.terminal_menu import choose_menu_option, render_app_screen


class PortableUiModeTests(unittest.TestCase):
    def test_interactive_terminal_uses_application_shell(self) -> None:
        mode = select_portable_ui_mode(
            requested_mode=None,
            stdin_is_tty=True,
            stdout_is_tty=True,
            term="xterm-256color",
        )

        self.assertIs(mode, PortableUiMode.APPLICATION)

    def test_launcher_can_preserve_application_mode_when_python_tty_probe_disagrees(self) -> None:
        requested = requested_portable_ui_mode(
            explicit_mode=None,
            environment={PORTABLE_UI_MODE_ENVIRONMENT_VARIABLE: "application"},
        )

        mode = select_portable_ui_mode(
            requested_mode=requested,
            stdin_is_tty=False,
            stdout_is_tty=True,
            term="dumb",
        )

        self.assertIs(mode, PortableUiMode.APPLICATION)

    def test_explicit_plain_mode_wins_over_launcher_application_mode(self) -> None:
        requested = requested_portable_ui_mode(
            explicit_mode=PortableUiMode.PLAIN,
            environment={PORTABLE_UI_MODE_ENVIRONMENT_VARIABLE: "application"},
        )

        self.assertIs(requested, PortableUiMode.PLAIN)


class PortableViewModelTests(unittest.TestCase):
    def test_selection_immediately_changes_the_preview(self) -> None:
        view = PortableViewModel(
            path=("Startup", "Resume"),
            title="Unfinished PRDs",
            items=(
                PortableListItem("one", "First PRD", ("3 remaining",)),
                PortableListItem("two", "Second PRD", ("1 remaining",)),
            ),
            selected_id="one",
        )

        selected = view.select("two")

        self.assertEqual(selected.selected_item.label, "Second PRD")
        self.assertEqual(selected.preview_lines, ("1 remaining",))
        self.assertEqual(view.selected_item.label, "First PRD")


class PortableActivityFeedTests(unittest.TestCase):
    def test_repeated_operation_updates_one_bounded_activity_item(self) -> None:
        feed = PortableActivityFeed().publish(
            PortableActivity(
                operation_id="command-7",
                message="Running repository command",
                status=PortableActivityStatus.RUNNING,
            )
        )

        completed = feed.publish(
            PortableActivity(
                operation_id="command-7",
                message="Repository command finished",
                status=PortableActivityStatus.SUCCEEDED,
            )
        )

        self.assertEqual(len(completed.items), 1)
        self.assertEqual(completed.items[0].message, "Repository command finished")
        self.assertIs(completed.items[0].status, PortableActivityStatus.SUCCEEDED)

    def test_updated_activity_moves_to_the_visible_end_of_the_feed(self) -> None:
        feed = PortableActivityFeed(
            (
                PortableActivity("first", "First", PortableActivityStatus.RUNNING),
                PortableActivity("second", "Second", PortableActivityStatus.NOTICE),
            )
        )

        updated = feed.publish(
            PortableActivity("first", "Finished", PortableActivityStatus.SUCCEEDED)
        )

        self.assertEqual(
            [item.operation_id for item in updated.items],
            ["second", "first"],
        )


class PortableRuntimeBridgeTests(unittest.TestCase):
    def test_stop_request_releases_a_blocked_choice(self) -> None:
        bridge = PortableRuntimeBridge()
        stopped: list[PortableRuntimeStopped] = []

        def choose() -> None:
            try:
                bridge.choose(
                    (("continue", "Continue"), ("exit", "Exit")),
                    default_key="continue",
                    cancel_key="exit",
                    render=lambda _key: None,
                )
            except PortableRuntimeStopped as error:
                stopped.append(error)

        worker = Thread(target=choose)
        worker.start()
        request = bridge.next_event(timeout=1)

        bridge.request_stop()
        worker.join(timeout=1)

        self.assertIs(
            request.kind,
            PortableRuntimeEventKind.CHOICE_REQUESTED,
        )
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(stopped), 1)

    def test_run_context_is_published_as_explicit_runtime_state(self) -> None:
        bridge = PortableRuntimeBridge()
        context = PortableRunContext(
            project_root=r"E:\LocalCode\eConnectorV2",
            implementation_branch="devloop/feature",
            implementation_worktree=r"E:\Worktrees\eConnectorV2-feature-dev",
            prd_path=r"E:\LocalCode\eConnectorV2\prd\feature\feature.md",
        )

        bridge.update_run_context(context)

        event = bridge.next_event(timeout=1)
        self.assertIs(event.kind, PortableRuntimeEventKind.RUN_CONTEXT_UPDATED)
        self.assertEqual(event.run_context, context)

    def test_choice_request_round_trips_through_the_presentation_seam(self) -> None:
        bridge = PortableRuntimeBridge()
        selected: list[str] = []
        worker = Thread(
            target=lambda: selected.append(
                bridge.choose(
                    (("start", "Start a new change"), ("exit", "Exit")),
                    default_key="start",
                    cancel_key="exit",
                    render=lambda _key: None,
                )
            )
        )

        worker.start()
        request = bridge.next_event(timeout=1)
        self.assertIs(request.kind, PortableRuntimeEventKind.CHOICE_REQUESTED)
        bridge.respond(request.request_id, "exit")
        worker.join(timeout=1)

        self.assertEqual(selected, ["exit"])

    def test_highlight_change_refreshes_preview_before_confirmation(self) -> None:
        bridge = PortableRuntimeBridge()
        preview_updated = Event()
        rendered: list[str] = []

        def render(key: str) -> None:
            rendered.append(key)
            if key == "resume":
                preview_updated.set()

        worker = Thread(
            target=lambda: bridge.choose(
                (("start", "Start"), ("resume", "Resume")),
                default_key="start",
                cancel_key=None,
                render=render,
            )
        )
        worker.start()
        request = bridge.next_event(timeout=1)

        bridge.preview(request.request_id, "resume")
        self.assertTrue(preview_updated.wait(timeout=1))
        bridge.respond(request.request_id, "resume")
        worker.join(timeout=1)

        self.assertEqual(rendered, ["start", "resume"])

    def test_free_form_input_is_requested_inside_the_application(self) -> None:
        bridge = PortableRuntimeBridge()
        entered: list[str] = []
        worker = Thread(
            target=lambda: entered.append(bridge.read_line("Target project root"))
        )
        worker.start()

        request = bridge.next_event(timeout=1)
        self.assertIs(request.kind, PortableRuntimeEventKind.INPUT_REQUESTED)
        self.assertEqual(request.prompt, "Target project root")
        bridge.respond(request.request_id, "E:\\LocalCode\\example")
        worker.join(timeout=1)

        self.assertEqual(entered, ["E:\\LocalCode\\example"])

    def test_existing_terminal_editor_uses_the_application_input_view(self) -> None:
        bridge = PortableRuntimeBridge()
        entered: list[str] = []

        def read() -> None:
            entered.append(
                TerminalEditor(
                    on_paste_image=lambda: None,
                    fallback_hint=None,
                ).read_line("Goal: ")
            )

        with portable_runtime_session(bridge):
            worker = Thread(target=read)
            worker.start()
            request = bridge.next_event(timeout=1)
            bridge.respond(request.request_id, "Build the shell")
            worker.join(timeout=1)

        self.assertEqual(entered, ["Build the shell"])

    def test_existing_screen_render_is_contained_by_the_application(self) -> None:
        bridge = PortableRuntimeBridge()

        with portable_runtime_session(bridge):
            render_app_screen("Dev Loop > Startup")

        event = bridge.next_event(timeout=1)
        self.assertIs(event.kind, PortableRuntimeEventKind.SCREEN_UPDATED)
        self.assertEqual(event.content, "Dev Loop > Startup")

    def test_existing_finite_menu_uses_the_application_choice_view(self) -> None:
        bridge = PortableRuntimeBridge()
        selected: list[str] = []

        def choose() -> None:
            selected.append(
                choose_menu_option(
                    (("start", "Start"), ("exit", "Exit")),
                    default_key="start",
                    cancel_key="exit",
                    render=lambda key: render_app_screen(f"selected:{key}"),
                    fallback=lambda: "fallback",
                )
            )

        with portable_runtime_session(bridge):
            worker = Thread(target=choose)
            worker.start()
            request = bridge.next_event(timeout=1)
            self.assertIs(request.kind, PortableRuntimeEventKind.CHOICE_REQUESTED)
            preview = bridge.next_event(timeout=1)
            self.assertEqual(preview.content, "selected:start")
            bridge.respond(request.request_id, "exit")
            worker.join(timeout=1)

        self.assertEqual(selected, ["exit"])

    def test_worker_output_is_routed_inside_the_application(self) -> None:
        bridge = PortableRuntimeBridge()
        terminal = io.StringIO()
        stream = PortableRoutedStream(
            bridge,
            terminal,
            application_thread_id=get_ident(),
            is_error=False,
        )

        stream.write("application-render")
        worker = Thread(target=lambda: stream.write("repository command finished\n"))
        worker.start()
        worker.join(timeout=1)

        event = bridge.next_event(timeout=1)
        self.assertEqual(terminal.getvalue(), "application-render")
        self.assertIs(event.kind, PortableRuntimeEventKind.OUTPUT_WRITTEN)
        self.assertEqual(event.content, "repository command finished\n")


if __name__ == "__main__":
    unittest.main()

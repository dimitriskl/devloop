from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from textual.widgets import Input, OptionList, Static

from devloop.issue_reply import issue_reply_context
from devloop.portable_sessions import (
    PortableSessionEvent,
    PortableSessionInputKind,
    PortableSessionInputRequest,
    PortableSessionIntentKind,
    PortableSessionLaunch,
    PortableSessionProgress,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableWorkflowOperation,
)

from devloopv2.activity_stream import NOTICE, PROBLEM, ActivityStream, unseen_tail
from devloopv2.guidance_store import (
    GuidanceStore,
    NoActiveIssue,
    guidance_store_for,
    issues_index_path,
)
from devloopv2.input_prompt import UnresolvedAnswer, prompt_lines, resolve_answer
from devloopv2.session_target import resolve_session_target
from devloopv2.shell import MinimalShell
from devloopv2 import runner

CHECKOUT = Path.cwd().resolve()


def snapshot(
    *,
    session_id: str = "session",
    status: PortableSessionStatus = PortableSessionStatus.RUNNING,
    activity: tuple[str, ...] = (),
    diagnostics: tuple[str, ...] = (),
    prd_path: Path | None = None,
    updated_at: float = 0.0,
    result: int | None = None,
    recovery_available: bool = False,
    input_request: PortableSessionInputRequest | None = None,
    progress: PortableSessionProgress = PortableSessionProgress(),
    current_screen: str = "",
) -> PortableSessionSnapshot:
    return PortableSessionSnapshot(
        session_id=session_id,
        checkout=CHECKOUT,
        status=status,
        activity=activity,
        diagnostics=diagnostics,
        prd_path=prd_path,
        updated_at=updated_at,
        result=result,
        recovery_available=recovery_available,
        input_request=input_request,
        progress=progress,
        current_screen=current_screen,
    )


class UnseenTailTests(unittest.TestCase):
    def test_plain_append_returns_only_the_new_entries(self) -> None:
        self.assertEqual(unseen_tail(("a", "b"), ("a", "b", "c")), ("c",))

    def test_truncated_history_recovers_the_new_entries_by_overlap(self) -> None:
        """The supervisor caps activity at 100 entries, so positions shift left."""
        previous = tuple(str(index) for index in range(100))
        current = tuple(str(index) for index in range(3, 103))

        self.assertEqual(unseen_tail(previous, current), ("100", "101", "102"))

    def test_unrelated_history_replays_everything(self) -> None:
        self.assertEqual(unseen_tail(("a",), ("x", "y")), ("x", "y"))

    def test_unchanged_activity_produces_nothing(self) -> None:
        self.assertEqual(unseen_tail(("a", "b"), ("a", "b")), ())


class ActivityStreamTests(unittest.TestCase):
    def test_only_unseen_output_and_diagnostics_are_emitted(self) -> None:
        stream = ActivityStream()
        stream.consume(snapshot(activity=("first",)))

        lines = stream.consume(
            snapshot(activity=("first", "second"), diagnostics=("boom",))
        )

        self.assertEqual(
            [(line.text, line.kind) for line in lines],
            [("second", "output"), ("boom", PROBLEM)],
        )

    def test_multi_line_output_becomes_one_line_each(self) -> None:
        stream = ActivityStream()

        lines = stream.consume(snapshot(activity=("one\ntwo",)))

        self.assertEqual([line.text for line in lines], ["one", "two"])

    def test_pausing_is_announced_once(self) -> None:
        stream = ActivityStream()
        stream.consume(snapshot(status=PortableSessionStatus.RUNNING))

        first = stream.consume(snapshot(status=PortableSessionStatus.PAUSED))
        again = stream.consume(snapshot(status=PortableSessionStatus.PAUSED))

        self.assertEqual([line.kind for line in first], [NOTICE])
        self.assertIn("Paused", first[0].text)
        self.assertEqual(again, ())

    def test_stage_changes_are_announced_with_the_active_issue(self) -> None:
        stream = ActivityStream()

        lines = stream.consume(
            snapshot(progress=PortableSessionProgress(stage="QA", active_issue="0004"))
        )

        self.assertIn("QA - issue 0004", [line.text for line in lines])


class InputPromptTests(unittest.TestCase):
    def choice(self) -> PortableSessionInputRequest:
        return PortableSessionInputRequest(
            kind=PortableSessionInputKind.CHOICE,
            request_id="request",
            generation=1,
            prompt="Apply the patch?",
            options=(("accept", "Accept"), ("deny", "Deny")),
            default_key="deny",
        )

    def test_choices_render_as_a_numbered_list(self) -> None:
        lines = prompt_lines(self.choice())

        self.assertEqual(lines[0], "Apply the patch?")
        self.assertIn("  1. Accept", lines)
        self.assertIn("  2. Deny (default)", lines)
        self.assertIn("Type the number or the option text and press Enter.", lines)

    def test_a_typed_number_selects_that_option(self) -> None:
        self.assertEqual(resolve_answer(self.choice(), "1"), "accept")

    def test_a_typed_label_selects_that_option_regardless_of_case(self) -> None:
        self.assertEqual(resolve_answer(self.choice(), "  DENY "), "deny")

    def test_an_unambiguous_prefix_selects_that_option(self) -> None:
        self.assertEqual(resolve_answer(self.choice(), "acc"), "accept")

    def test_an_empty_answer_takes_the_default(self) -> None:
        self.assertEqual(resolve_answer(self.choice(), ""), "deny")

    def test_an_out_of_range_number_is_refused(self) -> None:
        with self.assertRaises(UnresolvedAnswer):
            resolve_answer(self.choice(), "9")

    def test_unknown_text_is_refused(self) -> None:
        with self.assertRaises(UnresolvedAnswer):
            resolve_answer(self.choice(), "maybe")

    def test_text_requests_pass_the_typed_value_through(self) -> None:
        request = PortableSessionInputRequest(
            kind=PortableSessionInputKind.TEXT,
            prompt="Branch name?",
        )

        self.assertEqual(resolve_answer(request, " feature/x "), "feature/x")

    def test_reply_requests_are_encoded_as_an_issue_reply(self) -> None:
        request = PortableSessionInputRequest(kind=PortableSessionInputKind.REPLY)

        self.assertIn('"text": "looks wrong"', resolve_answer(request, "looks wrong"))


class SessionTargetTests(unittest.TestCase):
    def test_no_recorded_session_starts_a_new_one(self) -> None:
        target = resolve_session_target(
            (), new_session_id="fresh", prd_path=Path("a.md")
        )

        self.assertEqual((target.session_id, target.resume), ("fresh", False))

    def test_the_newest_paused_session_for_the_prd_is_continued(self) -> None:
        prd = (CHECKOUT / "feature.md").resolve()
        sessions = (
            snapshot(
                session_id="old",
                status=PortableSessionStatus.PAUSED,
                prd_path=prd,
                updated_at=1.0,
            ),
            snapshot(
                session_id="new",
                status=PortableSessionStatus.INTERRUPTED,
                prd_path=prd,
                updated_at=2.0,
            ),
        )

        target = resolve_session_target(
            sessions, new_session_id="fresh", prd_path=prd
        )

        self.assertEqual((target.session_id, target.resume), ("new", True))

    def test_finished_sessions_for_the_prd_are_not_continued(self) -> None:
        prd = (CHECKOUT / "done.md").resolve()
        sessions = (
            snapshot(
                session_id="done",
                status=PortableSessionStatus.COMPLETED,
                prd_path=prd,
                updated_at=5.0,
            ),
        )

        target = resolve_session_target(
            sessions, new_session_id="fresh", prd_path=prd
        )

        self.assertEqual((target.session_id, target.resume), ("fresh", False))

    def test_a_session_for_another_prd_is_not_continued(self) -> None:
        sessions = (
            snapshot(
                session_id="other",
                status=PortableSessionStatus.PAUSED,
                prd_path=(CHECKOUT / "other.md").resolve(),
                updated_at=5.0,
            ),
        )

        target = resolve_session_target(
            sessions,
            new_session_id="fresh",
            prd_path=(CHECKOUT / "mine.md").resolve(),
        )

        self.assertEqual((target.session_id, target.resume), ("fresh", False))

    def test_an_orphaned_running_session_is_adopted(self) -> None:
        prd = (CHECKOUT / "orphan.md").resolve()
        sessions = (
            snapshot(
                session_id="orphan",
                status=PortableSessionStatus.RUNNING,
                prd_path=prd,
                recovery_available=True,
            ),
        )

        target = resolve_session_target(
            sessions, new_session_id="fresh", prd_path=prd
        )

        self.assertEqual((target.session_id, target.resume), ("orphan", True))


class GuidanceStoreTests(unittest.TestCase):
    def test_saved_guidance_is_read_back_as_issue_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = GuidanceStore(Path(directory) / ".loop.logs")

            store.save("0004", "check that this field is validated")

            context, _images = issue_reply_context(store.log_root, "0004")
            self.assertIn("check that this field is validated", context)

    def test_guidance_without_an_active_issue_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = GuidanceStore(Path(directory) / ".loop.logs")

            with self.assertRaises(NoActiveIssue):
                store.save(None, "nowhere to put this")

    def test_the_log_root_matches_the_runner_default_for_a_prd(self) -> None:
        prd = (CHECKOUT / "prd" / "feature" / "feature.md").resolve()

        store = guidance_store_for(prd, None)

        self.assertEqual(
            store.log_root,
            issues_index_path(prd, None).parent / ".loop.logs",
        )

    def test_an_explicit_issues_index_decides_the_log_root(self) -> None:
        prd = (CHECKOUT / "prd" / "feature" / "feature.md").resolve()
        index = CHECKOUT / "elsewhere" / "README.md"

        store = guidance_store_for(prd, str(index))

        self.assertEqual(store.log_root, index.parent.resolve() / ".loop.logs")


class FakeSupervisor:
    """Record intents instead of launching workers, and replay queued snapshots."""

    def __init__(self, sessions: tuple[PortableSessionSnapshot, ...] = ()) -> None:
        self._sessions = sessions
        self._events: deque[PortableSessionEvent] = deque()
        self.intents: list[object] = []
        self.paused: list[str] = []
        self.shutdowns = 0
        self.reply = snapshot()
        #: Intent kinds this supervisor refuses, and the error it raises for each.
        self.refusals: dict[PortableSessionIntentKind, Exception] = {}

    def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
        return self._sessions

    def handle_intent(self, intent: object) -> PortableSessionSnapshot:
        self.intents.append(intent)
        refusal = self.refusals.get(intent.kind)  # type: ignore[attr-defined]
        if refusal is not None:
            raise refusal
        return self.reply

    def pause_session(self, session_id: str) -> PortableSessionSnapshot:
        self.paused.append(session_id)
        return self.reply

    def try_next_event(self) -> PortableSessionEvent | None:
        return self._events.popleft() if self._events else None

    def shutdown(self) -> None:
        self.shutdowns += 1

    def pending(self) -> bool:
        return bool(self._events)

    def publish(self, value: PortableSessionSnapshot) -> None:
        self._events.append(PortableSessionEvent(value))


class MinimalShellRunnerTests(unittest.TestCase):
    def test_options_exit_returns_to_the_minimal_shell(self) -> None:
        launch = PortableSessionLaunch(
            session_id="fresh",
            checkout=CHECKOUT,
            operation=PortableWorkflowOperation.DELIVERY,
            arguments=("--prd", str(CHECKOUT / "feature.md")),
        )
        catalog = mock.Mock()
        options_shell = mock.Mock(
            options_requested=True,
            options_session_id="persisted-session",
            launch_error=None,
            exit_code=130,
        )
        resumed_shell = mock.Mock(options_requested=False, launch_error=None, exit_code=0)
        with (
            mock.patch(
                "devloop.portable_session_catalog.PortableSessionCatalog",
                return_value=catalog,
            ),
            mock.patch("devloop.portable_sessions.PortableSessionSupervisor"),
            mock.patch.object(
                runner,
                "MinimalShell",
                side_effect=(options_shell, resumed_shell),
            ) as shell_type,
            mock.patch.object(runner, "guidance_store_for"),
            mock.patch("devloop.cli.run_options_command", return_value=0) as open_options,
        ):
            result = runner.run_minimal_shell(
                launch,
                prd_path=CHECKOUT / "feature.md",
                issues_argument=None,
            )

        self.assertEqual(result, 0)
        options_shell.run.assert_called_once_with()
        resumed_shell.run.assert_called_once_with()
        open_options.assert_called_once_with(())
        self.assertIsNone(shell_type.call_args_list[0].kwargs["resume_session_id"])
        self.assertEqual(
            shell_type.call_args_list[1].kwargs["resume_session_id"],
            "persisted-session",
        )


class MinimalShellTests(unittest.IsolatedAsyncioTestCase):
    def build(
        self,
        supervisor: FakeSupervisor,
        *,
        log_root: Path,
        prd_path: Path = CHECKOUT / "feature.md",
    ) -> MinimalShell:
        return MinimalShell(
            supervisor=supervisor,  # type: ignore[arg-type]
            launch=PortableSessionLaunch(
                session_id="fresh",
                checkout=CHECKOUT,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=("--prd", str(prd_path)),
            ),
            guidance=GuidanceStore(log_root),
            prd_path=prd_path,
        )

    async def settle(self, pilot, supervisor: FakeSupervisor) -> None:
        """Wait until the shell's poll timer has actually consumed the queue."""
        for _ in range(200):
            if not supervisor.pending():
                await pilot.pause()
                return
            await pilot.pause(0.01)
        raise AssertionError("the shell never drained the published events")

    async def test_a_fresh_prd_starts_a_new_session_without_a_picker(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()

        self.assertEqual(
            [intent.kind for intent in supervisor.intents],  # type: ignore[attr-defined]
            [PortableSessionIntentKind.START],
        )

    async def test_relaunching_the_same_prd_continues_the_paused_session(self) -> None:
        prd = (CHECKOUT / "feature.md").resolve()
        supervisor = FakeSupervisor(
            sessions=(
                snapshot(
                    session_id="earlier",
                    status=PortableSessionStatus.PAUSED,
                    prd_path=prd,
                    updated_at=9.0,
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory), prd_path=prd)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()

        intent = supervisor.intents[0]
        self.assertEqual(intent.kind, PortableSessionIntentKind.RESUME)  # type: ignore[attr-defined]
        self.assertEqual(intent.session_id, "earlier")  # type: ignore[attr-defined]

    async def test_an_uncontinuable_earlier_session_falls_back_to_a_fresh_start(
        self,
    ) -> None:
        """Interrupted before its first checkpoint, the earlier session has nothing to recover."""
        prd = (CHECKOUT / "feature.md").resolve()
        supervisor = FakeSupervisor(
            sessions=(
                snapshot(
                    session_id="earlier",
                    status=PortableSessionStatus.INTERRUPTED,
                    prd_path=prd,
                ),
            )
        )
        supervisor.refusals[PortableSessionIntentKind.RESUME] = ValueError(
            "Cannot recover delivery: durable loop state is missing."
        )
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory), prd_path=prd)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                self.assertFalse(app._finished)

        self.assertEqual(
            [intent.kind for intent in supervisor.intents],  # type: ignore[attr-defined]
            [PortableSessionIntentKind.RESUME, PortableSessionIntentKind.START],
        )
        self.assertIsNone(app.launch_error)

    async def test_a_refused_start_keeps_the_reason_for_the_caller(self) -> None:
        supervisor = FakeSupervisor()
        supervisor.refusals[PortableSessionIntentKind.START] = RuntimeError(
            "worktree is leased by another session"
        )
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()

        self.assertEqual(app.exit_code, 73)
        self.assertEqual(app.launch_error, "worktree is leased by another session")
        self.assertEqual(supervisor.shutdowns, 1)

    async def test_escape_pauses_the_running_session(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()

        self.assertEqual(supervisor.paused, ["session"])

    async def test_first_control_c_pauses_and_the_second_exits(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("ctrl+c")
                await pilot.pause()
                self.assertEqual(supervisor.paused, ["session"])
                supervisor.publish(snapshot(status=PortableSessionStatus.PAUSED))
                await self.settle(pilot, supervisor)
                await pilot.press("ctrl+c")
                await pilot.pause()

        self.assertEqual(app.exit_code, 130)
        self.assertEqual(supervisor.shutdowns, 1)

    async def test_slash_menu_opens_options_after_a_durable_pause(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("/")
                await pilot.pause()

                menu = app.query_one("#slash-commands", OptionList)
                self.assertTrue(menu.display)
                self.assertEqual(
                    [menu.get_option_at_index(index).id for index in range(menu.option_count)],
                    ["/exit", "/options"],
                )

                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertEqual(supervisor.paused, ["session"])
                self.assertFalse(app._finished)

                supervisor.publish(snapshot(status=PortableSessionStatus.PAUSED))
                await self.settle(pilot, supervisor)

        self.assertTrue(app.options_requested)
        self.assertEqual(app.exit_code, 0)
        self.assertEqual(supervisor.shutdowns, 1)

    async def test_slash_menu_opens_options_after_an_interrupted_pause(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                entry = app.query_one("#entry", Input)
                entry.value = "/options"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(supervisor.paused, ["session"])

                supervisor.publish(snapshot(status=PortableSessionStatus.INTERRUPTED))
                await self.settle(pilot, supervisor)

        self.assertTrue(app.options_requested)
        self.assertEqual(app.exit_code, 0)
        self.assertEqual(supervisor.shutdowns, 1)

    async def test_exit_slash_command_exits_without_saving_guidance(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            log_root = Path(directory) / ".loop.logs"
            app = self.build(supervisor, log_root=log_root)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                entry = app.query_one("#entry", Input)
                entry.value = "/exit"
                await pilot.press("enter")
                await pilot.pause()

            self.assertFalse(log_root.exists())

        self.assertEqual(app.exit_code, 130)
        self.assertEqual(supervisor.shutdowns, 1)

    async def test_a_numbered_choice_is_answered_by_typing_its_number(self) -> None:
        supervisor = FakeSupervisor()
        request = PortableSessionInputRequest(
            kind=PortableSessionInputKind.CHOICE,
            request_id="request-1",
            generation=2,
            prompt="Apply the patch?",
            options=(("accept", "Accept"), ("deny", "Deny")),
            default_key="deny",
        )
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.publish(
                    snapshot(
                        status=PortableSessionStatus.WAITING_FOR_INPUT,
                        input_request=request,
                    )
                )
                await self.settle(pilot, supervisor)
                await pilot.press("1", "enter")
                await pilot.pause()

        answer = supervisor.intents[-1]
        self.assertEqual(answer.kind, PortableSessionIntentKind.PROVIDE_INPUT)  # type: ignore[attr-defined]
        self.assertEqual(answer.value, "accept")  # type: ignore[attr-defined]
        self.assertEqual(answer.request_id, "request-1")  # type: ignore[attr-defined]
        self.assertEqual(answer.request_generation, 2)  # type: ignore[attr-defined]

    async def test_guidance_typed_while_paused_is_saved_then_the_run_continues(
        self,
    ) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            log_root = Path(directory) / ".loop.logs"
            app = self.build(supervisor, log_root=log_root)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.publish(
                    snapshot(
                        status=PortableSessionStatus.PAUSED,
                        progress=PortableSessionProgress(active_issue="0004"),
                    )
                )
                await pilot.pause()
                await pilot.press(*"check the validation")
                await pilot.press("enter")
                await pilot.pause()

            context, _images = issue_reply_context(log_root, "0004")

        self.assertIn("check the validation", context)
        self.assertEqual(
            supervisor.intents[-1].kind,  # type: ignore[attr-defined]
            PortableSessionIntentKind.RESUME,
        )

    async def test_a_bare_enter_while_paused_continues_without_saving_guidance(
        self,
    ) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            log_root = Path(directory) / ".loop.logs"
            app = self.build(supervisor, log_root=log_root)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.publish(snapshot(status=PortableSessionStatus.PAUSED))
                await self.settle(pilot, supervisor)
                await pilot.press("enter")
                await pilot.pause()

            self.assertFalse(log_root.exists())

        self.assertEqual(
            supervisor.intents[-1].kind,  # type: ignore[attr-defined]
            PortableSessionIntentKind.RESUME,
        )

    async def test_the_workers_live_progress_panel_is_shown_above_the_prompt(
        self,
    ) -> None:
        """`current_screen` is re-rendered several times a second by the worker."""
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.publish(
                    snapshot(current_screen="Development\n  coder - 0001 - 00:42")
                )
                await self.settle(pilot, supervisor)
                panel = app.query_one("#live", Static)

                self.assertTrue(panel.display)
                self.assertIn("coder - 0001 - 00:42", str(panel.content))

    async def test_the_live_panel_replaces_itself_instead_of_accumulating(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.publish(snapshot(current_screen="frame one"))
                await self.settle(pilot, supervisor)
                supervisor.publish(snapshot(current_screen="frame two"))
                await self.settle(pilot, supervisor)
                panel = app.query_one("#live", Static)

                self.assertNotIn("frame one", str(panel.content))
                self.assertIn("frame two", str(panel.content))

    async def test_a_stale_live_panel_is_cleared_once_the_run_pauses(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.publish(snapshot(current_screen="spinner frame"))
                await self.settle(pilot, supervisor)
                supervisor.publish(
                    snapshot(
                        status=PortableSessionStatus.PAUSED,
                        current_screen="spinner frame",
                    )
                )
                await self.settle(pilot, supervisor)

                self.assertFalse(app.query_one("#live", Static).display)

    async def test_a_completed_run_exits_with_the_worker_result(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.publish(
                    snapshot(status=PortableSessionStatus.COMPLETED, result=0)
                )
                await self.settle(pilot, supervisor)

        self.assertEqual(app.exit_code, 0)

    async def test_a_failed_run_exits_with_the_worker_result(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.publish(
                    snapshot(status=PortableSessionStatus.FAILED, result=2)
                )
                await self.settle(pilot, supervisor)

        self.assertEqual(app.exit_code, 2)


if __name__ == "__main__":
    unittest.main()

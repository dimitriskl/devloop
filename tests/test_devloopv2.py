from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path

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

    def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
        return self._sessions

    def handle_intent(self, intent: object) -> PortableSessionSnapshot:
        self.intents.append(intent)
        return self.reply

    def pause_session(self, session_id: str) -> PortableSessionSnapshot:
        self.paused.append(session_id)
        return self.reply

    def try_next_event(self) -> PortableSessionEvent | None:
        return self._events.popleft() if self._events else None

    def shutdown(self) -> None:
        self.shutdowns += 1

    def publish(self, value: PortableSessionSnapshot) -> None:
        self._events.append(PortableSessionEvent(value))


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
                await pilot.pause()
                await pilot.press("ctrl+c")
                await pilot.pause()

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
                await pilot.pause()
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
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()

            self.assertFalse(log_root.exists())

        self.assertEqual(
            supervisor.intents[-1].kind,  # type: ignore[attr-defined]
            PortableSessionIntentKind.RESUME,
        )

    async def test_a_completed_run_exits_with_the_worker_result(self) -> None:
        supervisor = FakeSupervisor()
        with tempfile.TemporaryDirectory() as directory:
            app = self.build(supervisor, log_root=Path(directory))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.publish(
                    snapshot(status=PortableSessionStatus.COMPLETED, result=0)
                )
                await pilot.pause()

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
                await pilot.pause()

        self.assertEqual(app.exit_code, 2)


if __name__ == "__main__":
    unittest.main()

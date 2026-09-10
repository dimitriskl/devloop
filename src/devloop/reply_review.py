"""Completion-review interaction; answers never change issue completion markers."""
from __future__ import annotations

from pathlib import Path

from .issue_reply import IssueReply, ReplyAction, save_issue_reply
from .portable_runtime import active_portable_runtime
from .run_review import RunReview, replyable_issues
from .terminal_text import compact_terminal_text


def reply_to_issue(review: RunReview, log_root: Path) -> bool:
    # Import here to reuse the application's existing menu/plain-mode boundary.
    from .cli import choose_menu_option, read_prompt, render_app_screen

    candidates = replyable_issues(review)
    if not candidates:
        return False
    selected = candidates[0].issue_number
    if len(candidates) > 1:
        selected = choose_menu_option(
            tuple((item.issue_number, f"{item.issue_number}: {item.title}") for item in candidates)
            + ((ReplyAction.CANCEL.value, "Back"),),
            default_key=selected, cancel_key=ReplyAction.CANCEL.value,
            render=lambda _: render_app_screen("Dev Loop > Reply > Choose issue"),
            fallback=lambda: ReplyAction.CANCEL.value,
        )
        if selected == ReplyAction.CANCEL.value:
            return False
    item = next(item for item in candidates if item.issue_number == selected)
    context = compact_terminal_text(
        f"Issue {item.issue_number}: {item.title}\n\n{item.detail}\n\n"
        + "\n".join(item.recovery_actions), max_length=6000,
    )
    reply = IssueReply()
    error = ""
    while True:
        runtime = active_portable_runtime()
        if runtime is not None:
            encoded = runtime.read_reply(
                context + (f"\n\nCould not save reply: {error}" if error else ""),
                initial_value=reply.encode(),
            )
            if not encoded:
                return False
            reply = IssueReply.decode(encoded)
        else:
            # Plain-mode fallback stays line-oriented; the full-screen host uses its editor.
            render_app_screen(f"Dev Loop > Reply\n{context}\n\n{error}")
            action = choose_menu_option(
                (
                    (ReplyAction.TEXT.value, "Edit reply text"),
                    (ReplyAction.FILE.value, "Attach file path"),
                    (ReplyAction.SUBMIT.value, "Submit reply and continue"),
                    (ReplyAction.CANCEL.value, "Cancel"),
                ),
                default_key=ReplyAction.TEXT.value, cancel_key=ReplyAction.CANCEL.value,
                render=lambda _: None,
                fallback=lambda: read_prompt("Action (text/file/submit/cancel): "),
            )
            if action == ReplyAction.CANCEL.value:
                return False
            if action == ReplyAction.TEXT.value:
                reply = IssueReply(read_prompt("Reply: "), reply.attachments)
                continue
            if action == ReplyAction.FILE.value:
                path = Path(read_prompt("File path: ").strip().strip('"'))
                reply = IssueReply(reply.text, (*reply.attachments, path))
                continue
            if action != ReplyAction.SUBMIT.value:
                error = "Choose an available action; the reply has not been submitted."
                continue
        try:
            save_issue_reply(log_root, item.issue_number, reply)
        except (OSError, ValueError) as failure:
            error = compact_terminal_text(str(failure), max_length=500)
            continue
        return True

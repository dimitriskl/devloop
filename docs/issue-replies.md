# Replying to a blocked issue

Completion Review offers **Reply to an issue (text / images / files)** for issues
that are blocked, failed, waiting for input, or need changes. If several issues
need attention, choose the issue with the arrow keys and Enter.

The reply view shows the issue and its recorded clarification request. Type or
paste text in the editor; Enter inserts a new line. Tab moves between the editor,
file path field, actions, and attachments.

- Paste a screenshot with **Alt+V**, or select **Paste image / copied files**.
  Ctrl+V also works when the terminal forwards that key; Windows Terminal may
  handle Ctrl+V itself, so Alt+V is the reliable image shortcut.
- On Windows, copy files in Explorer and use the same paste action. Alternatively,
  paste a complete file path into the path field and press Enter. Quoted paths
  and filenames with spaces are supported. Pasting complete existing file paths
  into the reply editor attaches them.
- Select an attachment and press Enter to remove it.
- Choose **Submit reply and continue**, or press Ctrl+Enter. The menu action is
  available when a terminal cannot distinguish Ctrl+Enter from Enter.
- Esc or **Cancel** returns to Completion Review without submitting or rerunning.

Replies accept up to 6000 characters and 16 attachments, each at most 25 MiB.
Validation errors leave the reply available for correction. The application does
not infer an answer or approve a proposed design on the user's behalf.

Submitting copies attachments into the issue pack's `.loop.logs/user-replies/`
and writes the reply record after the copies succeed. Later attempts for that
issue read all submitted replies, including after a restart. The full answer is
separate from Step Guidance. Codex receives images through its native `-i` input;
all execution backends receive the reply text and durable attachment paths in
their prompts. File contents remain supporting material rather than instructions.

Submission renews the unfinished issues' retry budget and continues through the
existing scheduler. Completed issues stay completed, dependencies remain enforced,
and implementation, review, and QA gates are not waived.

An already-running Dev Loop process must be exited and restarted to load this UI
update. Its previous issue state remains on disk. The plain-mode fallback offers
text entry and file paths; clipboard capture is provided by the full-screen UI.

Use `--reply` with the PRD to open a saved blocked issue directly before any new
agent attempt. Cancelling this startup editor exits without starting work.

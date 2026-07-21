# Build Week Hackathon Submission Checklist

Use this page while filling out the Devpost form. Submissions close **Tuesday,
July 21, 2026 at 5:00 PM PT**.

Repository: `https://github.com/dimitriskl/devloop`

## Devpost form fields

### Project description (paste and edit in your own voice)

Dev Loop is a local workflow runner that takes a feature idea through analysis,
PRD creation, implementation, independent code review, QA, and handoff — all
through the real Codex App Server.

I built CodexCLI (`uv tool install .`) as the installable hackathon entry. You
submit a feature request, accept a generated PRD package, choose a workspace,
and watch each issue move through development, review, and QA with explicit
pause, resume, and finalization. Nothing merges or publishes implicitly.

**How I used Codex:** Codex CLI and the Codex App Server orchestrate every
workflow step — session management, tool use, permission prompts, fresh role
threads for review and QA, and `/resume` after interruption. The Textual launcher,
Issue Board, and slash commands all talk to the installed App Server; there is no
simulated backend.

**How I used GPT-5.6:** Each workflow component calls a GPT-5.6 model through
Codex. Analysis, development, code review, and QA default to `gpt-5.6-sol` with
locked reasoning effort (`xhigh` or `low` per execution profile). The portable
Dev Loop wrappers in the same repo also route coder, reviewer, and QA roles to
`gpt-5.6-luna`, `gpt-5.6-sol`, and `gpt-5.6-terra` respectively. I did not
call the OpenAI API directly.

**Try it:** `uv tool install .`, then `./examples/release-demo/run-demo.sh`.
Use the request in `examples/release-demo/feature-request.md`. Prerequisites:
Python 3.10+, Git, authenticated Codex CLI.

### Codex `/feedback` Session ID

1. Open this repository in Codex Desktop or Codex CLI.
2. Open the `/` command menu.
3. Run `/feedback`.
4. Copy the Session ID from the session where most of the project was built.
5. Paste it into the Devpost submission form.

### Repository access

If the repository is private, share it with:

- `testing@devpost.com`
- `build-week-event@openai.com`

### Demo video

Upload a public or unlisted YouTube video and paste the link into Devpost.
Required voiceover topics:

1. What you built (the workflow and resumable run model).
2. How Codex was used (App Server, real `codexcli run`, components, `/resume`).
3. How GPT-5.6 was used (which steps call which models — not just "we used AI").

Keep it under three minutes. Trim loading and typing; speed up playback if
needed.

### Final checks

- [ ] Fresh `codexcli doctor` and demo run succeed.
- [ ] YouTube link is in the form and the video has finished processing.
- [ ] `/feedback` Session ID is entered.
- [ ] README documents setup, sample data, and Codex + GPT-5.6 usage.
- [ ] Team members are invited and have accepted.
- [ ] Submission shows **Submitted** in green on Devpost My Projects (not draft).

## Demo video script (~2:30)

Read this as a voiceover while recording `examples/release-demo/run-demo.sh`.

---

**[0:00] Hook**

"This is Dev Loop CodexCLI — a local workflow runner I built for Build Week. It
takes a feature request through analysis, PRD creation, development, code
review, QA, and handoff, and you can pause and resume exactly where you left
off."

**[0:20] Install and doctor**

"I install it with `uv tool install .`, then run `codexcli doctor` against a Git
repository. Doctor checks Python, Git, Codex CLI, authentication, and the App
Server contract before any real work starts."

**[0:35] Codex orchestration**

"Every step runs through the real Codex App Server — not a mock. Codex manages
sessions, tool calls, and permission prompts. When I submit a feature request,
Codex drives the analysis chat. When I accept the PRD package, Codex prepares the
workspace and runs development, review, and QA as separate components with fresh
threads."

**[0:55] GPT-5.6 models**

"Behind Codex, each component calls GPT-5.6. Analysis, development, review, and
QA default to `gpt-5.6-sol` with high reasoning effort. I did not call the
OpenAI API directly — models are selected through Codex CLI and locked in
execution profiles so every run is reproducible."

**[1:15] Workflow demo**

"I submit the reading-list request from the sample repo. Analysis asks clarifying
questions and publishes a ten-issue PRD package. I accept it, choose a dedicated
worktree, and watch development implement the first issue. Review and QA run in
their own views with independent findings."

**[1:40] Rework and resume**

"When review or QA requests changes, development gets a fresh rework attempt
with the feedback attached — not a continuation of the old thread. Here I pause
during QA, close the app, restart with `/resume`, and land on the same issue,
attempt, and workspace. The interrupted operation is marked unknown instead of
being replayed blindly."

**[2:05] Finalization**

"After every issue passes QA, I run `/finalize`. CodexCLI writes a redacted
Handoff Summary and leaves the workspace intact. It never merges, pushes, or
opens a pull request for you."

**[2:20] Close**

"Dev Loop is open source. Install with `uv tool install .`, try the release demo
under `examples/release-demo`, and see the README for how Codex and GPT-5.6 are
used throughout. Thanks for watching."

---

## Recording steps

1. Run `./examples/release-demo/run-demo.sh` (or the Windows `.ps1` variant).
2. Follow the beat list in `examples/release-demo/README.md`.
3. Record screen + voiceover (clear audio is required).
4. Upload to YouTube (public or unlisted).
5. Wait for YouTube processing to finish before pasting the link into Devpost.

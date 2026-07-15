# Dev Loop

This repository contains two separate Dev Loop applications. Product identity
must be established before applying the vocabulary below.

## Product Boundary

**Portable Dev Loop**:
The `devloop-plan.sh` / `.ps1` planning intake plus the `devloop.sh` / `.ps1`
Markdown issue runner. It uses the portable line editor, console dashboard,
Codex exec role sessions, and PRD-local `*.loop.state.json` state.
_Avoid_: CodexCLI, Textual application, App Server Workflow Run

**CodexCLI**:
The separately installed `codexcli` Textual application built around Codex App
Server, `.devloop/runs/`, component locks, and its own Workflow Run model. It is
not the backend or next phase of Portable Dev Loop.
_Avoid_: devloop-plan, portable wrapper, Markdown issue runner

**Portable Resume Candidate**:
An unfinished Portable Dev Loop PRD and linked issue pack listed at
`devloop-plan` startup or by its `/resume` command. Selecting it opens the
development handoff; the issue runner then restores its exact unfinished
role/pass from PRD-local loop state.
_Avoid_: CodexCLI Resume Candidate, App Server thread

Terms involving the Application Shell, Run Launcher, Run Directory, Execution
Thread, Recovery Attempt, Context Manifest, or `.devloop/runs/` below are
CodexCLI terms unless explicitly qualified as portable. They must not be used to
scope work requested for `devloop-plan + devloop`.

## Language

**Workflow**:
The complete Dev Loop process composed of Workflow Steps connected by Transitions.
_Avoid_: Step, issue, pipeline stage

**Workflow ID**:
A stable validated identifier used to discover and select a Workflow Definition across built-in, user-wide, and project sources.
_Avoid_: Workflow filename, display title

**Workflow Run**:
One durable execution of a Workflow, including its current snapshot, event history, Issues, step attempts, and Artifacts.
_Avoid_: Workflow, session

**Run Directory**:
The project-local, Git-ignored directory `.devloop/runs/<run-id>/` containing one Workflow Run's snapshot, events, manifests, and Artifacts.
_Avoid_: Log folder, temporary directory

**Run Launcher**:
The initial Application Shell view where the user may enter a new feature or invoke `/resume` without creating or starting a Workflow Run automatically.
_Avoid_: Workflow Run, automatic preflight prompt

**Resume Candidate**:
An unfinished Workflow Run listed for explicit user selection by `/resume`.
_Avoid_: Legacy PRD, automatically restarted run

**Execution Backend**:
The boundary Dev Loop uses to start or resume an agent run, independent of the model provider behind it.
_Avoid_: AI provider, model API

**Codex CLI Backend**:
The execution backend that delegates agent runs to the user's installed and configured Codex CLI.
_Avoid_: Codex API

**Execution Thread**:
A Codex App Server conversation scoped to agent work for a Workflow Step attempt.
_Avoid_: Workflow Run, shared cross-step transcript

**Recovery Attempt**:
A fresh Workflow Step attempt offered when an interrupted attempt's Execution Thread is unavailable, created from locked structured context without transcript replay.
_Avoid_: Resumed thread, automatic retry

**Step Attempt Record**:
The immutable history entry for one execution of a Workflow Step, keyed by Step Instance ID and recording its Issue when applicable, pass, backend thread, Step Outcome, typed output Artifacts, and timing.
_Avoid_: Component-wide result, fixed development/review/QA fields

**Step Runtime State**:
The resumable current state of one Workflow Step instance, keyed by Step Instance ID and also by Issue ID when the step is issue-scoped.
_Avoid_: Analysis cursor, review cursor, component-specific top-level snapshot field

**Context Manifest**:
The persisted inventory of the exact instructions, capabilities, Artifacts, and repository scope supplied to an Execution Thread.
_Avoid_: Prompt transcript, raw log bundle

**Workflow Definition**:
A declarative description of a workflow's steps and its outcome-driven transitions.
_Avoid_: Pipeline file, step list

**Workflow Step**:
A distinct named instance of a Workflow Step Component within a Workflow Definition. It has its own identity, inputs, outputs, transitions, display name, and Codex Execution Settings.
_Avoid_: Phase, stage, component class

**Workflow Step Type**:
The installed Workflow Step Component selected as the reusable class of a Workflow Step. Available types form an extensible catalog rather than a closed phase enum.
_Avoid_: Step ID, display name, fixed analysis/development/review/QA enum

**Issue**:
A development work item produced by a Workflow Step and processed by later Workflow Steps.
_Avoid_: Workflow Step, workflow

**Issue ID**:
A stable validated identifier for one Issue within an IssueSet.
_Avoid_: List position, filename assumption

**IssueSet**:
The ordered collection of Issues and their dependency relationships produced for development by a Workflow Step.
_Avoid_: Unvalidated task list, Issue Board

**PRD Section ID**:
The stable standard identifier of a PRD section: `PROBLEM`, `OUTCOME`, `USERS`, `SCOPE`, `OUT_OF_SCOPE`, `FUNCTIONAL_REQUIREMENTS`, `NON_FUNCTIONAL_REQUIREMENTS`, `CONSTRAINTS`, `RISKS`, or `SUCCESS_MEASURES`.
_Avoid_: Localized heading text, line number

**Issue Section ID**:
The stable standard identifier of an Issue section: `OBJECTIVE`, `SCOPE`, `OUT_OF_SCOPE`, `REQUIREMENT_REFERENCES`, `ACCEPTANCE_CRITERIA`, `IMPLEMENTATION_CONSTRAINTS`, or `VERIFICATION`.
_Avoid_: Localized heading text, Markdown position

**Requirement ID**:
A stable PRD requirement identifier such as `FR-001` or `NFR-001` referenced by Issues.
_Avoid_: Requirement list position, heading slug

**Acceptance Criterion ID**:
A stable identifier such as `AC-001` for a verifiable condition within an Issue.
_Avoid_: QA Check ID, bullet position

**PRD Package**:
The project-owned `prd/<feature-slug>/` directory containing `<feature-slug>.md` and an `issues/` subdirectory whose accepted Issue files drive development.
_Avoid_: Run Directory, single detached PRD file

**Analysis Draft**:
The resumable PRD and IssueSet working data stored only in the Run Directory until analysis acceptance atomically publishes a PRD Package.
_Avoid_: Accepted PRD Package, temporary memory

**Feature Title**:
The human-readable name of the main feature described by a PRD Package.
_Avoid_: Directory name, generated identifier

**Feature Slug**:
The validated lowercase kebab-case filesystem identity derived from the Feature Title, editable before first persistence and stable thereafter.
_Avoid_: Raw title, mutable folder name

**Implementation Result**:
The versioned Artifact from a development attempt describing repository changes, commands run, verification evidence, and unresolved concerns for one Issue.
_Avoid_: Agent transcript, completed Issue

**Change Kind**:
The source-control classification of a changed file: `ADDED`, `MODIFIED`, `DELETED`, or `RENAMED`.
_Avoid_: Git porcelain code, free-form change description

**Implementation Status**:
The development coverage of one Issue acceptance criterion: `IMPLEMENTED`, `PARTIAL`, `NOT_IMPLEMENTED`, or `BLOCKED`.
_Avoid_: Issue Status, Step Outcome

**Resolution Status**:
The development disposition of one incoming rework item: `RESOLVED`, `UNRESOLVED`, or evidence-backed `NOT_APPLICABLE`.
_Avoid_: Finding Disposition, Review Result

**Review Result**:
The versioned Artifact from a code-review attempt containing its Step Outcome and structured findings for one Implementation Result.
_Avoid_: Review chat, QA Result

**Review Finding**:
An evidence-backed code-review observation with a stable ID, severity, disposition, repository location, rationale, and acceptance condition.
_Avoid_: Free-form comment, unsupported suspicion

**Finding Severity**:
The impact classification of a Review Finding: `CRITICAL`, `HIGH`, `MEDIUM`, or `LOW`.
_Avoid_: Required-action flag, arbitrary label

**Finding Disposition**:
The required action for a Review Finding: `MUST_FIX` or `ADVISORY`.
_Avoid_: Severity, review outcome

**QA Result**:
The versioned Artifact from a QA attempt containing its Step Outcome, executed checks, evidence, and residual risks for one Issue.
_Avoid_: Test log, Review Result

**QA Check**:
A structured verification mapped to an Issue acceptance criterion, with a stable ID, kind, requirement, status, execution details, evidence, and reason.
_Avoid_: Raw command output, unmapped test

**QA Check Kind**:
The verification method of a QA Check: `BUILD`, `TEST`, `LINT`, `TYPE_CHECK`, `SECURITY`, or `MANUAL_INSPECTION`.
_Avoid_: Command name, capability ID

**Check Requirement**:
Whether a QA Check is `REQUIRED` or `OPTIONAL` for Issue completion.
_Avoid_: Check status, severity

**Check Status**:
The lifecycle/result of a QA Check: `PENDING`, `RUNNING`, `PASSED`, `FAILED`, `BLOCKED`, `SKIPPED`, or `UNKNOWN`.
_Avoid_: Step Outcome, process exit code

**Rework Request**:
The versioned structured corrections emitted by code review or QA when `CHANGES_REQUESTED` routes an Issue back to development.
_Avoid_: Raw findings list, previous agent transcript

**Workflow Step Component**:
An installed package that provides a kind of Workflow Step and can be referenced by a Workflow Definition.
_Avoid_: Stage handler, workflow enum member

**Component Manifest**:
The versioned declaration through which an installed Workflow Step Component registers its identity, compatibility, Step Contract, view, capabilities, and Slash Commands.
_Avoid_: Import convention, undocumented module metadata

**Component Version Lock**:
The exact component version, distribution identity, and package hash resolved for a Workflow Run and required when that run resumes.
_Avoid_: Latest installed version, implicit upgrade

**Step Component ID**:
A stable, validated identifier that registers and resolves a Workflow Step Component. The set of IDs is open to installed extensions.
_Avoid_: Step enum

**Step Instance ID**:
The stable canonical lowercase UUIDv4 assigned once when a Workflow Step is created and retained through rename, movement, type changes, export, and Run Snapshotting.
_Avoid_: Step Component ID, display name, list position

**Step Display Name**:
The non-empty, user-editable name that identifies a Workflow Step in `/options`, transition selectors, and the dashboard; it is unique within its Workflow Definition.
_Avoid_: Step Instance ID, component type

**Step Contract**:
The declaration of a Workflow Step's required inputs, produced outputs, and allowed Step Outcomes.
_Avoid_: Step configuration, implicit convention

**Input Port**:
A named, typed requirement declared by a Step Contract and satisfied by a validated binding or Workflow Run input.
_Avoid_: Dictionary key, implicit dependency

**Output Port**:
A named, typed value or Artifact reference produced by a Workflow Step.
_Avoid_: Return dictionary, temporary result

**Port Binding**:
A validated connection from a Workflow Run input or upstream Output Port to a compatible downstream Input Port.
_Avoid_: Shared context lookup, copied schema

**Automatic Port Binding**:
A Port Binding Dev Loop may create when exactly one compatible upstream source satisfies an Input Port; zero or multiple candidates require explicit user resolution.
_Avoid_: Best guess, ambiguous connection

**Data Contract ID**:
A stable, versioned identifier for the shape and meaning of values carried through ports. The set is open to contracts registered by installed components.
_Avoid_: Port type enum, unversioned type name

**Capability Catalog**:
The discoverable collection of installed Skills and Agent References available for selection by Workflow Steps.
_Avoid_: Active prompt, selected capabilities

**Step Capability Profile**:
The focused set of Skills and Agent References selected for one Workflow Step, initially supplied by the component and optionally overridden by the user.
_Avoid_: Entire catalog, global prompt

**Step Guidance**:
The optional bounded user-authored instructions attached to one Workflow Step and supplied in every attempt's Context Manifest beneath the component contract and execution policy.
_Avoid_: Component instructions, Skill, permission override

**Step Execution Policy**:
The component-declared execution permissions and mutation constraints enforced for a Workflow Step attempt.
_Avoid_: Prompt-only instruction, user capability preference

**Codex Execution Settings**:
The authoritative Codex model, reasoning effort, and Fast service-tier preference selected independently for one agent-backed Workflow Step and used by every one of its attempts in a Workflow Run.
_Avoid_: Strength, role model, global Codex default

**Component Execution Defaults**:
The Codex Execution Settings initially supplied by a Workflow Step Component when a new Workflow Step of that component is added.
_Avoid_: Locked component settings, shared settings for every instance

**Execution Budget**:
The timeout and checkpoint limits governing a Workflow Step attempt independently of its Codex Execution Settings.
_Avoid_: Model profile, reasoning-effort preset

**Codex Model Catalog**:
The live account-aware catalog exposed by the installed Codex backend that defines selectable models, supported reasoning efforts, and Fast availability.
_Avoid_: Hard-coded model list, cached authorization

**Required Capability**:
A Skill or Agent Reference that a Workflow Step Component depends on and that cannot be removed from its Step Capability Profile.
_Avoid_: Default selection, hidden dependency

**Default Capability**:
A Skill or Agent Reference initially selected by a Workflow Step Component but replaceable or removable through `/options`.
_Avoid_: Required dependency, entire catalog

**User Workflow Default**:
The permanent user-wide editable Workflow Definition used as the starting template for future Workflow Runs across projects.
_Avoid_: Active Run Snapshot, installed component manifest

**User Configuration Directory**:
The platform-native per-user location for permanent Dev Loop preferences, separate from project files and run data.
_Avoid_: Project config, current working directory

**User Data Directory**:
The platform-native per-user location for installed declarative capabilities, installation receipts, and other durable non-configuration data.
_Avoid_: Run Directory, repository vendor folder

**Retention Policy**:
The user-wide run-cleanup mode: `KEEP_ALL` or `DELETE_TERMINAL_AFTER_DAYS`; nonterminal runs are never automatically deleted.
_Avoid_: Purge command, artifact expiration string

**Composer**:
The reusable terminal editor through which a user enters Workflow Step input and invokes contextual commands.
_Avoid_: Analysis-only editor, raw input prompt

**Language Mode**:
How a Workflow Run selects its content language: `AUTO` follows the initial user language and `EXPLICIT` uses a supplied language tag.
_Avoid_: UI locale, language-name string

**Content Language**:
The validated BCP 47 language tag governing user-facing agent output and project planning documents for a Workflow Run.
_Avoid_: Programming language, enum member

**Analysis Decision**:
A closed user response to draft planning output: `REQUEST_CHANGES` or `ACCEPT`.
_Avoid_: Agent completion, implicit approval

**Application Shell**:
The reusable terminal layout that surrounds every Step View with workflow identity, navigation, Issue state, output, the Composer, and current status.
_Avoid_: Step-specific layout, duplicated screen chrome

**Hybrid Console Dashboard**:
The non-full-screen presentation of the shared Workflow Progress Dashboard, combining one bounded live Current Issue and activity region separated only by width-aware horizontal rules. It renders every relevant Workflow Step instance with live or frozen elapsed time, reuses the same terminal region across Issues, and preserves the Composer, command surface, and durable file-based history.
_Avoid_: Vertical or corner borders, appended event spam, dashboard-only interface

**Step Progress**:
The GUID-keyed projection of one Workflow Step instance's display name, status, pass, accumulated duration, and active Codex settings for dashboard rendering.
_Avoid_: Component-type status, hard-coded phase row

**Workflow Progress Dashboard**:
The shared dynamic projection of workflow-scoped and issue-scoped Step Progress used by the Textual shell, Hybrid Console Dashboard, PowerShell, Bash, and redirected output.
_Avoid_: Fixed analysis/development/review/QA list, backend log

**Step View**:
The component-specific main presentation that receives a typed view model and emits typed user intents inside the Application Shell.
_Avoid_: Workflow runner, Textual domain model

**View Element**:
A reusable presentation component such as an Artifact Viewer, Issue Brief, Diff Viewer, Findings List, Check Matrix, Streaming Output, or Attempt Timeline.
_Avoid_: Workflow Step Component, duplicated widget group

**Workflow Status Bar**:
The fixed one-row active-step summary within the Workflow Progress Dashboard, containing Workflow Run status, active Workflow Step, current Issue progress, attempt, backend activity, and elapsed time.
_Avoid_: Component footer, free-form status message

**Status Bar Model**:
The typed presentation data supplied to the Workflow Status Bar, with optional backend-reported provider, model, and token usage fields.
_Avoid_: Preformatted string, domain entity

**Issue Board**:
The shared read-only projection of every Issue's dependency readiness, status, current Workflow Step, attempts, Artifacts, and blocker details.
_Avoid_: Current-Issue label, step-local task list

**Slash Command**:
A registered Composer action with a stable identifier and context-dependent availability.
_Avoid_: Hard-coded input branch, arbitrary slash text

**Command Scope**:
The availability boundary of a Slash Command: `GLOBAL`, `WORKFLOW`, or `STEP`.
_Avoid_: Visibility flag, command-name convention

**Artifact**:
A persisted, named output produced by a Workflow Step and referenced by later steps.
_Avoid_: Temporary result, shared mutable value

**Workspace Ref**:
The typed reference to the repository checkout or dedicated worktree selected for agent work in a Workflow Run.
_Avoid_: Raw path string, implicit current directory

**Handoff Summary**:
The persisted final Artifact describing completed Issues, verification evidence, changed files, residual risks, and the implementation workspace disposition.
_Avoid_: Console farewell, raw event log

**Run Event**:
An immutable record of a state change within a Workflow Run.
_Avoid_: Log message, current state

**Approval Request**:
A typed backend request for user authorization that identifies its Workflow Step, Issue when applicable, requested action, target, reason, and supported decisions.
_Avoid_: Confirmation string, implicit consent

**Approval Decision**:
A closed user response to an Approval Request: `APPROVE_ONCE`, `APPROVE_FOR_SESSION`, `DENY`, or `ABORT_RUN`; only decisions supported by the backend request are enabled.
_Avoid_: Yes/no string, automatic approval

**Stop Action**:
A closed user control choice: `CONTINUE`, `INTERRUPT_TURN`, `PAUSE_RUN`, or `CANCEL_RUN`.
_Avoid_: Ctrl+C side effect, generic abort

**Run Lease**:
A renewable ownership record used to distinguish a live Dev Loop process from an unexpectedly interrupted Workflow Run.
_Avoid_: Workflow lock, permanent process ID

**Run Snapshot**:
The current resumable state of a Workflow Run, including its immutable resolved Workflow Definition and per-step settings, derived from its recorded changes.
_Avoid_: Event history, display cache

**Durable Checkpoint**:
The recoverable Workflow Run cursor persisted through a flushed Run Event followed by an atomically replaced Run Snapshot.
_Avoid_: UI refresh, streamed token buffer

**Persisted Evidence**:
A bounded, redacted excerpt or Artifact reference intentionally retained to support a structured step result.
_Avoid_: Raw tool stream, complete terminal log

**Redaction Service**:
The shared boundary that masks detected secrets before user input, commands, evidence, or diagnostics enter persistent storage.
_Avoid_: Best-effort UI filter, credential store

**Operation Status**:
The lifecycle of an App Server tool operation: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, or `UNKNOWN`; shutdown leaves an unconfirmed running operation `UNKNOWN` rather than replaying it.
_Avoid_: Step Run Status, command output string

**Step Scope**:
The level at which a Workflow Step runs, declared by its selected component's Step Contract: `WORKFLOW` once for the whole Workflow, or `ISSUE` once for each selected Issue.
_Avoid_: Global flag, per-issue flag

**Step Outcome**:
The typed terminal result produced by a Workflow Step and used to select a Transition: `SUCCEEDED`, `CHANGES_REQUESTED`, `BLOCKED`, `FAILED`, or `CANCELLED`. Retry decisions and lifecycle states are not Step Outcomes.
_Avoid_: Status string, result string

**Transition**:
A rule that maps a Step Outcome to another Workflow Step or to the end of the workflow.
_Avoid_: Next step

**Primary Path**:
The ordered `/options` projection obtained by following `SUCCEEDED` Transitions through a Workflow Definition. Other Step Outcomes remain explicit branches of the underlying workflow graph.
_Avoid_: Entire workflow graph, execution history

**Primary Path Position**:
The editable one-based position of a Workflow Step on the Primary Path; moving a step renumbers the path without gaps and never changes its Step Instance ID.
_Avoid_: Step identity, component priority

**Retry Policy**:
The bounded rules that distinguish transient Execution Backend retries, requested-change cycles, and explicit user retries of blocked work.
_Avoid_: Unlimited loop, hard-coded retry count

**Workflow Run Status**:
The lifecycle state of the Workflow as a whole: `PENDING`, `RUNNING`, `WAITING_FOR_INPUT`, `PAUSED`, `SUCCEEDED`, `BLOCKED`, `FAILED`, or `CANCELLED`. `PAUSED` and `BLOCKED` are resumable; the other completed states are terminal.
_Avoid_: Step status, issue status

**Step Run Status**:
The lifecycle state of one Workflow Step execution: `PENDING`, `READY`, `RUNNING`, `WAITING_FOR_INPUT`, `PAUSED`, `COMPLETED`, or `SKIPPED`. A completed run stores its Step Outcome separately, and a retry creates another run attempt.
_Avoid_: Workflow status, Step Outcome

**Issue Status**:
The lifecycle state of one Issue as it is processed by Workflow Steps: `PENDING`, `READY`, `IN_PROGRESS`, `WAITING_FOR_INPUT`, `CHANGES_REQUESTED`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED`, or `SKIPPED`. The Issue's current Workflow Step is tracked separately rather than encoded in this status.
_Avoid_: Step status, workflow status

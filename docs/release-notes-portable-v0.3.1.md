# Portable Dev Loop v3 0.3.1

Portable Dev Loop v3 adds the machine-wide Sessions tab, isolated workflow
workers, saved-project discovery, canonical worktree leases, configurable
machine-wide concurrency, durable pre-PRD session state, History, Relink, and
transactional adoption of existing `0.2.1` portable projects.

The Windows and Linux installers now use a stable bootstrap with immutable
side-by-side releases and one atomic validated current pointer. An installed
updater never rewrites its running file or containing tree, the prior release
is retained, and failed validation or adoption leaves the previous pointer
runnable. Legacy checkout edits are preserved; displaced command entrypoints
are recorded for restore during verified uninstall.
The installed updater also provides foreground `-Rollback` / `--rollback`
selection of the verified previous release.

This release is for the portable `devloop-plan` and `devloop` wrappers. It does
not change the separately installed **CodexCLI 0.2.1** application, its App
Server workflows, package metadata, or `.devloop/runs/` state.

Live cross-platform release validation remains tracked by Issue 0013.

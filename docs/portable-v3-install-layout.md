# Portable v3 side-by-side install layout

Portable Dev Loop v3 uses one stable `InstallDir` bootstrap and immutable,
side-by-side release payloads:

```text
InstallDir/
  bin/                         stable user command launchers
  install/                     stable update and uninstall launchers
  bootstrap/
    current.json               atomic current-and-previous pointer state
    install-transaction.json   durable in-progress update journal
    candidate-<transaction>.json transaction-bound ownership evidence
    layout.json                stable asset hashes and legacy backups
    release-<commit>.json      immutable-release ownership evidence
    legacy-assets/             displaced legacy entrypoints, byte-for-byte
    dispatch.ps1/.sh           fail-closed command dispatch
    verify.py                  shared pointer and release verification
    transaction.py             POSIX transaction implementation
  releases/
    <40-hex-git-commit>/       clean Git payload plus isolated `.venv`
```

The public interface remains `InstallDir/bin/devloop*`,
`InstallDir/bin/devloop-plan*`, and `InstallDir/install/devloop*`. The stable
updater is generic: it validates `current.json`, then runs the update driver in
the current immutable release. It never replaces itself or renames its
containing tree. A later release may change its update driver without changing
the locked stable launcher.

The bootstrap command interface is protocol version 2. Every mutating command
selects that protocol explicitly and carries the UUID returned by `begin`.
`begin` atomically acquires an install-wide lock outside `InstallDir` and records
the owning process identity. A live owner blocks another updater; only a
confirmed-dead owner permits stale-lock recovery. The release driver may clone,
validate the runtime, and invoke adoption, but it does not encode or write the
layout, release manifest, pointer, or transaction journal.

In path-contract shorthand, `bootstrap/current.json` is one schema-versioned
record containing the current pointer and an optional previous pointer. A
rollback exchanges those nested pointers with one durable write-and-rename.
Each nested pointer names exactly one canonical `releases/<commit>` payload.

## Activation invariant

The installer clones outside `InstallDir`, resolves the exact Git commit,
builds the isolated runtime, validates release metadata, pinned Textual, and
standard-library SQLite, and fingerprints both the Git index and runtime. It
writes the release manifest and independent candidate-ownership evidence before
moving the candidate into its canonical release directory. Recovery accepts a
validated candidate, canonical release, or both at that exact crash boundary. Adoption
must commit before activation. Activation is one durable write-and-rename of
`current.json`; the previous release directory remains runnable and recorded.
The installed updater's `-Rollback` (PowerShell) or `--rollback` (Bash) option
validates both recorded releases and atomically exchanges the current and
previous pointers in that one record; it does not rebuild or delete either payload.

Every dispatch verifies the exact pointer and manifest schemas, canonical relative release path,
matching release manifest, Git commit and cleanliness, tracked fingerprint,
runtime fingerprint, declared bootstrap-protocol and pointer/manifest/transaction
schema ranges, and absence of reparse points or symbolic links. Invalid,
torn, or tampered metadata fails closed and is never used as a deletion target.

## Legacy migration and uninstall

An existing `0.2.1` or direct-checkout `0.3.1` tree is not moved, cleaned,
reset, or deleted. Tracked edits, untracked files, configuration, and project
data remain in place. Only command paths that become stable bootstrap assets
are displaced; their exact bytes are retained under
`bootstrap/legacy-assets/<original-path>` and recorded in `layout.json`.

Uninstall first parses and validates the exact layout allowlist, all paths,
hashes, backups, pointer state, manifests, and every owned release without
mutating anything. It then copies its capability cleanup plan outside the
release tree, commits core release/bootstrap removal, restores displaced legacy
entrypoints when the stable copy is unchanged, and performs unchanged-capability
cleanup as a nonfatal post-commit action. Modified bootstrap or capability files
are retained. Catalog and configuration state outside `InstallDir`, and
all source checkouts, PRDs, issues, logs, branches, and worktrees are preserved.

First bootstrap publication uses a durable pending layout containing the full
asset allowlist, desired hashes, and original backup hashes before replacing the
first command asset. Each file is staged and atomically replaced where the
platform allows it; retry resumes from that record without treating an
already-published stable asset as legacy content.

If `layout.json` is already committed, startup validates any surviving
`layout.pending.json` exactly. A completed pending publication that describes
the same assets and backups is removed; every mismatch is retained and fails
closed.

Uninstall writes a UUID-scoped journal outside the removal set after its full
read-only preflight. The journal binds the exact layout hash and ordered action
plan, and checkpoints before and after capability staging, every release,
manifest, pointer, stable asset, legacy restoration, and capability cleanup.
Retry resumes that plan even after a pointed-to release has been removed. The
copied verifier and transaction entrypoint remain external until commit.
Capability cleanup is post-core and nonfatal; failure retains the journal so it
can resume.

No detached helper, background updater, third-party database, IPC library, or
process manager is part of this layout.

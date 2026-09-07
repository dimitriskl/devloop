# Portable test lanes

The default pytest selection is `not integration and not operator_install`.
The equivalent explicit agent command is:

```powershell
.venv\Scripts\python.exe -m pytest -q -m "not integration and not operator_install"
```

Selecting installation tests without `--run-operator-install` fails during
collection, before test setup. Consequently the old `-m "not integration"`
command fails closed instead of starting unmarked installer tests. The operator
TestCase also rejects direct unittest execution without the pytest opt-in.
Deselected installation tests remain required external evidence, not a passed
release gate. Real Codex `integration` tests remain operator-only.

Each pytest session reserves a new `.tmp-test-session-*` workspace directory.
Its marker and session directories are retained for inspection. Temporary files
and pytest paths stay under fresh workspace roots; ordinary fixtures may clean
their own temporary content. Installer and source-safety fixture leaves are
retained. A caller may supply `--basetemp` only when the
absolute target is a nonexistent path below the workspace with no symlink or
reparse-point ancestor. Existing and outside-workspace targets fail before
pytest can recursively clear them. Do not reuse a prior `--basetemp` path.

Candidate and release-content source safety suites also use fresh validated
session leaves, retain their fixtures, and block `transaction.shutil.rmtree`
and `transaction.os._exit` before invoking production functions. Their audited
removal-seam assertions use scoped interceptors; real production recursive
removal is never part of these suites. Release-content tests initialize only
fresh private Git metadata, then check the disposable repository's root, common
directory and index before allowlisted commands. Direct unittest runs of these
suites require a configured session.

## Installer fixture matrices: operator terminal only

After independent review and QA of the harness, freeze the reviewed manifest at
`.tmp-safe-harness-rework-20260907/reviewed-manifest.json`. The operator can then
run the Windows fixture matrix in a separate terminal using this one physical command:

```powershell
.venv\Scripts\python.exe tests\run_portable_operator_gate.py --matrix windows-fixtures --acknowledge-operator-install --reviewed-manifest .tmp-safe-harness-rework-20260907\reviewed-manifest.json
```

On a native Linux checkout with its reviewed Python test environment, the Unix
bundle fixture command is:

```bash
.venv/bin/python tests/run_portable_operator_gate.py --matrix linux-bundle-fixtures --acknowledge-operator-install --reviewed-manifest .tmp-safe-harness-rework-20260907/reviewed-manifest.json
```

The launcher prints its fresh workspace evidence directory, containing
`operator.log`, a copy of the supplied reviewed manifest, `junit.xml` when pytest
starts, and `result.json`. It records complete hashes before and after pytest,
prerequisites and the exact test command. The explicit matrix contains all 33
Windows tests (6 bundle and 27 side-by-side) or all 8 Linux bundle tests.
Missing, deselected, unexpected, duplicate, skipped, failed or errored cases and
nonzero pytest exits prevent a pass. Source, config, inventory or historical
data changes also prevent a pass. Its environment changes
apply only to its pytest child; it never clears the operator terminal's Git
variables. It suppresses Git user/system config, templates, hooks and signing,
redirects profile/config/cache/temp paths to validated disposable leaves, and disables
interactive Git authentication. No background or detached process is used.
This includes `XDG_STATE_HOME`, which the actual Linux catalog resolver selects
before its HOME fallback, and `LOCALAPPDATA`, which the Windows resolver selects.
Inherited `CODEX_SKILLS_PATH` and `CODEX_AGENTS_PATH` are removed; installer and
uninstaller defaults use the isolated HOME or USERPROFILE.

Both matrices use `DEVLOOP_TESTING=1` and a Textual version stub. They verify
fixture installer behavior, not a real dependency installation. Windows Git
Bash cases do not establish native Linux side-by-side behavior. Real pinned
runtime installation, native Linux side-by-side update/rollback, authenticated
behavior and the integrated multi-session demonstration remain separate gates.

## Source and historical fixture identity

The reviewed manifest is external review evidence; the launcher never creates
or refreshes it to authorize changed source. Its exact fields are `format: 1`,
`inventory_sha256` (relative paths to lowercase SHA-256 values), and `matrices`
(the explicit node ID lists returned by `matrix_contract`). Its inventory uses
the same `read_current_source` routine as installer fixtures and includes all
tracked and non-ignored current files under `src`, `bin`, `install`, and `tests`,
plus `.gitignore`, `portable-release.json`, `requirements-portable.lock`,
`pyproject.toml`, `AGENTS.md`, and this document. This covers the harness, all
historical JSON and relevant source/config bytes, including uncommitted repairs.
Generate it only from the final independently reviewed source freeze; a changed
tree requires renewed review and a new manifest. HEAD alone is not the identity.
The launcher explicitly selects `pyproject.toml` as pytest configuration.

Installer source repositories enumerate tracked and non-ignored untracked files
under the required source paths, then copy the current bytes, including dirty
repairs. They reject linked inputs and do not copy caches or `.git` wholesale.
Each fixture keeps a provenance JSON beside its disposable source repository
with per-file SHA-256 values, the initial fixture commit, and the runtime stub
identity. Later `previous`, `candidate` and `third` tags deliberately add the
test's release marker and driver/bootstrap mutation; the retained Git repository
records these exact candidate changes.

The historical case validates the complete frozen 18-file JSON inventory,
UTF-8/LF encoding, lengths, SHA-256 and Git blob hashes before materialization.
Only those 18 files are historical. All other modules, wrappers, dependency
locks and the Textual stub are labeled current-checkout test support. The full
historical commit is still unavailable. Agent integrity tests consume only
inert JSON; materialization and execution require the operator lane.

The compatibility assertions remain: install the v1 bootstrap, update to v2,
rollback to the old release, then update to the **same retained candidate**.
The root-uninstall negative case executes only AST-extracted argument checks
with an allowlist of non-mutating path methods. A real filesystem root is never
passed to the full uninstaller body.

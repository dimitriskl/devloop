from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash")
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
SPEC = importlib.util.spec_from_file_location(
    "distribution_scope_verify", ROOT / "install/bootstrap/verify.py"
)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)


def bash_function(name: str) -> str:
    lines = (ROOT / "install/devloop.sh").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{name}() {{")
    end = lines.index("}", start)
    return "\n".join(lines[start:end + 1])


def powershell_function(name: str) -> str:
    source = (ROOT / "install/devloop.ps1").read_text(encoding="utf-8")
    start = source.index(f"function {name} {{")
    end = source.index("\n}", start) + 2
    return source[start:end]


class DistributionGitScopeTests(unittest.TestCase):
    def shell(self, script: str, *, powershell: bool = False) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
            and key.upper() not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "CDPATH"}
            and not key.startswith("BASH_FUNC_")
        }
        if powershell:
            if POWERSHELL is None:
                self.skipTest("PowerShell is required for the PowerShell source-only probes")
            # No installer entrypoint: only named source functions and in-memory seams.
            import base64

            command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                       base64.b64encode(script.encode("utf-16-le")).decode("ascii")]
            return subprocess.run(
                command, text=True, capture_output=True, check=False,
                cwd=ROOT, env=environment, timeout=15,
            )
        if BASH is None:
            self.skipTest("Bash is required for the Bash source-only probes")
        return subprocess.run(
            [BASH, "--noprofile", "--norc", "-s"], input=script,
            text=True, capture_output=True, check=False, cwd=ROOT, env=environment, timeout=15,
        )

    def test_verifier_removes_all_inherited_repository_selectors(self) -> None:
        poisoned = dict.fromkeys(verify.GIT_LOCAL_ENVIRONMENT, "/scope-probe-does-not-exist")
        poisoned.update({
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": "/scope-probe-does-not-exist",
            "GIT_CONFIG_PARAMETERS": "'core.bare=true'",
            "GIT_CONFIG_GLOBAL": "/scope-probe-does-not-exist",
            "GIT_CONFIG_SYSTEM": "/scope-probe-does-not-exist",
        })
        with mock.patch.dict(os.environ, poisoned):
            # Real project Git read-only path: no clone, checkout, index write or removal.
            output = verify._git(ROOT, "rev-parse", "--show-toplevel")
            self.assertEqual(Path(output.strip()).resolve(), ROOT)
            files = verify._git(ROOT, "ls-files", "--stage")
            self.assertIn("install/bootstrap/verify.py", files)
            self.assertEqual(os.environ["GIT_INDEX_FILE"], poisoned["GIT_INDEX_FILE"])

    def test_verifier_rejects_each_redirected_effective_path(self) -> None:
        expected = [ROOT, ROOT / ".git", ROOT / ".git", ROOT / ".git/index"]
        for index in range(4):
            paths = [str(path) for path in expected]
            paths[index] = str(ROOT.parent / "not-owned")
            with self.subTest(index=index), mock.patch.object(
                verify, "_run_git", return_value="\n".join(paths)
            ), self.assertRaisesRegex(RuntimeError, "Git scope escapes"):
                verify._git(ROOT, "ls-files", "--stage")

    def test_verifier_rejects_incomplete_scope_before_validation(self) -> None:
        with mock.patch.object(verify, "_run_git", return_value=str(ROOT)) as execute:
            with self.assertRaisesRegex(RuntimeError, "all repository paths"):
                verify._git(ROOT, "ls-files", "--stage")
            self.assertEqual(execute.call_count, 1)

    def test_bash_git_scope_sanitizes_child_and_preserves_caller(self) -> None:
        script = r"""
set -euo pipefail
PATH=''; readonly PATH
export GIT_DIR=/bad GIT_WORK_TREE=/bad GIT_INDEX_FILE=/bad GIT_COMMON_DIR=/bad
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.worktree GIT_CONFIG_VALUE_0=/bad
export GIT_CONFIG_PARAMETERS=bad GIT_SSH_COMMAND=preserved
git() {
  local variable
  for variable in GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR ${!GIT_CONFIG@}; do
    [[ ! -v "$variable" ]] || return 97
  done
  [[ "$GIT_SSH_COMMAND" == preserved ]] || return 98
  printf 'isolated:%s\n' "$*"
  return 23
}
""" + bash_function("with_git_environment") + "\n" + bash_function("scoped_git") + r"""
status=0
scoped_git rev-parse HEAD || status=$?
[[ "$status" == 23 && "$GIT_DIR" == /bad && "$GIT_CONFIG_COUNT" == 1 ]]
"""
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("isolated:-c core.hooksPath=/dev/null rev-parse HEAD", result.stdout)

    def test_powershell_git_scope_sanitizes_child_and_preserves_caller(self) -> None:
        script = r"""
$ErrorActionPreference = 'Stop'
$env:GIT_DIR='/bad'; $env:GIT_WORK_TREE='/bad'; $env:GIT_INDEX_FILE='/bad'
$env:GIT_CONFIG_COUNT='1'; $env:GIT_CONFIG_KEY_0='core.worktree'
$env:GIT_CONFIG_VALUE_0='/bad'; $env:GIT_SSH_COMMAND='preserved'
function git {
    $names = @('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CONFIG_COUNT',
        'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0')
    foreach ($name in $names) {
        if ([Environment]::GetEnvironmentVariable($name)) { throw 'selector leaked' }
    }
    if ($env:GIT_SSH_COMMAND -ne 'preserved') { throw 'transport was changed' }
    Write-Output "isolated:$args"
    $global:LASTEXITCODE=23
}
""" + powershell_function("Invoke-GitEnvironment") + "\n" + powershell_function(
            "Invoke-ScopedGit"
        ) + r"""
Invoke-ScopedGit -GitArguments @('rev-parse', 'HEAD')
if ($LASTEXITCODE -ne 23 -or $env:GIT_DIR -ne '/bad' -or $env:GIT_CONFIG_COUNT -ne '1') {
    throw 'caller state or exit status lost'
}
"""
        result = self.shell(script, powershell=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("isolated:-c core.hooksPath=/dev/null rev-parse HEAD", result.stdout)

    def test_bash_standalone_and_streamed_stage_before_dispatch(self) -> None:
        for source_dir in ("", "/probe/download"):
            with self.subTest(source_dir=source_dir):
                script = r"""
set -euo pipefail
PATH=''; readonly PATH
REPO_URL=fixture REF=main
log() { printf 'event:%s\n' "$*" >&2; }
mktemp() { printf '/probe/staging\n'; }
cd() { [[ "$1" == /probe/staging ]]; }
pwd() { printf '/probe/staging\n'; }
copy_release_source() { log "clone:$1"; }
with_git_environment() { "$@"; }
find_python() { printf probe_python; }
probe_python() {
  [[ "$1" == -B && "$2" == /probe/staging/install/bootstrap/verify.py &&
     "$3" == /probe/staging && "$4" == --bootstrap-source ]] || return 97
  log verified
}
[() {
  case "$1" in -f) return 1 ;; -e) return 0 ;; esac
  builtin [ "$@"
}
""" + f"\nSCRIPT_DIR='{source_dir}'\n" + bash_function("source_transaction") + r"""
entry="$(source_transaction)"
printf 'dispatch:%s\n' "$entry"
"""
                result = self.shell(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("event:clone:/probe/staging", result.stderr)
                self.assertIn("event:verified", result.stderr)
                self.assertEqual(
                    result.stdout.strip(),
                    "dispatch:/probe/staging/install/bootstrap/transaction.py",
                )

    def test_powershell_download_and_stream_stage_before_dispatch(self) -> None:
        # Function-only execution has no PSScriptRoot, as with the documented iex path.
        script = r"""
$ErrorActionPreference='Stop'; $RepoUrl='fixture'; $Ref='main'
function Test-Path { return $false }
function New-Item { param($ItemType, $Path); Write-Host 'event:mkdir' }
function Assert-PlainPath { param($Path); Write-Host 'event:plain-ancestor' }
function Invoke-GitEnvironment { param([scriptblock]$Action); & $Action }
function Write-InstallLog { param($Message); Write-Host $Message }
function Copy-ReleaseSource {
    param($Destination); $script:Staged=$Destination; Write-Host 'event:clone'
}
function Get-DevLoopPython { return 'probe_python' }
function probe_python {
    if ($args[0] -ne '-B' -or $args[-1] -ne '--bootstrap-source' -or
        $args[2] -ne $script:Staged) { throw 'unexpected source verifier invocation' }
    Write-Host 'event:verified'; $global:LASTEXITCODE=0
}
""" + powershell_function("Get-SourceTransaction") + r"""
foreach ($sourceRoot in @('', 'C:\probe\download')) {
    $PSScriptRoot=$sourceRoot
    $entry=Get-SourceTransaction
    if ($entry -ne (Join-Path $script:Staged 'install\bootstrap\transaction.py')) {
        throw 'wrong dispatch'
    }
    Write-Host 'event:dispatch'
}
"""
        result = self.shell(script, powershell=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(result.stdout.index("event:clone"), result.stdout.index("event:verified"))
        self.assertLess(
            result.stdout.index("event:verified"), result.stdout.index("event:dispatch")
        )

    def test_both_clone_drivers_check_scope_before_worktree_mutation(self) -> None:
        bash = r"""
set -euo pipefail
PATH=''; readonly PATH
REPO_URL=fixture REF=main
scoped_git() { printf 'git:%s\n' "$*"; }
assert_git_scope() { printf 'scope:%s\n' "$1"; }
""" + bash_function("copy_release_source") + "\ncopy_release_source /probe/candidate\n"
        ps = r"""
$ErrorActionPreference='Stop'; $RepoUrl='fixture'; $Ref='main'
function Invoke-ScopedGit {
    param([string[]]$GitArguments)
    Write-Output "git:$GitArguments"; $global:LASTEXITCODE=0
}
function Assert-GitScope { param($Root); Write-Output "scope:$Root" }
""" + powershell_function("Copy-ReleaseSource") + "\nCopy-ReleaseSource /probe/candidate\n"
        for script, powershell in ((bash, False), (ps, True)):
            with self.subTest(powershell=powershell):
                result = self.shell(script, powershell=powershell)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.strip().splitlines()
                self.assertIn("clone --no-checkout --no-local", lines[0])
                self.assertTrue(lines[1].startswith("scope:"), lines)
                self.assertIn("fetch --depth 1", lines[2])
                self.assertTrue(lines[3].startswith("scope:"), lines)
                self.assertIn("checkout --detach --force FETCH_HEAD", lines[4])
                self.assertTrue(lines[5].startswith("scope:"), lines)

    def test_both_clone_drivers_stop_after_scope_refusal(self) -> None:
        bash = r"""
set -euo pipefail
PATH=''; readonly PATH
REPO_URL=fixture REF=main
scoped_git() { printf 'git:%s\n' "$*"; }
assert_git_scope() { return 37; }
""" + bash_function("copy_release_source") + "\ncopy_release_source /probe/candidate\n"
        ps = r"""
$ErrorActionPreference='Stop'; $RepoUrl='fixture'; $Ref='main'
function Invoke-ScopedGit {
    param([string[]]$GitArguments)
    Write-Output "git:$GitArguments"; $global:LASTEXITCODE=0
}
function Assert-GitScope { throw 'scope refusal' }
""" + powershell_function("Copy-ReleaseSource") + "\nCopy-ReleaseSource /probe/candidate\n"
        for script, powershell in ((bash, False), (ps, True)):
            with self.subTest(powershell=powershell):
                result = self.shell(script, powershell=powershell)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("clone --no-checkout --no-local", result.stdout)
                self.assertNotIn("fetch", result.stdout)
                self.assertNotIn("checkout --detach", result.stdout)

    def test_bash_bootstrap_validation_failure_never_dispatches(self) -> None:
        script = r"""
set -euo pipefail
PATH=''; readonly PATH
REPO_URL=fixture REF=main SCRIPT_DIR=''
log() { :; }
mktemp() { printf '/probe/staging\n'; }
cd() { [[ "$1" == /probe/staging ]]; }
pwd() { printf '/probe/staging\n'; }
copy_release_source() { :; }
with_git_environment() { "$@"; }
find_python() { printf probe_python; }
probe_python() { return 41; }
[() { if [[ "$1" == -e ]]; then return 0; fi; builtin [ "$@"; }
""" + bash_function("source_transaction") + r"""
entry="$(source_transaction)"
printf 'dispatch:%s\n' "$entry"
"""
        result = self.shell(script)
        self.assertEqual(result.returncode, 41, result.stderr)
        self.assertNotIn("dispatch:", result.stdout)

    def test_powershell_bootstrap_validation_failure_never_dispatches(self) -> None:
        script = r"""
$ErrorActionPreference='Stop'; $RepoUrl='fixture'; $Ref='main'
function Test-Path { return $false }
function Assert-PlainPath { param($Path) }
function Invoke-GitEnvironment { param([scriptblock]$Action); & $Action }
function New-Item { param($ItemType, $Path) }
function Write-InstallLog { param($Message) }
function Copy-ReleaseSource { param($Destination) }
function Get-DevLoopPython { return 'probe_python' }
function probe_python { $global:LASTEXITCODE=41 }
""" + powershell_function("Get-SourceTransaction") + r"""
$entry=Get-SourceTransaction
Write-Output "dispatch:$entry"
"""
        result = self.shell(script, powershell=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("bootstrap source validation failed", result.stderr)
        self.assertNotIn("dispatch:", result.stdout)

    def test_streamed_bash_does_not_require_a_script_filename(self) -> None:
        source = (ROOT / "install/devloop.sh").read_text(encoding="utf-8")
        initialization = source[:source.index('INSTALL_DIR="')]
        result = self.shell(initialization + "\nprintf 'source:%s\\n' \"$SCRIPT_DIR\"\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "source:")

    def test_invalid_repository_or_ref_refuses_before_clone(self) -> None:
        for repository, ref in (("", "main"), ("fixture", ""), ("--upload-pack=bad", "main"),
                                ("fixture", "--all")):
            bash = r"""
set -euo pipefail
PATH=''; readonly PATH
scoped_git() { printf 'unexpected-git\n'; }
die() { printf 'invalid selector\n' >&2; exit 31; }
""" + f"\nREPO_URL='{repository}' REF='{ref}'\n" + bash_function("copy_release_source")
            bash += "\ncopy_release_source /probe/candidate\n"
            ps = r"""
$ErrorActionPreference='Stop'
function Invoke-ScopedGit { throw 'unexpected-git' }
""" + f"\n$RepoUrl='{repository}'; $Ref='{ref}'\n"
            ps += powershell_function("Copy-ReleaseSource")
            ps += "\nCopy-ReleaseSource /probe/candidate\n"
            for script, powershell in ((bash, False), (ps, True)):
                with self.subTest(repository=repository, ref=ref, powershell=powershell):
                    result = self.shell(script, powershell=powershell)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("unexpected-git", result.stdout + result.stderr)

    def test_bash_effective_scope_rejects_every_external_path(self) -> None:
        for altered in range(-1, 4):
            script = r"""
set -euo pipefail
PATH=''; readonly PATH
die() { printf 'scope refusal\n' >&2; exit 37; }
[() { if [[ "$1" == -d ]]; then return 0; fi; builtin [ "$@"; }
scoped_git() {
  local paths=(/probe/root /probe/root/.git /probe/root/.git /probe/root/.git/index)
  if [[ "$ALTERED" != -1 ]]; then paths[$ALTERED]=/probe/external; fi
  printf '%s\n' "${paths[@]}"
}
""" + f"\nALTERED={altered}\n" + bash_function("assert_git_scope")
            script += "\nassert_git_scope /probe/root\n"
            with self.subTest(altered=altered):
                result = self.shell(script)
                self.assertEqual(result.returncode, 0 if altered == -1 else 37, result.stderr)

    def test_powershell_effective_scope_rejects_every_external_path(self) -> None:
        script = r"""
$ErrorActionPreference='Stop'
function Assert-PlainPath { param($Path, [switch]$AllowMissing) }
function Test-Path { return $true }
function Invoke-ScopedGit {
    $paths=@('C:\probe\root','C:\probe\root\.git',
        'C:\probe\root\.git','C:\probe\root\.git\index')
    if ($script:Altered -ge 0) { $paths[$script:Altered]='C:\probe\external' }
    $global:LASTEXITCODE=0
    return $paths
}
""" + powershell_function("Assert-GitScope") + r"""
foreach ($script:Altered in -1..3) {
    $refused=$false
    try { Assert-GitScope 'C:\probe\root' }
    catch { $refused=$true }
    if ($refused -ne ($script:Altered -ge 0)) { throw 'wrong scope result' }
}
"""
        result = self.shell(script, powershell=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_older_powershell_bootstrap_receives_isolated_environment_and_exact_entry(self) -> None:
        script = r"""
$ErrorActionPreference='Stop'; $BootstrapProtocol=2
$env:GIT_WORK_TREE='/bad'
function Get-DevLoopPython { return 'probe_python' }
function probe_python {
    if ($env:GIT_WORK_TREE) { throw 'bootstrap selector leaked' }
    if ($args[0] -ne '-B' -or $args[1] -ne 'C:\probe\transaction.py' -or
        $args[2] -ne 'recover' -or $args[3] -ne 'C:\probe\install' -or
        $args[4] -ne 'transaction-id' -or $args[5] -ne '--protocol' -or $args[6] -ne 2) {
        throw 'bootstrap arguments changed'
    }
    $global:LASTEXITCODE=29
}
""" + powershell_function("Invoke-GitEnvironment") + "\n" + powershell_function(
            "Invoke-Bootstrap"
        ) + r"""
Invoke-Bootstrap 'C:\probe\transaction.py' recover 'C:\probe\install' transaction-id
if ($LASTEXITCODE -ne 29 -or $env:GIT_WORK_TREE -ne '/bad') {
    throw 'bootstrap exit status or caller environment changed'
}
"""
        result = self.shell(script, powershell=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bash_readonly_selector_refuses_before_child_invocation(self) -> None:
        script = r"""
set -euo pipefail
PATH=''; readonly PATH
readonly GIT_WORK_TREE=/bad
probe_python() { printf 'unexpected-child\n'; }
""" + bash_function("with_git_environment") + r"""
status=0
with_git_environment probe_python || status=$?
[[ "$status" != 0 ]]
"""
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unexpected-child", result.stdout)

    def test_bash_public_retry_parses_options_before_any_resume(self) -> None:
        source = (ROOT / "install/bootstrap/install/uninstall-devloop.sh").read_text()
        definitions = source.split('parse_retry_options "$@"', 1)[0]
        # The extracted prefix only defines option handling; no path lookup,
        # interpreter discovery, recovery probe or entrypoint is executed.
        branch = source.split('if [ -n "$RECOVERY_ROOT" ]; then', 1)[1].split("\nfi", 1)[0]
        for root in ("/virtual/bundle/install", "/virtual/.bundle.uninstall-id"):
            for options, expected_status, expected in (
                ("--help", 0, None), ("--unknown", 1, None), ("--dir", 1, None),
                ("", 0, "resume-uninstall"),
                ("--keep-skills", 0, "--keep-capabilities"),
                ("--dir /virtual/bundle", 0, "--install-root|/virtual/bundle"),
                ("--bin-dir /virtual/bin", 0, "--bin-directory|/virtual/bin"),
            ):
                with self.subTest(root=root, options=options):
                    script = "PATH=''; readonly PATH\nunset CODEX_SKILLS_PATH CODEX_AGENTS_PATH\n"
                    script += definitions + f"\nRECOVERY_ROOT='{root}'\n"
                    script += "PYTHON=capture_python\n"
                    script += 'capture_python() { printf "RESUME:%s\\n" "$*"; }\n'
                    script += 'exec() { IFS="|"; "$@"; }\n'
                    script += f"parse_retry_options {options}\n" + branch
                    result = self.shell(script)
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    if expected is None:
                        self.assertNotIn("RESUME:", result.stdout)
                    else:
                        self.assertIn(expected, result.stdout)
                        self.assertIn(root, result.stdout)
        script = "PATH=''; readonly PATH\n" + definitions + "\n"
        script += "CODEX_SKILLS_PATH=/virtual/skills\nCODEX_AGENTS_PATH=/virtual/agents\n"
        script += 'parse_retry_options\nprintf "%s\\n" "${RETRY_ARGS[@]}"\n'
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "--skills-destination", "/virtual/skills", "--agents-destination", "/virtual/agents",
        ])

    def test_powershell_public_retry_parses_options_before_any_resume(self) -> None:
        source = (ROOT / "install/bootstrap/install/uninstall-devloop.ps1").read_text()
        option_prefix = source.split("$python = $null", 1)[0]
        header = "if (-not [string]::IsNullOrWhiteSpace($recoveryRoot)) {"
        branch = header + source.split(header, 1)[1].split("\n}", 1)[0] + "\n}"
        for root in (r"E:\virtual\bundle\install", r"E:\virtual\.bundle.uninstall-id"):
            for options, succeeds, expected in (
                ("-Help", True, None), ("-Unknown", False, None),
                ("-CodexSkillsPath", False, None), ("", True, "resume-uninstall"),
                ("-KeepSkills", True, "--keep-capabilities"),
                ("-InstallDir E:\\virtual\\bundle", True, "--install-root|E:\\virtual\\bundle"),
                ("-CodexSkillsPath E:\\virtual\\skills", True,
                 "--skills-destination|E:\\virtual\\skills"),
                ("-CodexAgentsPath E:\\virtual\\agents", True,
                 "--agents-destination|E:\\virtual\\agents"),
                ("-BinDir E:\\virtual\\bin", True, "--bin-directory|E:\\virtual\\bin"),
            ):
                with self.subTest(root=root, options=options):
                    script = "$ErrorActionPreference='Stop'\n"
                    script += "function capture_python { Write-Output ('RESUME:' + "
                    script += "($args -join '|')); $global:LASTEXITCODE=0 }\n"
                    script += "function Invoke-PublicOptions {\n" + option_prefix
                    script += f"\n$python='capture_python'; $recoveryRoot='{root}'\n"
                    script += branch + "\n}\n" + f"Invoke-PublicOptions {options}\n"
                    result = self.shell(script, powershell=True)
                    self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                    if expected is None:
                        self.assertNotIn("RESUME:", result.stdout)
                    else:
                        self.assertIn(expected, result.stdout)
                        self.assertIn(root, result.stdout)


if __name__ == "__main__":
    unittest.main()

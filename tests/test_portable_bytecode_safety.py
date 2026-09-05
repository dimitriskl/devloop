from __future__ import annotations

import ast
import base64
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests import test_portable_release_content_safety as content_safety

verify = sys.modules["verify"]  # Loaded by the shared, guarded bootstrap fixture.
ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh")
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.is_file() else shutil.which("bash")


def _function(source: str, first_line: str) -> str:
    lines = source.splitlines()
    start = lines.index(first_line)
    end = lines.index("}", start)
    return "\n".join(lines[start : end + 1])


class ReleaseBytecodeSafetyTests(content_safety.ReleaseContentSafetyTests):
    """Real tracked fixtures, but never installer, uninstall, or runner execution."""

    def git(self, *arguments: str) -> str:
        if arguments == ("init", "--quiet"):
            # Both runner module invocations first load this actual package and
            # version module. Keep their source under the fixture's tracked install.
            package = self.candidate / "install" / "bootstrap" / "devloop"
            package.mkdir()
            for name in ("__init__.py", "version.py"):
                (package / name).write_bytes((ROOT / "src" / "devloop" / name).read_bytes())
        return super().git(*arguments)

    def shell_arguments(self, language: str, source: str) -> list[str]:
        environment = {
            key: value for key, value in os.environ.items()
            if key.upper() not in {
                "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "CDPATH",
                "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX",
            }
            and not key.startswith("BASH_FUNC_")
        }
        if language == "powershell":
            if POWERSHELL is None:
                self.skipTest("PowerShell call coverage requires pwsh; Bash/Python run separately")
            command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand"]
            script = r"""
$ErrorActionPreference = 'Stop'
$env:PATH = ''
$BootstrapProtocol = 2
$probeRoot = Join-Path ([IO.Path]::GetTempPath()) '__devloop_probe_no_filesystem__'
$script:CandidateDir = Join-Path $probeRoot 'candidate'
$InstallDir = Join-Path $probeRoot 'install'
$InstallRoot = $InstallDir
$verifier = Join-Path $probeRoot 'verify.py'
$transaction = Join-Path $probeRoot 'transaction.py'
$transactionId = 'probe-transaction'
$script:TransactionId = 'probe-transaction'
$python = 'probe_python'
$RemainingArgs = @('--plain', '--help')
function Get-DevLoopPython { 'probe_python' }
function probe_python {
    @($args) | ConvertTo-Json -Compress
    $global:LASTEXITCODE = 0
}
""" + source
        else:
            if BASH is None:
                self.skipTest("Bash call coverage requires Bash; Python worker runs separately")
            command = [BASH, "--noprofile", "--norc", "-s"]
            script = r"""
set -euo pipefail
PATH=''
readonly PATH
python=probe_python
PYTHON=probe_python
PYTHON_BIN=probe_python
CANDIDATE_DIR=/__devloop_probe_no_filesystem__/candidate
INSTALL_DIR=/__devloop_probe_no_filesystem__/install
INSTALL_ROOT=$INSTALL_DIR
TRANSACTION_ID=probe-transaction
operation=install
entry=/__devloop_probe_no_filesystem__/transaction.py
bootstrap_entry=$entry
transaction_candidate=$CANDIDATE_DIR
CANDIDATE_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
probe_python() { printf '%s\0' "$@"; }
find_python() { printf '%s\n' probe_python; }
exec() { "$@"; }
""" + source
        # Only the extracted calls/functions above run. Their sole process seam
        # is probe_python; no filesystem commands or complete wrappers are loaded.
        if language == "powershell":
            command.append(base64.b64encode(script.encode("utf-16-le")).decode("ascii"))
            script = ""
        result = self.real_run(
            command, input=script + "\n", text=True, capture_output=True,
            cwd=self.root, env=environment, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        if language == "powershell":
            return list(json.loads(result.stdout.strip()))
        return result.stdout.rstrip("\0").split("\0")

    def import_release_code(self, arguments: list[str], module: str = "transaction") -> None:
        # Reuse exactly the interpreter options emitted by the shipped call;
        # replace its mutating script/module body with a source-import-only probe.
        options = []
        for argument in arguments:
            if not argument.startswith("-") or argument in {"-m", "-c"}:
                break
            self.assertIn(argument, {"-B", "-u", "-I"})
            options.append(argument)
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith("PYTHON")
        }
        self.validate_root()
        code_root = self.release / "install" / "bootstrap"
        script = r"""
import sys
original_no_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True
import os, pathlib, pkgutil
import argparse, dataclasses, hashlib, json, re, runpy, shutil, socket, stat, subprocess, time, uuid
root = pathlib.Path(sys.argv[1]).resolve(strict=True)
sys.dont_write_bytecode = original_no_bytecode
def guard(event, args):
    if event.startswith(('subprocess.', 'os.exec', 'os.spawn', 'os.system')):
        raise AssertionError('child process blocked: ' + event)
    if event == 'open':
        path, mode, flags = args
        if not (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            return
        if isinstance(path, int):
            return  # fdopen reuses the already audited path-based os.open.
        target = pathlib.Path(path).resolve()
        assert target.is_relative_to(root) and target.parent.name == '__pycache__', target
    elif event == 'os.mkdir':
        target = pathlib.Path(args[0]).resolve()
        assert target.is_relative_to(root) and target.name == '__pycache__', target
    elif event == 'os.rename':
        for path in args[:2]:
            target = pathlib.Path(path).resolve()
            assert target.is_relative_to(root) and target.parent.name == '__pycache__', target
    elif event in ('os.remove', 'os.rmdir', 'os.chmod'):
        raise AssertionError('unexpected mutation: ' + event)
sys.addaudithook(guard)
sys.path.insert(0, str(root))
if sys.argv[2] == 'transaction':
    # Script loading does not cache the entry script itself. Its real sibling
    # verify import does. Never execute the __main__ transaction command body.
    runpy.run_path(str(root / 'transaction.py'), run_name='__source_probe__')
else:
    __import__(sys.argv[2])
print('source-imported')
"""
        result = self.real_run(
            [sys.executable, *options, "-S", "-c", script, str(code_root), module],
            cwd=self.root, env=environment, text=True, capture_output=True,
            check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "source-imported")

    def test_candidate_bootstrap_loading_leaves_release_verifiable(self) -> None:
        source = (ROOT / "install" / "devloop.ps1").read_text(encoding="utf-8")
        bootstrap = _function(source, "function Invoke-Bootstrap {")
        call = next(line for line in source.splitlines() if "') publish $InstallDir" in line)
        arguments = self.shell_arguments("powershell", bootstrap + "\n" + call)
        before = self.snapshot()
        self.import_release_code(arguments)
        self.assertEqual(verify.verify(self.install), self.release)
        self.assertEqual(self.snapshot(), before)

    def test_bash_candidate_loading_leaves_release_verifiable(self) -> None:
        source = (ROOT / "install" / "devloop.sh").read_text(encoding="utf-8")
        call = next(line for line in source.splitlines() if 'transaction.py" publish ' in line)
        arguments = self.shell_arguments("bash", call)
        before = self.snapshot()
        self.import_release_code(arguments)
        self.assertEqual(verify.verify(self.install), self.release)
        self.assertEqual(self.snapshot(), before)

    def test_runner_and_planner_startup_leave_release_verifiable(self) -> None:
        for name in ("devloop", "devloop-plan"):
            for suffix, language in (("ps1", "powershell"), ("sh", "bash")):
                source = (ROOT / "bin" / f"{name}.{suffix}").read_text(encoding="utf-8")
                calls = [
                    line for line in source.splitlines()
                    if " -m devloop" in line
                ]
                self.assertEqual(len(calls), 2 if suffix == "ps1" else 1)
                for call in calls:
                    with self.subTest(wrapper=f"{name}.{suffix}", call=call.strip()):
                        arguments = self.shell_arguments(language, call)
                        before = self.snapshot()
                        self.import_release_code(arguments, "devloop")
                        self.assertEqual(verify.verify(self.install), self.release)
                        self.assertEqual(self.snapshot(), before)

    def test_worker_startup_preserves_release_and_independent_environments(self) -> None:
        path = ROOT / "src" / "devloop" / "portable_sessions.py"
        source = path.read_text(encoding="utf-8")
        node = next(
            node for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "_launch_portable_worker"
        )
        function = ast.get_source_segment(source, node)
        assert function is not None
        captured: list[tuple[list[str], dict[str, Any]]] = []

        def capture(command: list[str], **kwargs: Any) -> object:
            captured.append((command, kwargs))
            return object()

        namespace: dict[str, Any] = {
            "os": os, "sys": sys, "subprocess": subprocess,
            "launch_process_tree": capture,
            "validated_portable_launch_target": lambda launch: SimpleNamespace(
                argument_base=self.root / launch.session_id,
            ),
        }
        compiled = compile("from __future__ import annotations\n" + function, str(path), "exec")
        exec(compiled, namespace)
        inherited = {
            key: value for key, value in os.environ.items()
            if key.upper() != "PYTHONDONTWRITEBYTECODE"
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            before_environment = dict(os.environ)
            before = self.snapshot()
            for session in ("worker-one", "worker-two"):
                namespace["_launch_portable_worker"](
                    SimpleNamespace(session_id=session),
                    catalog_path=self.root / "catalog.db", owner_id="owner",
                )
            self.assertEqual(dict(os.environ), before_environment)
            self.assertIsNot(captured[0][1]["env"], captured[1][1]["env"])
            for (command, options), session in zip(
                captured, ("worker-one", "worker-two"), strict=True,
            ):
                environment = options["env"]
                self.assertNotIn("PYTHONDONTWRITEBYTECODE", environment)
                self.assertEqual(environment["DEVLOOP_PORTABLE_SESSION_ID"], session)
                self.assertEqual(options["cwd"], self.root / session)
                self.assertEqual(command[-2:], ["--session-id", session])
                self.assertIn("-u", command)
                self.import_release_code(command[1:], "devloop")
                self.assertEqual(verify.verify(self.install), self.release)
            self.assertEqual(self.snapshot(), before)

    def test_update_and_dispatch_verification_do_not_create_release_caches(self) -> None:
        cases = []
        source = (ROOT / "install" / "devloop.ps1").read_text(encoding="utf-8")
        cases.append(("powershell", _function(source, "function Get-VerifiedRelease {")
                      + "\nGet-VerifiedRelease"))
        source = (ROOT / "install" / "devloop.sh").read_text(encoding="utf-8")
        cases.append(("bash", _function(source, "current_release() {") + "\ncurrent_release"))
        source = (ROOT / "install" / "bootstrap" / "dispatch.ps1").read_text(encoding="utf-8")
        statements = [line for line in source.splitlines() if line.startswith(
            ("$verifyArguments =", "$releaseRoot ="),
        )]
        self.assertEqual(len(statements), 2)
        cases.append(("powershell", "\n".join(statements) + "\n$releaseRoot"))
        source = (ROOT / "install" / "bootstrap" / "dispatch.sh").read_text(encoding="utf-8")
        statements = [line for line in source.splitlines() if line.startswith("VERIFY_ARGS=")]
        # Execute the actual verifier call, without its command-substitution
        # assignment (Bash strips NUL bytes from the in-memory argument capture).
        line = next(line for line in source.splitlines() if line.startswith("RELEASE_ROOT="))
        call = line.removeprefix('RELEASE_ROOT="$(').removesuffix(')"')
        cases.append(("bash", "\n".join([*statements, call])))
        for language, script in cases:
            with self.subTest(language=language, call=script):
                arguments = self.shell_arguments(language, script)
                before = self.snapshot()
                self.import_release_code(arguments, "verify")
                self.assertEqual(verify.verify(self.install), self.release)
                self.assertEqual(self.snapshot(), before)

    def test_guarded_script_probe_reproduces_unsuppressed_sibling_cache(self) -> None:
        self.import_release_code(["transaction.py"])
        caches = list((self.release / "install" / "bootstrap").rglob("*.pyc"))
        self.assertTrue(any(path.name.startswith("verify.") for path in caches))
        self.assertFalse(any(path.name.startswith("transaction.") for path in caches))
        with self.assertRaisesRegex(RuntimeError, "untracked content"):
            verify.verify(self.install)

    def test_transaction_commands_keep_bytecode_outside_owned_payloads(self) -> None:
        cases: list[tuple[str, str]] = []
        for relative in ("install/bootstrap/dispatch.ps1", "install/uninstall-devloop.ps1"):
            lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
            begin = next(line for line in lines if "$transactionId = (& $python" in line)
            cases.append(("powershell", begin + "\n$transactionId"))
            array_name = "transactionArgs" if "dispatch" in relative else "arguments"
            definition = next(line for line in lines if f"${array_name} = @(" in line)
            call = next(line for line in lines if f"@{array_name}" in line)
            cases.append(("powershell", definition + "\n" + call))
        for relative in (
            "install/devloop.sh", "install/bootstrap/dispatch.sh", "install/uninstall-devloop.sh",
        ):
            lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
            for line in lines:
                if "--protocol 2" not in line:
                    continue
                tokens = ('"$python"', '"$(find_python)"', '"$PYTHON"', '$PYTHON ')
                starts = [line.index(token) for token in tokens if token in line]
                if starts:
                    # Isolate only the Python call, excluding substitutions,
                    # conditionals, redirections and any transaction body.
                    call = line[min(starts):].split(" --protocol 2", 1)[0] + " --protocol 2"
                    cases.append(("bash", call))
            if "uninstall" in relative or "dispatch" in relative:
                array_name = "UNINSTALL_ARGS" if "dispatch" in relative else "ARGS"
                definition = next(line for line in lines if f"{array_name}=(" in line)
                call = next(line for line in lines if '${' + array_name + '[@]}' in line)
                cases.append(("bash", definition + "\n" + call))
        self.assertGreaterEqual(len(cases), 15)
        for language, script in cases:
            with self.subTest(language=language, call=script):
                arguments = self.shell_arguments(language, script)
                before = self.snapshot()
                self.import_release_code(arguments)
                self.assertEqual(verify.verify(self.install), self.release)
                self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()

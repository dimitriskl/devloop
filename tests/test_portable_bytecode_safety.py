from __future__ import annotations

import ast
import base64
import hashlib
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


# Exact Git blobs from recovered historical checkpoint a69baa8, not current wrappers.
# These are inert source fixtures; tests execute only extracted invocation statements.
HISTORICAL_WRAPPERS = {
    "bin/devloop-plan.ps1": (
        "765b90e70a0c12d5bd3d2d6cacf0976f2c6cbd1a",
        "W0NtZGxldEJpbmRpbmcoKV0KcGFyYW0oCiAgICBbQWxpYXMoJ2gnKV0KICAgIFtzd2l0Y2hdICRIZWxwLAoKICAg"
        "IFtQYXJhbWV0ZXIoVmFsdWVGcm9tUmVtYWluaW5nQXJndW1lbnRzID0gJHRydWUpXQogICAgW3N0cmluZ1tdXSAk"
        "UmVtYWluaW5nQXJncwopCgokRXJyb3JBY3Rpb25QcmVmZXJlbmNlID0gJ1N0b3AnCgokYnVuZGxlUm9vdCA9IFNw"
        "bGl0LVBhdGggLVBhcmVudCAkUFNTY3JpcHRSb290CiRweXRob24gPSBKb2luLVBhdGggJGJ1bmRsZVJvb3QgJy52"
        "ZW52XFNjcmlwdHNccHl0aG9uLmV4ZScKaWYgKC1ub3QgKFRlc3QtUGF0aCAtTGl0ZXJhbFBhdGggJHB5dGhvbiAt"
        "UGF0aFR5cGUgTGVhZikpIHsKICAgICRkZXZlbG9wbWVudFNldHVwID0gSm9pbi1QYXRoICRidW5kbGVSb290ICdp"
        "bnN0YWxsXHNldHVwLWRldmVsb3BtZW50LnBzMScKICAgIGlmICgtbm90IChUZXN0LVBhdGggLUxpdGVyYWxQYXRo"
        "ICRkZXZlbG9wbWVudFNldHVwIC1QYXRoVHlwZSBMZWFmKSkgewogICAgICAgIHRocm93ICJEZXYgTG9vcCBydW50"
        "aW1lIGFuZCBib290c3RyYXAgc2NyaXB0IGFyZSBtaXNzaW5nIGZyb20gJGJ1bmRsZVJvb3QiCiAgICB9CiAgICBX"
        "cml0ZS1Ib3N0ICdEZXYgTG9vcCBydW50aW1lIG5vdCBmb3VuZDsgcHJlcGFyaW5nIHRoZSBjaGVja291dC1sb2Nh"
        "bCBydW50aW1lLicKICAgICYgJGRldmVsb3BtZW50U2V0dXAKfQppZiAoLW5vdCAoVGVzdC1QYXRoIC1MaXRlcmFs"
        "UGF0aCAkcHl0aG9uIC1QYXRoVHlwZSBMZWFmKSkgewogICAgdGhyb3cgJ0RldiBMb29wIGNvdWxkIG5vdCBwcmVw"
        "YXJlIGl0cyBjaGVja291dC1sb2NhbCBydW50aW1lLicKfQoKJHB5dGhvblBhdGggPSBKb2luLVBhdGggJGJ1bmRs"
        "ZVJvb3QgJ3NyYycKJGVudjpQWVRIT05QQVRIID0gaWYgKFtzdHJpbmddOjpJc051bGxPcldoaXRlU3BhY2UoJGVu"
        "djpQWVRIT05QQVRIKSkgewogICAgJHB5dGhvblBhdGgKfQplbHNlIHsKICAgICIkcHl0aG9uUGF0aCQoW0lPLlBh"
        "dGhdOjpQYXRoU2VwYXJhdG9yKSRlbnY6UFlUSE9OUEFUSCIKfQokZW52OkRFVkxPT1BfVUlfTU9ERSA9IGlmICgK"
        "ICAgIC1ub3QgW0NvbnNvbGVdOjpJc0lucHV0UmVkaXJlY3RlZCAtYW5kCiAgICAtbm90IFtDb25zb2xlXTo6SXNP"
        "dXRwdXRSZWRpcmVjdGVkCikgewogICAgJ2FwcGxpY2F0aW9uJwp9CmVsc2UgewogICAgJ3BsYWluJwp9CgppZiAo"
        "JEhlbHApIHsKICAgICYgJHB5dGhvbiAtbSBkZXZsb29wLmludGVyYWN0aXZlX3J1bm5lciAtLWhlbHAKICAgIGV4"
        "aXQgJExBU1RFWElUQ09ERQp9CgomICRweXRob24gLW0gZGV2bG9vcC5pbnRlcmFjdGl2ZV9ydW5uZXIgQFJlbWFp"
        "bmluZ0FyZ3MKZXhpdCAkTEFTVEVYSVRDT0RFCg=="
    ),
    "bin/devloop-plan.sh": (
        "8714413c20e0e724f8f5abb4ba618b9a371d1e7d",
        "IyEvdXNyL2Jpbi9lbnYgYmFzaApzZXQgLWV1byBwaXBlZmFpbAoKU0NSSVBUX0RJUj0iJChjZCAiJChkaXJuYW1l"
        "ICIke0JBU0hfU09VUkNFWzBdfSIpIiAmJiBwd2QpIgpCVU5ETEVfUk9PVD0iJChjZCAiJFNDUklQVF9ESVIvLi4i"
        "ICYmIHB3ZCkiCgpQWVRIT05fQklOPSIkQlVORExFX1JPT1QvLnZlbnYvYmluL3B5dGhvbiIKaWYgWyAhIC14ICIk"
        "UFlUSE9OX0JJTiIgXTsgdGhlbgogIERFVkVMT1BNRU5UX1NFVFVQPSIkQlVORExFX1JPT1QvaW5zdGFsbC9zZXR1"
        "cC1kZXZlbG9wbWVudC5zaCIKICBpZiBbICEgLWYgIiRERVZFTE9QTUVOVF9TRVRVUCIgXTsgdGhlbgogICAgcHJp"
        "bnRmICdEZXYgTG9vcCBydW50aW1lIGFuZCBib290c3RyYXAgc2NyaXB0IGFyZSBtaXNzaW5nIGZyb20gJXNcbicg"
        "IiRCVU5ETEVfUk9PVCIgPiYyCiAgICBleGl0IDEKICBmaQogIHByaW50ZiAnRGV2IExvb3AgcnVudGltZSBub3Qg"
        "Zm91bmQ7IHByZXBhcmluZyB0aGUgY2hlY2tvdXQtbG9jYWwgcnVudGltZS5cbicKICBiYXNoICIkREVWRUxPUE1F"
        "TlRfU0VUVVAiCmZpCmlmIFsgISAteCAiJFBZVEhPTl9CSU4iIF07IHRoZW4KICBwcmludGYgJ0RldiBMb29wIGNv"
        "dWxkIG5vdCBwcmVwYXJlIGl0cyBjaGVja291dC1sb2NhbCBydW50aW1lLlxuJyA+JjIKICBleGl0IDEKZmkKZXhw"
        "b3J0IFBZVEhPTlBBVEg9IiRCVU5ETEVfUk9PVC9zcmMke1BZVEhPTlBBVEg6KzokUFlUSE9OUEFUSH0iCmlmIFtb"
        "IC10IDAgJiYgLXQgMSBdXTsgdGhlbgogIGV4cG9ydCBERVZMT09QX1VJX01PREU9YXBwbGljYXRpb24KZWxzZQog"
        "IGV4cG9ydCBERVZMT09QX1VJX01PREU9cGxhaW4KZmkKCmV4ZWMgIiRQWVRIT05fQklOIiAtbSBkZXZsb29wLmlu"
        "dGVyYWN0aXZlX3J1bm5lciAiJEAiCg=="
    ),
    "bin/devloop.ps1": (
        "30c99504e7ad551b98100e7cb700bb20b516a52e",
        "W0NtZGxldEJpbmRpbmcoKV0KcGFyYW0oCiAgICBbQWxpYXMoJ2gnKV0KICAgIFtzd2l0Y2hdICRIZWxwLAoKICAg"
        "IFtQYXJhbWV0ZXIoVmFsdWVGcm9tUmVtYWluaW5nQXJndW1lbnRzID0gJHRydWUpXQogICAgW3N0cmluZ1tdXSAk"
        "UmVtYWluaW5nQXJncwopCgokRXJyb3JBY3Rpb25QcmVmZXJlbmNlID0gJ1N0b3AnCgokYnVuZGxlUm9vdCA9IFNw"
        "bGl0LVBhdGggLVBhcmVudCAkUFNTY3JpcHRSb290CiRweXRob24gPSBKb2luLVBhdGggJGJ1bmRsZVJvb3QgJy52"
        "ZW52XFNjcmlwdHNccHl0aG9uLmV4ZScKaWYgKC1ub3QgKFRlc3QtUGF0aCAtTGl0ZXJhbFBhdGggJHB5dGhvbiAt"
        "UGF0aFR5cGUgTGVhZikpIHsKICAgICRkZXZlbG9wbWVudFNldHVwID0gSm9pbi1QYXRoICRidW5kbGVSb290ICdp"
        "bnN0YWxsXHNldHVwLWRldmVsb3BtZW50LnBzMScKICAgIGlmICgtbm90IChUZXN0LVBhdGggLUxpdGVyYWxQYXRo"
        "ICRkZXZlbG9wbWVudFNldHVwIC1QYXRoVHlwZSBMZWFmKSkgewogICAgICAgIHRocm93ICJEZXYgTG9vcCBydW50"
        "aW1lIGFuZCBib290c3RyYXAgc2NyaXB0IGFyZSBtaXNzaW5nIGZyb20gJGJ1bmRsZVJvb3QiCiAgICB9CiAgICBX"
        "cml0ZS1Ib3N0ICdEZXYgTG9vcCBydW50aW1lIG5vdCBmb3VuZDsgcHJlcGFyaW5nIHRoZSBjaGVja291dC1sb2Nh"
        "bCBydW50aW1lLicKICAgICYgJGRldmVsb3BtZW50U2V0dXAKfQppZiAoLW5vdCAoVGVzdC1QYXRoIC1MaXRlcmFs"
        "UGF0aCAkcHl0aG9uIC1QYXRoVHlwZSBMZWFmKSkgewogICAgdGhyb3cgJ0RldiBMb29wIGNvdWxkIG5vdCBwcmVw"
        "YXJlIGl0cyBjaGVja291dC1sb2NhbCBydW50aW1lLicKfQokcHl0aG9uUGF0aCA9IEpvaW4tUGF0aCAkYnVuZGxl"
        "Um9vdCAnc3JjJwokZW52OlBZVEhPTlBBVEggPSBpZiAoW3N0cmluZ106OklzTnVsbE9yV2hpdGVTcGFjZSgkZW52"
        "OlBZVEhPTlBBVEgpKSB7CiAgICAkcHl0aG9uUGF0aAp9CmVsc2UgewogICAgIiRweXRob25QYXRoJChbSU8uUGF0"
        "aF06OlBhdGhTZXBhcmF0b3IpJGVudjpQWVRIT05QQVRIIgp9CiRlbnY6REVWTE9PUF9VSV9NT0RFID0gaWYgKAog"
        "ICAgLW5vdCBbQ29uc29sZV06OklzSW5wdXRSZWRpcmVjdGVkIC1hbmQKICAgIC1ub3QgW0NvbnNvbGVdOjpJc091"
        "dHB1dFJlZGlyZWN0ZWQKKSB7CiAgICAnYXBwbGljYXRpb24nCn0KZWxzZSB7CiAgICAncGxhaW4nCn0KCmlmICgk"
        "SGVscCkgewogICAgJiAkcHl0aG9uIC1tIGRldmxvb3AgLS1oZWxwCiAgICBleGl0ICRMQVNURVhJVENPREUKfQoK"
        "JiAkcHl0aG9uIC1tIGRldmxvb3AgQFJlbWFpbmluZ0FyZ3MKZXhpdCAkTEFTVEVYSVRDT0RFCg=="
    ),
    "bin/devloop.sh": (
        "3c5095007e75ad662fb1afd08d02d7faba3def77",
        "IyEvdXNyL2Jpbi9lbnYgYmFzaApzZXQgLWV1byBwaXBlZmFpbAoKU0NSSVBUX0RJUj0iJChjZCAiJChkaXJuYW1l"
        "ICIke0JBU0hfU09VUkNFWzBdfSIpIiAmJiBwd2QpIgpCVU5ETEVfUk9PVD0iJChjZCAiJFNDUklQVF9ESVIvLi4i"
        "ICYmIHB3ZCkiCgpQWVRIT05fQklOPSIkQlVORExFX1JPT1QvLnZlbnYvYmluL3B5dGhvbiIKaWYgWyAhIC14ICIk"
        "UFlUSE9OX0JJTiIgXTsgdGhlbgogIERFVkVMT1BNRU5UX1NFVFVQPSIkQlVORExFX1JPT1QvaW5zdGFsbC9zZXR1"
        "cC1kZXZlbG9wbWVudC5zaCIKICBpZiBbICEgLWYgIiRERVZFTE9QTUVOVF9TRVRVUCIgXTsgdGhlbgogICAgcHJp"
        "bnRmICdEZXYgTG9vcCBydW50aW1lIGFuZCBib290c3RyYXAgc2NyaXB0IGFyZSBtaXNzaW5nIGZyb20gJXNcbicg"
        "IiRCVU5ETEVfUk9PVCIgPiYyCiAgICBleGl0IDEKICBmaQogIHByaW50ZiAnRGV2IExvb3AgcnVudGltZSBub3Qg"
        "Zm91bmQ7IHByZXBhcmluZyB0aGUgY2hlY2tvdXQtbG9jYWwgcnVudGltZS5cbicKICBiYXNoICIkREVWRUxPUE1F"
        "TlRfU0VUVVAiCmZpCmlmIFsgISAteCAiJFBZVEhPTl9CSU4iIF07IHRoZW4KICBwcmludGYgJ0RldiBMb29wIGNv"
        "dWxkIG5vdCBwcmVwYXJlIGl0cyBjaGVja291dC1sb2NhbCBydW50aW1lLlxuJyA+JjIKICBleGl0IDEKZmkKZXhw"
        "b3J0IFBZVEhPTlBBVEg9IiRCVU5ETEVfUk9PVC9zcmMke1BZVEhPTlBBVEg6KzokUFlUSE9OUEFUSH0iCmlmIFtb"
        "IC10IDAgJiYgLXQgMSBdXTsgdGhlbgogIGV4cG9ydCBERVZMT09QX1VJX01PREU9YXBwbGljYXRpb24KZWxzZQog"
        "IGV4cG9ydCBERVZMT09QX1VJX01PREU9cGxhaW4KZmkKCmV4ZWMgIiRQWVRIT05fQklOIiAtbSBkZXZsb29wICIk"
        "QCIKCgo="
    ),
}


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
            for relative, (blob, encoded) in HISTORICAL_WRAPPERS.items():
                content = base64.b64decode(encoded, validate=True)
                header = f"blob {len(content)}\0".encode()
                self.assertEqual(hashlib.sha1(header + content).hexdigest(), blob)
                target = self.candidate / "install" / "bootstrap" / "legacy" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
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

    def import_release_code(
        self, arguments: list[str], module: str = "transaction", *,
        bytecode_policy: str | None = None,
    ) -> None:
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
        if bytecode_policy is not None:
            environment["PYTHONDONTWRITEBYTECODE"] = bytecode_policy
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

    def capture_retained_dispatch(self, language: str, command_name: str) -> dict[str, Any]:
        environment = {
            key: value for key, value in os.environ.items()
            if key.upper() not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "CDPATH"}
            and not key.startswith("BASH_FUNC_")
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "0"
        arguments = ["", "two words", "--plain", 'quote"value', "unicode-ü"]
        suffix = "ps1" if language == "powershell" else "sh"
        source = (ROOT / "install" / "bootstrap" / f"dispatch.{suffix}").read_text()
        target = str(self.release / "install" / "bootstrap" / "legacy" / "bin"
                     / f"{command_name}.{suffix}")
        if language == "bash":
            assert BASH is not None
            call = next(line for line in source.splitlines() if 'exec bash "$TARGET"' in line)
            # The shell process seam captures dispatch; no wrapper is sourced or run.
            script = r"""
set -euo pipefail
PATH=''
readonly PATH
TARGET=$PROBE_TARGET
set -- '' 'two words' '--plain' 'quote"value' 'unicode-ü'
bash() { printf '%s\0' "$PYTHONDONTWRITEBYTECODE" "$PWD" "$@"; }
exec() { "$@"; }
""" + call + '\nprintf "%s\\0" "$PYTHONDONTWRITEBYTECODE" "$PWD"\n'
            environment["PROBE_TARGET"] = target
            result = self.real_run(
                [BASH, "--noprofile", "--norc", "-s"], input=script,
                cwd=self.root, env=environment, text=True, encoding="utf-8",
                capture_output=True, check=False, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = result.stdout.rstrip("\0").split("\0")
            self.assertEqual(values[2:-2], [target, *arguments])
            self.assertEqual(values[-2], "0")
            self.assertEqual(values[1], values[-1])
            return {"bytecode_policy": values[0]}
        assert POWERSHELL is not None
        tail = source[source.rindex("if ($Command -in @('update', 'uninstall'))"):]
        start = "[System.Diagnostics.Process]::Start($startInfo)"
        self.assertLessEqual(tail.count(start), 1)
        tail = tail.replace(start, "(Capture-Process $startInfo)")
        script = r"""
$ErrorActionPreference = 'Stop'
$env:PATH = ''
Set-Location -LiteralPath $env:PROBE_CALLER_CWD
$target = $env:PROBE_TARGET
$Command = $env:PROBE_COMMAND
$RemainingArgs = @('', 'two words', '--plain', 'quote"value', 'unicode-ü')
$beforeEnvironment = [Environment]::GetEnvironmentVariables()
function Capture-Process($info) {
    $childEnvironment = @{}
    foreach ($entry in $info.Environment.GetEnumerator()) {
        $childEnvironment[$entry.Key] = $entry.Value
    }
    $capture = [ordered]@{
        bytecode_policy = $info.Environment['PYTHONDONTWRITEBYTECODE']
        arguments = @($info.ArgumentList)
        cwd = $info.WorkingDirectory
        caller_cwd = (Get-Location).ProviderPath
        dotnet_cwd = [Environment]::CurrentDirectory
        before_environment = $beforeEnvironment
        after_environment = [Environment]::GetEnvironmentVariables()
        child_environment = $childEnvironment
        shell_execute = $info.UseShellExecute
        redirect_input = $info.RedirectStandardInput
        redirect_output = $info.RedirectStandardOutput
        redirect_error = $info.RedirectStandardError
    }
    [Console]::WriteLine(($capture | ConvertTo-Json -Depth 6 -Compress))
    $process = [pscustomobject]@{ ExitCode = 23 }
    $process | Add-Member ScriptMethod WaitForExit { }
    $process | Add-Member ScriptMethod Dispose { }
    return $process
}
function pwsh {
    $info = [System.Diagnostics.ProcessStartInfo]::new('pwsh')
    $info.WorkingDirectory = (Get-Location).ProviderPath
    foreach ($argument in $args) { $info.ArgumentList.Add($argument) }
    $null = Capture-Process $info
    $global:LASTEXITCODE = 23
}
""" + tail
        caller = self.root / "empty-git-support"
        environment.update({
            "PROBE_TARGET": target, "PROBE_COMMAND": command_name,
            "PROBE_CALLER_CWD": str(caller),
        })
        result = self.real_run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            cwd=self.root, env=environment, text=True, encoding="utf-8",
            capture_output=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        value = json.loads(result.stdout.strip())
        self.assertEqual(value["arguments"], [
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", target, *arguments,
        ])
        self.assertEqual(Path(value["cwd"]), caller)
        self.assertEqual(value["cwd"], value["caller_cwd"])
        self.assertNotEqual(value["cwd"], value["dotnet_cwd"])
        self.assertTrue(value["before_environment"] == value["after_environment"],
                        "caller environment changed")
        expected_child = {**value["before_environment"],
                          "PYTHONDONTWRITEBYTECODE": value["bytecode_policy"]}
        self.assertTrue(value["child_environment"] == expected_child,
                        "unexpected child environment difference")
        for field in ("shell_execute", "redirect_input", "redirect_output", "redirect_error"):
            self.assertFalse(value[field], field)
        return dict(value)

    def test_retained_historical_bash_wrapper_cold_start_preserves_release(self) -> None:
        for name in ("devloop", "devloop-plan"):
            with self.subTest(wrapper=name):
                captured = self.capture_retained_dispatch("bash", name)
                old = self.release / "install" / "bootstrap" / "legacy" / "bin" / f"{name}.sh"
                call = next(line for line in old.read_text().splitlines() if " -m devloop" in line)
                self.assertNotIn(" -B ", call)
                arguments = self.shell_arguments("bash", call)
                self.assertFalse(list(self.release.rglob("*.pyc")), "fixture must be cold")
                before = self.snapshot()
                self.import_release_code(
                    arguments, "devloop", bytecode_policy=captured["bytecode_policy"],
                )
                self.assertEqual(verify.verify(self.install), self.release)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(captured["bytecode_policy"], "1")

    def test_retained_historical_powershell_wrapper_cold_start_preserves_release(self) -> None:
        for name in ("devloop", "devloop-plan"):
            with self.subTest(wrapper=name):
                captured = self.capture_retained_dispatch("powershell", name)
                old = self.release / "install" / "bootstrap" / "legacy" / "bin" / f"{name}.ps1"
                calls = [line for line in old.read_text().splitlines() if " -m devloop" in line]
                self.assertEqual(len(calls), 2)
                for call in calls:
                    self.assertNotIn(" -B ", call)
                    arguments = self.shell_arguments("powershell", call)
                    self.assertFalse(list(self.release.rglob("*.pyc")), "fixture must be cold")
                    before = self.snapshot()
                    self.import_release_code(
                        arguments, "devloop", bytecode_policy=captured["bytecode_policy"],
                    )
                    self.assertEqual(verify.verify(self.install), self.release)
                    self.assertEqual(self.snapshot(), before)
                self.assertEqual(captured["bytecode_policy"], "1")

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

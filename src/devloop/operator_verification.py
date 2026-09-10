"""Typed .NET verification gate and operator-side runner (standard library only).

The session runner executes authorized gates through a supervised process.
The standalone entry point remains available for diagnostic reruns.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class VerificationKind(str, Enum):
    DOTNET_TEST = "DOTNET_TEST"


REQUEST_FILE = "request.json"
RECEIPT_FILE = "result.json"
REPORT_FILE = "verification.trx"
TRX_NAMESPACE = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"
MAX_REPORT_BYTES = 20 * 1024 * 1024
GIT_INSPECTION_TIMEOUT_SECONDS = 30
SOURCE_EXTENSIONS = frozenset(
    {
        ".cs",
        ".csproj",
        ".fs",
        ".fsproj",
        ".vb",
        ".vbproj",
        ".props",
        ".targets",
        ".sln",
        ".slnx",
        ".json",
        ".config",
        ".runsettings",
        ".xml",
        ".sql",
        ".resx",
        ".razor",
        ".cshtml",
        ".dll",
        ".ps1",
        ".py",
        ".sh",
    }
)
IGNORED_PARTS = frozenset(
    {
        ".git",
        ".loop.logs",
        "bin",
        "obj",
        "node_modules",
        "testresults",
        ".venv",
    }
)


@dataclass(frozen=True)
class OperatorVerification:
    kind: VerificationKind
    project_path: str
    test_filter: str
    expected_tests: int
    reason: str

    @classmethod
    def parse(cls, value: Any) -> OperatorVerification | None:
        if value is None:
            return None
        keys = {"kind", "project_path", "test_filter", "expected_tests", "reason"}
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Operator verification must contain the documented gate fields.")
        for key in ("project_path", "test_filter", "reason"):
            if not isinstance(value[key], str) or not value[key].strip():
                raise ValueError(f"Operator verification {key} must be nonempty text.")
            if any(ord(char) < 32 for char in value[key]):
                raise ValueError(f"Operator verification {key} contains control characters.")
        count = value["expected_tests"]
        if type(count) is not int or count < 1:
            raise ValueError("Operator verification expected_tests must be a positive integer.")
        return cls(
            VerificationKind(value["kind"]),
            value["project_path"],
            value["test_filter"],
            count,
            value["reason"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def project(self, repository: Path) -> Path:
        relative = Path(self.project_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Verification project must be a repository-relative path.")
        project = (repository / relative).resolve()
        if not project.is_relative_to(repository.resolve()):
            raise ValueError("Verification project escapes the checkout.")
        if project.suffix.lower() != ".csproj" or not project.is_file():
            raise ValueError("Verification requires an existing .csproj in this checkout.")
        return project


def _git_source_listing(repository: Path) -> bytes:
    try:
        return subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            # The worker's stdin is a live control pipe with a reader thread.
            # Git for Windows probes inherited stdin and can block on that
            # reader indefinitely, before even inspecting the repository.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=GIT_INSPECTION_TIMEOUT_SECONDS,
        ).stdout
    except subprocess.TimeoutExpired as error:
        raise ValueError(
            f"Git source inspection timed out after {GIT_INSPECTION_TIMEOUT_SECONDS} seconds. "
            "Check local Git access before resuming external verification."
        ) from error
    except subprocess.CalledProcessError as error:
        raise ValueError(
            "Git could not list this checkout's source files. "
            "Check repository access and Git configuration before resuming external verification."
        ) from error


def source_fingerprint(repository: Path) -> str:
    """Bind evidence to Git-visible .NET source, configuration and build inputs."""
    listing = _git_source_listing(repository)
    names = set(listing.decode("utf-8").split("\0")) - {""}
    # Local appsettings are often ignored by Git but drive these SQL tests.
    # Hash them without persisting their contents or connection strings.
    directories = {repository, *(repository / Path(name).parent for name in names)}
    for directory in directories:
        if any(part.lower() in IGNORED_PARTS for part in directory.relative_to(repository).parts):
            continue
        for pattern in ("appsettings*.json", "*.runsettings", "Directory.Build.*", "NuGet.Config"):
            names.update(
                path.relative_to(repository).as_posix() for path in directory.glob(pattern)
            )
    digest = hashlib.sha256()
    for name in sorted(names):
        relative = Path(name)
        if relative.name.endswith(".loop.state.json") or relative.name == "devloop.status.json":
            continue
        if any(part.lower() in IGNORED_PARTS for part in relative.parts):
            continue
        if relative.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        path = (repository / relative).resolve()
        if not path.is_relative_to(repository.resolve()):
            raise ValueError("Verification source input escapes the checkout.")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"deleted")
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("Verification file exceeds the size limit.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Verification file must be a JSON object.")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def dotnet_command(request: dict[str, Any], directory: Path) -> list[str]:
    gate = OperatorVerification.parse(request["gate"])
    if gate is None:
        raise ValueError("Missing verification gate.")
    project = gate.project(Path(request["repository"]))
    return [
        "dotnet",
        "test",
        str(project),
        "--no-restore",
        "--filter",
        gate.test_filter,
        "--logger",
        f"trx;LogFileName={REPORT_FILE}",
        "--results-directory",
        str(directory),
    ]


def read_report(report: Path) -> ET.Element:
    if report.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("TRX exceeds the size limit.")
    data = report.read_bytes()
    # Do not permit entities/DTDs, including UTF-16 encoded declarations.
    if b"<!DOCTYPE" in data.replace(b"\0", b"").upper():
        raise ValueError("TRX must not contain a DTD.")
    return ET.fromstring(data)


def validate_report(report: Path, expected: int) -> None:
    root = read_report(report)
    ns = TRX_NAMESPACE
    counters = root.find(f"{ns}ResultSummary/{ns}Counters")
    results = root.findall(f"{ns}Results/{ns}UnitTestResult")
    if counters is None:
        raise ValueError("TRX is missing test counters.")
    if any(int(counters.get(key, "-1")) != expected for key in ("total", "executed", "passed")):
        raise ValueError(f"Expected {expected} tests, all executed and passed, with zero skips.")
    if any(
        int(value) != 0
        for key, value in counters.attrib.items()
        if key not in {"total", "executed", "passed"}
    ):
        raise ValueError("TRX contains skipped, failed or incomplete tests.")
    if len(results) != expected or any(item.get("outcome") != "Passed" for item in results):
        raise ValueError("Individual TRX results do not confirm every expected test passed.")
    if any(not item.get("testId") for item in results):
        raise ValueError("TRX contains missing test identities.")
    if len({(item.get("testId"), item.get("testName")) for item in results}) != expected:
        raise ValueError("TRX contains missing or duplicate test identities.")


def validate_receipt(request: dict[str, Any], directory: Path) -> str:
    receipt = read_json(directory / RECEIPT_FILE)
    for key in ("request_id", "source_fingerprint"):
        if receipt.get(key) != request[key]:
            raise ValueError(f"Verification receipt has a different {key}.")
    if receipt.get("exit_code") != 0:
        raise ValueError(
            "Test command failed; inspect verification.log before retrying the gate."
        )
    repository = Path(request["repository"])
    if source_fingerprint(repository) != request["source_fingerprint"]:
        raise ValueError("Source inputs changed; a fresh verification request is required.")
    report = directory / str(receipt["report_path"])
    if not report.resolve().is_relative_to(directory.resolve()):
        raise ValueError("TRX report escapes the verification directory.")
    if report.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("TRX exceeds the size limit.")
    if hashlib.sha256(report.read_bytes()).hexdigest() != receipt.get("report_sha256"):
        raise ValueError("TRX report changed after the operator command finished.")
    gate = OperatorVerification.parse(request["gate"])
    assert gate is not None
    validate_report(report, gate.expected_tests)
    return (
        f"Operator verification {request['request_id']} verified: {gate.project_path}; "
        f"filter {gate.test_filter}; {gate.expected_tests} passed, zero skipped. "
        f"TRX: {report}. Git-visible source/build fingerprint: {request['source_fingerprint']}. "
        "Use this evidence for this gate on these source inputs. Continue the issue; "
        "do not repeat the same sandbox-incompatible preflight. This does not waive "
        "implementation, review, QA or tests required by subsequent source changes."
    )


def execute_request(
    request_path: Path,
    *,
    run_command: Callable[[list[str], Path, Path], int] | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Execute a typed test gate and record its independently validated receipt."""
    request = read_json(request_path)
    repository = Path(request["repository"])
    if source_fingerprint(repository) != request["source_fingerprint"]:
        raise ValueError("Source inputs changed. Wait for Dev Loop to refresh the command.")
    output = request_path.parent / uuid.uuid4().hex
    output.mkdir()
    command = dotnet_command(request, output)
    emit(f"Verifying {request['gate']['expected_tests']} tests in {repository}")
    before = request["source_fingerprint"]
    exit_code = (
        run_command(command, repository, output)
        if run_command is not None
        else subprocess.run(
            command, cwd=repository, stdin=subprocess.DEVNULL, check=False
        ).returncode
    )
    after = source_fingerprint(repository)
    report = output / REPORT_FILE
    receipt = {
        "request_id": request["request_id"],
        "source_fingerprint": before,
        "exit_code": exit_code if before == after else -1,
        "report_path": str(report.relative_to(request_path.parent)),
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()
        if report.is_file()
        else "",
    }
    write_json(request_path.parent / RECEIPT_FILE, receipt)
    try:
        validate_receipt(request, request_path.parent)
    except (ValueError, OSError, ET.ParseError) as error:
        emit(f"Verification not accepted: {error}")
        return 1
    emit("Verification passed. Dev Loop will continue automatically.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: operator_verification.py <request.json>")
    try:
        raise SystemExit(execute_request(Path(sys.argv[1]).resolve()))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Verification could not run: {type(error).__name__}") from error

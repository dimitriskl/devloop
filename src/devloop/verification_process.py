"""Run authorized test gates as owned children of the Dev Loop session."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from queue import Empty, Queue

from .portable_runtime import active_portable_runtime
from .redaction import redact_persisted_evidence
from .subprocess_utils import launch_process_tree, terminate_process
from .terminal_text import sanitize_terminal_text

VERIFICATION_LOG = "verification.log"
PROCESS_POLL_SECONDS = 0.1
STREAM_JOIN_SECONDS = 2


def publish_verification_output(message: str) -> None:
    safe = sanitize_terminal_text(redact_persisted_evidence(message), preserve_newlines=True)
    runtime = active_portable_runtime()
    if runtime is None:
        print(safe, flush=True)
    else:
        runtime.write_output(safe + "\n", is_error=False)


def run_verification_command(command: list[str], repository: Path, output: Path) -> int:
    """Inherit the session account; keep test descendants under lifecycle control."""
    runtime = active_portable_runtime()
    wait = runtime.wait_for_retry if runtime is not None else time.sleep
    wait(0)  # Honor a pending Pause/Cancel before starting any test code.
    messages: Queue[str | Exception] = Queue()
    with (output / VERIFICATION_LOG).open("w", encoding="utf-8") as log:
        process = launch_process_tree(
            command,
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        def read_output() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    messages.put(line)
            except (OSError, ValueError) as error:
                messages.put(error)

        def drain_output() -> None:
            # Bound each drain so noisy tests cannot starve Pause/Cancel.
            for _ in range(100):
                try:
                    item = messages.get_nowait()
                except Empty:
                    return
                if isinstance(item, Exception):
                    raise OSError("Could not read verification output.") from item
                safe = sanitize_terminal_text(
                    redact_persisted_evidence(item), preserve_newlines=True
                )
                log.write(safe)
                log.flush()
                publish_verification_output(safe.rstrip("\r\n"))

        reader = threading.Thread(target=read_output, name="verification-output", daemon=True)
        try:
            reader.start()
            while process.poll() is None:
                drain_output()
                wait(PROCESS_POLL_SECONDS)
            wait(0)
        finally:
            stopped = terminate_process(process)
            reader.join(timeout=STREAM_JOIN_SECONDS)
            if not reader.is_alive() and process.stdout is not None:
                process.stdout.close()
        if not stopped.tree_terminated or reader.is_alive():
            raise OSError("Verification process tree or output stream did not stop.")
        while not messages.empty():
            drain_output()
            wait(0)
        assert process.returncode is not None
        return process.returncode

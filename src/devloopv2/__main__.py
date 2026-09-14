"""Entry point for ``python -m devloopv2``."""
from __future__ import annotations

import os
import sys

from .cli import main

if __name__ == "__main__":
    # os._exit bypasses interpreter finalization. The supervisor's reader
    # threads stay blocked on worker pipes, and letting normal shutdown run
    # into them crashes the process on Windows instead of exiting with the
    # code main() resolved. Descendants are already reaped by the Windows Job
    # Object, which kills the tree when this process's handles close.
    code = main(sys.argv[1:])
    # os._exit also skips flushing, so anything printed after the TUI closed
    # would otherwise be lost.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)

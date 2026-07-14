from __future__ import annotations

from collections.abc import Sequence

from devloop.application.main import run_application


def main(arguments: Sequence[str] | None = None) -> int:
    return run_application(arguments)


if __name__ == "__main__":
    raise SystemExit(main())

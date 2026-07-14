from __future__ import annotations

import pytest

from devloop.components.development import parse_development_output
from devloop.domain.development import (
    ReworkResolution,
    ReworkResolutionStatus,
    validate_rework_resolutions,
)


def test_final_structured_object_wins_when_app_server_returns_progress_object_first() -> None:
    progress = '{"summary":"progress"}'
    final = '{"summary":"final","criteria":[]}'

    parsed = parse_development_output(progress + final)

    assert parsed["summary"] == "final"


def test_rework_resolutions_require_exact_unique_terminal_coverage() -> None:
    resolved = ReworkResolution(
        "RF-001",
        ReworkResolutionStatus.RESOLVED,
        "The implementation and focused test now cover the finding.",
    )

    validate_rework_resolutions(("RF-001",), (resolved,))

    with pytest.raises(ValueError, match="cover every requested item"):
        validate_rework_resolutions(("RF-001",), ())
    with pytest.raises(ValueError, match="unique"):
        validate_rework_resolutions(("RF-001",), (resolved, resolved))
    with pytest.raises(ValueError, match="unresolved"):
        validate_rework_resolutions(
            ("RF-001",),
            (
                ReworkResolution(
                    "RF-001",
                    ReworkResolutionStatus.UNRESOLVED,
                    "The requested correction remains incomplete.",
                ),
            ),
        )

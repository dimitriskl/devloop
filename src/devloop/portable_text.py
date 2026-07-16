from __future__ import annotations

from .terminal_text import has_unsafe_terminal_controls


def normalize_single_line_display_name(value: object, *, field_name: str) -> str:
    """Return a trimmed display name without controls or line separators."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text.")
    if has_unsafe_terminal_controls(value):
        raise ValueError(
            f"{field_name} must not contain control characters or line breaks."
        )
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty.")
    return normalized

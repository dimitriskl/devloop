from __future__ import annotations

import re
import unicodedata


_CONTROL_STRING_PATTERN = re.compile(
    r"(?:\x1b[PX^_]|[\x90\x98\x9e\x9f]).*?(?:\x1b\\|\x9c)",
    re.DOTALL,
)
_OSC_PATTERN = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c)",
    re.DOTALL,
)
_CSI_PATTERN = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_BIDIRECTIONAL_CONTROL_CHARACTERS = frozenset(
    (
        "\u061c",
        "\u200e",
        "\u200f",
        *(chr(codepoint) for codepoint in range(0x202A, 0x202F)),
        *(chr(codepoint) for codepoint in range(0x2066, 0x2070)),
    )
)


def has_unsafe_terminal_controls(value: object) -> bool:
    """Return whether text contains controls unsafe for terminal metadata."""
    return any(
        unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"}
        or character in _BIDIRECTIONAL_CONTROL_CHARACTERS
        for character in str(value)
    )


def sanitize_terminal_text(
    value: object,
    *,
    preserve_newlines: bool = True,
) -> str:
    """Return terminal-safe text without executable or deceptive controls."""
    safe_parts: list[str] = []
    text = _CONTROL_STRING_PATTERN.sub("", str(value))
    text = _OSC_PATTERN.sub("", text)
    text = _CSI_PATTERN.sub("", text)
    for character in text:
        if character == "\n":
            safe_parts.append("\n" if preserve_newlines else " ")
            continue
        if not has_unsafe_terminal_controls(character):
            safe_parts.append(character)
    return "".join(safe_parts)


def compact_terminal_text(value: object, *, max_length: int) -> str:
    """Return bounded, single-line terminal-safe text."""
    normalized = " ".join(
        sanitize_terminal_text(value, preserve_newlines=False).split()
    )
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."

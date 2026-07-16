from __future__ import annotations

import unicodedata


_ESCAPE = "\x1b"
_STRING_TERMINATOR = "\x9c"
_OSC_BEL = "\x07"
_ESC_CONTROL_STRING_INTRODUCERS = frozenset("PX^_")
_C1_CONTROL_STRING_INTRODUCERS = frozenset(
    ("\x90", "\x98", "\x9e", "\x9f")
)
_CSI_INTRODUCER = "["
_CSI_C1_INTRODUCER = "\x9b"
_OSC_INTRODUCER = "]"
_OSC_C1_INTRODUCER = "\x9d"
_STRING_TERMINATOR_SUFFIX = "\\"
_CSI_FINAL_MIN = "@"
_CSI_FINAL_MAX = "~"
_BIDIRECTIONAL_CONTROL_CHARACTERS = frozenset(
    (
        "\u061c",
        "\u200e",
        "\u200f",
        *(chr(codepoint) for codepoint in range(0x202A, 0x202F)),
        *(chr(codepoint) for codepoint in range(0x2066, 0x2070)),
    )
)


def _consume_control_string(text: str, index: int, *, osc: bool) -> int:
    """Return the first position after a terminated control string."""
    while index < len(text):
        character = text[index]
        if osc and character == _OSC_BEL:
            return index + 1
        if character == _STRING_TERMINATOR:
            return index + 1
        if (
            character == _ESCAPE
            and index + 1 < len(text)
            and text[index + 1] == _STRING_TERMINATOR_SUFFIX
        ):
            return index + 2
        index += 1
    return len(text)


def _consume_csi(text: str, index: int) -> int:
    """Return the first position after a CSI sequence or the input end."""
    while index < len(text):
        if _CSI_FINAL_MIN <= text[index] <= _CSI_FINAL_MAX:
            return index + 1
        index += 1
    return len(text)


def _skip_terminal_sequences(text: str) -> str:
    """Remove recognized terminal sequences in one linear scan."""
    safe_parts: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == _ESCAPE and index + 1 < len(text):
            introducer = text[index + 1]
            if introducer == _OSC_INTRODUCER:
                index = _consume_control_string(text, index + 2, osc=True)
                continue
            if introducer in _ESC_CONTROL_STRING_INTRODUCERS:
                index = _consume_control_string(text, index + 2, osc=False)
                continue
            if introducer == _CSI_INTRODUCER:
                index = _consume_csi(text, index + 2)
                continue
        elif character == _OSC_C1_INTRODUCER:
            index = _consume_control_string(text, index + 1, osc=True)
            continue
        elif character in _C1_CONTROL_STRING_INTRODUCERS:
            index = _consume_control_string(text, index + 1, osc=False)
            continue
        elif character == _CSI_C1_INTRODUCER:
            index = _consume_csi(text, index + 1)
            continue
        safe_parts.append(character)
        index += 1
    return "".join(safe_parts)


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
    text = _skip_terminal_sequences(str(value))
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

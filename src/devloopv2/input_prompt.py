"""Render a worker input request as text, and turn a typed answer back into a protocol value."""
from __future__ import annotations

from devloop.issue_reply import IssueReply
from devloop.portable_sessions import (
    PortableSessionInputKind,
    PortableSessionInputRequest,
)

_LISTED_KINDS = frozenset(
    {PortableSessionInputKind.CHOICE, PortableSessionInputKind.APPROVAL}
)


class UnresolvedAnswer(ValueError):
    """The typed answer does not name any offered option."""


def prompt_lines(request: PortableSessionInputRequest) -> tuple[str, ...]:
    """Render the request the way the operator will answer it: prose, then a numbered list."""
    lines = [line for line in request.prompt.splitlines()] or [""]
    if request.kind not in _LISTED_KINDS:
        if request.initial_value and request.kind is PortableSessionInputKind.TEXT:
            lines.append(f"Current value: {request.initial_value}")
        lines.append("Type an answer and press Enter.")
        return tuple(lines)
    lines.append("")
    for position, (key, label) in enumerate(request.options, start=1):
        marker = " (default)" if key == request.default_key else ""
        lines.append(f"  {position}. {label}{marker}")
    lines.append("")
    lines.append("Type the number or the option text and press Enter.")
    return tuple(lines)


def resolve_answer(request: PortableSessionInputRequest, typed: str) -> str:
    """Turn what the operator typed into the value the supervisor expects.

    Raises:
        UnresolvedAnswer: the text names no offered option and there is no
            default to fall back on.
    """
    answer = typed.strip()
    if request.kind is PortableSessionInputKind.REPLY:
        return IssueReply(text=answer).encode()
    if request.kind not in _LISTED_KINDS:
        return answer
    if not answer:
        if request.default_key:
            return request.default_key
        raise UnresolvedAnswer("Type the number or the text of one of the options.")
    return _match_option(request, answer)


def _match_option(request: PortableSessionInputRequest, answer: str) -> str:
    options = request.options
    if answer.isdigit():
        position = int(answer)
        if 1 <= position <= len(options):
            return options[position - 1][0]
        raise UnresolvedAnswer(
            f"Choose a number between 1 and {len(options)}."
        )
    folded = answer.casefold()
    for key, label in options:
        if folded in {key.casefold(), label.casefold()}:
            return key
    prefixed = [
        key
        for key, label in options
        if label.casefold().startswith(folded) or key.casefold().startswith(folded)
    ]
    if len(prefixed) == 1:
        return prefixed[0]
    if prefixed:
        raise UnresolvedAnswer(f"{answer!r} matches more than one option.")
    raise UnresolvedAnswer(f"{answer!r} is not one of the offered options.")

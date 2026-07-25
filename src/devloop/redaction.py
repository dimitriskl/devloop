"""The Redaction Service: the one boundary secrets cross before persistence.

Dev Loop persists two very different kinds of text it did not author. Bounded
user guidance is prose, so a secret assignment inside it can safely be
over-redacted through the rest of its logical line. Persisted Evidence and
durable attempt logs are machine transcripts — JSONL event streams, structured
role results, provider diagnostics — where swallowing the rest of the line would
destroy the surrounding record, so a detected secret is masked value by value
instead.

The evidence policy therefore has a second obligation beyond masking: a record
that parsed as JSON before redaction must still parse as JSON after it, because
these transcripts are read back by the run reviewer and by role-pass recovery.
Text that is JSON is redacted through the parser and handed back to the
serializer, so every quote and escape stays the serializer's problem rather than
a regular expression's. Only text that is not JSON is scanned as text.

Both policies share one detector so a secret recognised in guidance is
recognised in evidence. Neither policy is a credential store and neither can
prove text is secret-free; they mask what is detectable before it reaches
storage.
"""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "[redacted]"
REDACTED_PRIVATE_KEY = "[redacted-private-key]"
REDACTED_BEARER = "Bearer [redacted]"
REDACTED_KEY = "[redacted-key]"
REDACTED_GITHUB_TOKEN = "[redacted-github-token]"

_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[^\s]+")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_GITHUB_TOKEN = re.compile(
    r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----.*?"
    r"(?:-----END \1-----|\Z)",
    re.DOTALL,
)
_SECRET_KEY_PARTS = frozenset(
    {"secret", "token", "password", "passwd", "credential", "credentials"}
)
_KEY_CHARACTER_SET = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
# Exactly the guidance scanner's key vocabulary, expressed so that a key is
# recognised only when a secret word is a whole trailing part of it. Both
# policies therefore detect the same secrets and differ only in how much they
# mask. The singular `token` is deliberate: `input_tokens` and
# `cache_read_input_tokens` are a provider's usage accounting, and masking them
# would destroy the cost evidence an attempt is supposed to leave behind.
_SECRET_KEY_WORDS = (
    "secret",
    "token",
    "password",
    "passwd",
    "credentials?",
    "api[_-]?key",
    "access[_-]?key",
    "private[_-]?key",
    "connection[_-]?string",
)
_SECRET_KEY_PATTERN = (
    r"(?:[A-Za-z0-9]+[_-])*(?:" + "|".join(_SECRET_KEY_WORDS) + r")"
)
# The same key vocabulary, matched against a parsed JSON field name instead of
# against serialized text, so the structural policy below recognises exactly the
# keys the text scanner recognises and no others.
_EVIDENCE_SECRET_KEY = re.compile(r"(?i)" + _SECRET_KEY_PATTERN)
# A quote that reached the transcript already escaped, as it is in every
# serialized JSON string. It is matched ahead of a bare quote so an assignment
# inside serialized text is bounded by the escaped quote that really delimits it,
# rather than falling through to the unquoted branch and stopping on the
# backslash — which would leave the secret in place and the escaping broken.
_ESCAPED_QUOTE = '\\"'
_ESCAPED_QUOTED_VALUE = r'\\"(?:\\.|[^\\"\r\n])*\\"'
_EVIDENCE_ASSIGNED_SECRET = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])"
    r"(?P<key>" + _SECRET_KEY_PATTERN + r")"
    r"(?![A-Za-z0-9_-])"
    r"(?P<separator>[\"']?\s*[:=]\s*)"
    r"(?P<value>"
    + _ESCAPED_QUOTED_VALUE
    + r"|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;)\]}\"']+)"
)
_JSON_DOCUMENT_OPENINGS = ("{", "[")


def redact_persisted_evidence(value: str) -> str:
    """Mask detected secrets in a machine transcript without eating its structure.

    Applied to every durable attempt log and to every provider-authored text
    that becomes Persisted Evidence, on every Execution Backend, so the same
    detector protects both providers' output. Only the secret value is replaced,
    which keeps the enclosing JSON object, event line, or command readable — and
    a transcript that parsed as JSON still parses once its secrets are masked,
    because the masking happens inside the parsed record.

    A JSON document is redacted as one whole; a JSONL stream is redacted record
    by record; anything else is scanned as text. Text that has nothing to mask is
    returned exactly as it arrived.
    """
    document = _redacted_json_document(value)
    if document is not None:
        return document
    return _redacted_transcript_lines(value)


def _redacted_transcript_lines(value: str) -> str:
    """Redact a transcript record by record, structurally wherever one is JSON.

    Lines that are not JSON are scanned together rather than one at a time, so a
    private key block spanning several plain lines is still recognised whole.
    """
    output: list[str] = []
    scanned: list[str] = []

    def flush_scanned() -> None:
        if scanned:
            output.append(_redact_evidence_text("".join(scanned)))
            scanned.clear()

    for line in value.splitlines(keepends=True):
        record = _redacted_json_document(line)
        if record is None:
            scanned.append(line)
            continue
        flush_scanned()
        output.append(record)
    flush_scanned()
    return "".join(output)


def _redacted_json_document(text: str) -> str | None:
    """Redact one JSON document, leaving every escape to the serializer.

    Returns ``None`` when the text is not a JSON object or array, which is the
    caller's signal to scan it as text instead. A document with nothing to mask
    is returned byte for byte rather than reserialized, so redaction only ever
    rewrites a record that actually carried a secret.
    """
    body = text.rstrip("\r\n")
    if not body.lstrip().startswith(_JSON_DOCUMENT_OPENINGS):
        return None
    try:
        parsed = json.loads(body)
        if not isinstance(parsed, (dict, list)):
            return None
        redacted = _redacted_json_value(parsed)
    except (ValueError, RecursionError):
        # Text this policy cannot read structurally is scanned as text instead,
        # which masks the same secrets rather than trusting a structure it failed
        # to parse.
        return None
    if redacted == parsed:
        return text
    ending = text[len(body) :]
    return json.dumps(redacted, ensure_ascii=False, separators=(",", ":")) + ending


def _redacted_json_value(value: Any) -> Any:
    """Mask secrets inside a parsed JSON value, returning a redacted copy."""
    if isinstance(value, dict):
        return _redacted_json_object(value)
    if isinstance(value, list):
        return [_redacted_json_value(member) for member in value]
    if isinstance(value, str):
        return _redacted_json_string(value)
    return value


def _redacted_json_object(value: dict[str, Any]) -> dict[str, Any]:
    """Mask a JSON object member by member, using its field names.

    Parsing has already paired a field name with its value, so a member named for
    a secret is masked outright instead of the pairing having to be inferred from
    `key=value` text. Every string the member holds is masked, because a field
    called `credentials` is no less a secret for being an object or a list.
    """
    redacted: dict[str, Any] = {}
    for key, member in value.items():
        redacted[_redact_evidence_text(key)] = (
            _masked_named_member(member)
            if _EVIDENCE_SECRET_KEY.fullmatch(key)
            else _redacted_json_value(member)
        )
    return redacted


def _masked_named_member(value: Any) -> Any:
    """Mask every string held by a member whose field name names a secret."""
    if isinstance(value, str):
        return REDACTED if value else value
    if isinstance(value, dict):
        return {key: _masked_named_member(member) for key, member in value.items()}
    if isinstance(value, list):
        return [_masked_named_member(member) for member in value]
    return value


def _redacted_json_string(value: str) -> str:
    """Redact one JSON string, recursing when the string is itself a document.

    A tool result that read a JSON file back, or a command that carries a JSON
    payload, is redacted through the parser as well, so nested escaping is the
    serializer's problem at every depth rather than a regular expression's.
    """
    nested = _redacted_json_document(value)
    if nested is not None:
        return nested
    return _redact_evidence_text(value)


def _redact_evidence_text(value: str) -> str:
    """Mask detected secrets in text that is not JSON, value by value."""
    redacted = _PRIVATE_KEY.sub(REDACTED_PRIVATE_KEY, value)
    redacted = _BEARER_SECRET.sub(REDACTED_BEARER, redacted)
    redacted = _EVIDENCE_ASSIGNED_SECRET.sub(_masked_assignment, redacted)
    redacted = _OPENAI_KEY.sub(REDACTED_KEY, redacted)
    return _GITHUB_TOKEN.sub(REDACTED_GITHUB_TOKEN, redacted)


def redact_guidance_secrets(value: str) -> str:
    """Mask detected secrets in bounded prose, over-redacting where unsure."""
    redacted = _PRIVATE_KEY.sub(REDACTED_PRIVATE_KEY, value)
    redacted = _BEARER_SECRET.sub(REDACTED_BEARER, redacted)
    redacted = _redact_assigned_secrets(redacted)
    redacted = _OPENAI_KEY.sub(REDACTED_KEY, redacted)
    return _GITHUB_TOKEN.sub(REDACTED_GITHUB_TOKEN, redacted)


def _masked_assignment(match: re.Match[str]) -> str:
    key = match.group("key")
    return f"{key}{match.group('separator')}{_masked_value(match.group('value'))}"


def _masked_value(value: str) -> str:
    """Mask one value in place, keeping the quoting the transcript used.

    An escaped quote is recognised as well as a bare one, so masking a secret
    inside serialized text leaves the escaping it was written with intact instead
    of emitting a quote the surrounding record never accounted for.
    """
    for quote in (_ESCAPED_QUOTE, '"', "'"):
        if (
            len(value) >= 2 * len(quote)
            and value.startswith(quote)
            and value.endswith(quote)
        ):
            return f"{quote}{REDACTED}{quote}"
    return REDACTED


def _redact_assigned_secrets(value: str) -> str:
    """Redact secret assignments with a monotonic, bounded scanner.

    Quoted values are parsed explicitly so malformed input is redacted through
    the end of the guidance instead of being left available to persistence.
    """
    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        assignment = _find_secret_assignment(value, cursor)
        if assignment is None:
            output.append(value[cursor:])
            break

        token_start, separator = assignment
        value_start = separator + 1
        while value_start < len(value) and value[value_start] in " \t\r\n":
            value_start += 1
        output.append(value[cursor:value_start])
        if value_start >= len(value):
            break
        if value[value_start] in "|>":
            output.append(REDACTED)
            cursor = _skip_secret_block(value, value_start, token_start)
            continue
        if value[value_start] in "'\"":
            replacement, cursor = _redact_quoted_secret_value(value, value_start)
            output.append(replacement)
            continue

        output.append(REDACTED)
        line_end = value.find("\n", value_start)
        cursor = len(value) if line_end == -1 else line_end

    return "".join(output)


def _find_secret_assignment(value: str, start: int) -> tuple[int, int] | None:
    """Find the next recognized assignment without backtracking or rescans."""
    cursor = start
    while cursor < len(value):
        if value[cursor] not in _KEY_CHARACTER_SET or (
            cursor > 0 and value[cursor - 1] in _KEY_CHARACTER_SET
        ):
            cursor += 1
            continue

        token_start = cursor
        cursor += 1
        while cursor < len(value) and value[cursor] in _KEY_CHARACTER_SET:
            cursor += 1
        if not _is_secret_key_name(value[token_start:cursor]):
            continue

        separator = cursor
        while separator < len(value) and value[separator] in " \t'\"":
            separator += 1
        if separator < len(value) and value[separator] in ":=":
            return token_start, separator

    return None


def _is_secret_key_name(value: str) -> bool:
    parts = value.lower().replace("-", "_").split("_")
    if not parts or any(not part for part in parts):
        return False
    if any(part in _SECRET_KEY_PARTS for part in parts):
        return True
    if any(
        part == "connection" and index + 1 < len(parts)
        and parts[index + 1] == "string"
        for index, part in enumerate(parts)
    ):
        return True
    if "connectionstring" in parts:
        return True
    return any(
        part in {"api", "access", "private"} and index + 1 < len(parts)
        and parts[index + 1] == "key"
        or part in {"apikey", "accesskey", "privatekey"}
        for index, part in enumerate(parts)
    )


def _redact_quoted_secret_value(value: str, start: int) -> tuple[str, int]:
    """Redact a quoted assignment through its complete logical line.

    A closing quote is not a safe boundary for a secret assignment because the
    value may continue through concatenation or another expression.  Preserve
    the quote style for readable diagnostics, but resume copying only at the
    next line.
    """
    delimiter = value[start : start + 3]
    if delimiter not in {"'''", '\"\"\"'}:
        delimiter = value[start]
    content_start = start + len(delimiter)
    cursor = content_start
    while cursor < len(value):
        if value.startswith(delimiter, cursor):
            if len(delimiter) == 1 and value.startswith(delimiter * 2, cursor):
                cursor += 2
                continue
            following = cursor + len(delimiter)
            if len(delimiter) > 1 or following == len(value) or value[following] in (
                " \t\r\n,.;:)]}"
            ):
                line_end = value.find("\n", following)
                return (
                    f"{delimiter}{REDACTED}{delimiter}",
                    len(value) if line_end == -1 else line_end,
                )
        if value[cursor] == "\\" and cursor + 1 < len(value):
            cursor += 2
        else:
            cursor += 1
    return f"{delimiter}{REDACTED}", len(value)


def _skip_secret_block(value: str, value_start: int, assignment_start: int) -> int:
    line_start = value.rfind("\n", 0, assignment_start) + 1
    line_prefix = value[line_start:assignment_start]
    base_indent = len(line_prefix) if line_prefix.strip() == "" else 0
    cursor = value.find("\n", value_start)
    if cursor == -1:
        return len(value)

    while cursor < len(value):
        next_line_start = cursor + 1
        line_end = value.find("\n", next_line_start)
        if line_end == -1:
            line_end = len(value)
        line = value[next_line_start:line_end]
        indentation = len(line) - len(line.lstrip(" \t"))
        if line.strip() and indentation <= base_indent:
            return cursor
        cursor = line_end
    return cursor

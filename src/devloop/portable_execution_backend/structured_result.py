"""Lenient recovery of the structured result an Execution Backend returns.

Both the role result parser and the backends that recover a structured message
from a provider transcript need the same tolerant extraction, so it lives beside
the Execution Backend boundary rather than inside one provider's module.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCED_JSON_OBJECT = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Recover a single JSON object from bare text, a code fence, or prose."""
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    code_block = _FENCED_JSON_OBJECT.search(text)
    if code_block:
        try:
            return json.loads(code_block.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    return None

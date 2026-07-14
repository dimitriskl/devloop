from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast


class StructuredOutputError(ValueError):
    pass


def final_object(message: str, role: str) -> Mapping[str, object]:
    decoder = json.JSONDecoder()
    index = 0
    objects: list[dict[str, object]] = []
    try:
        while index < len(message):
            while index < len(message) and message[index].isspace():
                index += 1
            if index >= len(message):
                break
            value, index = decoder.raw_decode(message, index)
            if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
                raise StructuredOutputError(f"{role} output must contain only JSON objects.")
            objects.append(cast(dict[str, object], value))
    except json.JSONDecodeError as error:
        raise StructuredOutputError(f"{role} returned invalid structured output.") from error
    if not objects:
        raise StructuredOutputError(f"{role} structured output is empty.")
    return objects[-1]


def required_string(data: Mapping[str, object], name: str, role: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise StructuredOutputError(f"{role} output is missing {name}.")
    return value


def optional_string(data: Mapping[str, object], name: str, role: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StructuredOutputError(f"{role} output has invalid {name}.")
    return value


def string_tuple(value: object, name: str, role: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StructuredOutputError(f"{role} output has invalid {name}.")
    return tuple(cast(list[str], value))

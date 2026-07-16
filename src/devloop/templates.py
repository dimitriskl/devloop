from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TEMPLATE_TOKEN = re.compile(r"\{\{(?P<key>[A-Z][A-Z0-9_]*)\}\}")


@dataclass(frozen=True)
class BundleContext:
    root: Path
    prompts: Path
    schemas: Path

    @classmethod
    def from_file(cls, current_file: Path) -> "BundleContext":
        root = current_file.parents[2]
        return cls(
            root=root,
            prompts=root / "prompts",
            schemas=root / "schemas",
        )


@dataclass(frozen=True)
class Preset:
    name: str
    required_docs: list[str]
    roles: dict[str, dict[str, Any]]


def load_preset(path: Path) -> Preset:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Preset(
        name=data["name"],
        required_docs=list(data.get("requiredDocs", [])),
        roles=dict(data.get("roles", {})),
    )


def render_template(path: Path, values: dict[str, Any]) -> str:
    text = path.read_text(encoding="utf-8")
    return _TEMPLATE_TOKEN.sub(
        lambda match: _render_template_value(match, values),
        text,
    )


def _render_template_value(
    match: re.Match[str],
    values: dict[str, Any],
) -> str:
    key = match.group("key")
    if key not in values:
        return match.group(0)
    value = values[key]
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {item}" for item in value)
    return str(value)

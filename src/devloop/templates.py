from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    roles: dict[str, dict[str, list[str]]]


def load_preset(path: Path) -> Preset:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Preset(
        name=data["name"],
        required_docs=list(data.get("requiredDocs", [])),
        roles=dict(data.get("roles", {})),
    )


def render_template(path: Path, values: dict[str, Any]) -> str:
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        if isinstance(value, (list, tuple)):
            rendered = "\n".join(f"- {item}" for item in value)
        else:
            rendered = str(value)
        text = text.replace("{{" + key + "}}", rendered)
    return text


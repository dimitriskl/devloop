from __future__ import annotations

import re

DEFAULT_BRANCH_NAME = "devloop-work"
BRANCH_COMPONENT_SEPARATOR = "/"


def sanitize_branch_name(value: str, *, default: str = DEFAULT_BRANCH_NAME) -> str:
    text = value.strip().replace("\\", BRANCH_COMPONENT_SEPARATOR)
    text = re.sub(r"\s+", "-", text)
    text = text.replace("@{", "-")
    text = re.sub(r"[~^:?*\[\]\x00-\x20\x7f]+", "-", text)
    text = re.sub(r"[^A-Za-z0-9._/\-]+", "-", text)
    text = re.sub(r"/+", BRANCH_COMPONENT_SEPARATOR, text)
    text = re.sub(r"\.{2,}", ".", text)

    components = [_sanitize_branch_component(part) for part in text.split(BRANCH_COMPONENT_SEPARATOR)]
    branch_name = BRANCH_COMPONENT_SEPARATOR.join(part for part in components if part)
    branch_name = branch_name.strip(BRANCH_COMPONENT_SEPARATOR)
    branch_name = branch_name.strip(".-")
    if branch_name == "@":
        branch_name = "at"
    if not branch_name:
        branch_name = default
    if branch_name.startswith("-"):
        branch_name = f"branch{branch_name}"
    return branch_name[:120].rstrip(".-/") or default


def _sanitize_branch_component(value: str) -> str:
    component = value.strip(".-")
    while component.lower().endswith(".lock"):
        component = component[:-5].rstrip(".-")
    return component

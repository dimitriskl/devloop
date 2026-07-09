from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PLANNING_SKILLS = ("grill-with-docs", "domain-modeling", "to-prd", "to-issues")


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    path: Path
    kind: str


@dataclass
class Catalog:
    skills: list[CatalogEntry]
    agents: list[CatalogEntry]

    def skill_by_name(self, name: str) -> CatalogEntry | None:
        for entry in self.skills:
            if entry.name == name:
                return entry
        return None


def discover(bundle_root: Path) -> Catalog:
    skills: list[CatalogEntry] = []
    skills_dir = bundle_root / "skills" / "codex"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir(), key=lambda path: path.name):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.is_file():
                skills.append(CatalogEntry(name=child.name, path=skill_md, kind="skill"))

    agents: list[CatalogEntry] = []
    agents_dir = bundle_root / "agents" / "codex"
    if agents_dir.is_dir():
        for child in sorted(agents_dir.glob("*.md"), key=lambda path: path.name):
            agents.append(CatalogEntry(name=child.stem, path=child, kind="agent"))

    return Catalog(skills=skills, agents=agents)


@dataclass
class Selection:
    planning_skills: list[str] = field(default_factory=lambda: list(DEFAULT_PLANNING_SKILLS))
    role_skills: dict[str, list[str]] = field(default_factory=dict)
    role_agents: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def defaults(cls) -> "Selection":
        return cls()

    def has_role_overrides(self) -> bool:
        return bool(self.role_skills or self.role_agents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "planning_skills": list(self.planning_skills),
            "role_skills": {role: list(paths) for role, paths in self.role_skills.items()},
            "role_agents": {role: list(paths) for role, paths in self.role_agents.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Selection":
        if not isinstance(data, dict):
            return cls.defaults()
        planning = data.get("planning_skills")
        selection = cls.defaults()
        if isinstance(planning, list):
            selection.planning_skills = [str(item) for item in planning]
        role_skills = data.get("role_skills")
        if isinstance(role_skills, dict):
            selection.role_skills = {
                str(role): [str(item) for item in items]
                for role, items in role_skills.items()
                if isinstance(items, list)
            }
        role_agents = data.get("role_agents")
        if isinstance(role_agents, dict):
            selection.role_agents = {
                str(role): [str(item) for item in items]
                for role, items in role_agents.items()
                if isinstance(items, list)
            }
        return selection


def planning_skill_paths(selection: Selection, catalog: Catalog) -> list[Path]:
    paths: list[Path] = []
    for name in selection.planning_skills:
        entry = catalog.skill_by_name(name)
        if entry is None:
            print(f"Warning: planning skill not found in bundle: {name}", file=sys.stderr)
            continue
        paths.append(entry.path)
    return paths


def load_selection(state_path: Path) -> Selection:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Selection.defaults()
    return Selection.from_dict(data.get("selection"))


def save_selection(state_path: Path, selection: Selection) -> None:
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, json.JSONDecodeError):
        pass
    data["selection"] = selection.to_dict()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"Could not save selection: {exc}", file=sys.stderr)


def write_session_preset(bundle_root: Path, selection: Selection, dest_path: Path) -> Path | None:
    if not selection.has_role_overrides():
        return None

    base_path = bundle_root / "presets" / "generic-minimal.json"
    data = json.loads(base_path.read_text(encoding="utf-8"))
    roles = data.setdefault("roles", {})
    for role, skills in selection.role_skills.items():
        roles.setdefault(role, {})["skills"] = list(skills)
    for role, agents in selection.role_agents.items():
        roles.setdefault(role, {})["agents"] = list(agents)
    data["name"] = f"{data.get('name', 'preset')}-session"

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return dest_path

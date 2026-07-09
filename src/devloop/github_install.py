from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .subprocess_utils import run_captured_text


@dataclass(frozen=True)
class InstallCandidate:
    kind: str
    name: str
    source: Path


@dataclass
class InstallResult:
    installed: list[str]
    message: str


def parse_github_ref(url: str) -> tuple[str, str]:
    base, _, subpath = url.partition("#")
    return base.strip(), subpath.strip().strip("/")


def find_candidates(root: Path) -> list[InstallCandidate]:
    candidates: list[InstallCandidate] = []
    if root.is_file() and root.suffix.lower() == ".md":
        return [InstallCandidate(kind="agent", name=root.stem, source=root)]
    if not root.is_dir():
        return []

    for skill_md in sorted(root.rglob("SKILL.md")):
        if ".git" in skill_md.parts:
            continue
        folder = skill_md.parent
        candidates.append(InstallCandidate(kind="skill", name=folder.name, source=folder))

    for agent_md in sorted(root.rglob("*.md")):
        if ".git" in agent_md.parts:
            continue
        if agent_md.parent.name != "agents":
            continue
        candidates.append(InstallCandidate(kind="agent", name=agent_md.stem, source=agent_md))

    return candidates


def install_from_github(
    url: str,
    bundle_root: Path,
    *,
    runner: Callable = run_captured_text,
    confirm: Callable[[str], bool],
) -> InstallResult:
    clone_url, subpath = parse_github_ref(url)
    temp_root = Path(tempfile.mkdtemp(prefix="devloop-install-"))
    clone_dir = temp_root / "clone"
    try:
        result = runner(["git", "clone", "--depth", "1", clone_url, str(clone_dir)])
        if result.returncode != 0:
            return InstallResult(
                installed=[],
                message=f"git clone failed: {result.stderr.strip() or clone_url}",
            )

        search_root = clone_dir / subpath if subpath else clone_dir
        candidates = find_candidates(search_root)
        if not candidates:
            return InstallResult(
                installed=[],
                message=(
                    "No skills or agents found. Skills are folders containing SKILL.md; "
                    "agents are .md files inside an agents/ directory."
                ),
            )

        listing = "\n".join(f"  {item.kind}: {item.name}" for item in candidates)
        if not confirm(f"Install the following into the Dev Loop bundle?\n{listing}"):
            return InstallResult(installed=[], message="Install cancelled.")

        installed: list[str] = []
        skipped: list[str] = []
        staging = bundle_root / ".install-tmp"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            for candidate in candidates:
                if candidate.kind == "skill":
                    dest = bundle_root / "skills" / "codex" / candidate.name
                else:
                    dest = bundle_root / "agents" / "codex" / f"{candidate.name}.md"
                if dest.exists():
                    skipped.append(f"{candidate.kind}:{candidate.name}")
                    continue

                stage = staging / f"{candidate.name}-{uuid.uuid4().hex}"
                if candidate.source.is_dir():
                    shutil.copytree(candidate.source, stage)
                else:
                    shutil.copy2(candidate.source, stage)
                dest.parent.mkdir(parents=True, exist_ok=True)
                stage.rename(dest)
                installed.append(f"{candidate.kind}:{candidate.name}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        message = f"Installed {len(installed)} item(s)."
        if skipped:
            message += f" Skipped (already exists): {', '.join(skipped)}."
        return InstallResult(installed=installed, message=message)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

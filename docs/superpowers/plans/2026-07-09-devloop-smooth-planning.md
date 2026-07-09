# Dev Loop Smooth Planning Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the embedded Codex TUI in `devloop-plan` with a devloop-owned chat loop (Alt+V image paste, `/options` agent/skill selection, always-visible stage banner) that flows from analysis into development/review/qa without any forced exit, with the self-improvement wiki always on.

**Architecture:** The chat loop shells out to `codex exec` / `codex exec resume <session-id>` per turn with plain streaming output; artifacts are detected by rescanning `prd/` after each turn (existing `find_artifacts`). Development invokes `devloop.cli.main()` in-process. Every coder/reviewer/qa session stays a clean `codex exec` (never `resume`).

**Tech Stack:** Python 3.10+ stdlib only (`subprocess`, `termios`/`tty` on POSIX, `ctypes`+`msvcrt` on Windows, `unittest`). Codex CLI ≥ 0.143.0 (`exec`, `exec resume`, `-i/--image`).

**Spec:** `docs/superpowers/specs/2026-07-09-devloop-smooth-planning-design.md`

## Global Constraints

- Python 3.10+ standard library only — no pip dependencies, ever.
- All new tests use stdlib `unittest`; run from bundle root: `PYTHONPATH=src python -m unittest discover -s tests -v` (Git Bash) or `$env:PYTHONPATH='src'; python -m unittest discover -s tests -v` (PowerShell).
- `bin/devloop-plan.ps1` and `bin/devloop-plan.sh` stay thin forwarders to `python -m devloop.interactive_runner`; any behavior change lands in Python so both platforms stay identical.
- The self-improvement wiki is ALWAYS enabled in the interactive flow (no question asked). `cli.py` keeps its `--self-improvement-wiki`/`--no-self-improvement-wiki` flags unchanged (default on) for standalone/CI use.
- Development sessions (coder/reviewer/qa) must remain clean `codex exec` invocations — never `resume`. Do not modify `codex_runner.py` session mechanics.
- Alt+V arrives as the two-character sequence `ESC` + `v` on both POSIX raw mode and Windows VT input mode; the parser is platform-independent.
- No new symbols in banner output when the stream cannot encode them — ASCII fallback required (`*`, `.`, `->`).
- Working directory for all commands below: `F:\devloop` (Git Bash path `/f/devloop`).
- Commit after every task with the exact message given in the task.

---

### Task 1: Stage pipeline UI (`statusui.py`)

**Files:**
- Create: `src/devloop/statusui.py`
- Test: `tests/test_statusui.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `Stage` enum (`ANALYSIS`, `DEVELOPMENT`, `REVIEW`, `QA`, values `"analysis"` etc.), `render_banner(stage: Stage, context: str = "", stream=None) -> str`, `stage_prompt(stage: Stage) -> str`. Tasks 6, 7, 8 import these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_statusui.py`:

```python
from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from devloop import statusui
from devloop.statusui import Stage


class FakeStream(io.StringIO):
    def __init__(self, *, encoding: str = "utf-8", tty: bool = True) -> None:
        super().__init__()
        self._encoding = encoding
        self._tty = tty

    @property
    def encoding(self) -> str:
        return self._encoding

    def isatty(self) -> bool:
        return self._tty


class StageTests(unittest.TestCase):
    def test_pipeline_order(self) -> None:
        self.assertEqual(
            [stage.value for stage in statusui.PIPELINE],
            ["analysis", "development", "review", "qa"],
        )


class RenderBannerTests(unittest.TestCase):
    def test_banner_marks_current_stage(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            banner = statusui.render_banner(Stage.ANALYSIS, stream=FakeStream())
        self.assertIn("analysis ●", banner)
        self.assertIn("development ○", banner)
        self.assertIn("review ○", banner)
        self.assertIn("qa ○", banner)
        self.assertNotIn("\x1b[", banner)

    def test_banner_includes_context(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            banner = statusui.render_banner(
                Stage.REVIEW, context="issue 2/5 · pass 1", stream=FakeStream()
            )
        self.assertIn("issue 2/5", banner)
        self.assertIn("review ●", banner)

    def test_ascii_fallback_when_stream_cannot_encode(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            banner = statusui.render_banner(
                Stage.QA, stream=FakeStream(encoding="ascii")
            )
        self.assertNotIn("●", banner)
        self.assertNotIn("→", banner)
        self.assertIn("qa *", banner)
        self.assertIn("->", banner)

    def test_color_used_only_on_tty_without_no_color(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
        with mock.patch.dict(os.environ, env, clear=True):
            colored = statusui.render_banner(Stage.ANALYSIS, stream=FakeStream(tty=True))
            plain = statusui.render_banner(Stage.ANALYSIS, stream=FakeStream(tty=False))
        self.assertIn("\x1b[", colored)
        self.assertNotIn("\x1b[", plain)


class StagePromptTests(unittest.TestCase):
    def test_prompt_names_stage(self) -> None:
        self.assertEqual(statusui.stage_prompt(Stage.ANALYSIS), "[analysis] > ")
        self.assertEqual(statusui.stage_prompt(Stage.QA), "[qa] > ")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_statusui -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'devloop.statusui'`

- [ ] **Step 3: Write minimal implementation**

Create `src/devloop/statusui.py`:

```python
from __future__ import annotations

import os
import sys
from enum import Enum


class Stage(Enum):
    ANALYSIS = "analysis"
    DEVELOPMENT = "development"
    REVIEW = "review"
    QA = "qa"


PIPELINE = [Stage.ANALYSIS, Stage.DEVELOPMENT, Stage.REVIEW, Stage.QA]

_ACTIVE_COLOR = "\x1b[1;36m"
_RESET = "\x1b[0m"
_BANNER_WIDTH = 79


def _stream(stream=None):
    return stream if stream is not None else sys.stdout


def _use_color(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    stream = _stream(stream)
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _can_encode(text: str, stream=None) -> bool:
    encoding = getattr(_stream(stream), "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def render_banner(stage: Stage, context: str = "", stream=None) -> str:
    unicode_ok = _can_encode("●○→·─", stream)
    active_marker = "●" if unicode_ok else "*"
    idle_marker = "○" if unicode_ok else "."
    arrow = " → " if unicode_ok else " -> "
    dot = " · " if unicode_ok else " - "
    rule_char = "─" if unicode_ok else "-"
    color = _use_color(stream)

    parts: list[str] = []
    for item in PIPELINE:
        marker = active_marker if item is stage else idle_marker
        label = f"{item.value} {marker}"
        if item is stage and color:
            label = f"{_ACTIVE_COLOR}{label}{_RESET}"
        parts.append(label)

    suffix = f"{dot}{context}" if context else ""
    line = f" devloop{dot}{arrow.join(parts)}{suffix} "
    rule = rule_char * _BANNER_WIDTH
    return f"{rule}\n{line}\n{rule}"


def stage_prompt(stage: Stage) -> str:
    return f"[{stage.value}] > "
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_statusui -v`
Expected: PASS (6 tests OK)

- [ ] **Step 5: Commit**

```bash
cd /f/devloop && git add src/devloop/statusui.py tests/test_statusui.py && git commit -m "Add stage pipeline banner and prompt rendering (statusui)"
```

---

### Task 2: Agent/skill catalog, selection, session preset (`catalog.py`)

**Files:**
- Create: `src/devloop/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Consumes: `devloop.templates.load_preset` (existing: `load_preset(path: Path) -> Preset` with `Preset(name, required_docs, roles)`).
- Produces (used by Tasks 5, 6, 7):
  - `CatalogEntry(name: str, path: Path, kind: str)` frozen dataclass, kind in `{"skill", "agent"}`.
  - `Catalog(skills: list[CatalogEntry], agents: list[CatalogEntry])`.
  - `discover(bundle_root: Path) -> Catalog`.
  - `DEFAULT_PLANNING_SKILLS = ("grill-with-docs", "domain-modeling", "to-prd", "to-issues")`.
  - `Selection(planning_skills: list[str], role_skills: dict[str, list[str]], role_agents: dict[str, list[str]])` with `to_dict()`, `Selection.from_dict(data) -> Selection`, `has_role_overrides() -> bool`.
  - `planning_skill_paths(selection: Selection, catalog: Catalog) -> list[Path]` (skips unknown names with a stderr warning).
  - `load_selection(state_path: Path) -> Selection` / `save_selection(state_path: Path, selection: Selection) -> None` (merges into the existing plan-state JSON, preserving other keys such as `target_repo`).
  - `write_session_preset(bundle_root: Path, selection: Selection, dest_path: Path) -> Path | None` (returns `None` when no role overrides; otherwise writes a preset JSON merging `presets/generic-minimal.json` with the overrides and returns `dest_path`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_catalog.py`:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from devloop import catalog
from devloop.catalog import Selection


def make_bundle(root: Path) -> None:
    (root / "skills" / "codex" / "grill-with-docs").mkdir(parents=True)
    (root / "skills" / "codex" / "grill-with-docs" / "SKILL.md").write_text("g", encoding="utf-8")
    (root / "skills" / "codex" / "tdd").mkdir(parents=True)
    (root / "skills" / "codex" / "tdd" / "SKILL.md").write_text("t", encoding="utf-8")
    (root / "skills" / "codex" / "not-a-skill").mkdir(parents=True)
    (root / "agents" / "codex").mkdir(parents=True)
    (root / "agents" / "codex" / "senior-code-reviewer.md").write_text("r", encoding="utf-8")
    (root / "presets").mkdir(parents=True)
    (root / "presets" / "generic-minimal.json").write_text(
        json.dumps(
            {
                "name": "generic-minimal",
                "requiredDocs": ["AGENTS.md"],
                "roles": {
                    "coder": {"skills": ["skills/codex/tdd/SKILL.md"], "agents": []},
                    "reviewer": {"skills": [], "agents": ["agents/codex/senior-code-reviewer.md"]},
                    "qa": {"skills": [], "agents": []},
                },
            }
        ),
        encoding="utf-8",
    )


class DiscoverTests(unittest.TestCase):
    def test_discovers_skills_and_agents(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_bundle(root)
            found = catalog.discover(root)
        self.assertEqual([entry.name for entry in found.skills], ["grill-with-docs", "tdd"])
        self.assertEqual([entry.name for entry in found.agents], ["senior-code-reviewer"])
        self.assertEqual(found.skills[0].kind, "skill")
        self.assertEqual(found.agents[0].kind, "agent")

    def test_missing_directories_yield_empty_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            found = catalog.discover(Path(raw))
        self.assertEqual(found.skills, [])
        self.assertEqual(found.agents, [])


class SelectionTests(unittest.TestCase):
    def test_defaults(self) -> None:
        selection = Selection.defaults()
        self.assertEqual(selection.planning_skills, list(catalog.DEFAULT_PLANNING_SKILLS))
        self.assertFalse(selection.has_role_overrides())

    def test_round_trip_dict(self) -> None:
        selection = Selection.defaults()
        selection.role_skills["coder"] = ["skills/codex/tdd/SKILL.md"]
        restored = Selection.from_dict(selection.to_dict())
        self.assertEqual(restored.role_skills, {"coder": ["skills/codex/tdd/SKILL.md"]})
        self.assertTrue(restored.has_role_overrides())

    def test_planning_skill_paths_skips_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_bundle(root)
            found = catalog.discover(root)
            selection = Selection(
                planning_skills=["grill-with-docs", "missing-skill"],
                role_skills={},
                role_agents={},
            )
            paths = catalog.planning_skill_paths(selection, found)
        self.assertEqual(len(paths), 1)
        self.assertTrue(str(paths[0]).endswith("SKILL.md"))


class PersistenceTests(unittest.TestCase):
    def test_save_preserves_other_state_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "devloop-plan.json"
            state_path.write_text(json.dumps({"target_repo": "C:/x"}), encoding="utf-8")
            selection = Selection.defaults()
            selection.planning_skills = ["grill-with-docs"]
            catalog.save_selection(state_path, selection)
            data = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(data["target_repo"], "C:/x")
            restored = catalog.load_selection(state_path)
        self.assertEqual(restored.planning_skills, ["grill-with-docs"])

    def test_load_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            restored = catalog.load_selection(Path(raw) / "nope.json")
        self.assertEqual(restored.planning_skills, list(catalog.DEFAULT_PLANNING_SKILLS))


class SessionPresetTests(unittest.TestCase):
    def test_no_overrides_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_bundle(root)
            dest = root / "session.preset.json"
            result = catalog.write_session_preset(root, Selection.defaults(), dest)
        self.assertIsNone(result)

    def test_overrides_merge_into_generic_preset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_bundle(root)
            selection = Selection.defaults()
            selection.role_skills["coder"] = ["skills/codex/grill-with-docs/SKILL.md"]
            dest = root / "session.preset.json"
            result = catalog.write_session_preset(root, selection, dest)
            data = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(result, dest)
        self.assertEqual(data["roles"]["coder"]["skills"], ["skills/codex/grill-with-docs/SKILL.md"])
        self.assertEqual(data["roles"]["reviewer"]["agents"], ["agents/codex/senior-code-reviewer.md"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_catalog -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'devloop.catalog'`

- [ ] **Step 3: Write minimal implementation**

Create `src/devloop/catalog.py`:

```python
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
        if isinstance(planning, list) and planning:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_catalog -v`
Expected: PASS (9 tests OK)

- [ ] **Step 5: Commit**

```bash
cd /f/devloop && git add src/devloop/catalog.py tests/test_catalog.py && git commit -m "Add agent/skill catalog with selection persistence and session presets"
```

---

### Task 3: Clipboard image capture (`clipboard.py`)

**Files:**
- Create: `src/devloop/clipboard.py`
- Test: `tests/test_clipboard.py`

**Interfaces:**
- Consumes: nothing from devloop (leaf module).
- Produces (used by Task 6): `capture_clipboard_image(dest_dir: Path, *, runner=None, platform_name: str | None = None) -> Path | None`. `runner` is `Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]]` — bytes mode, `capture_output=True`. Returns the written PNG path or `None` (no image / tool missing). Prints a one-line hint naming the missing tool when applicable.

- [ ] **Step 1: Write the failing test**

Create `tests/test_clipboard.py`:

```python
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from devloop import clipboard

PNG_BYTES = b"\x89PNG\r\n\x1a\nfakepng"


class FakeRunner:
    def __init__(self, responses: dict[str, tuple[int, bytes]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, command):
        self.calls.append([str(part) for part in command])
        program = Path(command[0]).name.lower()
        returncode, stdout = self.responses.get(program, (127, b""))
        if returncode == 127:
            raise FileNotFoundError(program)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=b"")


class WindowsCaptureTests(unittest.TestCase):
    def test_success_invokes_windows_powershell_and_returns_path(self) -> None:
        def runner(command):
            dest = Path(command[-1])
            dest.write_bytes(PNG_BYTES)
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

        with tempfile.TemporaryDirectory() as raw:
            result = clipboard.capture_clipboard_image(
                Path(raw), runner=runner, platform_name="win32"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.suffix, ".png")

    def test_windows_command_uses_powershell_get_clipboard(self) -> None:
        fake = FakeRunner({"powershell.exe": (1, b"")})
        with tempfile.TemporaryDirectory() as raw:
            result = clipboard.capture_clipboard_image(
                Path(raw), runner=fake, platform_name="win32"
            )
        self.assertIsNone(result)
        self.assertEqual(Path(fake.calls[0][0]).name.lower(), "powershell.exe")
        joined = " ".join(fake.calls[0])
        self.assertIn("Get-Clipboard", joined)
        self.assertIn("-Format Image", joined)


class LinuxCaptureTests(unittest.TestCase):
    def test_wl_paste_stdout_written_to_file(self) -> None:
        fake = FakeRunner({"wl-paste": (0, PNG_BYTES)})
        with tempfile.TemporaryDirectory() as raw:
            result = clipboard.capture_clipboard_image(
                Path(raw), runner=fake, platform_name="linux"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.read_bytes(), PNG_BYTES)
        self.assertEqual(fake.calls[0][:2], ["wl-paste", "--type"])

    def test_falls_back_to_xclip_when_wl_paste_missing(self) -> None:
        fake = FakeRunner({"xclip": (0, PNG_BYTES)})
        with tempfile.TemporaryDirectory() as raw:
            result = clipboard.capture_clipboard_image(
                Path(raw), runner=fake, platform_name="linux"
            )
        self.assertIsNotNone(result)
        self.assertEqual(fake.calls[-1][0], "xclip")
        self.assertIn("image/png", fake.calls[-1])

    def test_no_tools_returns_none(self) -> None:
        fake = FakeRunner({})
        with tempfile.TemporaryDirectory() as raw:
            result = clipboard.capture_clipboard_image(
                Path(raw), runner=fake, platform_name="linux"
            )
        self.assertIsNone(result)

    def test_empty_clipboard_returns_none(self) -> None:
        fake = FakeRunner({"wl-paste": (1, b"")})
        with tempfile.TemporaryDirectory() as raw:
            result = clipboard.capture_clipboard_image(
                Path(raw), runner=fake, platform_name="linux"
            )
        self.assertIsNone(result)


class MacCaptureTests(unittest.TestCase):
    def test_pngpaste_writes_dest(self) -> None:
        def runner(command):
            if Path(command[0]).name != "pngpaste":
                raise FileNotFoundError(command[0])
            Path(command[-1]).write_bytes(PNG_BYTES)
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

        with tempfile.TemporaryDirectory() as raw:
            result = clipboard.capture_clipboard_image(
                Path(raw), runner=runner, platform_name="darwin"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.read_bytes(), PNG_BYTES)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_clipboard -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'devloop.clipboard'`

- [ ] **Step 3: Write minimal implementation**

Create `src/devloop/clipboard.py`:

```python
from __future__ import annotations

import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[bytes]"]

_WINDOWS_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName System.Windows.Forms; "
    "$img = Get-Clipboard -Format Image; "
    "if ($null -eq $img) {{ exit 1 }}; "
    "$img.Save('{dest}', [System.Drawing.Imaging.ImageFormat]::Png); "
    "exit 0"
)


def _default_runner(command: Sequence[str]) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(list(command), capture_output=True, check=False)


def capture_clipboard_image(
    dest_dir: Path,
    *,
    runner: Runner | None = None,
    platform_name: str | None = None,
) -> Path | None:
    runner = runner or _default_runner
    platform_name = platform_name or sys.platform
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"clipboard-{uuid.uuid4().hex}.png"

    if platform_name.startswith("win"):
        return _capture_windows(dest, runner)
    if platform_name.startswith("darwin"):
        return _capture_macos(dest, runner)
    return _capture_linux(dest, runner)


def _capture_windows(dest: Path, runner: Runner) -> Path | None:
    script = _WINDOWS_SCRIPT.format(dest=str(dest).replace("'", "''"))
    command = ["powershell.exe", "-NoProfile", "-Command", script]
    try:
        result = runner(command)
    except FileNotFoundError:
        print("Clipboard capture needs powershell.exe on PATH.", file=sys.stderr)
        return None
    if result.returncode != 0 or not dest.is_file():
        return None
    return dest


def _capture_linux(dest: Path, runner: Runner) -> Path | None:
    attempts = [
        ["wl-paste", "--type", "image/png"],
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
    ]
    missing: list[str] = []
    for command in attempts:
        try:
            result = runner(command)
        except FileNotFoundError:
            missing.append(command[0])
            continue
        if result.returncode == 0 and result.stdout:
            dest.write_bytes(result.stdout)
            return dest
    if len(missing) == len(attempts):
        print(
            "Clipboard capture needs wl-paste (Wayland) or xclip (X11) installed.",
            file=sys.stderr,
        )
    return None


def _capture_macos(dest: Path, runner: Runner) -> Path | None:
    try:
        result = runner(["pngpaste", str(dest)])
        if result.returncode == 0 and dest.is_file():
            return dest
    except FileNotFoundError:
        pass

    try:
        result = runner(["osascript", "-e", "the clipboard as «class PNGf»"])
    except FileNotFoundError:
        print("Clipboard capture needs pngpaste (brew install pngpaste).", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    match = re.search(rb"data PNGf([0-9A-Fa-f]+)", result.stdout)
    if not match:
        return None
    dest.write_bytes(bytes.fromhex(match.group(1).decode("ascii")))
    return dest
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_clipboard -v`
Expected: PASS (7 tests OK)

- [ ] **Step 5: Commit**

```bash
cd /f/devloop && git add src/devloop/clipboard.py tests/test_clipboard.py && git commit -m "Add cross-platform clipboard image capture"
```

---

### Task 4: Raw-mode line editor with Alt+V hook (`lineeditor.py`)

**Files:**
- Create: `src/devloop/lineeditor.py`
- Test: `tests/test_lineeditor.py`

**Interfaces:**
- Consumes: nothing from devloop (leaf module).
- Produces (used by Task 6): `LineEditor(on_paste_image: Callable[[], str | None], write: Callable[[str], None] | None = None)` with:
  - `read_line(prompt: str) -> str` — real-terminal entry point; falls back to `input(prompt)` when raw mode is unavailable (non-TTY or setup error) after printing a one-time `/paste` hint.
  - `feed(prompt: str, chars: Iterable[str]) -> str` — test entry point running the same parser over synthetic characters.
  - Alt+V (`ESC` + `v`) calls `on_paste_image`; a returned string is inserted at the cursor; `None` inserts nothing.
  - Supports: printable insert, backspace (`\x7f`/`\x08`), left/right arrows (`ESC [ D` / `ESC [ C`), up/down history (`ESC [ A` / `ESC [ B`), Enter (`\r`/`\n`), Ctrl+C → `KeyboardInterrupt`, Ctrl+D on empty buffer → `EOFError`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_lineeditor.py`:

```python
from __future__ import annotations

import unittest

from devloop.lineeditor import LineEditor


def editor(paste_result: str | None = "[image 1 attached] ") -> LineEditor:
    return LineEditor(on_paste_image=lambda: paste_result, write=lambda text: None)


class TypingTests(unittest.TestCase):
    def test_plain_text_and_enter(self) -> None:
        line = editor().feed("> ", list("hello") + ["\r"])
        self.assertEqual(line, "hello")

    def test_backspace_removes_before_cursor(self) -> None:
        line = editor().feed("> ", list("heyy") + ["\x7f", "\r"])
        self.assertEqual(line, "hey")

    def test_left_arrow_then_insert(self) -> None:
        keys = list("ac") + ["\x1b", "[", "D", "b", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "abc")


class AltVTests(unittest.TestCase):
    def test_alt_v_inserts_paste_token(self) -> None:
        keys = list("see ") + ["\x1b", "v"] + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "see [image 1 attached] ")

    def test_alt_v_with_no_image_inserts_nothing(self) -> None:
        keys = list("ok") + ["\x1b", "v", "\r"]
        line = editor(paste_result=None).feed("> ", keys)
        self.assertEqual(line, "ok")


class HistoryTests(unittest.TestCase):
    def test_up_arrow_recalls_previous_line(self) -> None:
        ed = editor()
        ed.feed("> ", list("first") + ["\r"])
        line = ed.feed("> ", ["\x1b", "[", "A", "\r"])
        self.assertEqual(line, "first")

    def test_down_arrow_restores_stash(self) -> None:
        ed = editor()
        ed.feed("> ", list("first") + ["\r"])
        keys = list("dra") + ["\x1b", "[", "A", "\x1b", "[", "B", "ft", "\r"]
        # up recalls "first", down restores the stashed draft "dra"
        line = ed.feed("> ", [key for key in keys])
        self.assertEqual(line, "draft")


class ControlTests(unittest.TestCase):
    def test_ctrl_c_raises_keyboard_interrupt(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            editor().feed("> ", ["\x03"])

    def test_ctrl_d_on_empty_raises_eof(self) -> None:
        with self.assertRaises(EOFError):
            editor().feed("> ", ["\x04"])

    def test_exhausted_keys_raise_eof(self) -> None:
        with self.assertRaises(EOFError):
            editor().feed("> ", list("abc"))


if __name__ == "__main__":
    unittest.main()
```

Note on `test_down_arrow_restores_stash`: the key list interleaves multi-character strings (`"ft"`); the editor consumes an *iterable of single characters*, so `feed` must flatten multi-char strings. Make `feed` iterate characters of each item.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_lineeditor -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'devloop.lineeditor'`

- [ ] **Step 3: Write minimal implementation**

Create `src/devloop/lineeditor.py`:

```python
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

PasteHook = Callable[[], "str | None"]

ESC = "\x1b"
CTRL_C = "\x03"
CTRL_D = "\x04"
ENTER_KEYS = {"\r", "\n"}
BACKSPACE_KEYS = {"\x7f", "\x08"}


@dataclass
class _EditorState:
    buffer: list[str] = field(default_factory=list)
    cursor: int = 0
    history_index: int | None = None
    stash: str = ""

    def text(self) -> str:
        return "".join(self.buffer)

    def set_text(self, text: str) -> None:
        self.buffer = list(text)
        self.cursor = len(self.buffer)


class _KeySource:
    def read(self) -> str | None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class _IteratorKeySource(_KeySource):
    def __init__(self, chars: Iterator[str]) -> None:
        self._iter = chars

    def read(self) -> str | None:
        return next(self._iter, None)


class _PosixKeySource(_KeySource):
    def __init__(self) -> None:
        import termios
        import tty

        self._termios = termios
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)

    def read(self) -> str | None:
        char = sys.stdin.read(1)
        return char or None

    def close(self) -> None:
        self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._old)


class _WindowsKeySource(_KeySource):
    def __init__(self) -> None:
        import msvcrt  # noqa: F401  (import check)

        self._restore = _enable_windows_vt_modes()

    def read(self) -> str | None:
        import msvcrt

        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            msvcrt.getwch()
            return ""
        return char

    def close(self) -> None:
        if self._restore is not None:
            self._restore()


def _enable_windows_vt_modes() -> Callable[[], None] | None:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    STD_INPUT_HANDLE = -10
    STD_OUTPUT_HANDLE = -11

    handles: list[tuple[int, int]] = []
    for std_handle, flag in (
        (STD_INPUT_HANDLE, ENABLE_VIRTUAL_TERMINAL_INPUT),
        (STD_OUTPUT_HANDLE, ENABLE_VIRTUAL_TERMINAL_PROCESSING),
    ):
        handle = kernel32.GetStdHandle(std_handle)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            raise OSError("GetConsoleMode failed")
        handles.append((handle, mode.value))
        if not kernel32.SetConsoleMode(handle, mode.value | flag):
            raise OSError("SetConsoleMode failed")

    def restore() -> None:
        for handle, previous in handles:
            kernel32.SetConsoleMode(handle, previous)

    return restore


def _make_key_source() -> _KeySource | None:
    stdin_isatty = getattr(sys.stdin, "isatty", None)
    if not (stdin_isatty and stdin_isatty()):
        return None
    try:
        if sys.platform.startswith("win"):
            return _WindowsKeySource()
        return _PosixKeySource()
    except Exception:
        return None


def _default_write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


class LineEditor:
    """Minimal raw-mode line editor with an Alt+V (ESC+v) paste hook.

    Windows VT input mode and POSIX raw mode both deliver Alt+V as ESC
    followed by "v", so a single parser serves both platforms.
    """

    def __init__(
        self,
        *,
        on_paste_image: PasteHook,
        write: Callable[[str], None] | None = None,
    ) -> None:
        self.on_paste_image = on_paste_image
        self._write = write or _default_write
        self.history: list[str] = []
        self._fallback_hint_shown = False

    def read_line(self, prompt: str) -> str:
        keys = _make_key_source()
        if keys is None:
            if not self._fallback_hint_shown:
                self._fallback_hint_shown = True
                print("(Alt+V unavailable in this terminal; use /paste instead.)")
            line = input(prompt)
            if line.strip():
                self.history.append(line)
            return line

        self._write(prompt)
        try:
            line = self._edit(prompt, keys)
        finally:
            keys.close()
        if line.strip():
            self.history.append(line)
        return line

    def feed(self, prompt: str, chars: Iterable[str]) -> str:
        flattened = (char for item in chars for char in item)
        line = self._edit(prompt, _IteratorKeySource(flattened))
        if line.strip():
            self.history.append(line)
        return line

    def _edit(self, prompt: str, keys: _KeySource) -> str:
        state = _EditorState()
        while True:
            char = keys.read()
            if char is None:
                raise EOFError
            if char == "":
                continue
            if char == CTRL_C:
                raise KeyboardInterrupt
            if char == CTRL_D and not state.buffer:
                raise EOFError
            if char in ENTER_KEYS:
                self._write("\n")
                return state.text()
            if char == ESC:
                self._handle_escape(state, keys)
            elif char in BACKSPACE_KEYS:
                if state.cursor > 0:
                    state.cursor -= 1
                    del state.buffer[state.cursor]
            elif char.isprintable():
                state.buffer.insert(state.cursor, char)
                state.cursor += 1
            self._render(prompt, state)

    def _handle_escape(self, state: _EditorState, keys: _KeySource) -> None:
        follow = keys.read()
        if follow in ("v", "V"):
            token = self.on_paste_image()
            if token:
                for char in token:
                    state.buffer.insert(state.cursor, char)
                    state.cursor += 1
            return
        if follow != "[":
            return
        final = keys.read()
        if final == "D" and state.cursor > 0:
            state.cursor -= 1
        elif final == "C" and state.cursor < len(state.buffer):
            state.cursor += 1
        elif final == "A":
            self._history_previous(state)
        elif final == "B":
            self._history_next(state)
        elif final == "H":
            state.cursor = 0
        elif final == "F":
            state.cursor = len(state.buffer)

    def _history_previous(self, state: _EditorState) -> None:
        if not self.history:
            return
        if state.history_index is None:
            state.stash = state.text()
            state.history_index = len(self.history) - 1
        elif state.history_index > 0:
            state.history_index -= 1
        state.set_text(self.history[state.history_index])

    def _history_next(self, state: _EditorState) -> None:
        if state.history_index is None:
            return
        if state.history_index < len(self.history) - 1:
            state.history_index += 1
            state.set_text(self.history[state.history_index])
        else:
            state.history_index = None
            state.set_text(state.stash)

    def _render(self, prompt: str, state: _EditorState) -> None:
        text = state.text()
        self._write(f"\r\x1b[K{prompt}{text}")
        back = len(state.buffer) - state.cursor
        if back > 0:
            self._write(f"\x1b[{back}D")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_lineeditor -v`
Expected: PASS (10 tests OK)

- [ ] **Step 5: Commit**

```bash
cd /f/devloop && git add src/devloop/lineeditor.py tests/test_lineeditor.py && git commit -m "Add raw-mode line editor with Alt+V paste hook and history"
```

---

### Task 5: GitHub skill/agent installer (`github_install.py`)

**Files:**
- Create: `src/devloop/github_install.py`
- Test: `tests/test_github_install.py`

**Interfaces:**
- Consumes: `devloop.subprocess_utils.run_captured_text` (existing: `run_captured_text(command, *, cwd=None, input_text=None) -> CompletedProcess[str]`).
- Produces (used by Task 7):
  - `InstallCandidate(kind: str, name: str, source: Path)`.
  - `InstallResult(installed: list[str], message: str)`.
  - `parse_github_ref(url: str) -> tuple[str, str]` — splits optional `#subpath`.
  - `install_from_github(url: str, bundle_root: Path, *, runner=run_captured_text, confirm: Callable[[str], bool]) -> InstallResult`. Skills land in `skills/codex/<name>/`, agents in `agents/codex/<name>.md`; existing names are skipped with a message; staging goes through `<bundle_root>/.install-tmp/` then `Path.rename` (same filesystem, atomic).

- [ ] **Step 1: Write the failing test**

Create `tests/test_github_install.py`:

```python
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from devloop import github_install


def fake_git_runner(populate):
    def runner(command, *, cwd=None, input_text=None):
        if command[0] == "git" and command[1] == "clone":
            clone_dir = Path(command[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            populate(clone_dir)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="unexpected")

    return runner


def repo_with_skill_and_agent(clone_dir: Path) -> None:
    skill = clone_dir / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    (skill / "extra.md").write_text("extra", encoding="utf-8")
    agents = clone_dir / "agents"
    agents.mkdir(parents=True)
    (agents / "my-agent.md").write_text("agent", encoding="utf-8")


class ParseRefTests(unittest.TestCase):
    def test_plain_url(self) -> None:
        self.assertEqual(
            github_install.parse_github_ref("https://github.com/o/r"),
            ("https://github.com/o/r", ""),
        )

    def test_url_with_subpath(self) -> None:
        self.assertEqual(
            github_install.parse_github_ref("https://github.com/o/r#skills/my-skill"),
            ("https://github.com/o/r", "skills/my-skill"),
        )


class InstallTests(unittest.TestCase):
    def test_installs_skill_and_agent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            result = github_install.install_from_github(
                "https://github.com/o/r",
                bundle,
                runner=fake_git_runner(repo_with_skill_and_agent),
                confirm=lambda message: True,
            )
            self.assertEqual(sorted(result.installed), ["agent:my-agent", "skill:my-skill"])
            self.assertTrue((bundle / "skills" / "codex" / "my-skill" / "SKILL.md").is_file())
            self.assertTrue((bundle / "skills" / "codex" / "my-skill" / "extra.md").is_file())
            self.assertTrue((bundle / "agents" / "codex" / "my-agent.md").is_file())
            self.assertFalse((bundle / ".install-tmp").exists())

    def test_existing_skill_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            existing = bundle / "skills" / "codex" / "my-skill"
            existing.mkdir(parents=True)
            (existing / "SKILL.md").write_text("old", encoding="utf-8")
            result = github_install.install_from_github(
                "https://github.com/o/r",
                bundle,
                runner=fake_git_runner(repo_with_skill_and_agent),
                confirm=lambda message: True,
            )
            self.assertEqual(result.installed, ["agent:my-agent"])
            self.assertIn("already exists", result.message)
            self.assertEqual(
                (existing / "SKILL.md").read_text(encoding="utf-8"), "old"
            )

    def test_declined_confirmation_installs_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            result = github_install.install_from_github(
                "https://github.com/o/r",
                bundle,
                runner=fake_git_runner(repo_with_skill_and_agent),
                confirm=lambda message: False,
            )
            self.assertEqual(result.installed, [])
            self.assertFalse((bundle / "skills" / "codex" / "my-skill").exists())

    def test_no_candidates_reports_message(self) -> None:
        def populate(clone_dir: Path) -> None:
            (clone_dir / "README.md").write_text("nothing", encoding="utf-8")

        with tempfile.TemporaryDirectory() as raw:
            result = github_install.install_from_github(
                "https://github.com/o/r",
                Path(raw),
                runner=fake_git_runner(populate),
                confirm=lambda message: True,
            )
            self.assertEqual(result.installed, [])
            self.assertIn("No skills or agents found", result.message)

    def test_clone_failure_reports_message(self) -> None:
        def runner(command, *, cwd=None, input_text=None):
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="fatal: not found")

        with tempfile.TemporaryDirectory() as raw:
            result = github_install.install_from_github(
                "https://github.com/o/missing",
                Path(raw),
                runner=runner,
                confirm=lambda message: True,
            )
            self.assertEqual(result.installed, [])
            self.assertIn("git clone failed", result.message)

    def test_subpath_limits_search(self) -> None:
        def populate(clone_dir: Path) -> None:
            repo_with_skill_and_agent(clone_dir)
            other = clone_dir / "other" / "other-skill"
            other.mkdir(parents=True)
            (other / "SKILL.md").write_text("other", encoding="utf-8")

        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            result = github_install.install_from_github(
                "https://github.com/o/r#skills",
                bundle,
                runner=fake_git_runner(populate),
                confirm=lambda message: True,
            )
            self.assertEqual(result.installed, ["skill:my-skill"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_github_install -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'devloop.github_install'`

- [ ] **Step 3: Write minimal implementation**

Create `src/devloop/github_install.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_github_install -v`
Expected: PASS (8 tests OK)

- [ ] **Step 5: Commit**

```bash
cd /f/devloop && git add src/devloop/github_install.py tests/test_github_install.py && git commit -m "Add GitHub skill/agent installer with staging and confirmation"
```

---

### Task 6: Chat loop over codex exec resume (`chat_loop.py`)

**Files:**
- Create: `src/devloop/chat_loop.py`
- Test: `tests/test_chat_loop.py`

**Interfaces:**
- Consumes: `statusui.Stage`, `statusui.render_banner`, `statusui.stage_prompt` (Task 1); `LineEditor` (Task 4); `capture_clipboard_image` (Task 3).
- Produces (used by Task 7):
  - `ChatConfig(codex: str, repo_root: Path, bundle_root: Path, sandbox: str = "workspace-write", approval_policy: str = "never")`.
  - `ChatCallbacks(probe_artifacts: Callable[[], Any | None], manual_artifacts: Callable[[], Any | None], open_options: Callable[[], None], status_summary: Callable[[], str])`.
  - `run_planning_chat(*, config, initial_prompt, callbacks, turn_runner=run_streaming, editor=None, capture_image=capture_clipboard_image) -> Any | None` — returns whatever `probe_artifacts`/`manual_artifacts` return (the caller's `PlanningArtifacts`), or `None` on abort.
  - `parse_session_id(output: str) -> str | None`, `detect_image_paths(message: str) -> list[Path]`, `run_streaming(command, cwd) -> tuple[int, str]`.
  - `TurnRunner = Callable[[Sequence[str], Path], tuple[int, str]]`.

- [ ] **Step 1: Verify `codex exec resume` accepts exec options (real CLI check)**

Run: `codex exec resume --help | head -30`
Expected: usage shows `codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]` (or equivalent) including sandbox/config options inherited from `exec`. If `-C`/`--add-dir`/`-s`/`-c`/`-i` are NOT listed for the resume subcommand, note the actual supported flags and place shared options accordingly when implementing `build_turn_command` (options accepted by `resume` stay after `resume`; anything unsupported must be dropped from resume turns only — the session already carries cwd and sandbox).

- [ ] **Step 2: Write the failing test**

Create `tests/test_chat_loop.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devloop import chat_loop
from devloop.chat_loop import ChatCallbacks, ChatConfig, ChatSession


class ParseSessionIdTests(unittest.TestCase):
    def test_finds_uuid_on_session_line(self) -> None:
        output = "workdir: /x\nsession id: 0198c0de-1111-2222-3333-444455556666\nmodel: gpt-5\n"
        self.assertEqual(
            chat_loop.parse_session_id(output),
            "0198c0de-1111-2222-3333-444455556666",
        )

    def test_ignores_uuid_on_unrelated_line(self) -> None:
        output = "request id 0198c0de-1111-2222-3333-444455556666\n"
        self.assertIsNone(chat_loop.parse_session_id(output))

    def test_none_when_absent(self) -> None:
        self.assertIsNone(chat_loop.parse_session_id("no ids here"))


class DetectImagePathsTests(unittest.TestCase):
    def test_detects_existing_image_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            image = Path(raw) / "shot.png"
            image.write_bytes(b"png")
            found = chat_loop.detect_image_paths(f"see {image} please")
        self.assertEqual(found, [image.resolve()])

    def test_ignores_missing_files_and_non_images(self) -> None:
        self.assertEqual(chat_loop.detect_image_paths("no /tmp/missing.png here"), [])
        self.assertEqual(chat_loop.detect_image_paths("read docs/readme.md now"), [])


class BuildTurnCommandTests(unittest.TestCase):
    def make_session(self) -> ChatSession:
        config = ChatConfig(
            codex="codex",
            repo_root=Path("C:/repo"),
            bundle_root=Path("F:/devloop"),
        )
        return ChatSession(config=config)

    def test_first_turn_uses_plain_exec_with_prompt(self) -> None:
        session = self.make_session()
        command = session.build_turn_command("", first_prompt="PLAN PROMPT")
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertNotIn("resume", command)
        self.assertEqual(command[-1], "PLAN PROMPT")
        self.assertIn("--add-dir", command)
        self.assertIn("-s", command)

    def test_resume_turn_uses_session_id(self) -> None:
        session = self.make_session()
        session.session_id = "0198c0de-1111-2222-3333-444455556666"
        command = session.build_turn_command("next message")
        self.assertEqual(command[:4], ["codex", "exec", "resume", session.session_id])
        self.assertEqual(command[-1], "next message")

    def test_resume_without_id_falls_back_to_last(self) -> None:
        session = self.make_session()
        command = session.build_turn_command("next message")
        self.assertEqual(command[:4], ["codex", "exec", "resume", "--last"])

    def test_pending_images_added_as_image_flags(self) -> None:
        session = self.make_session()
        session.session_id = "0198c0de-1111-2222-3333-444455556666"
        session.pending_images = [Path("C:/tmp/a.png"), Path("C:/tmp/b.png")]
        command = session.build_turn_command("with images")
        self.assertEqual(command.count("-i"), 2)
        self.assertIn("C:/tmp/a.png", [part.replace("\\", "/") for part in command])


class FakeEditor:
    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)

    def read_line(self, prompt: str) -> str:
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


class RunPlanningChatTests(unittest.TestCase):
    def make_config(self, repo: Path) -> ChatConfig:
        return ChatConfig(codex="codex", repo_root=repo, bundle_root=repo / "bundle")

    def test_returns_artifacts_when_probe_finds_them(self) -> None:
        turns: list[list[str]] = []
        artifacts_box = {"ready": False}

        def turn_runner(command, cwd):
            turns.append(list(command))
            artifacts_box["ready"] = True
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "ARTIFACTS" if artifacts_box["ready"] else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor([]),
            )
        self.assertEqual(result, "ARTIFACTS")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0][-1], "PLAN")

    def test_user_message_sent_as_resume_turn(self) -> None:
        turns: list[list[str]] = []
        state = {"count": 0}

        def turn_runner(command, cwd):
            turns.append(list(command))
            state["count"] += 1
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\nok\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "DONE" if state["count"] >= 2 else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["build a login page"]),
            )
        self.assertEqual(result, "DONE")
        self.assertEqual(turns[1][2], "resume")
        self.assertEqual(turns[1][-1], "build a login page")

    def test_quit_returns_none(self) -> None:
        def turn_runner(command, cwd):
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["/quit", "y"]),
            )
        self.assertIsNone(result)

    def test_failed_turn_keeps_loop_alive(self) -> None:
        state = {"count": 0}

        def turn_runner(command, cwd):
            state["count"] += 1
            if state["count"] == 2:
                return 1, "boom"
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: "OK" if state["count"] >= 3 else None,
            manual_artifacts=lambda: None,
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["first try", "second try"]),
            )
        self.assertEqual(result, "OK")

    def test_done_command_uses_manual_fallback(self) -> None:
        def turn_runner(command, cwd):
            return 0, "session id: 0198c0de-1111-2222-3333-444455556666\n"

        callbacks = ChatCallbacks(
            probe_artifacts=lambda: None,
            manual_artifacts=lambda: "MANUAL",
            open_options=lambda: None,
            status_summary=lambda: "status",
        )
        with tempfile.TemporaryDirectory() as raw:
            result = chat_loop.run_planning_chat(
                config=self.make_config(Path(raw)),
                initial_prompt="PLAN",
                callbacks=callbacks,
                turn_runner=turn_runner,
                editor=FakeEditor(["/done"]),
            )
        self.assertEqual(result, "MANUAL")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_chat_loop -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'devloop.chat_loop'`

- [ ] **Step 4: Write minimal implementation**

Create `src/devloop/chat_loop.py`:

```python
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import statusui
from .clipboard import capture_clipboard_image
from .lineeditor import LineEditor
from .statusui import Stage

TurnRunner = Callable[[Sequence[str], Path], "tuple[int, str]"]

UUID_PATTERN = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

HELP_TEXT = """Commands:
  Alt+V    attach a screenshot from the clipboard (use /paste if unavailable)
  /paste   attach a screenshot from the clipboard
  /options open agent/skill and development options
  /status  show the stage banner, artifacts, and selection summary
  /done    detect the PRD and issue pack now (or enter paths manually)
  /help    show this help
  /quit    abort planning (never required to continue)"""


@dataclass
class ChatConfig:
    codex: str
    repo_root: Path
    bundle_root: Path
    sandbox: str = "workspace-write"
    approval_policy: str = "never"


@dataclass
class ChatCallbacks:
    probe_artifacts: Callable[[], Any | None]
    manual_artifacts: Callable[[], Any | None]
    open_options: Callable[[], None]
    status_summary: Callable[[], str]


@dataclass
class ChatSession:
    config: ChatConfig
    session_id: str | None = None
    started: bool = False
    pending_images: list[Path] = field(default_factory=list)
    image_counter: int = 0

    def build_turn_command(self, message: str, first_prompt: str | None = None) -> list[str]:
        command: list[str] = [self.config.codex, "exec"]
        if first_prompt is None:
            command.append("resume")
            if self.session_id:
                command.append(self.session_id)
            else:
                command.append("--last")
        command.extend(
            [
                "-C",
                str(self.config.repo_root),
                "--add-dir",
                str(self.config.bundle_root),
                "-s",
                self.config.sandbox,
                "-c",
                f'approval_policy="{self.config.approval_policy}"',
                "--skip-git-repo-check",
            ]
        )
        for image in self.pending_images:
            command.extend(["-i", str(image)])
        command.append(first_prompt if first_prompt is not None else message)
        return command


def parse_session_id(output: str) -> str | None:
    for line in output.splitlines():
        if "session" in line.lower():
            match = UUID_PATTERN.search(line)
            if match:
                return match.group(1)
    return None


def detect_image_paths(message: str) -> list[Path]:
    found: list[Path] = []
    for token in re.split(r"[\s\"']+", message):
        if not token or Path(token).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        candidate = Path(token).expanduser()
        if candidate.is_file():
            found.append(candidate.resolve())
    return found


def run_streaming(command: Sequence[str], cwd: Path) -> tuple[int, str]:
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    process.wait()
    return process.returncode, "".join(captured)


def run_planning_chat(
    *,
    config: ChatConfig,
    initial_prompt: str,
    callbacks: ChatCallbacks,
    turn_runner: TurnRunner = run_streaming,
    editor: Any | None = None,
    capture_image: Callable[[Path], Path | None] = capture_clipboard_image,
) -> Any | None:
    session = ChatSession(config=config)
    image_dir = Path(tempfile.mkdtemp(prefix="devloop-images-"))

    def paste_hook() -> str | None:
        image = capture_image(image_dir)
        if image is None:
            print("\nNo image found on the clipboard.")
            return None
        session.pending_images.append(image)
        session.image_counter += 1
        return f"[image {session.image_counter} attached] "

    if editor is None:
        editor = LineEditor(on_paste_image=paste_hook)

    print(statusui.render_banner(Stage.ANALYSIS))
    print("Describe the change. Type /help for commands; Alt+V pastes a screenshot.")

    returncode, output = _run_turn(session, turn_runner, first_prompt=initial_prompt)
    if returncode == 0:
        session.started = True
    else:
        print(
            f"Codex could not start (exit {returncode}). "
            "Your next message will retry the planning session.",
            file=sys.stderr,
        )

    while True:
        artifacts = callbacks.probe_artifacts()
        if artifacts is not None:
            print("\nPRD and issue pack detected; continuing to development.")
            return artifacts

        # The banner stays visible: reprint it before every input prompt so the
        # current stage survives any amount of scrolled Codex output.
        print(statusui.render_banner(Stage.ANALYSIS))
        try:
            line = editor.read_line(statusui.stage_prompt(Stage.ANALYSIS))
        except EOFError:
            return None
        except KeyboardInterrupt:
            if _confirm_abort(editor):
                return None
            continue

        text = line.strip()
        if not text:
            continue

        if text.startswith("/"):
            handled, result, finished = _handle_command(
                text, session, callbacks, editor, paste_hook
            )
            if finished:
                return result
            if handled:
                continue

        for image in detect_image_paths(text):
            if image not in session.pending_images:
                session.pending_images.append(image)

        if not session.started:
            returncode, output = _run_turn(session, turn_runner, first_prompt=initial_prompt)
            if returncode == 0:
                session.started = True
            else:
                continue
            # The goal text the user just typed still needs to reach Codex.
            returncode, output = _run_turn(session, turn_runner, message=text)
        else:
            returncode, output = _run_turn(session, turn_runner, message=text)

        if returncode != 0:
            print(
                f"Codex turn failed (exit {returncode}). Retry, rephrase, or /quit.",
                file=sys.stderr,
            )
            continue
        session.pending_images.clear()


def _run_turn(
    session: ChatSession,
    turn_runner: TurnRunner,
    *,
    message: str = "",
    first_prompt: str | None = None,
) -> tuple[int, str]:
    command = session.build_turn_command(message, first_prompt=first_prompt)
    returncode, output = turn_runner(command, session.config.repo_root)
    if returncode == 0 and session.session_id is None:
        session.session_id = parse_session_id(output)
    return returncode, output


def _handle_command(
    text: str,
    session: ChatSession,
    callbacks: ChatCallbacks,
    editor: Any,
    paste_hook: Callable[[], str | None],
) -> tuple[bool, Any | None, bool]:
    """Returns (handled, result, finished)."""
    command = text.split()[0].lower()
    if command == "/help":
        print(HELP_TEXT)
        return True, None, False
    if command == "/status":
        print(statusui.render_banner(Stage.ANALYSIS))
        print(callbacks.status_summary())
        if session.pending_images:
            print(f"Pending images: {len(session.pending_images)}")
        return True, None, False
    if command == "/options":
        callbacks.open_options()
        return True, None, False
    if command == "/paste":
        token = paste_hook()
        if token:
            print(f"Attached. Include it in your next message: {token.strip()}")
        return True, None, False
    if command == "/done":
        artifacts = callbacks.probe_artifacts()
        if artifacts is None:
            artifacts = callbacks.manual_artifacts()
        if artifacts is None:
            print("No artifacts selected; continuing the planning chat.")
            return True, None, False
        return True, artifacts, True
    if command == "/quit":
        if _confirm_abort(editor):
            return True, None, True
        return True, None, False
    print(f"Unknown command: {command}. Type /help for commands.")
    return True, None, False


def _confirm_abort(editor: Any) -> bool:
    try:
        answer = editor.read_line("Abort planning? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return True
    return answer.strip().lower() in {"y", "yes"}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_chat_loop -v`
Expected: PASS (12 tests OK)

- [ ] **Step 6: Commit**

```bash
cd /f/devloop && git add src/devloop/chat_loop.py tests/test_chat_loop.py && git commit -m "Add devloop-owned planning chat loop over codex exec resume"
```

---

### Task 7: Rewire the interactive runner (prompt, options menu, handoff, in-process development)

**Files:**
- Modify: `src/devloop/interactive_runner.py` (replace `run_codex_planning_session` at lines 234-262, `build_planning_prompt` at lines 265-300, `build_devloop_command` at lines 517-552, and `main` at lines 26-84; keep everything else)
- Test: `tests/test_interactive_runner.py`

**Interfaces:**
- Consumes: `run_planning_chat`, `ChatConfig`, `ChatCallbacks` (Task 6); `catalog.discover`, `catalog.Selection`, `catalog.load_selection`, `catalog.save_selection`, `catalog.planning_skill_paths`, `catalog.write_session_preset` (Task 2); `github_install.install_from_github` (Task 5); `statusui` (Task 1); existing `find_artifacts`, `resolve_planning_artifacts`, `PlanningArtifacts`, `plan_state_path`, `ask_*` helpers, `artifact_slug`, `default_worktree_path`; `devloop.self_improvement_wiki.DEFAULT_SELF_IMPROVEMENT_WIKI_PATH`; `devloop.cli.main` (existing signature `main(argv: list[str] | None) -> int`).
- Produces:
  - `build_planning_prompt(*, repo_root: Path, bundle_root: Path, goal: str, skill_paths: list[Path], wiki_index: Path) -> str` (new signature).
  - `HandoffParams(start_issue: str | None, run_all: bool, use_worktree: bool, worktree_path: Path, branch_name: str)` dataclass.
  - `build_devloop_args(params: HandoffParams, artifacts: PlanningArtifacts, preset_path: Path | None) -> list[str]` (replaces `build_devloop_command`; returns argv for `devloop.cli.main`, always containing `--self-improvement-wiki`, never asking about the wiki).
  - `run_options_menu(bundle_root: Path, selection: Selection, state_path: Path) -> None` (mutates + persists selection).

- [ ] **Step 1: Write the failing test**

Create `tests/test_interactive_runner.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devloop import interactive_runner
from devloop.interactive_runner import HandoffParams, PlanningArtifacts


class BuildPlanningPromptTests(unittest.TestCase):
    def make_prompt(self) -> str:
        return interactive_runner.build_planning_prompt(
            repo_root=Path("C:/repo"),
            bundle_root=Path("F:/devloop"),
            goal="add login",
            skill_paths=[
                Path("F:/devloop/skills/codex/grill-with-docs/SKILL.md"),
                Path("F:/devloop/skills/codex/to-prd/SKILL.md"),
            ],
            wiki_index=Path("F:/devloop/docs/devloop-self-improvement/wiki/index.md"),
        )

    def test_lists_selected_skills(self) -> None:
        prompt = self.make_prompt()
        self.assertIn("grill-with-docs", prompt)
        self.assertIn("to-prd", prompt)

    def test_references_wiki_index(self) -> None:
        prompt = self.make_prompt()
        self.assertIn("self-improvement wiki", prompt.lower())
        self.assertIn("index.md", prompt)

    def test_never_asks_user_to_exit(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertNotIn("/quit", prompt)
        self.assertNotIn("ctrl+c", prompt)
        self.assertIn("continues automatically", prompt)

    def test_includes_issue_self_containment_rules(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertIn("self-contained", prompt)
        self.assertIn("fresh codex session", prompt)
        self.assertIn("context window", prompt)

    def test_includes_goal(self) -> None:
        self.assertIn("add login", self.make_prompt())


class BuildDevloopArgsTests(unittest.TestCase):
    def make_artifacts(self, root: Path) -> PlanningArtifacts:
        prd = root / "prd" / "feature" / "feature.md"
        prd.parent.mkdir(parents=True)
        prd.write_text("prd", encoding="utf-8")
        issues = root / "prd" / "feature" / "issues" / "README.md"
        issues.parent.mkdir(parents=True)
        issues.write_text("issues", encoding="utf-8")
        return PlanningArtifacts(prd_path=prd, issues_index=issues)

    def test_default_params_run_all_with_worktree_and_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            params = HandoffParams(
                start_issue=None,
                run_all=True,
                use_worktree=True,
                worktree_path=root / "feature-dev",
                branch_name="devloop/feature",
            )
            args = interactive_runner.build_devloop_args(params, artifacts, None)
        self.assertIn("--all", args)
        self.assertIn("--self-improvement-wiki", args)
        self.assertIn("--create-worktree", args)
        self.assertIn("--branch-name", args)
        self.assertNotIn("--no-self-improvement-wiki", args)
        self.assertNotIn("--preset", args)

    def test_start_issue_and_no_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            params = HandoffParams(
                start_issue="0002",
                run_all=False,
                use_worktree=False,
                worktree_path=root / "unused",
                branch_name="unused",
            )
            args = interactive_runner.build_devloop_args(params, artifacts, None)
        self.assertIn("--start-issue", args)
        self.assertIn("0002", args)
        self.assertIn("--no-worktree", args)
        self.assertNotIn("--all", args)

    def test_session_preset_added_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            preset = root / "session.preset.json"
            preset.write_text("{}", encoding="utf-8")
            params = HandoffParams(
                start_issue=None,
                run_all=True,
                use_worktree=False,
                worktree_path=root / "unused",
                branch_name="unused",
            )
            args = interactive_runner.build_devloop_args(params, artifacts, preset)
        self.assertIn("--preset", args)
        self.assertIn(str(preset), args)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_interactive_runner -v`
Expected: FAIL with `ImportError: cannot import name 'HandoffParams'` (or `AttributeError` on `build_planning_prompt` keyword arguments)

- [ ] **Step 3: Implement the rewiring**

In `src/devloop/interactive_runner.py`, apply the following changes.

**3a. Imports** — extend the import block at the top:

```python
from . import catalog as catalog_module
from . import statusui
from .chat_loop import ChatCallbacks, ChatConfig, run_planning_chat
from .github_install import install_from_github
from .self_improvement_wiki import DEFAULT_SELF_IMPROVEMENT_WIKI_PATH
from .statusui import Stage
from .templates import BundleContext
```

**3b. Replace `build_planning_prompt`** (old lines 265-300) with:

```python
def build_planning_prompt(
    *,
    repo_root: Path,
    bundle_root: Path,
    goal: str,
    skill_paths: list[Path],
    wiki_index: Path,
) -> str:
    skills_block = "\n".join(f"- {path}" for path in skill_paths)
    return f"""You are running the Dev Loop interactive planning intake for this repository.

Repository root: {repo_root}
Dev Loop bundle root: {bundle_root}

Use these bundled Codex skill instructions:
{skills_block}

Read the Dev Loop self-improvement wiki index and apply relevant lessons to this planning session:
- {wiki_index}

Required workflow:
1. Use $grill-with-docs first. Interview the user until the requested change is sharp enough to build.
2. Use domain-modeling during the grill. Update glossary or ADR files only when the skill rules justify it.
3. After the user confirms the design, use $to-prd. Save the canonical PRD as {repo_root / "prd" / "<prd-name>" / "<prd-name>.md"}.
4. Then use $to-issues. Save the issue pack inside the same PRD folder at {repo_root / "prd" / "<prd-name>" / "issues" / "README.md"}.
5. Keep PRD-specific execution information inside {repo_root / "prd" / "<prd-name>"} unless a repository-wide glossary or ADR update is genuinely required.
6. The issue README must contain real Markdown links to numbered issue files.
7. Do not start implementation and do not run Dev Loop yourself from inside Codex.
8. The Dev Loop wrapper watches the repository and continues automatically once the PRD and issue README exist. Never ask the user to exit or close anything. When the artifacts are ready, report only the exact PRD path and issue README path.

Issue self-containment rules (critical):
- Each issue is later executed by a fresh Codex session with no memory of this conversation, so the full context window is preserved for development.
- Every issue file must be self-contained: state the goal, acceptance criteria, verification steps, relevant file paths, and the PRD path plus the specific PRD sections that apply.
- Never write "as discussed" or refer back to this chat.
- Keep each issue a thin vertical slice sized for one clean context window; split any issue whose required context grows too large.
- Save screenshots that matter for implementation into the PRD folder and link them by relative path from the issues that need them.

{initial_goal_block(goal)}
"""
```

**3c. Update `initial_goal_block`** (old lines 292-300) — replace the Alt+V wording since paste is now devloop-native:

```python
def initial_goal_block(goal: str) -> str:
    if goal:
        return f"Initial user goal:\n{goal}"

    return (
        "No initial user goal was supplied on the command line.\n"
        "Start by asking the user to describe the feature or fix. "
        "They may attach screenshots; attached images arrive with their messages."
    )
```

**3d. Delete `run_codex_planning_session`** (old lines 234-262) and **delete `codex_command_prefix`** (old lines 586-599) and **delete `devloop_command_prefix`** (old lines 573-583). They are replaced by the chat loop and the in-process call.

**3e. Add the options menu and handoff pieces** (place after `save_last_target_repo`):

```python
@dataclass
class HandoffParams:
    start_issue: str | None
    run_all: bool
    use_worktree: bool
    worktree_path: Path
    branch_name: str


def run_options_menu(bundle_root: Path, selection: "catalog_module.Selection", state_path: Path) -> None:
    found = catalog_module.discover(bundle_root)
    while True:
        print()
        print("Options")
        print(f"  1. Planning skills (current: {', '.join(selection.planning_skills)})")
        print("  2. Default agents & skills per role (coder / reviewer / qa)")
        print("  3. Add skill or agent from GitHub")
        print("  4. Back")
        choice = ask_choice("Select", {"1", "2", "3", "4"}, default="4")
        if choice == "4":
            catalog_module.save_selection(state_path, selection)
            return
        if choice == "1":
            edit_planning_skills(found, selection)
        elif choice == "2":
            edit_role_defaults(found, selection)
        elif choice == "3":
            url = ask_required("GitHub URL (optionally #subpath)")
            result = install_from_github(
                url,
                bundle_root,
                confirm=lambda message: ask_yes_no(f"{message}\nProceed?", default=False),
            )
            print(result.message)
            found = catalog_module.discover(bundle_root)


def edit_planning_skills(found: "catalog_module.Catalog", selection: "catalog_module.Selection") -> None:
    print()
    print("Available skills (Enter keeps the current selection):")
    for index, entry in enumerate(found.skills, start=1):
        marker = "*" if entry.name in selection.planning_skills else " "
        print(f"  [{marker}] {index}. {entry.name}")
    raw = input("Comma-separated numbers for planning skills []: ").strip()
    if not raw:
        return
    chosen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(found.skills):
            chosen.append(found.skills[int(part) - 1].name)
    if chosen:
        selection.planning_skills = chosen


def edit_role_defaults(found: "catalog_module.Catalog", selection: "catalog_module.Selection") -> None:
    role = ask_choice("Role to edit (coder/reviewer/qa)", {"coder", "reviewer", "qa"}, default="coder")
    print()
    print("Available skills (Enter keeps the embedded preset):")
    for index, entry in enumerate(found.skills, start=1):
        print(f"  {index}. {entry.name}")
    raw = input(f"Comma-separated skill numbers for {role} []: ").strip()
    if raw:
        paths: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(found.skills):
                paths.append(f"skills/codex/{found.skills[int(part) - 1].name}/SKILL.md")
        if paths:
            selection.role_skills[role] = paths
    print("Available agents (Enter keeps the embedded preset):")
    for index, entry in enumerate(found.agents, start=1):
        print(f"  {index}. {entry.name}")
    raw = input(f"Comma-separated agent numbers for {role} []: ").strip()
    if raw:
        agent_paths: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(found.agents):
                agent_paths.append(f"agents/codex/{found.agents[int(part) - 1].name}.md")
        if agent_paths:
            selection.role_agents[role] = agent_paths


def build_devloop_args(
    params: HandoffParams,
    artifacts: PlanningArtifacts,
    preset_path: Path | None,
) -> list[str]:
    args = [
        "--prd",
        str(artifacts.prd_path),
        "--issues",
        str(artifacts.issues_index),
        "--self-improvement-wiki",
    ]
    if preset_path is not None:
        args.extend(["--preset", str(preset_path)])
    if params.start_issue:
        args.extend(["--start-issue", params.start_issue])
    if params.run_all:
        args.append("--all")
    if params.use_worktree:
        args.extend(
            [
                "--create-worktree",
                "--worktree-path",
                str(params.worktree_path),
                "--branch-name",
                params.branch_name,
            ]
        )
    else:
        args.append("--no-worktree")
    return args


def run_handoff(
    bundle_root: Path,
    repo_root: Path,
    artifacts: PlanningArtifacts,
    selection: "catalog_module.Selection",
    state_path: Path,
) -> int:
    slug = artifact_slug(artifacts)
    params = HandoffParams(
        start_issue=None,
        run_all=True,
        use_worktree=True,
        worktree_path=default_worktree_path(repo_root, slug),
        branch_name=f"devloop/{slug}",
    )

    while True:
        print()
        print(statusui.render_banner(Stage.DEVELOPMENT))
        print(f"PRD:            {artifacts.prd_path}")
        print(f"Issue index:    {artifacts.issues_index}")
        print(f"Issues to run:  {'all pending' if params.run_all and not params.start_issue else params.start_issue or 'all pending'}")
        print(f"Worktree:       {params.worktree_path if params.use_worktree else 'disabled (work in checkout)'}")
        if params.use_worktree:
            print(f"Branch:         {params.branch_name}")
        print("Wiki:           always on (read + updated)")
        raw = input("Press Enter to start development, /options to adjust, /quit to stop: ").strip().lower()
        if raw == "":
            break
        if raw == "/quit":
            return 0
        if raw == "/options":
            adjust_handoff_params(params)
            continue
        print("Unrecognized input. Press Enter, or type /options or /quit.")

    preset_path = catalog_module.write_session_preset(
        bundle_root,
        selection,
        artifacts.prd_path.parent / "devloop.session.preset.json",
    )
    args = build_devloop_args(params, artifacts, preset_path)

    from .cli import main as devloop_main

    print()
    print("Starting Dev Loop development.")
    return devloop_main(args)


def adjust_handoff_params(params: HandoffParams) -> None:
    start_issue = normalize_start_issue(input('Start issue, or "all" for every pending issue [all]: '))
    params.start_issue = start_issue
    params.run_all = start_issue is None or ask_yes_no(
        "Run all pending issues from the selected start issue?", default=True
    )
    params.use_worktree = ask_yes_no("Use a dedicated implementation worktree?", default=True)
    if params.use_worktree:
        params.worktree_path = ask_path("Implementation worktree path", default=params.worktree_path)
        params.branch_name = ask_required("Implementation branch name", default=params.branch_name)
```

**3f. Delete `build_devloop_command`** (old lines 517-552) — replaced by `build_devloop_args` + `run_handoff`. Keep `normalize_start_issue`, `default_worktree_path`, `artifact_slug` unchanged.

**3g. Replace `main`** (old lines 26-84) with:

```python
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    bundle = BundleContext.from_file(Path(__file__).resolve())
    state_path = plan_state_path()
    selection = catalog_module.load_selection(state_path)

    if args.prd:
        try:
            artifacts = resolve_existing_prd_artifacts(args.prd)
            repo_root = git_repo_root(artifacts.prd_path.parent)
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))

        print()
        print(f"Target checkout: {repo_root}")
        print(f"Current branch: {current_branch(repo_root) or 'unknown'}")
        print(f"PRD: {artifacts.prd_path}")
        print(f"Issue index: {artifacts.issues_index}")
        print_prd_status(artifacts)
        return run_handoff(bundle.root, repo_root, artifacts, selection, state_path)

    repo_root = choose_target_repo(args.repo)
    repo_root = apply_branch_strategy(repo_root)

    goal = args.goal.strip() if args.goal else ""
    started_at = time.time()

    found_catalog = catalog_module.discover(bundle.root)
    skill_paths = catalog_module.planning_skill_paths(selection, found_catalog)
    wiki_index = bundle.root / DEFAULT_SELF_IMPROVEMENT_WIKI_PATH / "index.md"
    initial_prompt = build_planning_prompt(
        repo_root=repo_root,
        bundle_root=bundle.root,
        goal=goal,
        skill_paths=skill_paths,
        wiki_index=wiki_index,
    )

    config = ChatConfig(
        codex=args.codex,
        repo_root=repo_root,
        bundle_root=bundle.root,
        sandbox=args.sandbox,
        approval_policy=args.approval_policy,
    )
    callbacks = ChatCallbacks(
        probe_artifacts=lambda: _first_or_none(find_artifacts(repo_root, started_at)),
        manual_artifacts=lambda: _manual_artifacts(),
        open_options=lambda: run_options_menu(bundle.root, selection, state_path),
        status_summary=lambda: _status_summary(repo_root, selection),
    )

    artifacts = run_planning_chat(
        config=config,
        initial_prompt=initial_prompt,
        callbacks=callbacks,
    )
    if artifacts is None:
        print("Planning aborted.")
        return 0

    if isinstance(artifacts, list):
        artifacts = _choose_artifacts(artifacts)

    print()
    print(f"PRD: {artifacts.prd_path}")
    print(f"Issue index: {artifacts.issues_index}")
    return run_handoff(bundle.root, repo_root, artifacts, selection, state_path)


def _first_or_none(candidates: list[PlanningArtifacts]) -> "PlanningArtifacts | list[PlanningArtifacts] | None":
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return candidates


def _choose_artifacts(candidates: list[PlanningArtifacts]) -> PlanningArtifacts:
    print()
    print("Detected multiple PRD / issue-pack pairs:")
    for index, candidate in enumerate(candidates, start=1):
        print(f"  {index}. {candidate.prd_path.name} -> {candidate.issues_index}")
    choice = ask_choice(
        "Select artifact pair",
        {str(i) for i in range(1, len(candidates) + 1)},
        default="1",
    )
    return candidates[int(choice) - 1]


def _manual_artifacts() -> PlanningArtifacts:
    print()
    print("Enter the artifact paths manually.")
    prd_path = ask_existing_file("PRD path")
    issues_index = ask_existing_file("Issue README path")
    return PlanningArtifacts(prd_path=prd_path, issues_index=issues_index)


def _status_summary(repo_root: Path, selection: "catalog_module.Selection") -> str:
    lines = [
        f"Repository: {repo_root}",
        f"Planning skills: {', '.join(selection.planning_skills)}",
    ]
    if selection.has_role_overrides():
        lines.append("Role overrides: customized via /options")
    else:
        lines.append("Role agents/skills: embedded defaults")
    return "\n".join(lines)
```

Also update the `--approval-policy` default in `build_parser` from `"on-request"` to `"never"` (the chat loop's `codex exec` turns are non-interactive and cannot answer approval prompts), and update its help text to `"Codex approval policy for planning turns. Default: never."`.

Note: `resolve_planning_artifacts` (old lines 303-320) becomes unused by `main` but is kept — `/done`'s manual fallback and multi-candidate selection reuse its pieces (`_choose_artifacts`, `_manual_artifacts` above are simplified copies). Delete `resolve_planning_artifacts` only if nothing else references it (check with grep first: `grep -rn "resolve_planning_artifacts" src/ tests/`).

- [ ] **Step 4: Run the new tests and the full suite**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_interactive_runner -v`
Expected: PASS (8 tests OK)

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest discover -s tests -v`
Expected: PASS (all tests from Tasks 1-7 OK)

- [ ] **Step 5: Sanity-check both wrappers still forward correctly**

Run: `cd /f/devloop && ./bin/devloop-plan.sh --help`
Expected: argparse help for `devloop-plan` prints (no traceback), showing `--approval-policy ... Default: never.`

Run: `cd /f/devloop && powershell.exe -NoProfile -ExecutionPolicy Bypass -File bin/devloop-plan.ps1 -Help`
Expected: same help text.

- [ ] **Step 6: Commit**

```bash
cd /f/devloop && git add src/devloop/interactive_runner.py tests/test_interactive_runner.py && git commit -m "Rewire devloop-plan: chat loop, /options menu, Enter-to-start handoff, in-process development, wiki always on"
```

---

### Task 8: Stage banners in the development runner (`cli.py`)

**Files:**
- Modify: `src/devloop/cli.py` (imports at lines 1-21; `main` issue loop at lines 128-144; `run_issue` at lines 243-369)
- Test: `tests/test_cli_banners.py`

**Interfaces:**
- Consumes: `statusui.render_banner`, `statusui.Stage` (Task 1).
- Produces: `issue_progress_label(position: int, total: int, issue_number: str) -> str` (module-level helper in `cli.py`); `run_issue` gains keyword-only parameter `progress: str = ""`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_banners.py`:

```python
from __future__ import annotations

import unittest

from devloop import cli


class IssueProgressLabelTests(unittest.TestCase):
    def test_label_contains_position_and_number(self) -> None:
        label = cli.issue_progress_label(2, 5, "0003")
        self.assertEqual(label, "issue 0003 (2/5)")

    def test_single_issue(self) -> None:
        label = cli.issue_progress_label(1, 1, "0001")
        self.assertEqual(label, "issue 0001 (1/1)")


class RunIssueSignatureTests(unittest.TestCase):
    def test_run_issue_accepts_progress_keyword(self) -> None:
        import inspect

        signature = inspect.signature(cli.run_issue)
        self.assertIn("progress", signature.parameters)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_cli_banners -v`
Expected: FAIL with `AttributeError: module 'devloop.cli' has no attribute 'issue_progress_label'`

- [ ] **Step 3: Implement banner integration**

In `src/devloop/cli.py`:

Add to the imports (after the existing `from .worktree import resolve_worktree`):

```python
from . import statusui
from .statusui import Stage
```

Add the helper (place directly above `run_issue`):

```python
def issue_progress_label(position: int, total: int, issue_number: str) -> str:
    return f"issue {issue_number} ({position}/{total})"
```

In `main`, change the issue loop (old lines 130-136) to pass progress:

```python
    for position, issue in enumerate(issues, start=1):
        issue_result = run_issue(
            issue=issue,
            runner=runner,
            state_writer=state_writer,
            max_passes=args.max_passes,
            progress=issue_progress_label(position, len(issues), issue.number),
        )
```

In `run_issue`, add the keyword-only parameter to the signature (old lines 243-251):

```python
def run_issue(
    issue: Issue,
    runner: CodexRunner,
    state_writer: LoopStateWriter,
    max_passes: int,
    initial_fix_list: list[str] | None = None,
    attempt_label: str | None = None,
    retry_round: int | None = None,
    *,
    progress: str = "",
) -> RoleResult:
```

Inside `run_issue`, add a banner before each role invocation. Directly before `print(f"[{issue.number}] Pass {pass_number}: coder")` (old line 268) insert:

```python
        context = f"{progress or f'issue {issue.number}'} · pass {pass_number}"
        print(statusui.render_banner(Stage.DEVELOPMENT, context))
```

Directly before `print(f"[{issue.number}] Pass {pass_number}: reviewer")` (old line 296) insert:

```python
        print(statusui.render_banner(Stage.REVIEW, context))
```

Directly before `print(f"[{issue.number}] Pass {pass_number}: qa")` (old line 318) insert:

```python
        print(statusui.render_banner(Stage.QA, context))
```

Keep every existing `print(f"[{issue.number}] ...")` line — logs and users rely on them.

- [ ] **Step 4: Run the tests**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest tests.test_cli_banners -v`
Expected: PASS (3 tests OK)

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest discover -s tests -v`
Expected: PASS (full suite OK)

- [ ] **Step 5: Verify dry-run end-to-end still works**

Run (uses an existing PRD pack if one exists in any test repo; otherwise skip to the smoke test in Task 9):
`cd /f/devloop && PYTHONPATH=src python -m devloop.cli --help`
Expected: help prints, no import errors.

- [ ] **Step 6: Commit**

```bash
cd /f/devloop && git add src/devloop/cli.py tests/test_cli_banners.py && git commit -m "Show DEVELOPMENT/REVIEW/QA stage banners with issue progress in the dev runner"
```

---

### Task 9: Documentation, wrapper parity, and end-to-end smoke test

**Files:**
- Modify: `README.md` (sections "What You Can Run", "Quick Start: Plan Then Build", "How Skills Are Used")
- Modify: `docs/interactive-runner.md` (full flow rewrite)
- Modify: `docs/skills-and-agents.md` (add "Drop-in extensions" and "Install from GitHub" sections)
- Modify: `docs/usage.md` (stage pipeline description)

**Interfaces:**
- Consumes: everything shipped in Tasks 1-8.
- Produces: user-facing documentation; no code.

- [ ] **Step 1: Update `docs/interactive-runner.md`**

Replace the planning-session portion of the document with the new flow. The document must cover, in this order (write real prose, not bullets-only):

1. The stage pipeline: `analysis -> development -> review -> qa`, always shown as a banner and in the `[stage] >` prompt.
2. The chat loop: devloop owns the terminal; each message runs a `codex exec resume` turn; Codex output streams natively; no exit is ever required — when the PRD and issue pack appear on disk the wrapper flips to DEVELOPMENT automatically.
3. Alt+V screenshot paste (PrintScreen/Win+Shift+S then Alt+V), the `[image N attached]` marker, automatic image-path detection in messages, and the `/paste` fallback with the terminals where it applies (piped stdin, legacy consoles). Clipboard tool requirements per OS: Windows PowerShell (built in), `wl-paste`/`xclip` on Linux, `pngpaste` on macOS.
4. The slash commands table: Alt+V, `/paste`, `/options`, `/status`, `/done`, `/help`, `/quit` — with one-line descriptions matching `chat_loop.HELP_TEXT`.
5. `/options`: planning skills, per-role agents/skills (written to `devloop.session.preset.json` in the PRD folder and passed as `--preset`), add-from-GitHub, and persistence in the plan-state JSON.
6. The handoff summary screen and Enter-to-start; the wiki line ("always on"); `/options` adjustments (start issue, worktree, branch).
7. Clean-context guarantee: development roles never resume the planning session; issues must be self-contained (the planning prompt enforces this).

- [ ] **Step 2: Update `docs/skills-and-agents.md`**

Add two sections:

```markdown
## Drop-In Extensions

Dev Loop discovers agents and skills from the bundle at startup:

- A skill is a folder under `skills/codex/` containing a `SKILL.md` file.
- An agent is a single `.md` file under `agents/codex/`.

Copy a folder or file into place and it appears in the `/options` pickers on
the next planning session. Nothing needs registering.

## Install From GitHub

Inside the planning chat, `/options` -> "Add skill or agent from GitHub"
accepts a repository URL with an optional `#subpath`:

    https://github.com/someone/skills-repo#skills/my-skill

Dev Loop clones the repository shallowly to a temporary folder, lists every
skill (folder with `SKILL.md`) and agent (`.md` file inside an `agents/`
directory) it finds under the subpath, asks for confirmation, and moves the
approved items into `skills/codex/` and `agents/codex/`. Existing names are
never overwritten. Review third-party skill content before using it: skills
are instructions that Codex will follow inside your repository.
```

- [ ] **Step 3: Update `README.md`**

In "What You Can Run", replace the `devloop-plan` sentence with:

```markdown
- `devloop-plan` starts from an idea and runs one continuous session:
  a Codex-backed planning chat (analysis), then development, review, and QA,
  with the current stage always visible. Paste screenshots with Alt+V.
  No exit or Ctrl+C is ever needed between stages.
```

In "Quick Start: Plan Then Build", after the existing command examples, add:

```markdown
The session shows a stage banner (`analysis -> development -> review -> qa`).
Chat with Codex to sharpen the change; when the PRD and issue pack are
written, press Enter on the summary screen to start development. Type
`/options` at any prompt to pick agents/skills or install new ones from
GitHub; type `/help` for all commands. The self-improvement wiki is always
used: planning reads it, and every run updates it.
```

In "How Skills Are Used", replace the sentence "The interactive planning loop uses:" list intro with a note that these are the *default* planning skills and `/options` can change them.

- [ ] **Step 4: Update `docs/usage.md`**

Add a short "Stage pipeline" subsection stating: the four stages, that banners are printed at each transition and before each prompt, and that `NO_COLOR=1` disables colors while non-UTF-8 consoles automatically fall back to ASCII markers.

- [ ] **Step 5: Full suite + wrapper parity check**

Run: `cd /f/devloop && PYTHONPATH=src python -m unittest discover -s tests -v`
Expected: PASS (all tests OK)

Run: `cd /f/devloop && ./bin/devloop-plan.sh --help && powershell.exe -NoProfile -ExecutionPolicy Bypass -File bin/devloop-plan.ps1 -Help`
Expected: identical argparse help from both wrappers.

- [ ] **Step 6: Manual end-to-end smoke test (real Codex)**

Create a scratch git repo and run one real planning session:

```bash
mkdir -p /f/tmp/devloop-smoke && cd /f/tmp/devloop-smoke && git init -q
cd /f/devloop && ./bin/devloop-plan.sh --repo /f/tmp/devloop-smoke --goal "add a hello-world README section"
```

Verify manually:
1. Banner shows `analysis` active; prompt is `[analysis] > `.
2. First Codex turn streams output; a follow-up chat message gets a coherent contextual reply (session resume works).
3. `/status`, `/help`, `/options` (open + back), and Alt+V (with a screenshot on the clipboard — expect `[image 1 attached]`) all work.
4. Drive the planning to produce the PRD + issue pack; the wrapper flips to the DEVELOPMENT summary automatically — without typing `/quit` or Ctrl+C.
5. Press Enter; development starts in the same terminal; DEVELOPMENT/REVIEW/QA banners appear per role; the wiki update runs at the end.
6. Repeat once on Windows PowerShell (`bin\devloop-plan.ps1`) for the same checks.

If any check fails, fix before committing; document any Codex-version quirk discovered in `docs/troubleshooting.md`.

- [ ] **Step 7: Commit**

```bash
cd /f/devloop && git add README.md docs/interactive-runner.md docs/skills-and-agents.md docs/usage.md docs/troubleshooting.md && git commit -m "Document the smooth planning flow, stage pipeline, Alt+V paste, and GitHub skill installs"
```

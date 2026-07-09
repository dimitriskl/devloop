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

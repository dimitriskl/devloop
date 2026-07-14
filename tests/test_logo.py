from __future__ import annotations

import unittest
from pathlib import Path

import devloop
from devloop.logo import render_logo


class LogoTests(unittest.TestCase):
    def test_version_variable_is_010(self) -> None:
        self.assertEqual(devloop.__version__, "0.1.0")

    def test_rendered_logo_includes_version_and_aligned_border(self) -> None:
        root = Path(__file__).resolve().parents[1]
        logo = render_logo(root)
        lines = logo.splitlines()
        self.assertIn("v0.1.0", logo)
        self.assertEqual(len({len(line) for line in lines}), 1)

    def test_rendered_logo_uses_version_argument(self) -> None:
        root = Path(__file__).resolve().parents[1]
        logo = render_logo(root, version="1.2.3")
        self.assertIn("v1.2.3", logo)
        self.assertNotIn("v0.1.0", logo)


if __name__ == "__main__":
    unittest.main()

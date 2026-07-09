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

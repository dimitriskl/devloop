from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import codex_runner


class ResolveCodexExecutableTests(unittest.TestCase):
    def test_uses_shutil_which_when_available(self) -> None:
        with mock.patch.object(
            codex_runner.shutil,
            "which",
            return_value="C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd",
        ):
            result = codex_runner.resolve_codex_executable("codex")

        self.assertEqual(result, "C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd")

    def test_falls_back_to_windows_npm_shim_location(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            appdata = Path(raw) / "Roaming"
            npm_dir = appdata / "npm"
            npm_dir.mkdir(parents=True)
            codex_cmd = npm_dir / "codex.cmd"
            codex_cmd.write_text("@echo off\n", encoding="utf-8")

            with mock.patch.object(codex_runner.shutil, "which", return_value=None), \
                 mock.patch.object(codex_runner.sys, "platform", "win32"), \
                 mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                result = codex_runner.resolve_codex_executable("codex")

        self.assertEqual(result, str(codex_cmd.resolve()))


if __name__ == "__main__":
    unittest.main()

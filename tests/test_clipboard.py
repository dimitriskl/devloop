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

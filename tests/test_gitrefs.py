from __future__ import annotations

import unittest

from devloop.gitrefs import sanitize_branch_name


class SanitizeBranchNameTests(unittest.TestCase):
    def test_replaces_spaces_with_hyphens(self) -> None:
        self.assertEqual(sanitize_branch_name("Reset Queue"), "Reset-Queue")

    def test_preserves_slash_separated_names(self) -> None:
        self.assertEqual(
            sanitize_branch_name("devloop/Reset Queue"),
            "devloop/Reset-Queue",
        )

    def test_removes_invalid_git_ref_characters(self) -> None:
        self.assertEqual(
            sanitize_branch_name("feature:reset?queue.lock"),
            "feature-reset-queue",
        )

    def test_empty_name_uses_default(self) -> None:
        self.assertEqual(sanitize_branch_name("  "), "devloop-work")


if __name__ == "__main__":
    unittest.main()

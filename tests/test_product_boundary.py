from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devloop.cli import validate_issue_target_product, validate_prd_target_product


class ProductBoundaryTests(unittest.TestCase):
    def validate(self, markdown: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prd = Path(raw) / "prd.md"
            prd.write_text(markdown, encoding="utf-8")
            validate_prd_target_product(prd)

    def test_portable_target_is_accepted_even_when_codexcli_is_named_as_excluded(self) -> None:
        self.validate(
            "# Change\n\n"
            "## Target Product\n\n"
            "Product: devloop-plan + devloop\n\n"
            "CodexCLI is not the target.\n"
        )

    def test_explicit_codexcli_target_is_refused_by_portable_runner(self) -> None:
        with self.assertRaisesRegex(ValueError, "targets codexcli"):
            self.validate(
                "# Change\n\n"
                "## Target Product\n\n"
                "Product: codexcli\n\n"
                "The portable devloop-plan + devloop runner is not the target.\n"
            )

    def test_explicit_portable_declaration_wins_over_codexcli_exclusion(self) -> None:
        self.validate(
            "# Change\n\n"
            "## Target Product\n\n"
            "Product: devloop-plan + devloop\n\n"
            "The separate codexcli application is not the target.\n"
        )

    def test_existing_prd_without_target_section_remains_accepted(self) -> None:
        self.validate("# Existing change\n\n## Solution\n\nKeep compatibility.\n")

    def test_ambiguous_target_section_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid or ambiguous"):
            self.validate(
                "# Change\n\n"
                "## Target Product\n\n"
                "Portable devloop-plan + devloop or codexcli.\n"
            )

    def test_explicit_codexcli_issue_is_refused_even_if_parent_prd_is_portable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            issue = Path(raw) / "0001-wrong-product.md"
            issue.write_text(
                "# Wrong product\n\n"
                "## Target Product\n\n"
                "The separately installed `codexcli` application.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "issue targets codexcli"):
                validate_issue_target_product(issue)


if __name__ == "__main__":
    unittest.main()

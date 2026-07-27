from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from devloop.cli import validate_issue_target_product, validate_prd_target_product

PORTABLE_PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "devloop"
PORTABLE_EXECUTION_BACKEND_PACKAGE = "portable_execution_backend"
CODEXCLI_PACKAGES = frozenset(
    {
        "application",
        "components",
        "domain",
        "execution",
        "persistence",
        "ui",
        "workflow",
    }
)


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


class PortableExecutionBackendImportBoundaryTests(unittest.TestCase):
    """The portable Execution Backend package must not reach into CodexCLI."""

    def imported_devloop_modules(self, module_path: Path) -> set[str]:
        """Every `devloop.<...>` module the given module imports."""
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        package_parts = module_path.parent.relative_to(PORTABLE_PACKAGE_ROOT).parts
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(
                    alias.name for alias in node.names if alias.name.startswith("devloop")
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 0:
                    if module.startswith("devloop"):
                        imported.add(module)
                    continue
                base = package_parts[: len(package_parts) - (node.level - 1)]
                imported.add(".".join(("devloop", *base, module)).rstrip("."))
        return imported

    def test_package_modules_import_nothing_from_a_codexcli_package(self) -> None:
        package_root = PORTABLE_PACKAGE_ROOT / PORTABLE_EXECUTION_BACKEND_PACKAGE
        module_paths = sorted(package_root.glob("*.py"))
        self.assertTrue(module_paths, "The portable execution-backend package is missing.")

        for module_path in module_paths:
            with self.subTest(module=module_path.name):
                for imported in self.imported_devloop_modules(module_path):
                    parts = imported.split(".")
                    self.assertEqual(parts[0], "devloop", imported)
                    self.assertNotIn(
                        parts[1] if len(parts) > 1 else "",
                        CODEXCLI_PACKAGES,
                        f"{module_path.name} imports the CodexCLI module {imported}",
                    )

    def test_every_codexcli_package_stays_out_of_the_portable_backend(self) -> None:
        for package in sorted(CODEXCLI_PACKAGES):
            with self.subTest(package=package):
                self.assertTrue(
                    (PORTABLE_PACKAGE_ROOT / package).is_dir(),
                    f"The CodexCLI package {package} no longer exists; update the "
                    "portable product boundary.",
                )


if __name__ == "__main__":
    unittest.main()

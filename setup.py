from __future__ import annotations

from setuptools import setup
from setuptools.command.build_py import build_py as SetuptoolsBuildPy

CODEXCLI_ROOT_MODULES = {"__init__", "entrypoint", "version"}


class CodexCliBuildPy(SetuptoolsBuildPy):
    """Exclude the adjacent legacy bundle from the installable CodexCLI wheel."""

    def find_package_modules(
        self,
        package: str,
        package_dir: str,
    ) -> list[tuple[str, str, str]]:
        modules = super().find_package_modules(package, package_dir)
        if package != "devloop":
            return modules
        return [module for module in modules if module[1] in CODEXCLI_ROOT_MODULES]


setup(cmdclass={"build_py": CodexCliBuildPy})

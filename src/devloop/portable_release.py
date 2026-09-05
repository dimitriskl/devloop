from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .portable_project_adoption import (
    PortableAdoptionReport,
    PortableAdoptionStatus,
    adopt_v021_configuration,
)
from .portable_session_catalog import (
    PortableSessionCatalog,
    portable_session_catalog_path,
)
from .portable_version import (
    PORTABLE_GENERATION,
    PORTABLE_PRODUCT_NAME,
    PORTABLE_VERSION,
)
from .workflow_defaults import portable_planner_configuration_path

PORTABLE_TEXTUAL_VERSION = "8.2.8"
PORTABLE_RELEASE_METADATA = {
    "product": PORTABLE_PRODUCT_NAME,
    "generation": PORTABLE_GENERATION,
    "version": PORTABLE_VERSION,
    "codexcli": "separate product",
    "bootstrap_protocol_min": 2,
    "bootstrap_protocol_max": 2,
    "pointer_schema_min": 2,
    "pointer_schema_max": 2,
    "manifest_schema_min": 2,
    "manifest_schema_max": 2,
    "transaction_schema_min": 2,
    "transaction_schema_max": 2,
}


class PortableReleasePreparationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PortableReleasePreparation:
    catalog_path: Path
    adoption_report: PortableAdoptionReport | None


@dataclass(frozen=True)
class PortableRuntimeValidation:
    textual_version: str
    sqlite_version: str


def _validate_release_metadata(bundle_root: Path) -> None:
    metadata_path = bundle_root / "portable-release.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PortableReleasePreparationError(
            f"Portable release metadata is unreadable: {metadata_path}: {error}"
        ) from error
    if metadata != PORTABLE_RELEASE_METADATA:
        raise PortableReleasePreparationError(
            "Portable release metadata does not match this runtime: "
            f"expected {PORTABLE_RELEASE_METADATA!r}, found {metadata!r}."
        )


def validate_portable_runtime(
    *,
    bundle_root: Path | None = None,
) -> PortableRuntimeValidation:
    _validate_release_metadata(
        bundle_root.resolve() if bundle_root is not None else Path(__file__).resolve().parents[2]
    )
    import textual

    if textual.__version__ != PORTABLE_TEXTUAL_VERSION:
        raise PortableReleasePreparationError(
            "Portable runtime Textual version is unsupported: "
            f"expected {PORTABLE_TEXTUAL_VERSION}, found {textual.__version__}."
        )
    if not sqlite3.sqlite_version:
        raise PortableReleasePreparationError(
            "Portable runtime standard-library SQLite is unavailable."
        )
    return PortableRuntimeValidation(textual.__version__, sqlite3.sqlite_version)


def prepare_portable_user_state(
    *,
    catalog_path: Path | None = None,
    configuration_path: Path | None = None,
) -> PortableReleasePreparation:
    selected_catalog_path = (catalog_path or portable_session_catalog_path()).resolve()
    selected_configuration_path = (
        configuration_path or portable_planner_configuration_path()
    ).resolve()
    catalog = PortableSessionCatalog(selected_catalog_path)
    if not selected_configuration_path.exists():
        return PortableReleasePreparation(selected_catalog_path, None)

    report = adopt_v021_configuration(catalog, selected_configuration_path)
    unsupported = tuple(
        entry for entry in report.entries if entry.status is PortableAdoptionStatus.UNSUPPORTED
    )
    if unsupported:
        details = "; ".join(entry.detail for entry in unsupported)
        raise PortableReleasePreparationError(
            f"Portable v0.2.1 adoption could not complete: {details}"
        )
    return PortableReleasePreparation(selected_catalog_path, report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m devloop.portable_release",
        description="Validate and prepare the Portable Dev Loop v3 installation.",
    )
    parser.add_argument(
        "command",
        choices=("validate-runtime", "prepare-user-state"),
    )
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--configuration", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-runtime":
        validate_portable_runtime()
    else:
        prepare_portable_user_state(
            catalog_path=args.catalog,
            configuration_path=args.configuration,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

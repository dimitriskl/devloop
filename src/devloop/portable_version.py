from __future__ import annotations

PORTABLE_PRODUCT_NAME = "Portable Dev Loop"
PORTABLE_GENERATION = "v3"
PORTABLE_VERSION = "0.3.1"


def portable_version_text(program: str) -> str:
    return (
        f"{program} - {PORTABLE_PRODUCT_NAME} "
        f"{PORTABLE_GENERATION} {PORTABLE_VERSION}"
    )

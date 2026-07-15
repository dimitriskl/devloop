from __future__ import annotations

import re
from enum import Enum
from pathlib import Path

_TARGET_PRODUCT_SECTION = re.compile(
    r"(?ims)^##\s+Target Product\s*$\n(?P<body>.*?)(?=^##\s|\Z)"
)
_PRODUCT_DECLARATION = re.compile(r"(?im)^\s*Product\s*:\s*(?P<product>[^\r\n]+)$")
PORTABLE_TARGET_MARKER = "devloop-plan + devloop"
CODEXCLI_TARGET_MARKER = "codexcli"


class TargetProduct(str, Enum):
    UNSPECIFIED = "UNSPECIFIED"
    PORTABLE_DEV_LOOP = "PORTABLE_DEV_LOOP"
    CODEXCLI = "CODEXCLI"
    INVALID = "INVALID"


def detect_target_product(path: Path) -> TargetProduct:
    text = path.read_text(encoding="utf-8")
    match = _TARGET_PRODUCT_SECTION.search(text)
    if match is None:
        return TargetProduct.UNSPECIFIED
    target = match.group("body").casefold()
    declaration = _PRODUCT_DECLARATION.search(target)
    if declaration is not None:
        product = declaration.group("product").strip(" `*_\t.")
        if product == PORTABLE_TARGET_MARKER:
            return TargetProduct.PORTABLE_DEV_LOOP
        if product == CODEXCLI_TARGET_MARKER:
            return TargetProduct.CODEXCLI
        return TargetProduct.INVALID
    has_portable_marker = PORTABLE_TARGET_MARKER in target
    has_codexcli_marker = CODEXCLI_TARGET_MARKER in target
    if has_portable_marker and not has_codexcli_marker:
        return TargetProduct.PORTABLE_DEV_LOOP
    if has_codexcli_marker and not has_portable_marker:
        return TargetProduct.CODEXCLI
    return TargetProduct.INVALID


def require_portable_target(path: Path, *, artifact_name: str) -> None:
    target = detect_target_product(path)
    if target in {TargetProduct.UNSPECIFIED, TargetProduct.PORTABLE_DEV_LOOP}:
        return
    if target is TargetProduct.CODEXCLI:
        raise ValueError(
            f"This {artifact_name} targets codexcli, the separate Textual application. "
            "The portable devloop runner will not execute it; use the codexcli "
            f"workflow or correct the {artifact_name} Target Product section."
        )
    raise ValueError(
        f"This {artifact_name} has an invalid or ambiguous Target Product section. "
        f"Set it to exactly `Product: {PORTABLE_TARGET_MARKER}` for the portable runner."
    )

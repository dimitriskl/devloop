from __future__ import annotations

import re
from dataclasses import dataclass

_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*\Z")


@dataclass(frozen=True, order=True)
class LanguageTag:
    value: str

    def __post_init__(self) -> None:
        if _LANGUAGE_TAG.fullmatch(self.value) is None:
            raise ValueError("Language must be a valid BCP 47-style language tag.")

    def __str__(self) -> str:
        return self.value

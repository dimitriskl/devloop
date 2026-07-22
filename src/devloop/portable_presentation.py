from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum


PORTABLE_UI_MODE_ENVIRONMENT_VARIABLE = "DEVLOOP_UI_MODE"


class PortableUiMode(str, Enum):
    APPLICATION = "APPLICATION"
    PLAIN = "PLAIN"


class PortableActivityStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NOTICE = "NOTICE"


@dataclass(frozen=True)
class PortableActivity:
    operation_id: str
    message: str
    status: PortableActivityStatus


@dataclass(frozen=True)
class PortableActivityFeed:
    items: tuple[PortableActivity, ...] = ()

    def publish(self, activity: PortableActivity) -> PortableActivityFeed:
        existing = next(
            (
                index
                for index, item in enumerate(self.items)
                if item.operation_id == activity.operation_id
            ),
            None,
        )
        if existing is None:
            return replace(self, items=(*self.items, activity)[-100:])
        updated = [
            item for index, item in enumerate(self.items) if index != existing
        ]
        updated.append(activity)
        return replace(self, items=tuple(updated[-100:]))


@dataclass(frozen=True)
class PortableListItem:
    item_id: str
    label: str
    preview_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortableViewModel:
    path: tuple[str, ...]
    title: str
    items: tuple[PortableListItem, ...]
    selected_id: str

    @property
    def selected_item(self) -> PortableListItem:
        return next(item for item in self.items if item.item_id == self.selected_id)

    @property
    def preview_lines(self) -> tuple[str, ...]:
        return self.selected_item.preview_lines

    def select(self, item_id: str) -> PortableViewModel:
        if not any(item.item_id == item_id for item in self.items):
            raise ValueError(f"Unknown Portable View item: {item_id}")
        return replace(self, selected_id=item_id)


def requested_portable_ui_mode(
    *,
    explicit_mode: PortableUiMode | None,
    environment: Mapping[str, str],
) -> PortableUiMode | None:
    if explicit_mode is not None:
        return explicit_mode
    raw_mode = environment.get(PORTABLE_UI_MODE_ENVIRONMENT_VARIABLE, "").strip()
    if not raw_mode:
        return None
    try:
        return PortableUiMode(raw_mode.upper())
    except ValueError as error:
        supported = ", ".join(mode.value.lower() for mode in PortableUiMode)
        raise ValueError(
            f"Unsupported {PORTABLE_UI_MODE_ENVIRONMENT_VARIABLE} value "
            f"{raw_mode!r}; expected one of: {supported}."
        ) from error


def select_portable_ui_mode(
    *,
    requested_mode: PortableUiMode | None,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
    term: str | None,
) -> PortableUiMode:
    if requested_mode is not None:
        return requested_mode
    if not stdin_is_tty or not stdout_is_tty:
        return PortableUiMode.PLAIN
    if term is not None and term.casefold() == "dumb":
        return PortableUiMode.PLAIN
    return PortableUiMode.APPLICATION

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .portable_workflow import (
    PortableStepComponentCatalog,
    WorkflowDefinition,
    canonical_workflow_hash,
    default_portable_workflow,
    load_portable_workflow,
    validate_portable_workflow_for_apply,
)

USER_WORKFLOW_DEFAULT_KEY = "user_workflow_default"
USER_WORKFLOW_DEFAULT_HASH_KEY = "user_workflow_default_hash"
PORTABLE_PLANNER_CONFIGURATION_FILE = "devloop-plan.json"


def portable_planner_configuration_path() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "DevLoop" / PORTABLE_PLANNER_CONFIGURATION_FILE
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "devloop" / PORTABLE_PLANNER_CONFIGURATION_FILE
    return Path.home() / ".config" / "devloop" / PORTABLE_PLANNER_CONFIGURATION_FILE


def atomic_write_planner_configuration(
    path: Path,
    data: Mapping[str, object],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class WorkflowDefaultStore:
    """Loads and atomically replaces the portable User Workflow Default."""

    def __init__(
        self,
        path: Path,
        catalog: PortableStepComponentCatalog,
    ) -> None:
        self._path = path
        self._catalog = catalog

    def load(self) -> WorkflowDefinition:
        data = self._load_configuration()
        document = data.get(USER_WORKFLOW_DEFAULT_KEY)
        if document is None:
            return default_portable_workflow()
        if not isinstance(document, dict):
            raise ValueError("The User Workflow Default must be a JSON object.")
        workflow = load_portable_workflow(document, self._catalog)
        expected_hash = data.get(USER_WORKFLOW_DEFAULT_HASH_KEY)
        actual_hash = canonical_workflow_hash(workflow)
        if expected_hash != actual_hash:
            raise ValueError("The User Workflow Default hash does not match its content.")
        return workflow

    def replace(
        self,
        workflow: WorkflowDefinition,
        *,
        configuration_updates: Mapping[str, object] | None = None,
    ) -> WorkflowDefinition:
        validated = validate_portable_workflow_for_apply(workflow, self._catalog)
        data = self._load_configuration()
        if configuration_updates is not None:
            data.update(configuration_updates)
        data[USER_WORKFLOW_DEFAULT_KEY] = validated.to_dict()
        data[USER_WORKFLOW_DEFAULT_HASH_KEY] = canonical_workflow_hash(validated)
        atomic_write_planner_configuration(self._path, data)
        return validated

    def _load_configuration(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("Portable planner configuration is not valid JSON.") from error
        if not isinstance(data, dict):
            raise ValueError("Portable planner configuration must be a JSON object.")
        return data

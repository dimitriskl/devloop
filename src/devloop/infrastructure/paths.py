from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APPLICATION_DIRECTORY = "codexcli"
PROJECT_DATA_DIRECTORY = ".devloop"
RUNS_DIRECTORY = "runs"
RUN_EVENTS_FILENAME = "events.jsonl"
RUN_SNAPSHOT_FILENAME = "snapshot.json"
RUN_LEASE_FILENAME = "lease.json"
ANALYSIS_DRAFT_FILENAME = "analysis-draft.json"
RUN_QUARANTINE_DIRECTORY = "quarantine"
CONTEXT_MANIFESTS_DIRECTORY = "context-manifests"
IMPLEMENTATION_RESULTS_DIRECTORY = "implementation-results"
REVIEW_INPUTS_DIRECTORY = "review-inputs"
REVIEW_RESULTS_DIRECTORY = "review-results"
QA_INPUTS_DIRECTORY = "qa-inputs"
QA_RESULTS_DIRECTORY = "qa-results"
REWORK_REQUESTS_DIRECTORY = "rework-requests"


@dataclass(frozen=True)
class ApplicationPaths:
    """Resolved project-local and user-wide storage locations."""

    run_root: Path
    user_config: Path
    user_data: Path


def resolve_application_paths(
    repository: Path,
    *,
    platform: str = sys.platform,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ApplicationPaths:
    values = os.environ if environment is None else environment
    user_home = Path.home() if home is None else home

    if platform.startswith("win"):
        config_root = _platform_storage_root(
            values,
            "APPDATA",
            user_home / "AppData" / "Roaming",
        )
        data_root = _platform_storage_root(
            values,
            "LOCALAPPDATA",
            user_home / "AppData" / "Local",
        )
    elif platform == "darwin":
        config_root = user_home / "Library" / "Application Support"
        data_root = config_root
    else:
        config_root = _platform_storage_root(
            values,
            "XDG_CONFIG_HOME",
            user_home / ".config",
        )
        data_root = _platform_storage_root(
            values,
            "XDG_DATA_HOME",
            user_home / ".local" / "share",
        )

    return ApplicationPaths(
        run_root=repository / PROJECT_DATA_DIRECTORY / RUNS_DIRECTORY,
        user_config=config_root / APPLICATION_DIRECTORY,
        user_data=data_root / APPLICATION_DIRECTORY,
    )


def _platform_storage_root(
    environment: Mapping[str, str],
    variable: str,
    fallback: Path,
) -> Path:
    value = environment.get(variable)
    if value:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
    return fallback

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devloop.infrastructure.paths import ApplicationPaths, resolve_application_paths

APP_SERVER_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ApplicationConfig:
    repository: Path
    paths: ApplicationPaths
    app_server_timeout_seconds: float = APP_SERVER_TIMEOUT_SECONDS

    @classmethod
    def resolve(
        cls,
        repository: Path,
        *,
        platform: str = sys.platform,
        environment: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> ApplicationConfig:
        resolved_repository = repository.expanduser().resolve()
        return cls(
            repository=resolved_repository,
            paths=resolve_application_paths(
                resolved_repository,
                platform=platform,
                environment=environment,
                home=home,
            ),
        )

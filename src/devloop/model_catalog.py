from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .terminal_text import has_unsafe_terminal_controls


MODEL_LIST_METHOD = "model/list"
FAST_CATALOG_SERVICE_TIER_ID = "priority"
FAST_CATALOG_SERVICE_TIER_NAME = "Fast"
DEPRECATED_FAST_SPEED_TIER_ID = "fast"
DEFAULT_CATALOG_TIMEOUT_SECONDS = 10.0
MODEL_CATALOG_CACHE_SUFFIX = ".model-catalog-cache.json"


def _validate_terminal_safe_catalog_text(value: str, field_name: str) -> None:
    if has_unsafe_terminal_controls(value):
        raise ValueError(
            f"Codex Model Catalog {field_name} must not contain control "
            "characters or line breaks."
        )


def model_catalog_cache_path(configuration_path: Path) -> Path:
    return configuration_path.with_name(
        f"{configuration_path.stem}{MODEL_CATALOG_CACHE_SUFFIX}"
    )


class CatalogSource(str, Enum):
    LIVE = "LIVE"
    CACHE = "CACHE"


class CatalogDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexModel:
    model_id: str
    display_name: str
    description: str
    reasoning_efforts: tuple[str, ...]
    service_tier_ids: tuple[str, ...] = ()
    advertises_fast: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("Codex Model Catalog entries require a model ID.")
        _validate_terminal_safe_catalog_text(self.model_id, "model IDs")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValueError("Codex Model Catalog entries require a display name.")
        _validate_terminal_safe_catalog_text(self.display_name, "display names")
        if not isinstance(self.description, str):
            raise ValueError("Codex Model Catalog descriptions must be strings.")
        _validate_terminal_safe_catalog_text(self.description, "descriptions")
        if not self.reasoning_efforts:
            raise ValueError(
                f"Codex model {self.model_id!r} advertises no reasoning efforts."
            )
        if any(
            not isinstance(effort, str) or not effort.strip()
            for effort in self.reasoning_efforts
        ):
            raise ValueError("Codex reasoning-effort IDs must be non-empty.")
        for effort in self.reasoning_efforts:
            _validate_terminal_safe_catalog_text(effort, "reasoning-effort IDs")
        if any(
            not isinstance(tier_id, str) or not tier_id.strip()
            for tier_id in self.service_tier_ids
        ):
            raise ValueError("Codex service-tier IDs must be non-empty.")
        for tier_id in self.service_tier_ids:
            _validate_terminal_safe_catalog_text(tier_id, "service-tier IDs")
        if not isinstance(self.advertises_fast, bool):
            raise ValueError("Codex Fast availability must be a boolean.")

    @property
    def supports_fast(self) -> bool:
        return self.advertises_fast


@dataclass(frozen=True)
class CodexModelCatalog:
    models: tuple[CodexModel, ...]
    fetched_at: str
    source: CatalogSource = CatalogSource.LIVE

    def __post_init__(self) -> None:
        if not self.models:
            raise ValueError("The live Codex Model Catalog returned no selectable models.")
        if not isinstance(self.fetched_at, str) or not self.fetched_at.strip():
            raise ValueError("Codex Model Catalog fetched-at timestamps must be non-empty.")
        _validate_terminal_safe_catalog_text(
            self.fetched_at,
            "fetched-at timestamps",
        )
        model_ids = [model.model_id for model in self.models]
        if len(set(model_ids)) != len(model_ids):
            raise ValueError("The Codex Model Catalog returned duplicate model IDs.")

    @property
    def is_fresh(self) -> bool:
        return self.source is CatalogSource.LIVE

    def model(self, model_id: str) -> CodexModel:
        for model in self.models:
            if model.model_id == model_id:
                return model
        raise ValueError(f"Model {model_id!r} is unavailable in the live Codex catalog.")

    def to_dict(self) -> dict[str, object]:
        return {
            "fetched_at": self.fetched_at,
            "models": [
                {
                    "model_id": model.model_id,
                    "display_name": model.display_name,
                    "description": model.description,
                    "reasoning_efforts": list(model.reasoning_efforts),
                    "service_tier_ids": list(model.service_tier_ids),
                    "supports_fast": model.supports_fast,
                }
                for model in self.models
            ],
        }


class CodexModelCatalogCache:
    """Persists the last catalog for editor display, never run authorization."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> CodexModelCatalog | None:
        if not self._path.is_file():
            return None
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Cached Codex Model Catalog is not valid JSON.") from error
        if not isinstance(document, Mapping):
            raise ValueError("Cached Codex Model Catalog must be an object.")
        raw_models = document.get("models")
        fetched_at = document.get("fetched_at")
        if not isinstance(raw_models, list) or not isinstance(fetched_at, str):
            raise ValueError("Cached Codex Model Catalog is malformed.")
        models: list[CodexModel] = []
        for raw_model in raw_models:
            if not isinstance(raw_model, Mapping):
                raise ValueError("Cached Codex Model Catalog contains a malformed model.")
            reasoning_efforts = _cached_string_list(raw_model, "reasoning_efforts")
            service_tier_ids = _cached_string_list(raw_model, "service_tier_ids")
            supports_fast = _cached_fast_availability(
                raw_model,
                service_tier_ids,
            )
            models.append(
                CodexModel(
                    model_id=_cached_required_string(raw_model, "model_id"),
                    display_name=_cached_required_string(
                        raw_model,
                        "display_name",
                    ),
                    description=_cached_required_string(
                        raw_model,
                        "description",
                        allow_empty=True,
                    ),
                    reasoning_efforts=reasoning_efforts,
                    service_tier_ids=service_tier_ids,
                    advertises_fast=supports_fast,
                )
            )
        return CodexModelCatalog(
            models=tuple(models),
            fetched_at=fetched_at,
            source=CatalogSource.CACHE,
        )

    def replace(self, catalog: CodexModelCatalog) -> None:
        if not catalog.is_fresh:
            raise ValueError("Only a live Codex Model Catalog can refresh the cache.")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(catalog.to_dict(), handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            temporary_path.replace(self._path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


class CatalogSession(Protocol):
    def __enter__(self) -> CatalogSession: ...

    def __exit__(self, *args: object) -> None: ...

    def initialize(self) -> None: ...

    def request(
        self,
        method: str,
        params: Mapping[str, object],
    ) -> Mapping[str, Any]: ...


class CodexModelCatalogAdapter:
    """Loads the complete account-aware catalog behind one portable interface."""

    def __init__(
        self,
        codex: str,
        *,
        cwd: Path,
        session_factory: Callable[[Path], CatalogSession] | None = None,
        timeout_seconds: float = DEFAULT_CATALOG_TIMEOUT_SECONDS,
    ) -> None:
        if not codex.strip():
            raise ValueError("A Codex executable is required for model discovery.")
        self._codex = codex
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._session_factory = session_factory or self._create_session

    def discover(self) -> CodexModelCatalog:
        models: list[CodexModel] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        try:
            with self._session_factory(self._cwd) as session:
                session.initialize()
                while True:
                    page = session.request(
                        MODEL_LIST_METHOD,
                        {"cursor": cursor, "includeHidden": False},
                    )
                    models.extend(_models_from_page(page))
                    next_cursor = page.get("nextCursor")
                    if next_cursor is None:
                        break
                    if not isinstance(next_cursor, str) or not next_cursor.strip():
                        raise CatalogDiscoveryError(
                            "Codex Model Catalog returned an invalid pagination cursor."
                        )
                    if next_cursor in seen_cursors:
                        raise CatalogDiscoveryError(
                            "Codex Model Catalog returned a repeated pagination cursor."
                        )
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
        except CatalogDiscoveryError:
            raise
        except (OSError, ValueError) as error:
            raise CatalogDiscoveryError(
                f"Could not discover the live Codex Model Catalog: {error}"
            ) from error
        try:
            return CodexModelCatalog(
                models=tuple(models),
                fetched_at=datetime.now().isoformat(timespec="seconds"),
            )
        except ValueError as error:
            raise CatalogDiscoveryError(str(error)) from error

    def _create_session(self, cwd: Path) -> CatalogSession:
        return _AppServerCatalogSession(
            self._codex,
            cwd=cwd,
            timeout_seconds=self._timeout_seconds,
        )


def _models_from_page(page: Mapping[str, Any]) -> tuple[CodexModel, ...]:
    raw_models = page.get("data")
    if not isinstance(raw_models, list):
        raise CatalogDiscoveryError("Codex Model Catalog page has no model list.")
    models: list[CodexModel] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            raise CatalogDiscoveryError("Codex Model Catalog contains a malformed model.")
        if raw_model.get("hidden") is True:
            continue
        model_id = _required_string(raw_model, "model", "model ID")
        display_name = _required_string(raw_model, "displayName", "display name")
        description = raw_model.get("description", "")
        if not isinstance(description, str):
            raise CatalogDiscoveryError(
                f"Codex model {model_id!r} has a malformed description."
            )
        efforts = _reasoning_efforts(raw_model, model_id)
        service_tiers, supports_fast = _service_tier_metadata(raw_model, model_id)
        additional_speed_tiers = _additional_speed_tier_ids(raw_model, model_id)
        models.append(
            CodexModel(
                model_id=model_id,
                display_name=display_name,
                description=description,
                reasoning_efforts=efforts,
                service_tier_ids=service_tiers,
                advertises_fast=(
                    supports_fast
                    or DEPRECATED_FAST_SPEED_TIER_ID in additional_speed_tiers
                ),
            )
        )
    return tuple(models)


def _reasoning_efforts(
    raw_model: Mapping[str, Any],
    model_id: str,
) -> tuple[str, ...]:
    raw_efforts = raw_model.get("supportedReasoningEfforts")
    if not isinstance(raw_efforts, list):
        raise CatalogDiscoveryError(
            f"Codex model {model_id!r} has no advertised reasoning-effort list."
        )
    efforts: list[str] = []
    for raw_effort in raw_efforts:
        if not isinstance(raw_effort, Mapping):
            raise CatalogDiscoveryError(
                f"Codex model {model_id!r} has a malformed reasoning effort."
            )
        effort = _required_string(raw_effort, "reasoningEffort", "reasoning effort")
        if effort not in efforts:
            efforts.append(effort)
    return tuple(efforts)


def _service_tier_metadata(
    raw_model: Mapping[str, Any],
    model_id: str,
) -> tuple[tuple[str, ...], bool]:
    raw_tiers = raw_model.get("serviceTiers", [])
    if not isinstance(raw_tiers, list):
        raise CatalogDiscoveryError(
            f"Codex model {model_id!r} has a malformed service-tier list."
        )
    tier_ids: list[str] = []
    supports_fast = False
    for raw_tier in raw_tiers:
        if not isinstance(raw_tier, Mapping):
            raise CatalogDiscoveryError(
                f"Codex model {model_id!r} has a malformed service tier."
            )
        tier_id = _required_string(raw_tier, "id", "service-tier ID")
        tier_name = _required_string(raw_tier, "name", "service-tier name")
        if tier_id not in tier_ids:
            tier_ids.append(tier_id)
        if (
            tier_id == FAST_CATALOG_SERVICE_TIER_ID
            and tier_name == FAST_CATALOG_SERVICE_TIER_NAME
        ):
            supports_fast = True
    return tuple(tier_ids), supports_fast


def _additional_speed_tier_ids(
    raw_model: Mapping[str, Any],
    model_id: str,
) -> tuple[str, ...]:
    raw_tiers = raw_model.get("additionalSpeedTiers", [])
    if not isinstance(raw_tiers, list) or not all(
        isinstance(tier_id, str) and tier_id.strip() for tier_id in raw_tiers
    ):
        raise CatalogDiscoveryError(
            f"Codex model {model_id!r} has a malformed additional-speed-tier list."
        )
    try:
        for tier_id in raw_tiers:
            _validate_terminal_safe_catalog_text(tier_id, "speed-tier IDs")
    except ValueError as error:
        raise CatalogDiscoveryError(str(error)) from error
    return tuple(raw_tiers)


def _required_string(
    value: Mapping[str, Any],
    key: str,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or (not allow_empty and not raw.strip()):
        raise CatalogDiscoveryError(
            f"Codex Model Catalog contains an invalid {field_name}."
        )
    try:
        _validate_terminal_safe_catalog_text(raw, field_name)
    except ValueError as error:
        raise CatalogDiscoveryError(str(error)) from error
    return raw


def _cached_string_list(
    value: Mapping[str, Any],
    key: str,
) -> tuple[str, ...]:
    raw = value.get(key)
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise ValueError(f"Cached Codex Model Catalog field {key!r} is malformed.")
    for item in raw:
        _validate_terminal_safe_catalog_text(item, f"field {key!r}")
    return tuple(raw)


def _cached_fast_availability(
    value: Mapping[str, Any],
    service_tier_ids: tuple[str, ...],
) -> bool:
    raw = value.get("supports_fast")
    if raw is None:
        return any(
            tier_id in {
                FAST_CATALOG_SERVICE_TIER_ID,
                DEPRECATED_FAST_SPEED_TIER_ID,
            }
            for tier_id in service_tier_ids
        )
    if not isinstance(raw, bool):
        raise ValueError(
            "Cached Codex Model Catalog field 'supports_fast' is malformed."
        )
    return raw


def _cached_required_string(
    value: Mapping[str, Any],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or (not allow_empty and not raw.strip()):
        raise ValueError(f"Cached Codex Model Catalog field {key!r} is malformed.")
    _validate_terminal_safe_catalog_text(raw, f"field {key!r}")
    return raw


class _AppServerCatalogSession:
    def __init__(self, codex: str, *, cwd: Path, timeout_seconds: float) -> None:
        self._codex = shutil.which(codex) or codex
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self._next_request_id = 1

    def __enter__(self) -> _AppServerCatalogSession:
        try:
            self._process = subprocess.Popen(
                [self._codex, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._cwd,
            )
        except OSError as error:
            raise CatalogDiscoveryError(
                f"Could not start Codex Model Catalog discovery: {error}"
            ) from error
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        threading.Thread(
            target=self._read_stream,
            args=("stdout", self._process.stdout),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stream,
            args=("stderr", self._process.stderr),
            daemon=True,
        ).start()
        return self

    def __exit__(self, *args: object) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def initialize(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "devloop-portable-model-catalog",
                    "title": "Dev Loop Portable Model Catalog",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def request(
        self,
        method: str,
        params: Mapping[str, object],
    ) -> Mapping[str, Any]:
        result = self._request(method, params)
        if not isinstance(result, Mapping):
            raise CatalogDiscoveryError(
                f"Codex App Server returned a malformed {method} response."
            )
        return result

    def _request(
        self,
        method: str,
        params: Mapping[str, object],
    ) -> Any:
        request_id = self._next_request_id
        self._next_request_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CatalogDiscoveryError(
                    f"Timed out while waiting for Codex App Server {method}."
                )
            try:
                stream_name, line = self._messages.get(timeout=remaining)
            except queue.Empty as error:
                raise CatalogDiscoveryError(
                    f"Timed out while waiting for Codex App Server {method}."
                ) from error
            if stream_name == "stderr":
                continue
            if stream_name == "stdout-eof":
                raise CatalogDiscoveryError(
                    f"Codex App Server stopped before completing {method}."
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError as error:
                raise CatalogDiscoveryError(
                    "Codex App Server returned malformed JSON during model discovery."
                ) from error
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                error_value = message.get("error")
                detail = (
                    error_value.get("message")
                    if isinstance(error_value, Mapping)
                    else None
                )
                suffix = f": {detail}" if isinstance(detail, str) else "."
                raise CatalogDiscoveryError(
                    f"Codex App Server rejected {method}{suffix}"
                )
            if "result" not in message:
                raise CatalogDiscoveryError(
                    f"Codex App Server returned no result for {method}."
                )
            return message["result"]

    def _send(self, message: Mapping[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise CatalogDiscoveryError("Codex App Server is not running.")
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except OSError as error:
            raise CatalogDiscoveryError(
                "Could not write to Codex App Server during model discovery."
            ) from error

    def _read_stream(self, stream_name: str, stream: Any) -> None:
        for line in stream:
            self._messages.put((stream_name, line))
        if stream_name == "stdout":
            self._messages.put(("stdout-eof", ""))

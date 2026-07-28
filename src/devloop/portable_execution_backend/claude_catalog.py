"""The Claude Code Backend's Model Catalog: bundled entries, verified selections.

The Claude CLI has no catalog endpoint, so this backend's Model Catalog is
reference data carried in the bundle rather than a list embedded in code. That
keeps browsing free: opening `/options` resolves the executable and returns the
bundled entries as live, without a call per entry.

Selecting a model costs exactly one call. That call exists for two reasons a
bundled list cannot satisfy on its own:

* the operator's own account decides whether a model is usable, and a refusal
  belongs in the editor rather than mid-run; and
* the bundle offers short aliases, and an alias must never be persisted — it
  tracks "latest", so storing one would let a rerun of the same Workflow Run be
  served by a different model. A prototype established that the session
  initialisation event reports the concrete pinned identifier an alias resolved
  to, which is what makes offering aliases safe.

Verification runs behind an injected session factory, exactly as the Codex
catalog adapter's App Server session does, so tests drive it from recorded
provider output and no test spawns the CLI.
"""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from ..execution_backend_id import ExecutionBackendId, parse_execution_backend_id
from ..model_catalog import (
    CatalogDiscoveryError,
    CatalogModel,
    ModelCatalog,
)
from ..subprocess_utils import (
    process_tree_creation_kwargs,
    register_process_tree,
    terminate_process,
)
from ..templates import installed_bundle_context
from ..terminal_text import sanitize_terminal_text

CLAUDE_MODEL_CATALOG_FILENAME = "claude-code-models.json"
# The provider CLI fixes the efforts it accepts, so nothing is discovered; the
# bundle states them beside the models they apply to.
CLAUDE_CATALOG_FIELDS = frozenset(
    {"backend", "models", "reasoning_efforts", "accepts_free_text_model"}
)
CLAUDE_CATALOG_MODEL_FIELDS = frozenset(
    {"model_id", "display_name", "description", "alias", "reasoning_efforts"}
)
DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 30.0
# The session-initialisation event carries the concrete identifier the CLI
# resolved the requested model to. Nothing later in the stream is needed, so
# verification stops there rather than paying for a turn.
CLAUDE_INIT_EVENT_TYPE = "system"
CLAUDE_INIT_EVENT_SUBTYPE = "init"
CLAUDE_INIT_MODEL_KEY = "model"
VERIFICATION_PROMPT = "Reply with OK."
MAX_REFUSAL_TEXT_LENGTH = 600


class ModelVerificationFailure(Enum):
    """Why one model verification produced no concrete model identifier.

    The two members share nothing but their timing, and they lead an operator to
    entirely different places: an account refusal is about this one model and is
    repaired by choosing another, while a provider that never started says
    nothing about the account or the model and is repaired by installing the CLI
    or moving the Workflow Step to another Execution Backend. Callers therefore
    have to tell them apart, and this closed value is how — never by reading the
    cause back out of the message text.
    """

    PROVIDER_UNREACHABLE = "provider_unreachable"
    ACCOUNT_REFUSED = "account_refused"


class ModelVerificationError(RuntimeError):
    """A selected model was not verified, carrying which cause stopped it.

    ``failure`` defaults to an account refusal because that is what a provider
    answering and declining a model is, and it is the only cause a raiser that
    quotes the provider's own words can be reporting. The unreachable cause is
    set exactly where Dev Loop knows the provider never answered at all.
    """

    def __init__(
        self,
        message: str,
        *,
        failure: ModelVerificationFailure = ModelVerificationFailure.ACCOUNT_REFUSED,
    ) -> None:
        super().__init__(message)
        self.failure = failure


def bundled_claude_catalog_path() -> Path:
    """Where this installation's bundled Claude catalog reference data lives."""
    return installed_bundle_context().catalogs / CLAUDE_MODEL_CATALOG_FILENAME


def load_bundled_model_catalog(
    path: Path,
    *,
    fetched_at: str,
) -> ModelCatalog:
    """Read one backend's bundled catalog reference data into a Model Catalog.

    The document is validated rather than trusted: it ships with the bundle, but
    a mis-edited catalog must fail with a clear message instead of reaching the
    provider as a bad `--model` argument.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CatalogDiscoveryError(
            f"The bundled Model Catalog at {path} could not be read: {error}"
        ) from error
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CatalogDiscoveryError(
            f"The bundled Model Catalog at {path} is not valid JSON."
        ) from error
    if not isinstance(document, Mapping):
        raise CatalogDiscoveryError(
            f"The bundled Model Catalog at {path} must be an object."
        )
    unknown = set(document) - CLAUDE_CATALOG_FIELDS
    if unknown:
        raise CatalogDiscoveryError(
            f"The bundled Model Catalog at {path} has unsupported fields: "
            f"{sorted(unknown)}"
        )
    try:
        backend = parse_execution_backend_id(document.get("backend"))
        shared_efforts = _reference_efforts(document, "reasoning_efforts")
        models = tuple(
            _reference_model(raw_model, backend, shared_efforts)
            for raw_model in _reference_model_list(document)
        )
        return ModelCatalog(
            models=models,
            fetched_at=fetched_at,
            backend=backend,
            reasoning_efforts=shared_efforts,
            accepts_free_text_model=bool(document.get("accepts_free_text_model")),
            # Bundled entries are not account-aware, so a selection from this
            # catalog is only trustworthy once the provider has confirmed it —
            # which is also where an alias becomes a concrete identifier.
            verifies_selection=True,
        )
    except ValueError as error:
        raise CatalogDiscoveryError(
            f"The bundled Model Catalog at {path} is invalid: {error}"
        ) from error


class ModelVerificationSession(Protocol):
    """One provider call that resolves a requested model to a concrete one."""

    def __enter__(self) -> ModelVerificationSession: ...

    def __exit__(self, *args: object) -> None: ...

    def resolve_model(self, model_id: str) -> str: ...


VerificationSessionFactory = Callable[[Path], ModelVerificationSession]


@dataclass(frozen=True)
class ClaudeModelCatalogAdapter:
    """The Claude Code Backend's catalog: bundled to browse, verified to select.

    ``catalog_path`` and ``session_factory`` exist so the bundled reference data
    and the one verification call are both injectable; the defaults are the
    installed bundle and a real CLI session.
    """

    claude: str
    cwd: Path
    catalog_path: Path | None = None
    session_factory: VerificationSessionFactory | None = None
    timeout_seconds: float = DEFAULT_VERIFICATION_TIMEOUT_SECONDS

    def discover(self) -> ModelCatalog:
        """Return the bundled catalog as live, making no verification call."""
        if not self.claude.strip():
            raise CatalogDiscoveryError(
                "A Claude executable is required before models can be offered."
            )
        return load_bundled_model_catalog(
            self.catalog_path or bundled_claude_catalog_path(),
            fetched_at=_bundled_fetched_at(),
        )

    def verify(self, model_id: str) -> str:
        """Verify one selected model and return the identifier to persist.

        Exactly one call, and only what the concrete identifier needs: the
        session is closed as soon as the initialisation event names the model the
        provider resolved. A refusal is raised with the provider's own text.

        An operating-system failure talking to the CLI is reported as the
        provider being unreachable rather than as a refusal, because the account
        never got the chance to decide anything.
        """
        requested = model_id.strip()
        if not requested:
            raise ModelVerificationError("A model identifier is required.")
        factory = self.session_factory or self._create_session
        try:
            with factory(self.cwd) as session:
                resolved = session.resolve_model(requested)
        except ModelVerificationError:
            raise
        except OSError as error:
            raise ModelVerificationError(
                "The Claude CLI could not be reached to verify model "
                f"{requested!r}: "
                f"{sanitize_terminal_text(error, preserve_newlines=False)}",
                failure=ModelVerificationFailure.PROVIDER_UNREACHABLE,
            ) from error
        except ValueError as error:
            raise ModelVerificationError(
                f"Could not verify model {requested!r}: "
                f"{sanitize_terminal_text(error, preserve_newlines=False)}"
            ) from error
        concrete = resolved.strip()
        if not concrete:
            raise ModelVerificationError(
                f"The provider accepted model {requested!r} without reporting "
                "which model it resolved to, so nothing can be saved."
            )
        return concrete

    def _create_session(self, cwd: Path) -> ModelVerificationSession:
        return _ClaudeVerificationSession(
            self.claude,
            cwd=cwd,
            timeout_seconds=self.timeout_seconds,
        )


def claude_init_event_model(line: str) -> str | None:
    """The concrete model a session-initialisation line reports, if it is one."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != CLAUDE_INIT_EVENT_TYPE:
        return None
    if payload.get("subtype") != CLAUDE_INIT_EVENT_SUBTYPE:
        return None
    model = payload.get(CLAUDE_INIT_MODEL_KEY)
    return model if isinstance(model, str) and model.strip() else None


def _reference_model_list(document: Mapping[str, Any]) -> tuple[Any, ...]:
    raw_models = document.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ValueError("the catalog lists no models")
    return tuple(raw_models)


def _reference_model(
    raw_model: Any,
    backend: ExecutionBackendId,
    shared_efforts: tuple[str, ...],
) -> CatalogModel:
    if not isinstance(raw_model, Mapping):
        raise ValueError("every model entry must be an object")
    unknown = set(raw_model) - CLAUDE_CATALOG_MODEL_FIELDS
    if unknown:
        raise ValueError(f"model entry has unsupported fields: {sorted(unknown)}")
    efforts = _reference_efforts(raw_model, "reasoning_efforts") or shared_efforts
    is_alias = raw_model.get("alias", False)
    if not isinstance(is_alias, bool):
        raise ValueError("a model entry's alias flag must be a boolean")
    return CatalogModel(
        model_id=_reference_string(raw_model, "model_id"),
        display_name=_reference_string(raw_model, "display_name"),
        description=_reference_string(raw_model, "description", allow_empty=True),
        reasoning_efforts=efforts,
        backend=backend,
        is_alias=is_alias,
    )


def _reference_efforts(value: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw = value.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
        isinstance(effort, str) and effort.strip() for effort in raw
    ):
        raise ValueError(f"field {key!r} must be a list of non-empty strings")
    return tuple(raw)


def _reference_string(
    value: Mapping[str, Any],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    raw = value.get(key, "" if allow_empty else None)
    if not isinstance(raw, str) or (not allow_empty and not raw.strip()):
        raise ValueError(f"field {key!r} must be a non-empty string")
    return raw


def _bundled_fetched_at() -> str:
    return datetime.now().isoformat(timespec="seconds")


class _ClaudeVerificationSession:
    """One short-lived `claude -p` session, ended at the initialisation event.

    The prompt is supplied on standard input for the same reason a Workflow Step
    attempt supplies it there: several of this CLI's options are variadic and
    silently consume a positional prompt. The process tree is terminated as soon
    as the initialisation event arrives, so a valid model costs a process start
    rather than a turn, and an unusable model is refused by the CLI in about a
    second at no token cost.
    """

    def __init__(self, claude: str, *, cwd: Path, timeout_seconds: float) -> None:
        self._claude = claude
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None

    def __enter__(self) -> _ClaudeVerificationSession:
        return self

    def __exit__(self, *args: object) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        terminate_process(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def resolve_model(self, model_id: str) -> str:
        # Imported here, not at module scope: the backend module owns these
        # isolation decisions and imports this one for its catalog adapter, so a
        # module-level import would close the loop. Reading them at call time
        # keeps one definition of the isolation posture for both the attempt
        # invocation and this verification call.
        from .claude_code import (
            CLAUDE_PERMISSION_MODE,
            CLAUDE_SETTING_SOURCES_ARGUMENT,
            CLAUDE_STREAM_JSON_OUTPUT_FORMAT,
        )

        command = [
            self._claude,
            "-p",
            "--output-format",
            CLAUDE_STREAM_JSON_OUTPUT_FORMAT,
            "--verbose",
            "--model",
            model_id,
            "--permission-mode",
            CLAUDE_PERMISSION_MODE.value,
            "--setting-sources",
            CLAUDE_SETTING_SOURCES_ARGUMENT,
            "--strict-mcp-config",
            "--session-id",
            str(uuid.uuid4()),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **process_tree_creation_kwargs(),
            )
        except OSError as error:
            # The process never started, so nothing here is evidence about the
            # account or the model. Saying which command failed to start is what
            # an operator can act on, and the closed cause keeps every caller
            # from having to guess that from the text.
            raise ModelVerificationError(
                f"The Claude CLI at {self._claude!r} could not be started: "
                f"{error}",
                failure=ModelVerificationFailure.PROVIDER_UNREACHABLE,
            ) from error
        register_process_tree(process)
        self._process = process
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_parts: list[str] = []
        reader = threading.Thread(
            target=_drain,
            args=(process.stderr, stderr_parts),
            daemon=True,
        )
        reader.start()
        writer = threading.Thread(
            target=_write_prompt,
            args=(process.stdin, VERIFICATION_PROMPT),
            daemon=True,
        )
        writer.start()
        watchdog = threading.Timer(self._timeout_seconds, terminate_process, (process,))
        watchdog.start()
        try:
            for line in process.stdout:
                resolved = claude_init_event_model(line)
                if resolved is not None:
                    return resolved
        finally:
            watchdog.cancel()
            writer.join(timeout=1)
            reader.join(timeout=1)
        raise ModelVerificationError(_refusal_text(model_id, "".join(stderr_parts)))


def _refusal_text(model_id: str, stderr: str) -> str:
    """The provider's own words, or Dev Loop's only when it said nothing."""
    provider_text = sanitize_terminal_text(stderr, preserve_newlines=False).strip()
    if provider_text:
        return provider_text[:MAX_REFUSAL_TEXT_LENGTH]
    return (
        f"The Claude CLI refused model {model_id!r} without reporting a reason. "
        "Check the identifier and the account's model access."
    )


def _drain(stream: Any, parts: list[str]) -> None:
    for line in stream:
        parts.append(line)


def _write_prompt(stream: Any, prompt: str) -> None:
    try:
        stream.write(prompt)
        stream.flush()
        stream.close()
    except OSError:
        return

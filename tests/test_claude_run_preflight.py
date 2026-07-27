"""Run preflight authorization across every Execution Backend a Workflow uses.

Claude verification is driven through the backend's injected session factory over
the committed `stream-json` recording in `tests/fixtures/claude_code/`, and every
Model Catalog here is fixture data. Nothing in this module resolves or starts a
provider executable, and the two independence tests fail if a future change makes
preflight resolve or invoke a provider the Workflow does not name.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import cli, interactive_runner
from devloop.model_catalog import (
    CatalogDiscoveryError,
    CatalogModel,
    CatalogSource,
    ModelCatalog,
)
from devloop.portable_execution_backend import (
    REGISTERED_EXECUTION_BACKENDS,
    BackendAvailability,
    BackendResolver,
    ClaudeCodeExecutionBackend,
    ExecutionBackend,
    ExecutionBackendId,
    ModelVerificationError,
    load_bundled_model_catalog,
    resolve_execution_backend,
)
from devloop.portable_execution_backend.claude_catalog import claude_init_event_model
from devloop.portable_execution_backend.claude_code import CLAUDE_CLI_COMMAND
from devloop.portable_execution_backend.codex_cli import CODEX_CLI_COMMAND
from devloop.portable_execution_backend.registry import ExecutionBackendFactory
from devloop.portable_workflow import (
    ANALYSIS_STEP_ID,
    DEVELOPMENT_STEP_ID,
    FINAL_REVIEW_STEP_ID,
    QA_STEP_ID,
    SECURITY_REVIEW_STEP_ID,
    StepInstanceId,
    WorkflowDefinition,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
    preflight_step_execution_settings,
)
from devloop.state import LoopStateWriter
from devloop.workflow_defaults import WorkflowDefaultStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_CLAUDE_CATALOG = REPOSITORY_ROOT / "catalogs" / "claude-code-models.json"
RECORDED_SESSION = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "claude_code"
    / "alias-resolution-stream.jsonl"
).read_text(encoding="utf-8")
FETCHED_AT = "2026-07-25T12:00:00"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"
CLAUDE_EFFORT = "high"
# The provider's own words for a model an account cannot use, as recorded by the
# prototype run behind this backend's design.
RECORDED_REFUSAL = "Invalid model name: claude-sonnet-5. Please check the model name."
# The same refusal as the provider sends it when it ends no sentence of its own,
# which is what a composed diagnosis has to cope with.
UNPUNCTUATED_REFUSAL = "Invalid model name: claude-sonnet-5"
# How a missing executable reaches Dev Loop: both `CreateProcess` and `execvp`
# report it as an `OSError`, and that is the only signal the run ever gets.
MISSING_EXECUTABLE_DETAIL = "The system cannot find the file specified"


class _RecordedVerification:
    """The injectable verification session, replaying recorded provider output.

    One instance stands in for the CLI across a whole preflight, so ``calls`` is
    the exact list of models that preflight asked the provider about — which is
    the evidence behind one call per distinct model.
    """

    def __init__(self, refusals: Mapping[str, str] | None = None) -> None:
        self.calls: list[str] = []
        self._refusals = dict(refusals or {})

    def factory(self, _cwd: Path) -> _RecordedVerification:
        return self

    def __enter__(self) -> _RecordedVerification:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def resolve_model(self, model_id: str) -> str:
        self.calls.append(model_id)
        refusal = self._refusals.get(model_id)
        if refusal is not None:
            raise ModelVerificationError(refusal)
        for line in RECORDED_SESSION.splitlines():
            resolved = claude_init_event_model(line)
            if resolved is not None:
                return resolved
        raise AssertionError("The recording carries no session-initialisation event.")


class _RecordingCatalogs:
    """Per-backend Model Catalogs that record which backends were asked for.

    A backend absent from the mapping reports its catalog as unavailable, exactly
    as an uninstalled or unreachable provider would.
    """

    def __init__(self, catalogs: Mapping[ExecutionBackendId, ModelCatalog]) -> None:
        self.requests: list[ExecutionBackendId] = []
        self.catalogs = dict(catalogs)

    def load(self, backend_id: ExecutionBackendId) -> ModelCatalog:
        self.requests.append(backend_id)
        try:
            return self.catalogs[backend_id]
        except KeyError as error:
            raise CatalogDiscoveryError(
                f"The live {backend_id.display_name} Model Catalog is unavailable."
            ) from error


def _codex_catalog(source: CatalogSource = CatalogSource.LIVE) -> ModelCatalog:
    return ModelCatalog(
        models=(
            CatalogModel("gpt-5.6-luna", "Luna", "", ("high",)),
            CatalogModel("gpt-5.6-sol", "Sol", "", ("high", "xhigh")),
            CatalogModel("gpt-5.6-terra", "Terra", "", ("high",)),
        ),
        fetched_at=FETCHED_AT,
        source=source,
    )


def _claude_catalog() -> ModelCatalog:
    return load_bundled_model_catalog(BUNDLED_CLAUDE_CATALOG, fetched_at=FETCHED_AT)


def _claude_backend(verification: _RecordedVerification) -> ClaudeCodeExecutionBackend:
    """The Claude Code Backend with its bundle and its one call both injected."""
    return ClaudeCodeExecutionBackend(
        claude="claude",
        catalog_path=BUNDLED_CLAUDE_CATALOG,
        session_factory=verification.factory,
    )


def _backend_resolver(verification: _RecordedVerification) -> BackendResolver:
    """Resolve the Claude backend to the injected one, every other from the registry."""

    def resolve(backend_id: ExecutionBackendId) -> ExecutionBackend:
        if backend_id is ExecutionBackendId.CLAUDE_CODE:
            return _claude_backend(verification)
        return resolve_execution_backend(backend_id)

    return resolve


def _unreachable_claude_resolver() -> BackendResolver:
    """Resolve Claude to a backend whose real verification session cannot start.

    ``session_factory`` is deliberately left at its default, so the classification
    under test is the real one: the recorded session is not substituted, and only
    the process start itself is replaced.
    """

    def resolve(backend_id: ExecutionBackendId) -> ExecutionBackend:
        if backend_id is ExecutionBackendId.CLAUDE_CODE:
            return ClaudeCodeExecutionBackend(
                claude=CLAUDE_CLI_COMMAND,
                catalog_path=BUNDLED_CLAUDE_CATALOG,
            )
        return resolve_execution_backend(backend_id)

    return resolve


@contextmanager
def _no_claude_cli_installed() -> Iterator[None]:
    """Every process start fails exactly as a machine with no CLI fails it."""

    def missing_executable(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(2, MISSING_EXECUTABLE_DETAIL)

    with mock.patch.object(subprocess, "Popen", missing_executable):
        yield


def _lookups_naming(lookups: Sequence[str], command: str) -> list[str]:
    """Every recorded executable lookup that names one provider's command.

    A lookup can carry a bare command or a path already resolved from one, so the
    match is on the looked-up name rather than on equality with the bare command.
    """
    return [lookup for lookup in lookups if command in Path(lookup).name]


def _workflow_with_claude_steps(
    models: Mapping[StepInstanceId, str],
) -> WorkflowDefinition:
    """The built-in Workflow with the named Workflow Steps moved to Claude."""
    catalog = default_portable_component_catalog()
    document = default_portable_workflow().to_dict()
    for step in document["steps"]:
        model = models.get(StepInstanceId(step["instance_id"]))
        if model is None:
            continue
        step["execution_settings"] = {
            "backend": ExecutionBackendId.CLAUDE_CODE.value,
            "model": model,
            "reasoning_effort": CLAUDE_EFFORT,
            "fast": "OFF",
        }
    return load_portable_workflow(document, catalog)


@contextmanager
def _recorded_executable_lookups() -> Iterator[list[str]]:
    """Record every executable search-path lookup, not only process starts.

    Resolution is the boundary that matters: a `shutil.which` lookup for a
    provider the Workflow does not name already costs the user the dependency
    this feature promises they do not take on.
    """
    lookups: list[str] = []
    real_which = shutil.which

    def recording_which(command, *args, **kwargs):  # type: ignore[no-untyped-def]
        lookups.append(str(command))
        return real_which(command, *args, **kwargs)

    with mock.patch.object(shutil, "which", recording_which):
        yield lookups


@contextmanager
def _no_provider_process(test: unittest.TestCase) -> Iterator[None]:
    def forbidden(*args: object, **kwargs: object) -> None:
        test.fail(f"Run preflight started a provider process: {args[:1]!r}")

    with mock.patch.object(subprocess, "Popen", forbidden):
        yield


@contextmanager
def _resolvable_backends(
    test: unittest.TestCase,
    *,
    unreferenced: ExecutionBackendId,
    registered: Mapping[ExecutionBackendId, ExecutionBackendFactory] | None = None,
) -> Iterator[None]:
    """Fail if a backend no Workflow Step names is resolved at all.

    The registry is the documented resolution point for a backend, so replacing
    the unused backend's factory with one that fails the test is a direct check
    that it is never built.
    """

    def forbidden() -> ExecutionBackend:
        test.fail(
            f"The {unreferenced.display_name} Backend was resolved for a Workflow "
            "that names no Workflow Step on it."
        )
        raise AssertionError

    with mock.patch.dict(
        REGISTERED_EXECUTION_BACKENDS,
        {unreferenced: forbidden, **(registered or {})},
    ):
        yield


class ClaudeRunAuthorizationTests(unittest.TestCase):
    """The Claude Code Backend authorizing the models a Run Snapshot selects."""

    def test_each_distinct_model_is_verified_exactly_once(self) -> None:
        """Five Workflow Steps on two models cost two calls, not five."""
        workflow = _workflow_with_claude_steps(
            {
                ANALYSIS_STEP_ID: SONNET,
                DEVELOPMENT_STEP_ID: SONNET,
                SECURITY_REVIEW_STEP_ID: SONNET,
                FINAL_REVIEW_STEP_ID: SONNET,
                QA_STEP_ID: HAIKU,
            }
        )
        verification = _RecordedVerification()
        catalogs = _RecordingCatalogs(
            {ExecutionBackendId.CLAUDE_CODE: _claude_catalog()}
        )

        preflight_step_execution_settings(
            workflow,
            default_portable_component_catalog(),
            catalogs.load,
            cwd=Path.cwd(),
            resolve_backend=_backend_resolver(verification),
        )

        self.assertEqual(verification.calls, [SONNET, HAIKU])
        self.assertEqual(catalogs.requests, [ExecutionBackendId.CLAUDE_CODE])

    def test_a_model_the_account_cannot_use_fails_naming_the_step_and_options(
        self,
    ) -> None:
        workflow = _workflow_with_claude_steps({DEVELOPMENT_STEP_ID: SONNET})
        verification = _RecordedVerification({SONNET: RECORDED_REFUSAL})
        catalogs = _RecordingCatalogs(
            {
                ExecutionBackendId.CODEX_CLI: _codex_catalog(),
                ExecutionBackendId.CLAUDE_CODE: _claude_catalog(),
            }
        )

        with self.assertRaises(ValueError) as refused:
            preflight_step_execution_settings(
                workflow,
                default_portable_component_catalog(),
                catalogs.load,
                cwd=Path.cwd(),
                resolve_backend=_backend_resolver(verification),
            )

        message = str(refused.exception)
        self.assertIn("Claude Code Backend", message)
        self.assertIn("'Development'", message)
        self.assertIn(SONNET, message)
        self.assertIn(RECORDED_REFUSAL, message)
        self.assertIn("/options", message)
        # The remedy must be reachable: refreshing the catalog cannot help,
        # because the bundled entries are not what refused the model.
        self.assertNotIn("Retry Catalog", message)

    def test_an_account_refusal_names_the_account_and_offers_another_model(
        self,
    ) -> None:
        """The accurate branch keeps its wording and ends its own sentences."""
        workflow = _workflow_with_claude_steps({DEVELOPMENT_STEP_ID: SONNET})
        verification = _RecordedVerification({SONNET: UNPUNCTUATED_REFUSAL})
        catalogs = _RecordingCatalogs(
            {
                ExecutionBackendId.CODEX_CLI: _codex_catalog(),
                ExecutionBackendId.CLAUDE_CODE: _claude_catalog(),
            }
        )

        with self.assertRaises(ValueError) as refused:
            preflight_step_execution_settings(
                workflow,
                default_portable_component_catalog(),
                catalogs.load,
                cwd=Path.cwd(),
                resolve_backend=_backend_resolver(verification),
            )

        message = str(refused.exception)
        self.assertIn(
            f"Step 'Development' selects Claude model {SONNET!r}, which this "
            "account cannot use",
            message,
        )
        # The provider ended no sentence, so the composed message ends it before
        # the remedy starts.
        self.assertIn(
            f"{UNPUNCTUATED_REFUSAL}. Choose a model this account can use for "
            "that Workflow Step in /options.",
            message,
        )
        # The account answered, so reinstalling the CLI is not the remedy.
        self.assertNotIn("Install or repair", message)

    def test_an_unreachable_claude_cli_is_not_blamed_on_the_account(self) -> None:
        """A fresh machine with no Claude CLI is diagnosed as exactly that.

        This is the ordinary failure — a new machine, a CI runner, or a restored
        User Configuration Directory holding a Claude-backed saved Workflow
        Default — and blaming the account for it sent the operator to `/options`
        to choose another model, where every model fails identically.
        """
        workflow = _workflow_with_claude_steps({ANALYSIS_STEP_ID: SONNET})
        catalogs = _RecordingCatalogs(
            {ExecutionBackendId.CLAUDE_CODE: _claude_catalog()}
        )

        with _no_claude_cli_installed(), self.assertRaises(ValueError) as refused:
            preflight_step_execution_settings(
                workflow,
                default_portable_component_catalog(),
                catalogs.load,
                cwd=Path.cwd(),
                resolve_backend=_unreachable_claude_resolver(),
            )

        message = str(refused.exception)
        self.assertIn("Step 'Analysis'", message)
        self.assertIn(SONNET, message)
        self.assertIn(
            f"The Claude CLI at {CLAUDE_CLI_COMMAND!r} could not be started",
            message,
        )
        # Nothing here is evidence about the account, so nothing claims to be.
        self.assertNotIn("this account cannot use", message)
        # Both offered remedies can actually work, unlike choosing another model.
        self.assertIn(
            f"{MISSING_EXECUTABLE_DETAIL}. Install or repair the Claude CLI, or "
            "choose a different Execution Backend for that Workflow Step in "
            "/options.",
            message,
        )

    def test_a_refused_model_fails_before_any_attempt_budget_is_spent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            catalog = default_portable_component_catalog()
            WorkflowDefaultStore(configuration_path, catalog).replace(
                _workflow_with_claude_steps({QA_STEP_ID: HAIKU})
            )
            writer = LoopStateWriter(issue_index)
            verification = _RecordedVerification({HAIKU: RECORDED_REFUSAL})
            catalogs = _RecordingCatalogs(
                {
                    ExecutionBackendId.CODEX_CLI: _codex_catalog(),
                    ExecutionBackendId.CLAUDE_CODE: _claude_catalog(),
                }
            )

            with mock.patch.dict(
                REGISTERED_EXECUTION_BACKENDS,
                {
                    ExecutionBackendId.CLAUDE_CODE: (
                        lambda: _claude_backend(verification)
                    )
                },
            ), self.assertRaisesRegex(ValueError, "'QA'.*/options"):
                cli.resolve_run_workflow(
                    writer,
                    catalog,
                    user_workflow_path=configuration_path,
                    model_catalog_loader=catalogs.load,
                    preflight_cwd=root,
                    require_preflight=True,
                )

            # Nothing was snapshotted and no Issue attempt was recorded, so no
            # attempt budget was spent discovering the refusal.
            self.assertNotIn("resolved_workflow", writer.state)
            self.assertNotIn("resolved_workflow_hash", writer.state)
            self.assertEqual(writer.state.get("issues"), {})

    def test_an_unsupported_reasoning_effort_costs_no_verification_call(self) -> None:
        catalog = default_portable_component_catalog()
        document = _workflow_with_claude_steps({QA_STEP_ID: HAIKU}).to_dict()
        qa = next(
            step for step in document["steps"] if step["instance_id"] == QA_STEP_ID
        )
        qa["execution_settings"]["reasoning_effort"] = "ultra"
        workflow = load_portable_workflow(document, catalog)
        verification = _RecordedVerification()
        catalogs = _RecordingCatalogs(
            {
                ExecutionBackendId.CODEX_CLI: _codex_catalog(),
                ExecutionBackendId.CLAUDE_CODE: _claude_catalog(),
            }
        )

        with self.assertRaisesRegex(ValueError, "'QA'.*'ultra'.*/options"):
            preflight_step_execution_settings(
                workflow,
                catalog,
                catalogs.load,
                cwd=Path.cwd(),
                resolve_backend=_backend_resolver(verification),
            )

        self.assertEqual(verification.calls, [])

    def test_a_stale_cached_catalog_cannot_authorize_either_backend(self) -> None:
        verification = _RecordedVerification()
        stale = {
            ExecutionBackendId.CODEX_CLI: _codex_catalog(CatalogSource.CACHE),
            ExecutionBackendId.CLAUDE_CODE: replace(
                _claude_catalog(),
                source=CatalogSource.CACHE,
            ),
        }
        workflows = {
            ExecutionBackendId.CODEX_CLI: default_portable_workflow(),
            ExecutionBackendId.CLAUDE_CODE: _workflow_with_claude_steps(
                {
                    ANALYSIS_STEP_ID: SONNET,
                    DEVELOPMENT_STEP_ID: SONNET,
                    SECURITY_REVIEW_STEP_ID: SONNET,
                    FINAL_REVIEW_STEP_ID: SONNET,
                    QA_STEP_ID: HAIKU,
                }
            ),
        }
        for backend_id, workflow in workflows.items():
            with self.subTest(backend=backend_id.value):
                catalogs = _RecordingCatalogs(stale)

                with self.assertRaisesRegex(
                    ValueError,
                    f"{backend_id.display_name} Backend.*display-only",
                ):
                    preflight_step_execution_settings(
                        workflow,
                        default_portable_component_catalog(),
                        catalogs.load,
                        cwd=Path.cwd(),
                        resolve_backend=_backend_resolver(verification),
                    )

                self.assertEqual(verification.calls, [])


class MixedBackendPreflightTests(unittest.TestCase):
    """A Workflow using both providers is authorized against both catalogs."""

    def _mixed_workflow(self) -> WorkflowDefinition:
        return _workflow_with_claude_steps(
            {DEVELOPMENT_STEP_ID: SONNET, QA_STEP_ID: HAIKU}
        )

    def test_every_referenced_backend_authorizes_only_its_own_steps(self) -> None:
        verification = _RecordedVerification()
        catalogs = _RecordingCatalogs(
            {
                ExecutionBackendId.CODEX_CLI: _codex_catalog(),
                ExecutionBackendId.CLAUDE_CODE: _claude_catalog(),
            }
        )

        preflight_step_execution_settings(
            self._mixed_workflow(),
            default_portable_component_catalog(),
            catalogs.load,
            cwd=Path.cwd(),
            resolve_backend=_backend_resolver(verification),
        )

        self.assertEqual(
            catalogs.requests,
            [ExecutionBackendId.CODEX_CLI, ExecutionBackendId.CLAUDE_CODE],
        )
        # Only the Claude-backed Workflow Steps' models were verified; the
        # Codex-backed steps were authorized from the Codex catalog alone.
        self.assertEqual(verification.calls, [SONNET, HAIKU])

    def test_a_codex_refusal_in_a_mixed_workflow_still_names_codex(self) -> None:
        catalog = default_portable_component_catalog()
        document = self._mixed_workflow().to_dict()
        analysis = next(
            step
            for step in document["steps"]
            if step["instance_id"] == ANALYSIS_STEP_ID
        )
        analysis["execution_settings"]["model"] = "missing-model"
        workflow = load_portable_workflow(document, catalog)
        verification = _RecordedVerification()
        catalogs = _RecordingCatalogs(
            {
                ExecutionBackendId.CODEX_CLI: _codex_catalog(),
                ExecutionBackendId.CLAUDE_CODE: _claude_catalog(),
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "Codex CLI Backend.*'Analysis'.*missing-model.*Retry Catalog",
        ):
            preflight_step_execution_settings(
                workflow,
                catalog,
                catalogs.load,
                cwd=Path.cwd(),
                resolve_backend=_backend_resolver(verification),
            )

        # Codex is authorized first and refuses, so the Claude catalog is never
        # loaded and no verification call is spent.
        self.assertEqual(catalogs.requests, [ExecutionBackendId.CODEX_CLI])
        self.assertEqual(verification.calls, [])

    def test_an_unavailable_backend_catalog_names_that_backend(self) -> None:
        verification = _RecordedVerification()
        catalogs = _RecordingCatalogs(
            {ExecutionBackendId.CODEX_CLI: _codex_catalog()}
        )

        with self.assertRaisesRegex(
            CatalogDiscoveryError,
            "Claude Code Backend.*Claude Code Model Catalog is unavailable",
        ):
            preflight_step_execution_settings(
                self._mixed_workflow(),
                default_portable_component_catalog(),
                catalogs.load,
                cwd=Path.cwd(),
                resolve_backend=_backend_resolver(verification),
            )


class BackendIndependenceTests(unittest.TestCase):
    """Neither provider costs anything to a Workflow that does not name it.

    Both directions instrument the resolution boundary, not just process
    creation: the unused backend's registered factory fails the test if it is
    built at all, every executable search-path lookup is recorded and none of
    them may name the unreferenced provider's command, and no provider process
    may start. The recorded lookups are narrowed to that command deliberately —
    the guarantee is about the provider the Workflow does not name, so a
    legitimate availability check on the referenced backend must not fail here.
    """

    def _resolve(
        self,
        root: Path,
        workflow: WorkflowDefinition,
        catalogs: _RecordingCatalogs,
    ) -> WorkflowDefinition:
        catalog = default_portable_component_catalog()
        configuration_path = root / "devloop-plan.json"
        issue_index = root / "README.md"
        issue_index.write_text("", encoding="utf-8")
        WorkflowDefaultStore(configuration_path, catalog).replace(workflow)
        return cli.resolve_run_workflow(
            LoopStateWriter(issue_index),
            catalog,
            user_workflow_path=configuration_path,
            model_catalog_loader=catalogs.load,
            preflight_cwd=root,
            require_preflight=True,
        )

    def test_an_all_codex_workflow_never_reaches_the_claude_executable(self) -> None:
        catalogs = _RecordingCatalogs(
            {ExecutionBackendId.CODEX_CLI: _codex_catalog()}
        )

        with tempfile.TemporaryDirectory() as raw, _no_provider_process(
            self
        ), _resolvable_backends(
            self,
            unreferenced=ExecutionBackendId.CLAUDE_CODE,
        ), _recorded_executable_lookups() as lookups:
            resolved = self._resolve(
                Path(raw),
                default_portable_workflow(),
                catalogs,
            )

        self.assertEqual(resolved, default_portable_workflow())
        self.assertEqual(catalogs.requests, [ExecutionBackendId.CODEX_CLI])
        self.assertEqual(_lookups_naming(lookups, CLAUDE_CLI_COMMAND), [])

    def test_an_all_claude_workflow_never_reaches_the_codex_executable(self) -> None:
        """No Codex executable, and no signed-in Codex session, is required."""
        workflow = _workflow_with_claude_steps(
            {
                ANALYSIS_STEP_ID: SONNET,
                DEVELOPMENT_STEP_ID: SONNET,
                SECURITY_REVIEW_STEP_ID: SONNET,
                FINAL_REVIEW_STEP_ID: SONNET,
                QA_STEP_ID: HAIKU,
            }
        )
        verification = _RecordedVerification()
        catalogs = _RecordingCatalogs(
            {ExecutionBackendId.CLAUDE_CODE: _claude_catalog()}
        )

        with tempfile.TemporaryDirectory() as raw, _no_provider_process(
            self
        ), _resolvable_backends(
            self,
            unreferenced=ExecutionBackendId.CODEX_CLI,
            registered={
                ExecutionBackendId.CLAUDE_CODE: (
                    lambda: _claude_backend(verification)
                )
            },
        ), _recorded_executable_lookups() as lookups:
            resolved = self._resolve(Path(raw), workflow, catalogs)

        self.assertEqual(resolved, workflow)
        self.assertEqual(catalogs.requests, [ExecutionBackendId.CLAUDE_CODE])
        self.assertEqual(verification.calls, [SONNET, HAIKU])
        self.assertEqual(_lookups_naming(lookups, CODEX_CLI_COMMAND), [])


class MixedBackendRepairLoopTests(unittest.TestCase):
    """The existing repair loop, now covering both backends."""

    def _mixed_default(self, root: Path) -> Path:
        configuration_path = root / "devloop-plan.json"
        WorkflowDefaultStore(
            configuration_path,
            default_portable_component_catalog(),
        ).replace(
            _workflow_with_claude_steps(
                {DEVELOPMENT_STEP_ID: SONNET, QA_STEP_ID: HAIKU}
            )
        )
        return configuration_path

    def test_retry_refreshes_every_referenced_backends_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = self._mixed_default(root)
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            verification = _RecordedVerification()
            catalogs = _RecordingCatalogs(
                {ExecutionBackendId.CODEX_CLI: _codex_catalog()}
            )
            output: list[str] = []

            def recover_then_retry(_prompt: str) -> str:
                # The Claude CLI becomes reachable again, and the operator uses
                # the retry action rather than editing the Workflow.
                catalogs.catalogs[ExecutionBackendId.CLAUDE_CODE] = _claude_catalog()
                return "retry-catalog"

            with mock.patch.dict(
                REGISTERED_EXECUTION_BACKENDS,
                {
                    ExecutionBackendId.CLAUDE_CODE: (
                        lambda: _claude_backend(verification)
                    )
                },
            ):
                resolved = cli.resolve_run_workflow_with_repair(
                    LoopStateWriter(issue_index),
                    default_portable_component_catalog(),
                    user_workflow_path=configuration_path,
                    model_catalog_loader=lambda _backend: _codex_catalog(),
                    catalog_access=_RepairCatalogAccess(root, catalogs),
                    read_line=recover_then_retry,
                    write=output.append,
                )

        self.assertIsNotNone(resolved)
        # Both referenced backends are refreshed on the retry, not only the one
        # that failed.
        self.assertEqual(
            catalogs.requests,
            [
                ExecutionBackendId.CODEX_CLI,
                ExecutionBackendId.CLAUDE_CODE,
                ExecutionBackendId.CODEX_CLI,
                ExecutionBackendId.CLAUDE_CODE,
            ],
        )
        self.assertEqual(verification.calls, [SONNET, HAIKU])

    def test_repair_diagnostics_name_the_backend_that_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = self._mixed_default(root)
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            verification = _RecordedVerification({SONNET: RECORDED_REFUSAL})
            catalogs = _RecordingCatalogs(
                {
                    ExecutionBackendId.CODEX_CLI: _codex_catalog(),
                    ExecutionBackendId.CLAUDE_CODE: _claude_catalog(),
                }
            )
            output: list[str] = []

            with mock.patch.dict(
                REGISTERED_EXECUTION_BACKENDS,
                {
                    ExecutionBackendId.CLAUDE_CODE: (
                        lambda: _claude_backend(verification)
                    )
                },
            ):
                resolved = cli.resolve_run_workflow_with_repair(
                    LoopStateWriter(issue_index),
                    default_portable_component_catalog(),
                    user_workflow_path=configuration_path,
                    model_catalog_loader=lambda _backend: _codex_catalog(),
                    catalog_access=_RepairCatalogAccess(root, catalogs),
                    read_line=lambda _prompt: "/quit",
                    write=output.append,
                )

            rendered = "\n".join(output)

        # The terminal choices are unchanged: /quit still stops the run.
        self.assertIsNone(resolved)
        self.assertIn("Claude Code Backend", rendered)
        self.assertIn("'Development'", rendered)
        self.assertIn("/options", rendered)
        self.assertIn("retry-catalog", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_non_interactive_repair_still_refuses_without_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = self._mixed_default(root)
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            verification = _RecordedVerification({HAIKU: RECORDED_REFUSAL})
            catalogs = _RecordingCatalogs(
                {
                    ExecutionBackendId.CODEX_CLI: _codex_catalog(),
                    ExecutionBackendId.CLAUDE_CODE: _claude_catalog(),
                }
            )

            with mock.patch.dict(
                REGISTERED_EXECUTION_BACKENDS,
                {
                    ExecutionBackendId.CLAUDE_CODE: (
                        lambda: _claude_backend(verification)
                    )
                },
            ), self.assertRaisesRegex(ValueError, "Claude Code Backend.*'QA'"):
                cli.resolve_run_workflow_with_repair(
                    LoopStateWriter(issue_index),
                    default_portable_component_catalog(),
                    user_workflow_path=configuration_path,
                    model_catalog_loader=lambda _backend: _codex_catalog(),
                    catalog_access=_RepairCatalogAccess(root, catalogs),
                    read_line=lambda _prompt: self.fail(
                        "A non-interactive run must not prompt for repair."
                    ),
                    write=lambda _message: None,
                    allow_interactive_repair=False,
                )


class PlanningPreflightRepairLoopTests(unittest.TestCase):
    """The planning-side repair loop that authorizes the Analysis Workflow Step.

    `devloop-plan` authorizes its Workflow through its own repair loop, so the
    backend-naming diagnostic and the per-backend retry have to hold there too. An
    Analysis Workflow Step moved to Claude is where the Codex-worded refusal used
    to appear and send the operator round a retry that could never help.
    """

    def _analysis_on_claude(self, root: Path) -> Path:
        state_path = root / "devloop-plan.json"
        WorkflowDefaultStore(
            state_path,
            default_portable_component_catalog(),
        ).replace(_workflow_with_claude_steps({ANALYSIS_STEP_ID: SONNET}))
        return state_path

    def test_a_refused_analysis_model_names_the_backend_step_and_options(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = self._analysis_on_claude(root)
            verification = _RecordedVerification({SONNET: RECORDED_REFUSAL})
            catalogs = _RecordingCatalogs(
                {
                    ExecutionBackendId.CODEX_CLI: _codex_catalog(),
                    ExecutionBackendId.CLAUDE_CODE: _claude_catalog(),
                }
            )

            with mock.patch.dict(
                REGISTERED_EXECUTION_BACKENDS,
                {
                    ExecutionBackendId.CLAUDE_CODE: (
                        lambda: _claude_backend(verification)
                    )
                },
            ), mock.patch.object(
                interactive_runner,
                "read_prompt",
                return_value="/quit",
            ), redirect_stdout(StringIO()) as output:
                workflow = interactive_runner.preflight_analysis_workflow(
                    bundle_root=root,
                    state_path=state_path,
                    selection=interactive_runner.catalog_module.Selection.defaults(),
                    component_catalog=default_portable_component_catalog(),
                    catalog_access=_RepairCatalogAccess(root, catalogs),
                )

            rendered = output.getvalue()

        # The terminal choices are unchanged: /quit still stops planning.
        self.assertIsNone(workflow)
        self.assertIn("Claude Code Backend", rendered)
        self.assertIn("'Analysis'", rendered)
        self.assertIn(SONNET, rendered)
        self.assertIn(RECORDED_REFUSAL, rendered)
        self.assertIn("/options", rendered)
        self.assertIn("retry-catalog", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_retry_refreshes_every_referenced_backends_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = self._analysis_on_claude(root)
            verification = _RecordedVerification()
            catalogs = _RecordingCatalogs(
                {ExecutionBackendId.CODEX_CLI: _codex_catalog()}
            )

            def recover_then_retry(_prompt: str) -> str:
                # The Claude CLI becomes reachable again, and the operator uses
                # the retry action rather than editing the Workflow.
                catalogs.catalogs[ExecutionBackendId.CLAUDE_CODE] = _claude_catalog()
                return "retry-catalog"

            with mock.patch.dict(
                REGISTERED_EXECUTION_BACKENDS,
                {
                    ExecutionBackendId.CLAUDE_CODE: (
                        lambda: _claude_backend(verification)
                    )
                },
            ), mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=recover_then_retry,
            ), redirect_stdout(StringIO()):
                workflow = interactive_runner.preflight_analysis_workflow(
                    bundle_root=root,
                    state_path=state_path,
                    selection=interactive_runner.catalog_module.Selection.defaults(),
                    component_catalog=default_portable_component_catalog(),
                    catalog_access=_RepairCatalogAccess(root, catalogs),
                )

        self.assertIsNotNone(workflow)
        # Claude leads here because the Analysis Workflow Step names it, and the
        # retry reloads both referenced backends rather than only the one that
        # failed.
        self.assertEqual(
            catalogs.requests,
            [
                ExecutionBackendId.CLAUDE_CODE,
                ExecutionBackendId.CLAUDE_CODE,
                ExecutionBackendId.CODEX_CLI,
            ],
        )
        self.assertEqual(verification.calls, [SONNET])


class _RepairCatalogAccess:
    """The per-backend catalog access the repair loop and `/options` are given."""

    def __init__(self, cwd: Path, catalogs: _RecordingCatalogs) -> None:
        self.cwd = cwd
        self._catalogs = catalogs

    def load_catalog(self, backend_id: ExecutionBackendId) -> ModelCatalog:
        return self._catalogs.load(backend_id)

    def verify_model(self, _backend_id: ExecutionBackendId, model_id: str) -> str:
        return model_id

    def availability(self) -> tuple[BackendAvailability, ...]:
        # Probing availability resolves backends, which these tests forbid; the
        # repair loop only hands this to `/options`, which they never open.
        return ()


if __name__ == "__main__":
    unittest.main()

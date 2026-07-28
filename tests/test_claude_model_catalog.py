"""The Claude Code Backend's Model Catalog: bundled entries, verified selections.

Every test here is driven from bundled reference data or from the recorded
`stream-json` output committed under `tests/fixtures/claude_code/`. Nothing in
this module starts a provider executable.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import subprocess_utils
from devloop.execution_backend_id import ExecutionBackendId
from devloop.model_catalog import (
    CatalogDiscoveryError,
    CatalogModel,
    CatalogSource,
    ModelCatalog,
    ModelCatalogCache,
    model_catalog_cache_path,
)
from devloop.portable_execution_backend import claude_catalog
from devloop.portable_execution_backend.claude_catalog import (
    ClaudeModelCatalogAdapter,
    ModelVerificationError,
    ModelVerificationFailure,
    bundled_claude_catalog_path,
    claude_init_event_model,
    load_bundled_model_catalog,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_CATALOG = REPOSITORY_ROOT / "catalogs" / "claude-code-models.json"
ALIAS_STREAM = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "claude_code" / "alias-resolution-stream.jsonl"
)
FETCHED_AT = "2026-07-25T12:00:00"


class _RecordedVerificationSession:
    """Replays a recorded attempt transcript instead of starting the CLI."""

    def __init__(self, transcript: str, calls: list[str]) -> None:
        self._transcript = transcript
        self._calls = calls

    def __enter__(self) -> _RecordedVerificationSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def resolve_model(self, model_id: str) -> str:
        self._calls.append(model_id)
        for line in self._transcript.splitlines():
            resolved = claude_init_event_model(line)
            if resolved is not None:
                return resolved
        raise ModelVerificationError("the recording reports no session start")


class _RefusingVerificationSession:
    def __init__(self, message: str, calls: list[str]) -> None:
        self._message = message
        self._calls = calls

    def __enter__(self) -> _RefusingVerificationSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def resolve_model(self, model_id: str) -> str:
        self._calls.append(model_id)
        raise ModelVerificationError(self._message)


class _UnstartableVerificationSession:
    """A session whose provider never starts, as a missing executable presents it."""

    def __enter__(self) -> _UnstartableVerificationSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def resolve_model(self, model_id: str) -> str:
        raise FileNotFoundError(2, "The system cannot find the file specified")


class BundledClaudeCatalogTests(unittest.TestCase):
    def test_the_bundle_carries_the_catalog_as_reference_data(self) -> None:
        """The offered models are bundle data, not a list embedded in code."""
        self.assertTrue(BUNDLED_CATALOG.is_file(), BUNDLED_CATALOG)
        document = json.loads(BUNDLED_CATALOG.read_text(encoding="utf-8"))

        self.assertEqual(document["backend"], ExecutionBackendId.CLAUDE_CODE.value)
        self.assertEqual(bundled_claude_catalog_path(), BUNDLED_CATALOG)

    def test_the_bundled_catalog_offers_aliases_pins_and_free_text(self) -> None:
        catalog = load_bundled_model_catalog(BUNDLED_CATALOG, fetched_at=FETCHED_AT)

        self.assertIs(catalog.backend, ExecutionBackendId.CLAUDE_CODE)
        self.assertTrue(catalog.is_fresh)
        self.assertTrue(catalog.accepts_free_text_model)
        self.assertTrue(any(model.is_alias for model in catalog.models))
        pinned = [model for model in catalog.models if not model.is_alias]
        self.assertTrue(pinned)
        for model in pinned:
            with self.subTest(model=model.model_id):
                self.assertTrue(model.model_id.startswith("claude-"))

    def test_every_entry_advertises_only_provider_supported_efforts(self) -> None:
        catalog = load_bundled_model_catalog(BUNDLED_CATALOG, fetched_at=FETCHED_AT)

        self.assertTrue(catalog.reasoning_efforts)
        for model in catalog.models:
            with self.subTest(model=model.model_id):
                self.assertEqual(model.reasoning_efforts, catalog.reasoning_efforts)
                self.assertFalse(model.supports_fast)

    def test_a_free_text_identifier_inherits_the_backend_effort_set(self) -> None:
        catalog = load_bundled_model_catalog(BUNDLED_CATALOG, fetched_at=FETCHED_AT)

        entered = catalog.selectable_model("claude-opus-7")

        self.assertEqual(entered.model_id, "claude-opus-7")
        self.assertEqual(entered.reasoning_efforts, catalog.reasoning_efforts)
        self.assertIs(entered.backend, ExecutionBackendId.CLAUDE_CODE)

    def test_a_malformed_bundled_catalog_is_refused_with_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "claude-code-models.json"
            path.write_text('{"backend": "CLAUDE_CODE", "models": []}', encoding="utf-8")

            with self.assertRaisesRegex(CatalogDiscoveryError, "lists no models"):
                load_bundled_model_catalog(path, fetched_at=FETCHED_AT)

    def test_an_unknown_bundled_field_is_refused_rather_than_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "claude-code-models.json"
            path.write_text(
                json.dumps(
                    {
                        "backend": "CLAUDE_CODE",
                        "reasoning_efforts": ["high"],
                        "modles": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CatalogDiscoveryError, "unsupported fields"):
                load_bundled_model_catalog(path, fetched_at=FETCHED_AT)


class ClaudeModelCatalogAdapterTests(unittest.TestCase):
    def test_unconfirmed_verification_cleanup_retains_process_tree_ownership(
        self,
    ) -> None:
        session = claude_catalog._ClaudeVerificationSession(
            "claude",
            cwd=Path.cwd(),
            timeout_seconds=1,
        )
        process = mock.Mock()
        session._process = process
        cleanup = subprocess_utils.ProcessTerminationResult(
            tree_terminated=False,
            detail="retained child is still alive",
        )

        with (
            mock.patch.object(
                claude_catalog,
                "terminate_process",
                return_value=cleanup,
            ) as terminate,
            mock.patch.object(
                claude_catalog,
                "unregister_process_tree",
                create=True,
            ) as unregister,
        ):
            session.__exit__()

        terminate.assert_called_once_with(process)
        unregister.assert_not_called()

    def test_discovery_makes_no_verification_call(self) -> None:
        """Opening `/options` must cost nothing, whatever the bundle lists."""
        calls: list[str] = []
        adapter = ClaudeModelCatalogAdapter(
            "claude",
            cwd=Path.cwd(),
            catalog_path=BUNDLED_CATALOG,
            session_factory=lambda _cwd: _RecordedVerificationSession("", calls),
        )

        catalog = adapter.discover()

        self.assertEqual(calls, [])
        self.assertTrue(catalog.models)
        self.assertTrue(catalog.is_fresh)

    def test_selection_resolves_an_alias_to_its_recorded_pinned_identifier(self) -> None:
        """The recorded session start is where the concrete identifier comes from."""
        calls: list[str] = []
        transcript = ALIAS_STREAM.read_text(encoding="utf-8")
        adapter = ClaudeModelCatalogAdapter(
            "claude",
            cwd=Path.cwd(),
            catalog_path=BUNDLED_CATALOG,
            session_factory=lambda _cwd: _RecordedVerificationSession(
                transcript,
                calls,
            ),
        )

        resolved = adapter.verify("haiku")

        self.assertEqual(calls, ["haiku"])
        self.assertEqual(resolved, "claude-haiku-4-5-20251001")
        self.assertNotEqual(resolved, "haiku")

    def test_selection_costs_exactly_one_call(self) -> None:
        calls: list[str] = []
        transcript = ALIAS_STREAM.read_text(encoding="utf-8")
        adapter = ClaudeModelCatalogAdapter(
            "claude",
            cwd=Path.cwd(),
            catalog_path=BUNDLED_CATALOG,
            session_factory=lambda _cwd: _RecordedVerificationSession(
                transcript,
                calls,
            ),
        )

        adapter.verify("claude-opus-5")

        self.assertEqual(len(calls), 1)

    def test_a_refusal_surfaces_the_providers_own_message(self) -> None:
        calls: list[str] = []
        refusal = "Invalid model name: made-up-model. Please check the model name."
        adapter = ClaudeModelCatalogAdapter(
            "claude",
            cwd=Path.cwd(),
            catalog_path=BUNDLED_CATALOG,
            session_factory=lambda _cwd: _RefusingVerificationSession(refusal, calls),
        )

        with self.assertRaises(ModelVerificationError) as refused:
            adapter.verify("made-up-model")

        self.assertEqual(str(refused.exception), refusal)
        self.assertEqual(calls, ["made-up-model"])

    def test_a_refusal_and_an_unreachable_provider_carry_different_causes(
        self,
    ) -> None:
        """The cause is a closed value, so no caller reads it out of the text.

        The two lead to different remedies — another model versus the CLI itself —
        so a caller that could not tell them apart had to claim one of them for
        both.
        """
        refusing = ClaudeModelCatalogAdapter(
            "claude",
            cwd=Path.cwd(),
            catalog_path=BUNDLED_CATALOG,
            session_factory=lambda _cwd: _RefusingVerificationSession(
                "Invalid model name: made-up-model.",
                [],
            ),
        )
        unreachable = ClaudeModelCatalogAdapter(
            "claude",
            cwd=Path.cwd(),
            catalog_path=BUNDLED_CATALOG,
            session_factory=lambda _cwd: _UnstartableVerificationSession(),
        )

        with self.assertRaises(ModelVerificationError) as refused:
            refusing.verify("made-up-model")
        with self.assertRaises(ModelVerificationError) as unreached:
            unreachable.verify("claude-opus-5")

        self.assertIs(
            refused.exception.failure,
            ModelVerificationFailure.ACCOUNT_REFUSED,
        )
        self.assertIs(
            unreached.exception.failure,
            ModelVerificationFailure.PROVIDER_UNREACHABLE,
        )
        self.assertIn("could not be reached", str(unreached.exception))

    def test_discovery_without_an_executable_is_refused(self) -> None:
        adapter = ClaudeModelCatalogAdapter(
            "  ",
            cwd=Path.cwd(),
            catalog_path=BUNDLED_CATALOG,
        )

        with self.assertRaisesRegex(CatalogDiscoveryError, "Claude executable"):
            adapter.discover()

    def test_an_accepted_model_with_no_reported_identity_is_refused(self) -> None:
        adapter = ClaudeModelCatalogAdapter(
            "claude",
            cwd=Path.cwd(),
            catalog_path=BUNDLED_CATALOG,
            session_factory=lambda _cwd: _RecordedVerificationSession(
                '{"type":"system","subtype":"init","model":"   "}',
                [],
            ),
        )

        with self.assertRaisesRegex(ModelVerificationError, "no session start"):
            adapter.verify("sonnet")


class PerBackendCatalogCacheTests(unittest.TestCase):
    def test_the_codex_cache_keeps_its_name_and_others_are_qualified(self) -> None:
        configuration_path = Path("/config/devloop-plan.json")

        codex_path = model_catalog_cache_path(
            configuration_path,
            ExecutionBackendId.CODEX_CLI,
        )
        claude_path = model_catalog_cache_path(
            configuration_path,
            ExecutionBackendId.CLAUDE_CODE,
        )

        self.assertEqual(codex_path.name, "devloop-plan.model-catalog-cache.json")
        self.assertEqual(
            claude_path.name,
            "devloop-plan.model-catalog-cache.claude-code.json",
        )
        self.assertEqual(codex_path, model_catalog_cache_path(configuration_path))
        self.assertNotEqual(codex_path, claude_path)

    def test_a_cached_claude_catalog_is_display_only(self) -> None:
        catalog = load_bundled_model_catalog(BUNDLED_CATALOG, fetched_at=FETCHED_AT)
        with tempfile.TemporaryDirectory() as raw:
            path = model_catalog_cache_path(
                Path(raw) / "devloop-plan.json",
                ExecutionBackendId.CLAUDE_CODE,
            )
            cache = ModelCatalogCache(path, ExecutionBackendId.CLAUDE_CODE)
            cache.replace(catalog)

            restored = cache.load()

        assert restored is not None
        self.assertIs(restored.source, CatalogSource.CACHE)
        self.assertFalse(restored.is_fresh)
        self.assertIs(restored.backend, ExecutionBackendId.CLAUDE_CODE)
        self.assertEqual(
            [model.model_id for model in restored.models],
            [model.model_id for model in catalog.models],
        )
        self.assertTrue(restored.accepts_free_text_model)
        self.assertEqual(restored.reasoning_efforts, catalog.reasoning_efforts)

    def test_a_cached_catalog_can_never_refresh_the_cache(self) -> None:
        catalog = load_bundled_model_catalog(BUNDLED_CATALOG, fetched_at=FETCHED_AT)
        with tempfile.TemporaryDirectory() as raw:
            cache = ModelCatalogCache(
                Path(raw) / "cache.json",
                ExecutionBackendId.CLAUDE_CODE,
            )
            cache.replace(catalog)
            stale = cache.load()
            assert stale is not None

            with self.assertRaisesRegex(ValueError, "Only a live Model Catalog"):
                cache.replace(stale)

    def test_one_backends_cache_cannot_stand_in_for_another(self) -> None:
        codex_catalog = ModelCatalog(
            models=(CatalogModel("gpt-5.6-sol", "Sol", "", ("xhigh",)),),
            fetched_at=FETCHED_AT,
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "cache.json"
            ModelCatalogCache(path, ExecutionBackendId.CODEX_CLI).replace(codex_catalog)

            with self.assertRaisesRegex(ValueError, "Codex CLI Backend, not the"):
                ModelCatalogCache(path, ExecutionBackendId.CLAUDE_CODE).load()

            with self.assertRaisesRegex(ValueError, "cannot refresh"):
                ModelCatalogCache(path, ExecutionBackendId.CLAUDE_CODE).replace(
                    codex_catalog
                )


if __name__ == "__main__":
    unittest.main()

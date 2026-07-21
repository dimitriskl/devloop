from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Mapping

from devloop.model_catalog import (
    CatalogDiscoveryError,
    CatalogSource,
    CodexModelCatalogAdapter,
    CodexModelCatalogCache,
)


class _FakeCatalogSession:
    def __init__(self, pages: Mapping[str | None, Mapping[str, Any]]) -> None:
        self._pages = pages

    def __enter__(self) -> _FakeCatalogSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def initialize(self) -> None:
        return None

    def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, Any]:
        if method != "model/list":
            raise AssertionError(f"Unexpected method: {method}")
        return self._pages[params.get("cursor")]


class _FailingCatalogSession:
    def __enter__(self) -> _FailingCatalogSession:
        raise ConnectionError("app-server connection closed")

    def __exit__(self, *args: object) -> None:
        return None


class CodexModelCatalogAdapterTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_catalog_timeout_kills_child_retaining_inherited_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            executable = root / "codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess, sys\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'], "
                "stdout=sys.stdout, stderr=sys.stderr)\n"
                "import time; time.sleep(5)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            adapter = CodexModelCatalogAdapter(
                str(executable),
                cwd=root,
                timeout_seconds=0.2,
            )
            started_at = time.monotonic()

            with self.assertRaisesRegex(CatalogDiscoveryError, "Timed out"):
                adapter.discover()

        self.assertLess(time.monotonic() - started_at, 2.0)

    def test_discovery_uses_the_target_repository_configuration_context(self) -> None:
        target_repo = Path("/target/repository")
        received_cwds: list[Path] = []
        pages = {
            None: {
                "data": [
                    {
                        "model": "gpt-5.6-luna",
                        "displayName": "Luna",
                        "description": "Focused implementation model",
                        "hidden": False,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "high", "description": "Deep"}
                        ],
                        "serviceTiers": [],
                    }
                ],
                "nextCursor": None,
            }
        }

        def open_session(cwd: Path) -> _FakeCatalogSession:
            received_cwds.append(cwd)
            return _FakeCatalogSession(pages)

        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=target_repo,
            session_factory=open_session,
        )

        adapter.discover()

        self.assertEqual(received_cwds, [target_repo])

    def test_discovery_loads_every_page_and_exposes_advertised_choices(self) -> None:
        pages = {
            None: {
                "data": [
                    {
                        "id": "luna",
                        "model": "gpt-5.6-luna",
                        "displayName": "Luna",
                        "description": "Focused implementation model",
                        "hidden": False,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "high", "description": "Deep"}
                        ],
                        "serviceTiers": [],
                    }
                ],
                "nextCursor": "page-2",
            },
            "page-2": {
                "data": [
                    {
                        "id": "sol",
                        "model": "gpt-5.6-sol",
                        "displayName": "Sol",
                        "description": "Deep review model",
                        "hidden": False,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "high", "description": "Deep"},
                            {"reasoningEffort": "xhigh", "description": "Deeper"},
                        ],
                        "serviceTiers": [
                            {
                                "id": "priority",
                                "name": "Fast",
                                "description": "1.5x",
                            }
                        ],
                    }
                ],
                "nextCursor": None,
            },
        }
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(pages),
        )

        catalog = adapter.discover()

        self.assertEqual(
            [model.model_id for model in catalog.models],
            ["gpt-5.6-luna", "gpt-5.6-sol"],
        )
        self.assertEqual(catalog.model("gpt-5.6-sol").display_name, "Sol")
        self.assertEqual(
            catalog.model("gpt-5.6-sol").reasoning_efforts,
            ("high", "xhigh"),
        )
        self.assertTrue(catalog.model("gpt-5.6-sol").supports_fast)

    def test_discovery_supports_deprecated_additional_speed_tiers_fallback(self) -> None:
        pages = {
            None: {
                "data": [
                    {
                        "model": "gpt-5.6-sol",
                        "displayName": "Sol",
                        "description": "Deep review model",
                        "hidden": False,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "xhigh", "description": "Deeper"}
                        ],
                        "serviceTiers": [],
                        "additionalSpeedTiers": ["fast"],
                    }
                ],
                "nextCursor": None,
            }
        }
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(pages),
        )

        catalog = adapter.discover()

        self.assertTrue(catalog.model("gpt-5.6-sol").supports_fast)

    def test_discovery_excludes_hidden_models(self) -> None:
        pages = {
            None: {
                "data": [
                    {"model": "hidden-model", "hidden": True},
                    {
                        "model": "gpt-5.6-luna",
                        "displayName": "Luna",
                        "description": "Focused implementation model",
                        "hidden": False,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "high", "description": "Deep"}
                        ],
                        "serviceTiers": [],
                    },
                ],
                "nextCursor": None,
            }
        }
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(pages),
        )

        catalog = adapter.discover()

        self.assertEqual(
            [model.model_id for model in catalog.models],
            ["gpt-5.6-luna"],
        )

    def test_discovery_rejects_terminal_controls_in_model_display_names(self) -> None:
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(
                {
                    None: {
                        "data": [
                            {
                                "model": "gpt-5.6-luna",
                                "displayName": "Luna\x1b]52;c;dGVybWluYWw=\x07",
                                "description": "Focused implementation model",
                                "hidden": False,
                                "supportedReasoningEfforts": [
                                    {
                                        "reasoningEffort": "high",
                                        "description": "Deep",
                                    }
                                ],
                                "serviceTiers": [],
                            }
                        ],
                        "nextCursor": None,
                    }
                }
            ),
        )

        with self.assertRaisesRegex(
            CatalogDiscoveryError,
            "control characters or line breaks",
        ):
            adapter.discover()

    def test_discovery_rejects_terminal_controls_in_model_ids(self) -> None:
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(
                {
                    None: {
                        "data": [
                            {
                                "model": "gpt-5.6-luna\nspoofed-model",
                                "displayName": "Luna",
                                "description": "Focused implementation model",
                                "hidden": False,
                                "supportedReasoningEfforts": [
                                    {
                                        "reasoningEffort": "high",
                                        "description": "Deep",
                                    }
                                ],
                                "serviceTiers": [],
                            }
                        ],
                        "nextCursor": None,
                    }
                }
            ),
        )

        with self.assertRaisesRegex(
            CatalogDiscoveryError,
            "control characters or line breaks",
        ):
            adapter.discover()

    def test_discovery_rejects_terminal_controls_in_reasoning_efforts(self) -> None:
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(
                {
                    None: {
                        "data": [
                            {
                                "model": "gpt-5.6-luna",
                                "displayName": "Luna",
                                "description": "Focused implementation model",
                                "hidden": False,
                                "supportedReasoningEfforts": [
                                    {
                                        "reasoningEffort": "high\x1b[2J",
                                        "description": "Deep",
                                    }
                                ],
                                "serviceTiers": [],
                            }
                        ],
                        "nextCursor": None,
                    }
                }
            ),
        )

        with self.assertRaisesRegex(
            CatalogDiscoveryError,
            "control characters or line breaks",
        ):
            adapter.discover()

    def test_discovery_rejects_terminal_controls_in_stored_model_metadata(self) -> None:
        for field_name in ("description", "service-tier ID", "speed-tier ID"):
            with self.subTest(field_name=field_name):
                raw_model = {
                    "model": "gpt-5.6-luna",
                    "displayName": "Luna",
                    "description": "Focused implementation model",
                    "hidden": False,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "high", "description": "Deep"}
                    ],
                    "serviceTiers": [],
                }
                if field_name == "description":
                    raw_model["description"] = "Focused\nspoofed-status"
                elif field_name == "service-tier ID":
                    raw_model["serviceTiers"] = [
                        {"id": "priority\x1b[2J", "name": "Fast"}
                    ]
                else:
                    raw_model["additionalSpeedTiers"] = ["fast\u202e"]
                adapter = CodexModelCatalogAdapter(
                    "codex",
                    cwd=Path.cwd(),
                    session_factory=lambda _cwd: _FakeCatalogSession(
                        {
                            None: {
                                "data": [raw_model],
                                "nextCursor": None,
                            }
                        }
                    ),
                )

                with self.assertRaisesRegex(
                    CatalogDiscoveryError,
                    "control characters or line breaks",
                ):
                    adapter.discover()

    def test_discovery_rejects_an_empty_selectable_catalog(self) -> None:
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(
                {None: {"data": [], "nextCursor": None}}
            ),
        )

        with self.assertRaisesRegex(
            CatalogDiscoveryError,
            "returned no selectable models",
        ):
            adapter.discover()

    def test_discovery_rejects_a_malformed_model_list_response(self) -> None:
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(
                {None: {"data": {"model": "not-a-list"}, "nextCursor": None}}
            ),
        )

        with self.assertRaisesRegex(
            CatalogDiscoveryError,
            "page has no model list",
        ):
            adapter.discover()

    def test_discovery_rejects_a_repeated_pagination_cursor(self) -> None:
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(
                {
                    None: {"data": [], "nextCursor": "page-2"},
                    "page-2": {"data": [], "nextCursor": "page-2"},
                }
            ),
        )

        with self.assertRaisesRegex(
            CatalogDiscoveryError,
            "repeated pagination cursor",
        ):
            adapter.discover()

    def test_discovery_wraps_adapter_connection_failures(self) -> None:
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FailingCatalogSession(),
        )

        with self.assertRaisesRegex(
            CatalogDiscoveryError,
            "Could not discover.*app-server connection closed",
        ):
            adapter.discover()

    def test_cached_catalog_is_loaded_as_display_only_stale_data(self) -> None:
        adapter = CodexModelCatalogAdapter(
            "codex",
            cwd=Path.cwd(),
            session_factory=lambda _cwd: _FakeCatalogSession(
                {
                    None: {
                        "data": [
                            {
                                "id": "luna",
                                "model": "gpt-5.6-luna",
                                "displayName": "Luna",
                                "description": "Focused implementation model",
                                "hidden": False,
                                "supportedReasoningEfforts": [
                                    {
                                        "reasoningEffort": "high",
                                        "description": "Deep",
                                    }
                                ],
                                "serviceTiers": [],
                            }
                        ],
                        "nextCursor": None,
                    }
                }
            ),
        )
        with tempfile.TemporaryDirectory() as raw:
            cache = CodexModelCatalogCache(Path(raw) / "models.json")
            cache.replace(adapter.discover())

            restored = cache.load()

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertIs(restored.source, CatalogSource.CACHE)
        self.assertFalse(restored.is_fresh)
        self.assertEqual(restored.model("gpt-5.6-luna").display_name, "Luna")

    def test_cached_catalog_rejects_terminal_controls_in_fetched_at(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "models.json"
            path.write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-07-16T12:00:00\nspoofed-status",
                        "models": [
                            {
                                "model_id": "gpt-5.6-luna",
                                "display_name": "Luna",
                                "description": "Focused implementation model",
                                "reasoning_efforts": ["high"],
                                "service_tier_ids": [],
                                "supports_fast": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "control characters or line breaks",
            ):
                CodexModelCatalogCache(path).load()

    def test_cached_catalog_rejects_terminal_controls_in_rendered_model_metadata(
        self,
    ) -> None:
        hostile_values = (
            ("model_id", "gpt-5.6-luna\nspoofed-model"),
            ("display_name", "Luna\x1b]52;c;dGVybWluYWw=\x07"),
            ("reasoning_efforts", ["high\x1b[2J"]),
        )
        for field_name, hostile_value in hostile_values:
            with self.subTest(field_name=field_name), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "models.json"
                model = {
                    "model_id": "gpt-5.6-luna",
                    "display_name": "Luna",
                    "description": "Focused implementation model",
                    "reasoning_efforts": ["high"],
                    "service_tier_ids": [],
                    "supports_fast": False,
                }
                model[field_name] = hostile_value
                path.write_text(
                    json.dumps(
                        {
                            "fetched_at": "2026-07-16T12:00:00",
                            "models": [model],
                        }
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "control characters or line breaks",
                ):
                    CodexModelCatalogCache(path).load()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import cli, interactive_runner
from devloop.codex_runner import RoleResult
from devloop.interactive_runner import HandoffParams, PlanningArtifacts
from devloop.issue_pack import Issue
from devloop.model_catalog import (
    CatalogDiscoveryError,
    CatalogSource,
    CodexModel,
    CodexModelCatalog,
)
from devloop.portable_component_catalog import build_portable_component_catalog
from devloop.portable_workflow import (
    ANALYSIS_STEP_ID,
    DEVELOPMENT_COMPONENT_ID,
    DEVELOPMENT_STEP_ID,
    FastPreference,
    SECURITY_REVIEW_STEP_ID,
    canonical_workflow_hash,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
)
from devloop.state import LoopStateWriter
from devloop.templates import BundleContext, load_preset
from devloop.workflow_editor import WorkflowDraft
from devloop.workflow_defaults import WorkflowDefaultStore
from tests.terminal_safety import (
    HOSTILE_TERMINAL_TEXT,
    assert_terminal_text_is_safe,
)


class PlanningExecutionSettingsTests(unittest.TestCase):
    def test_new_analysis_uses_persisted_settings_and_fresh_target_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            catalog = default_portable_component_catalog()
            document = default_portable_workflow().to_dict()
            analysis = next(
                step
                for step in document["steps"]
                if step["instance_id"] == ANALYSIS_STEP_ID
            )
            analysis["codex_settings"] = {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "fast": "ON",
            }
            analysis["execution_budget"] = {
                "timeout_seconds": 1200,
                "checkpoint_seconds": 150,
            }
            WorkflowDefaultStore(state_path, catalog).replace(
                load_portable_workflow(document, catalog)
            )
            live_catalog = CodexModelCatalog(
                models=(
                    CodexModel("gpt-5.6-luna", "Luna", "", ("high",)),
                    CodexModel(
                        "gpt-5.6-sol",
                        "Sol",
                        "",
                        ("xhigh",),
                    ),
                    CodexModel(
                        "gpt-5.6-terra",
                        "Terra",
                        "",
                        ("high",),
                        advertises_fast=True,
                    ),
                ),
                fetched_at="2026-07-16T12:00:00",
                source=CatalogSource.LIVE,
            )
            parser = interactive_runner.build_parser()
            args = parser.parse_args(["--repo", str(root), "--goal", "plan it"])
            adapter = mock.Mock()
            adapter.discover.return_value = live_catalog
            bundle = mock.Mock(root=root)

            with mock.patch.object(
                interactive_runner.BundleContext,
                "from_file",
                return_value=bundle,
            ), mock.patch.object(
                interactive_runner,
                "plan_state_path",
                return_value=state_path,
            ), mock.patch.object(
                interactive_runner,
                "choose_target_repo",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "apply_branch_strategy",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "snapshot_artifacts",
                return_value={},
            ), mock.patch.object(
                interactive_runner.catalog_module,
                "discover",
                return_value=mock.Mock(),
            ), mock.patch.object(
                interactive_runner.catalog_module,
                "planning_skill_paths",
                return_value=[],
            ), mock.patch.object(
                interactive_runner,
                "build_portable_component_catalog",
                return_value=catalog,
            ), mock.patch.object(
                interactive_runner,
                "CodexModelCatalogAdapter",
                return_value=adapter,
            ) as adapter_type, mock.patch.object(
                interactive_runner,
                "preflight_codex_execution_settings",
                wraps=interactive_runner.preflight_codex_execution_settings,
            ) as preflight, mock.patch.object(
                interactive_runner,
                "run_planning_chat",
                return_value=None,
            ) as run_chat, redirect_stdout(StringIO()):
                result = interactive_runner._run_planning(parser, args)

        self.assertEqual(result, 0)
        adapter_type.assert_called_with("codex", cwd=root)
        adapter.discover.assert_called_once_with()
        preflight.assert_called_once_with(
            mock.ANY,
            catalog,
            live_catalog,
        )
        config = run_chat.call_args.kwargs["config"]
        self.assertEqual(
            config.codex_settings.as_tuple(),
            ("gpt-5.6-terra", "high", FastPreference.ON),
        )
        self.assertEqual(config.execution_budget.timeout_seconds, 1200)
        self.assertEqual(config.execution_budget.checkpoint_seconds, 150)
        self.assertIsNotNone(config.workflow_progress)
        self.assertEqual(
            config.workflow_progress.active_step.display_name,
            "Analysis",
        )
        self.assertEqual(
            config.workflow_progress.active_step.model,
            "gpt-5.6-terra",
        )

    def test_future_run_edits_during_analysis_do_not_replace_handoff_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            component_catalog = default_portable_component_catalog()
            original_workflow = default_portable_workflow()
            WorkflowDefaultStore(state_path, component_catalog).replace(
                original_workflow
            )
            artifacts = PlanningArtifacts(
                prd_path=root / "feature.md",
                issues_index=root / "README.md",
            )
            live_catalog = CodexModelCatalog(
                models=(
                    CodexModel("gpt-5.6-luna", "Luna", "", ("high",)),
                    CodexModel("gpt-5.6-sol", "Sol", "", ("xhigh",)),
                    CodexModel("gpt-5.6-terra", "Terra", "", ("high",)),
                ),
                fetched_at="2026-07-16T12:00:00",
                source=CatalogSource.LIVE,
            )
            adapter = mock.Mock()
            adapter.discover.return_value = live_catalog
            parser = interactive_runner.build_parser()
            args = parser.parse_args(["--repo", str(root), "--goal", "plan it"])

            def edit_future_runs(*_args: object, **_kwargs: object) -> None:
                store = WorkflowDefaultStore(state_path, component_catalog)
                document = store.load().to_dict()
                development = next(
                    step
                    for step in document["steps"]
                    if step["instance_id"] == DEVELOPMENT_STEP_ID
                )
                development["codex_settings"] = {
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "high",
                    "fast": "OFF",
                }
                store.replace(load_portable_workflow(document, component_catalog))

            def planning_chat(**kwargs: object) -> PlanningArtifacts:
                callbacks = kwargs["callbacks"]
                callbacks.open_options()  # type: ignore[union-attr]
                return artifacts

            with mock.patch.object(
                interactive_runner.BundleContext,
                "from_file",
                return_value=mock.Mock(root=root),
            ), mock.patch.object(
                interactive_runner,
                "plan_state_path",
                return_value=state_path,
            ), mock.patch.object(
                interactive_runner,
                "choose_target_repo",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "apply_branch_strategy",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "snapshot_artifacts",
                return_value={},
            ), mock.patch.object(
                interactive_runner.catalog_module,
                "discover",
                return_value=mock.Mock(),
            ), mock.patch.object(
                interactive_runner.catalog_module,
                "planning_skill_paths",
                return_value=[],
            ), mock.patch.object(
                interactive_runner,
                "build_portable_component_catalog",
                return_value=component_catalog,
            ), mock.patch.object(
                interactive_runner,
                "CodexModelCatalogAdapter",
                return_value=adapter,
            ), mock.patch.object(
                interactive_runner,
                "run_options_menu",
                side_effect=edit_future_runs,
            ), mock.patch.object(
                interactive_runner,
                "run_planning_chat",
                side_effect=planning_chat,
            ), mock.patch.object(
                interactive_runner,
                "run_handoff",
                return_value=0,
            ) as run_handoff, redirect_stdout(StringIO()):
                result = interactive_runner._run_planning(parser, args)

            future_workflow = WorkflowDefaultStore(
                state_path,
                component_catalog,
            ).load()

        self.assertEqual(result, 0)
        handed_off_workflow = run_handoff.call_args.kwargs["workflow_snapshot"]
        self.assertEqual(
            handed_off_workflow.step(DEVELOPMENT_STEP_ID).codex_settings.model,
            "gpt-5.6-luna",
        )
        self.assertEqual(
            future_workflow.step(DEVELOPMENT_STEP_ID).codex_settings.model,
            "gpt-5.6-terra",
        )

    def test_new_run_does_not_execute_unsupported_analysis_transformations(
        self,
    ) -> None:
        for transformation in ("duplicate", "delete", "type-change"):
            with (
                self.subTest(transformation=transformation),
                tempfile.TemporaryDirectory() as raw,
            ):
                root = Path(raw)
                state_path = root / "devloop-plan.json"
                component_catalog = default_portable_component_catalog()
                draft = WorkflowDraft(
                    default_portable_workflow(),
                    component_catalog,
                )
                if transformation == "duplicate":
                    draft.duplicate(ANALYSIS_STEP_ID)
                elif transformation == "delete":
                    draft.delete(draft.preview_delete(ANALYSIS_STEP_ID))
                else:
                    draft.change_type(
                        ANALYSIS_STEP_ID,
                        DEVELOPMENT_COMPONENT_ID,
                    )
                state_path.write_text(
                    json.dumps(
                        {
                            "user_workflow_default": draft.workflow.to_dict(),
                            "user_workflow_default_hash": canonical_workflow_hash(
                                draft.workflow
                            ),
                        }
                    ),
                    encoding="utf-8",
                )
                live_catalog = CodexModelCatalog(
                    models=(
                        CodexModel("gpt-5.6-luna", "Luna", "", ("high",)),
                        CodexModel("gpt-5.6-sol", "Sol", "", ("xhigh",)),
                        CodexModel("gpt-5.6-terra", "Terra", "", ("high",)),
                    ),
                    fetched_at="2026-07-16T12:00:00",
                    source=CatalogSource.LIVE,
                )
                adapter = mock.Mock()
                adapter.discover.return_value = live_catalog
                parser = interactive_runner.build_parser()
                args = parser.parse_args(
                    ["--repo", str(root), "--goal", "plan it"]
                )

                with mock.patch.object(
                    interactive_runner.BundleContext,
                    "from_file",
                    return_value=mock.Mock(root=root),
                ), mock.patch.object(
                    interactive_runner,
                    "plan_state_path",
                    return_value=state_path,
                ), mock.patch.object(
                    interactive_runner,
                    "choose_target_repo",
                    return_value=root,
                ), mock.patch.object(
                    interactive_runner,
                    "apply_branch_strategy",
                    return_value=root,
                ), mock.patch.object(
                    interactive_runner,
                    "snapshot_artifacts",
                    return_value={},
                ), mock.patch.object(
                    interactive_runner,
                    "build_portable_component_catalog",
                    return_value=component_catalog,
                ), mock.patch.object(
                    interactive_runner,
                    "CodexModelCatalogAdapter",
                    return_value=adapter,
                ), mock.patch.object(
                    interactive_runner,
                    "read_prompt",
                    return_value="/quit",
                ), mock.patch.object(
                    interactive_runner,
                    "run_planning_chat",
                    return_value=None,
                ) as run_chat, redirect_stdout(StringIO()) as output:
                    result = interactive_runner._run_planning(parser, args)

                self.assertEqual(result, 0)
                run_chat.assert_not_called()
                adapter.discover.assert_not_called()
                self.assertIn(
                    "exactly one WORKFLOW-scoped",
                    output.getvalue(),
                )

    def test_new_run_executes_a_replacement_analysis_step_with_a_new_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            component_catalog = default_portable_component_catalog()
            draft = WorkflowDraft(
                default_portable_workflow(),
                component_catalog,
            )
            duplicate = draft.duplicate(ANALYSIS_STEP_ID)
            draft.set_guidance(
                duplicate.step_instance_id,
                "Replacement planning guidance.",
            )
            draft.delete(draft.preview_delete(ANALYSIS_STEP_ID))
            replacement = WorkflowDefaultStore(
                state_path,
                component_catalog,
            ).replace(draft.workflow)
            live_catalog = CodexModelCatalog(
                models=(
                    CodexModel("gpt-5.6-luna", "Luna", "", ("high",)),
                    CodexModel("gpt-5.6-sol", "Sol", "", ("xhigh",)),
                    CodexModel("gpt-5.6-terra", "Terra", "", ("high",)),
                ),
                fetched_at="2026-07-16T12:00:00",
                source=CatalogSource.LIVE,
            )
            adapter = mock.Mock()
            adapter.discover.return_value = live_catalog
            parser = interactive_runner.build_parser()
            args = parser.parse_args(["--repo", str(root), "--goal", "plan it"])

            with mock.patch.object(
                interactive_runner.BundleContext,
                "from_file",
                return_value=mock.Mock(root=root),
            ), mock.patch.object(
                interactive_runner,
                "plan_state_path",
                return_value=state_path,
            ), mock.patch.object(
                interactive_runner,
                "choose_target_repo",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "apply_branch_strategy",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "snapshot_artifacts",
                return_value={},
            ), mock.patch.object(
                interactive_runner,
                "build_portable_component_catalog",
                return_value=component_catalog,
            ), mock.patch.object(
                interactive_runner,
                "CodexModelCatalogAdapter",
                return_value=adapter,
            ), mock.patch.object(
                interactive_runner,
                "run_planning_chat",
                return_value=None,
            ) as run_chat, redirect_stdout(StringIO()):
                result = interactive_runner._run_planning(parser, args)

        self.assertEqual(result, 0)
        self.assertNotEqual(replacement.start_step_id, ANALYSIS_STEP_ID)
        self.assertEqual(
            replacement.start_step_id,
            duplicate.step_instance_id,
        )
        run_chat.assert_called_once()
        self.assertIn(
            "Replacement planning guidance.",
            run_chat.call_args.kwargs["initial_prompt"],
        )

    def test_analysis_preflight_can_open_options_then_retry_live_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            component_catalog = default_portable_component_catalog()
            invalid_document = default_portable_workflow().to_dict()
            invalid_document["steps"][0]["codex_settings"]["model"] = "missing-model"
            WorkflowDefaultStore(state_path, component_catalog).replace(
                load_portable_workflow(invalid_document, component_catalog)
            )
            live_catalog = CodexModelCatalog(
                models=(
                    CodexModel("gpt-5.6-luna", "Luna", "", ("high",)),
                    CodexModel("gpt-5.6-sol", "Sol", "", ("xhigh",)),
                    CodexModel("gpt-5.6-terra", "Terra", "", ("high",)),
                ),
                fetched_at="2026-07-16T12:00:00",
                source=CatalogSource.LIVE,
            )
            adapter = mock.Mock()
            adapter.discover.side_effect = [
                CatalogDiscoveryError("temporary failure one"),
                CatalogDiscoveryError("temporary failure two"),
                live_catalog,
            ]
            parser = interactive_runner.build_parser()
            args = parser.parse_args(["--repo", str(root), "--goal", "plan it"])

            def repair_workflow(*_args: object, **_kwargs: object) -> None:
                WorkflowDefaultStore(state_path, component_catalog).replace(
                    default_portable_workflow()
                )

            with mock.patch.object(
                interactive_runner.BundleContext,
                "from_file",
                return_value=mock.Mock(root=root),
            ), mock.patch.object(
                interactive_runner,
                "plan_state_path",
                return_value=state_path,
            ), mock.patch.object(
                interactive_runner,
                "choose_target_repo",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "apply_branch_strategy",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "snapshot_artifacts",
                return_value={},
            ), mock.patch.object(
                interactive_runner.catalog_module,
                "discover",
                return_value=mock.Mock(),
            ), mock.patch.object(
                interactive_runner.catalog_module,
                "planning_skill_paths",
                return_value=[],
            ), mock.patch.object(
                interactive_runner,
                "build_portable_component_catalog",
                return_value=component_catalog,
            ), mock.patch.object(
                interactive_runner,
                "CodexModelCatalogAdapter",
                return_value=adapter,
            ), mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["/options", "retry-catalog"],
            ), mock.patch.object(
                interactive_runner,
                "run_options_menu",
                side_effect=repair_workflow,
            ) as options, mock.patch.object(
                interactive_runner,
                "run_planning_chat",
                return_value=None,
            ), redirect_stdout(StringIO()) as output:
                result = interactive_runner._run_planning(parser, args)

        self.assertEqual(result, 0)
        options.assert_called_once()
        self.assertEqual(adapter.discover.call_count, 3)
        self.assertIn("/options", output.getvalue())
        self.assertIn("retry-catalog", output.getvalue())

    def test_analysis_preflight_sanitizes_backend_catalog_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            interactive_runner,
            "read_prompt",
            return_value="/quit",
        ), redirect_stdout(StringIO()) as output:
            workflow = interactive_runner.preflight_analysis_workflow(
                bundle_root=Path(raw),
                state_path=Path(raw) / "devloop-plan.json",
                selection=interactive_runner.catalog_module.Selection.defaults(),
                component_catalog=default_portable_component_catalog(),
                model_catalog_loader=lambda: (_ for _ in ()).throw(
                    CatalogDiscoveryError(HOSTILE_TERMINAL_TEXT)
                ),
            )

        self.assertIsNone(workflow)
        assert_terminal_text_is_safe(self, output.getvalue(), redirected=True)

    def test_analysis_repair_resets_and_applies_a_schema_v1_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            state_path.write_text(
                json.dumps(
                    {
                        "target_repo": "/repo",
                        "user_workflow_default": {
                            "schema": "devloop.portable-workflow/v1",
                        },
                        "user_workflow_default_hash": "invalid",
                    }
                ),
                encoding="utf-8",
            )
            catalog = default_portable_component_catalog()
            live_catalog = CodexModelCatalog(
                models=(
                    CodexModel("gpt-5.6-luna", "Luna", "", ("high",)),
                    CodexModel("gpt-5.6-sol", "Sol", "", ("xhigh",)),
                    CodexModel("gpt-5.6-terra", "Terra", "", ("high",)),
                ),
                fetched_at="2026-07-16T12:00:00",
                source=CatalogSource.LIVE,
            )

            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["/options", "reset-workflow", "apply"],
            ), redirect_stdout(StringIO()):
                workflow = interactive_runner.preflight_analysis_workflow(
                    bundle_root=root,
                    state_path=state_path,
                    selection=interactive_runner.catalog_module.Selection.defaults(),
                    component_catalog=catalog,
                    model_catalog_loader=lambda: live_catalog,
                )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            stored_workflow = WorkflowDefaultStore(state_path, catalog).load()

        self.assertEqual(workflow, default_portable_workflow())
        self.assertEqual(persisted["target_repo"], "/repo")
        self.assertEqual(stored_workflow, default_portable_workflow())

    def test_analysis_repair_cancel_preserves_a_malformed_v2_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            original = json.dumps(
                {
                    "target_repo": "/repo",
                    "user_workflow_default": {
                        "schema": "devloop.portable-workflow/v2",
                        "steps": [],
                    },
                    "user_workflow_default_hash": "invalid",
                },
                indent=2,
            )
            state_path.write_text(original, encoding="utf-8")

            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["/options", "cancel", "/quit"],
            ), redirect_stdout(StringIO()):
                workflow = interactive_runner.preflight_analysis_workflow(
                    bundle_root=root,
                    state_path=state_path,
                    selection=interactive_runner.catalog_module.Selection.defaults(),
                    component_catalog=default_portable_component_catalog(),
                    model_catalog_loader=lambda: self.fail(
                        "Malformed defaults must fail before catalog discovery."
                    ),
                )

            persisted = state_path.read_text(encoding="utf-8")

        self.assertIsNone(workflow)
        self.assertEqual(persisted, original)


class BuildPlanningPromptTests(unittest.TestCase):
    def make_prompt(self) -> str:
        return interactive_runner.build_planning_prompt(
            repo_root=Path("C:/repo"),
            bundle_root=Path("F:/devloop"),
            goal="add login",
            skill_paths=[
                Path("F:/devloop/skills/codex/grill-with-docs/SKILL.md"),
                Path("F:/devloop/skills/codex/to-prd/SKILL.md"),
            ],
            wiki_index=Path("F:/devloop/docs/devloop-self-improvement/wiki/index.md"),
        )

    def test_lists_selected_skills(self) -> None:
        prompt = self.make_prompt()
        self.assertIn("grill-with-docs", prompt)
        self.assertIn("to-prd", prompt)

    def test_planning_prompt_includes_step_guidance_with_visible_precedence(self) -> None:
        prompt = interactive_runner.build_planning_prompt(
            repo_root=Path("C:/repo"),
            bundle_root=Path("F:/devloop"),
            goal="add login",
            skill_paths=[],
            agent_paths=[Path("F:/devloop/agents/codex/security.md")],
            step_guidance="Ask about authentication boundaries.",
            wiki_index=Path("F:/devloop/docs/devloop-self-improvement/wiki/index.md"),
        )

        self.assertIn("agents/codex/security.md", prompt)
        self.assertIn("Ask about authentication boundaries.", prompt)
        self.assertIn("permissions, and safety boundaries", prompt)

    def test_references_wiki_index(self) -> None:
        prompt = self.make_prompt()
        self.assertIn("self-improvement wiki", prompt.lower())
        self.assertIn("index.md", prompt)

    def test_never_asks_user_to_exit(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertNotIn("/quit", prompt)
        self.assertNotIn("ctrl+c", prompt)
        self.assertIn("continues automatically", prompt)

    def test_includes_issue_self_containment_rules(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertIn("self-contained", prompt)
        self.assertIn("fresh codex session", prompt)
        self.assertIn("context window", prompt)

    def test_includes_goal(self) -> None:
        self.assertIn("add login", self.make_prompt())

    def test_settled_existing_analysis_advances_to_prd_and_issues(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertIn("existing analysis is already settled", prompt)
        self.assertIn("move directly to $to-prd and then $to-issues", prompt)
        self.assertIn("do not repeat the interview", prompt)

    def test_names_the_active_product_and_excludes_codexcli_by_default(self) -> None:
        prompt = self.make_prompt().lower()
        self.assertIn("target product: devloop-plan + devloop", prompt)
        self.assertIn("codexcli is a separate application", prompt)
        self.assertIn("do not target codexcli", prompt)


class BuildDevloopArgsTests(unittest.TestCase):
    def make_artifacts(self, root: Path) -> PlanningArtifacts:
        prd = root / "prd" / "feature" / "feature.md"
        prd.parent.mkdir(parents=True)
        prd.write_text("prd", encoding="utf-8")
        issues = root / "prd" / "feature" / "issues" / "README.md"
        issues.parent.mkdir(parents=True)
        issues.write_text("issues", encoding="utf-8")
        return PlanningArtifacts(prd_path=prd, issues_index=issues)

    def test_default_params_run_all_with_worktree_and_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            params = HandoffParams(
                start_issue=None,
                run_all=True,
                use_worktree=True,
                worktree_path=root / "feature-dev",
                branch_name="devloop/feature",
            )
            args = interactive_runner.build_devloop_args(params, artifacts, None)
        self.assertIn("--all", args)
        self.assertIn("--self-improvement-wiki", args)
        self.assertIn("--create-worktree", args)
        self.assertIn("--branch-name", args)
        self.assertNotIn("--no-self-improvement-wiki", args)
        self.assertNotIn("--preset", args)

    def test_start_issue_and_no_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            params = HandoffParams(
                start_issue="0002",
                run_all=False,
                use_worktree=False,
                worktree_path=root / "unused",
                branch_name="unused",
            )
            args = interactive_runner.build_devloop_args(params, artifacts, None)
        self.assertIn("--start-issue", args)
        self.assertIn("0002", args)
        self.assertIn("--no-worktree", args)
        self.assertNotIn("--all", args)

    def test_handoff_summary_reports_pending_issue_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            issue_1 = artifacts.issues_index.parent / "0001-first.md"
            issue_2 = artifacts.issues_index.parent / "0002-second.md"
            issue_1.write_text("# First\n\nCompleted: [ ]\n", encoding="utf-8")
            issue_2.write_text("# Second\n\nCompleted: [x]\n", encoding="utf-8")
            artifacts.issues_index.write_text(
                "- [Issue 0001](./0001-first.md)\n"
                "- [Issue 0002](./0002-second.md)\n",
                encoding="utf-8",
            )
            params = HandoffParams(
                start_issue=None,
                run_all=True,
                use_worktree=False,
                worktree_path=root / "unused",
                branch_name="unused",
            )

            summary = interactive_runner.handoff_issue_summary(params, artifacts)

        self.assertEqual(summary, "1 pending")

    def test_session_preset_added_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            preset = root / "session.preset.json"
            preset.write_text("{}", encoding="utf-8")
            params = HandoffParams(
                start_issue=None,
                run_all=True,
                use_worktree=False,
                worktree_path=root / "unused",
                branch_name="unused",
            )
            args = interactive_runner.build_devloop_args(params, artifacts, preset)
        self.assertIn("--preset", args)
        self.assertIn(str(preset), args)

    def test_custom_codex_executable_is_forwarded_to_devloop_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            params = HandoffParams(
                start_issue=None,
                run_all=True,
                use_worktree=False,
                worktree_path=root / "unused",
                branch_name="unused",
            )

            args = interactive_runner.build_devloop_args(
                params,
                artifacts,
                None,
                "/opt/custom-codex",
            )

        codex_index = args.index("--codex")
        self.assertEqual(args[codex_index + 1], "/opt/custom-codex")

    def test_handoff_passes_analysis_snapshot_and_custom_codex_to_devloop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            issue = artifacts.issues_index.parent / "0001-run.md"
            issue.write_text("# Run\n\nCompleted: [ ]\n", encoding="utf-8")
            artifacts.issues_index.write_text(
                "[Issue 0001](./0001-run.md)\n",
                encoding="utf-8",
            )
            workflow_snapshot = default_portable_workflow()

            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                return_value="",
            ), mock.patch(
                "devloop.cli.main",
                return_value=17,
            ) as devloop_main, redirect_stdout(StringIO()):
                result = interactive_runner.run_handoff(
                    root,
                    root,
                    artifacts,
                    interactive_runner.catalog_module.Selection.defaults(),
                    root / "devloop-plan.json",
                    codex="/opt/custom-codex",
                    workflow_snapshot=workflow_snapshot,
                )

        self.assertEqual(result, 17)
        launched_args = devloop_main.call_args.args[0]
        codex_index = launched_args.index("--codex")
        self.assertEqual(launched_args[codex_index + 1], "/opt/custom-codex")
        self.assertIs(
            devloop_main.call_args.kwargs["workflow_snapshot"],
            workflow_snapshot,
        )

    def test_handoff_options_opens_workflow_editor_with_current_run_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_artifacts(root)
            issue = artifacts.issues_index.parent / "0001-edit.md"
            issue.write_text("# Edit\n\nCompleted: [ ]\n", encoding="utf-8")
            artifacts.issues_index.write_text(
                "[Issue 0001](./0001-edit.md)\n",
                encoding="utf-8",
            )
            current_workflow = default_portable_workflow()
            state_path = root / "devloop-plan.json"
            selection = interactive_runner.catalog_module.Selection.defaults()

            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["/options", "/quit"],
            ), mock.patch.object(
                interactive_runner,
                "load_current_run_workflow",
                return_value=current_workflow,
            ), mock.patch.object(
                interactive_runner,
                "run_options_menu",
            ) as options, mock.patch.object(
                interactive_runner,
                "CodexModelCatalogAdapter",
            ) as adapter_type, redirect_stdout(StringIO()):
                result = interactive_runner.run_handoff(
                    root,
                    root,
                    artifacts,
                    selection,
                    state_path,
                    codex="/opt/custom-codex",
                )

        self.assertEqual(result, 0)
        options.assert_called_once_with(
            root,
            selection,
            state_path,
            current_workflow=current_workflow,
            component_catalog=mock.ANY,
            model_catalog_loader=mock.ANY,
        )
        adapter_type.assert_called_once_with("/opt/custom-codex", cwd=root)

    def test_handoff_options_loads_current_run_from_reused_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_repo = root / "source"
            source_repo.mkdir()
            artifacts = self.make_artifacts(source_repo)
            issue = artifacts.issues_index.parent / "0001-edit.md"
            issue.write_text("# Edit\n\nCompleted: [ ]\n", encoding="utf-8")
            artifacts.issues_index.write_text(
                "[Issue 0001](./0001-edit.md)\n",
                encoding="utf-8",
            )
            worktree = root / "feature-dev"
            worktree_issues_index = worktree / artifacts.issues_index.relative_to(
                source_repo
            )
            worktree_issues_index.parent.mkdir(parents=True)
            worktree_issues_index.write_text(
                artifacts.issues_index.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            worktree_state = LoopStateWriter(worktree_issues_index)
            worktree_state.record_resolved_workflow(
                default_portable_workflow(),
                default_portable_component_catalog(),
            )
            source_state_path = artifacts.issues_index.with_name(
                "README.loop.state.json"
            )

            with mock.patch.object(
                interactive_runner,
                "default_worktree_path",
                return_value=worktree,
            ), mock.patch.object(
                interactive_runner,
                "resolve_existing_worktree",
                return_value=worktree,
            ), mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["/options", "current", "cancel", "/quit"],
            ), mock.patch.object(
                interactive_runner,
                "terminal_width",
                return_value=100,
            ), mock.patch.object(
                interactive_runner,
                "CodexModelCatalogAdapter",
            ) as adapter_type, redirect_stdout(StringIO()) as output:
                adapter_type.return_value.discover.side_effect = (
                    CatalogDiscoveryError("offline test catalog")
                )
                result = interactive_runner.run_handoff(
                    root,
                    source_repo,
                    artifacts,
                    interactive_runner.catalog_module.Selection.defaults(),
                    root / "devloop-plan.json",
                )
            source_state_was_created = source_state_path.exists()

        self.assertEqual(result, 0)
        self.assertFalse(source_state_was_created)
        self.assertIn("Current Run (read-only)", output.getvalue())
        self.assertIn("Viewing Current Run settings.", output.getvalue())


class WorktreePromptTests(unittest.TestCase):
    def test_worktree_location_asks_parent_path_then_folder_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=[str(parent), "feature-dev"],
            ):
                result = interactive_runner.ask_worktree_location("Implementation worktree")

        self.assertEqual(result, (parent / "feature-dev").resolve())

    def test_worktree_location_keeps_default_parent_and_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            default = Path(raw) / "feature-dev"
            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["", ""],
            ):
                result = interactive_runner.ask_worktree_location(
                    "Implementation worktree",
                    default=default,
                )

        self.assertEqual(result, default.resolve())

    def test_worktree_location_can_default_parent_without_default_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["", "feature-dev"],
            ):
                result = interactive_runner.ask_worktree_location(
                    "New worktree",
                    default_parent=parent,
                )

        self.assertEqual(result, (parent / "feature-dev").resolve())

    def test_worktree_location_remembers_parent_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            parent = root / "worktrees"
            with mock.patch.object(interactive_runner, "plan_state_path", return_value=state_path), \
                 mock.patch.object(
                     interactive_runner,
                     "read_prompt",
                     side_effect=[str(parent), "feature-dev"],
                 ):
                result = interactive_runner.ask_worktree_location(
                    "New worktree",
                    remember_parent=True,
                )
                restored = interactive_runner.load_last_worktree_parent()

        self.assertEqual(result, (parent / "feature-dev").resolve())
        self.assertEqual(restored, parent.resolve())

    def test_branch_name_accepts_human_text_and_sanitizes_for_git(self) -> None:
        with mock.patch.object(
            interactive_runner,
            "read_prompt",
            return_value="Reset Queue",
        ):
            with redirect_stdout(StringIO()) as output:
                result = interactive_runner.ask_branch_name("New worktree branch name")

        self.assertEqual(result, "Reset-Queue")
        self.assertIn("Using branch name: Reset-Queue", output.getvalue())

    def test_create_or_reuse_worktree_reuses_existing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            with mock.patch.object(
                interactive_runner,
                "resolve_existing_worktree",
                return_value=target.resolve(),
            ), mock.patch.object(interactive_runner, "run_git") as run_git:
                result = interactive_runner.create_or_reuse_worktree(root, target, "Reset-Queue")

        self.assertEqual(result, target.resolve())
        run_git.assert_not_called()

    def test_create_or_reuse_worktree_uses_existing_branch_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "feature-dev"
            command = ["git", "worktree", "add", str(target), "Reset-Queue"]
            with mock.patch.object(interactive_runner, "resolve_existing_worktree", return_value=None), \
                 mock.patch.object(
                     interactive_runner,
                     "build_worktree_add_command",
                     return_value=command,
                 ), \
                 mock.patch.object(interactive_runner, "run_git") as run_git:
                result = interactive_runner.create_or_reuse_worktree(root, target, "Reset-Queue")

        self.assertEqual(result, target.resolve())
        run_git.assert_called_once_with(command[1:], cwd=root)


class BranchStrategyTests(unittest.TestCase):
    def test_existing_current_branch_is_reused(self) -> None:
        repo_root = Path("C:/repo")
        with mock.patch.object(
            interactive_runner,
            "current_branch",
            return_value="Basic-analysis",
        ), mock.patch.object(
            interactive_runner,
            "ask_choice",
            return_value="2",
        ), mock.patch.object(
            interactive_runner,
            "ask_branch_name",
            return_value="Basic-analysis",
        ), mock.patch.object(
            interactive_runner,
            "run_git",
        ) as run_git, redirect_stdout(StringIO()) as output:
            result = interactive_runner.apply_branch_strategy(repo_root)

        self.assertEqual(result, repo_root)
        run_git.assert_not_called()
        self.assertIn("Using existing branch: Basic-analysis", output.getvalue())

    def test_existing_other_branch_is_checked_out_without_create_flag(self) -> None:
        repo_root = Path("C:/repo")
        with mock.patch.object(
            interactive_runner,
            "current_branch",
            return_value="main",
        ), mock.patch.object(
            interactive_runner,
            "ask_choice",
            return_value="2",
        ), mock.patch.object(
            interactive_runner,
            "ask_branch_name",
            return_value="Basic-analysis",
        ), mock.patch.object(
            interactive_runner,
            "branch_exists",
            return_value=True,
        ), mock.patch.object(
            interactive_runner,
            "run_git",
        ) as run_git, redirect_stdout(StringIO()) as output:
            result = interactive_runner.apply_branch_strategy(repo_root)

        self.assertEqual(result, repo_root)
        run_git.assert_called_once_with(["checkout", "Basic-analysis"], cwd=repo_root)
        self.assertIn("Using existing branch: Basic-analysis", output.getvalue())

    def test_missing_branch_is_created(self) -> None:
        repo_root = Path("C:/repo")
        with mock.patch.object(
            interactive_runner,
            "current_branch",
            return_value="main",
        ), mock.patch.object(
            interactive_runner,
            "ask_choice",
            return_value="2",
        ), mock.patch.object(
            interactive_runner,
            "ask_branch_name",
            return_value="New-analysis",
        ), mock.patch.object(
            interactive_runner,
            "branch_exists",
            return_value=False,
        ), mock.patch.object(
            interactive_runner,
            "run_git",
        ) as run_git:
            result = interactive_runner.apply_branch_strategy(repo_root)

        self.assertEqual(result, repo_root)
        run_git.assert_called_once_with(
            ["checkout", "-b", "New-analysis"],
            cwd=repo_root,
        )


class PlanStateTests(unittest.TestCase):
    @staticmethod
    def write_custom_component_bundle(root: Path) -> Path:
        preset_path = root / "presets" / "generic-minimal.json"
        preset_path.parent.mkdir(parents=True)
        skill_path = root / "skills" / "codex" / "security-review" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text("# Security review\n", encoding="utf-8")
        preset_path.write_text(
            json.dumps(
                {
                    "name": "custom-portable-components",
                    "roles": {
                        "security-review": {
                            "step_adapter": "reviewer",
                            "component_id": "example.security-review",
                            "display_name": "Security Review",
                            "skills": [
                                "skills/codex/security-review/SKILL.md"
                            ],
                            "agents": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return preset_path

    def test_options_discovers_a_custom_portable_component_from_an_installed_role(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.write_custom_component_bundle(root)
            state_path = root / "devloop-plan.json"
            output = StringIO()

            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["add", "5", "apply"],
            ), mock.patch.object(
                interactive_runner,
                "terminal_width",
                return_value=100,
            ), redirect_stdout(output):
                interactive_runner.run_options_menu(
                    root,
                    interactive_runner.catalog_module.Selection.defaults(),
                    state_path,
                )

            stored = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertIn("Security Review (ISSUE)", output.getvalue())
        self.assertEqual(
            stored["user_workflow_default"]["steps"][-1]["component_id"],
            "example.security-review",
        )

    def test_custom_portable_component_snapshots_and_executes_through_the_issue_loop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            preset_path = self.write_custom_component_bundle(root)
            configuration_path = root / "devloop-plan.json"
            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["add", "5", "apply"],
            ), mock.patch.object(
                interactive_runner,
                "terminal_width",
                return_value=100,
            ), redirect_stdout(StringIO()):
                interactive_runner.run_options_menu(
                    root,
                    interactive_runner.catalog_module.Selection.defaults(),
                    configuration_path,
                )

            issue_path = root / "0004-custom.md"
            issue_path.write_text(
                "# Custom component\n\nCompleted: [ ]\n",
                encoding="utf-8",
            )
            issue = Issue("0004", "Custom component", issue_path, False)
            issues_index = root / "feature-issues.md"
            issues_index.write_text("[0004](./0004-custom.md)\n", encoding="utf-8")
            state_writer = LoopStateWriter(issues_index)
            preset = load_preset(preset_path)
            installed_catalog = build_portable_component_catalog(
                root,
                preset.roles,
            )
            cli.resolve_run_workflow(
                state_writer,
                installed_catalog,
                user_workflow_path=configuration_path,
            )
            current_workflow = interactive_runner.load_current_run_workflow(
                issues_index,
                component_catalog=installed_catalog,
            )

            class PassingRunner:
                dry_run = False
                bundle = BundleContext(
                    root=root,
                    prompts=root / "prompts",
                    schemas=root / "schemas",
                )
                preset = load_preset(preset_path)

                def __init__(self) -> None:
                    self.calls: list[dict[str, object]] = []

                def run_role(self, **arguments: object) -> RoleResult:
                    self.calls.append(arguments)
                    return RoleResult(status="PASS")

            runner = PassingRunner()
            with redirect_stdout(StringIO()):
                result = cli.run_issue(
                    issue,
                    runner,  # type: ignore[arg-type]
                    state_writer,
                    max_passes=1,
                )

            dry_issue_path = root / "0005-dry.md"
            dry_issue_path.write_text("# Dry run\n", encoding="utf-8")
            dry_issue = Issue("0005", "Dry run", dry_issue_path, False)
            dry_index = root / "dry-issues.md"
            dry_index.write_text("[0005](./0005-dry.md)\n", encoding="utf-8")
            dry_state_writer = LoopStateWriter(dry_index)
            cli.resolve_run_workflow(
                dry_state_writer,
                installed_catalog,
                user_workflow_path=configuration_path,
            )

            class DryRunRunner:
                dry_run = True
                bundle = runner.bundle
                preset = runner.preset

                def __init__(self) -> None:
                    self.steps: list[tuple[object, ...]] = []

                def render_dry_run_prompts(
                    self,
                    _issue: Issue,
                    steps: object,
                ) -> None:
                    self.steps = list(steps)  # type: ignore[arg-type]

            dry_runner = DryRunRunner()
            with redirect_stdout(StringIO()):
                cli.run_issue(
                    dry_issue,
                    dry_runner,  # type: ignore[arg-type]
                    dry_state_writer,
                    max_passes=1,
                )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            current_workflow.primary_path()[-1].component_id,
            "example.security-review",
        )
        self.assertEqual(
            [call["role"] for call in runner.calls],
            ["coder", "reviewer", "reviewer", "qa", "security-review"],
        )
        self.assertEqual(runner.calls[-1]["role_adapter"], "reviewer")
        self.assertEqual(
            dry_runner.steps[-1][:2],
            ("security-review", "reviewer"),
        )

    def test_options_opens_the_public_workflow_editor_and_applies_future_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            selection = interactive_runner.catalog_module.Selection.defaults()
            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["3", "rename", "Planner Review", "apply"],
            ), mock.patch.object(
                interactive_runner,
                "terminal_width",
                return_value=100,
                create=True,
            ), redirect_stdout(StringIO()) as output:
                interactive_runner.run_options_menu(root, selection, state_path)

            stored = WorkflowDefaultStore(
                state_path,
                default_portable_component_catalog(),
            ).load()
            restored_selection = interactive_runner.catalog_module.load_selection(
                state_path
            )

        self.assertIn("Workflow Editor", output.getvalue())
        self.assertNotIn("Planning skills", output.getvalue())
        self.assertEqual(
            stored.step(SECURITY_REVIEW_STEP_ID).display_name,
            "Planner Review",
        )
        self.assertEqual(restored_selection.to_dict(), selection.to_dict())

    def test_active_options_edits_future_runs_without_mutating_the_run_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issues_index = root / "feature-issues.md"
            issues_index.write_text("", encoding="utf-8")
            state_path = root / "devloop-plan.json"
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(issues_index)
            writer.record_resolved_workflow(
                default_portable_workflow(),
                catalog,
            )
            writer.state["issues"]["0001"] = {
                "status": "IN_PROGRESS",
                "current_step_instance_id": str(SECURITY_REVIEW_STEP_ID),
                "current_pass": 3,
            }
            writer.flush()
            before = writer.state_path.read_bytes()
            current_workflow = interactive_runner.load_current_run_workflow(
                issues_index
            )

            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=["3", "rename", "Next Run Review", "apply"],
            ), mock.patch.object(
                interactive_runner,
                "terminal_width",
                return_value=100,
            ), redirect_stdout(StringIO()) as output:
                interactive_runner.run_options_menu(
                    root,
                    interactive_runner.catalog_module.Selection.defaults(),
                    state_path,
                    current_workflow=current_workflow,
                )

            stored = WorkflowDefaultStore(state_path, catalog).load()
            after = writer.state_path.read_bytes()

        self.assertIn("Current Run (read-only)", output.getvalue())
        self.assertEqual(before, after)
        self.assertEqual(
            stored.step(SECURITY_REVIEW_STEP_ID).display_name,
            "Next Run Review",
        )

    def test_cancel_discards_staged_capability_and_workflow_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_path = Path(raw) / "devloop-plan.json"
            bundle_root = Path(__file__).resolve().parents[1]
            selection = interactive_runner.catalog_module.Selection.defaults()
            original = selection.to_dict()

            with mock.patch.object(
                interactive_runner,
                "read_prompt",
                side_effect=[
                    "capabilities",
                    "1",
                    "angular-typescript-developer",
                    "1",
                    "4",
                    "cancel",
                ],
            ), mock.patch.object(
                interactive_runner,
                "terminal_width",
                return_value=100,
            ), redirect_stdout(StringIO()):
                interactive_runner.run_options_menu(
                    bundle_root,
                    selection,
                    state_path,
                )

        self.assertEqual(selection.to_dict(), original)
        self.assertFalse(state_path.exists())

    def test_save_last_target_repo_preserves_selection_and_worktree_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "devloop-plan.json"
            repo = root / "repo"
            repo.mkdir()
            state_path.write_text(
                json.dumps(
                    {
                        "selection": {"planning_skills": ["grill-with-docs"]},
                        "last_worktree_parent": str(root / "worktrees"),
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(interactive_runner, "plan_state_path", return_value=state_path):
                interactive_runner.save_last_target_repo(repo)

            data = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(data["target_repo"], str(repo))
        self.assertEqual(data["selection"], {"planning_skills": ["grill-with-docs"]})
        self.assertEqual(data["last_worktree_parent"], str(root / "worktrees"))

    def test_default_worktree_path_uses_remembered_parent_when_supplied(self) -> None:
        root = Path("E:/LocalCode/eConnectorV2")
        parent = Path("E:/Worktrees")
        self.assertEqual(
            interactive_runner.default_worktree_path(root, "reset-queue", parent=parent),
            parent / "eConnectorV2-reset-queue-dev",
        )


class ResumePlanningTests(unittest.TestCase):
    def make_prd_pair(
        self,
        root: Path,
        name: str,
        *,
        completed: tuple[bool, ...],
    ) -> PlanningArtifacts:
        prd = root / "prd" / name / f"{name}.md"
        prd.parent.mkdir(parents=True)
        prd.write_text(f"# {name}\n", encoding="utf-8")
        issues = prd.parent / "issues" / "README.md"
        issues.parent.mkdir()
        links: list[str] = []
        for number, is_completed in enumerate(completed, start=1):
            issue = issues.parent / f"{number:04d}-issue.md"
            marker = "x" if is_completed else " "
            issue.write_text(
                f"# Issue {number:04d}\n\nCompleted: [{marker}]\n",
                encoding="utf-8",
            )
            links.append(f"- [Issue {number:04d}](./{issue.name})")
        issues.write_text("\n".join(links) + "\n", encoding="utf-8")
        return PlanningArtifacts(prd.resolve(), issues.resolve())

    def test_discovers_only_prd_packs_with_unfinished_issues(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            unfinished = self.make_prd_pair(
                root,
                "unfinished",
                completed=(True, False, False),
            )
            self.make_prd_pair(root, "finished", completed=(True, True))

            candidates = interactive_runner.find_resume_candidates(root)

        self.assertEqual([item.artifacts for item in candidates], [unfinished])
        self.assertEqual(candidates[0].completed_issues, 1)
        self.assertEqual(candidates[0].pending_issues, 2)
        self.assertEqual(candidates[0].total_issues, 3)

    def test_does_not_offer_an_explicit_codexcli_prd_to_portable_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(root, "codexcli-only", completed=(False,))
            artifacts.prd_path.write_text(
                "# CodexCLI work\n\n"
                "## Target Product\n\n"
                "The separately installed `codexcli` Textual application.\n",
                encoding="utf-8",
            )

            candidates = interactive_runner.find_resume_candidates(root)

        self.assertEqual(candidates, [])

    def test_uses_loop_state_to_describe_the_active_issue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(root, "active", completed=(True, False))
            state_path = artifacts.issues_index.with_name("README.loop.state.json")
            state_path.write_text(
                json.dumps(
                    {
                        "issues": {
                            "0001": {"status": "Completed"},
                            "0002": {"status": "In Progress: reviewer"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            candidate = interactive_runner.find_resume_candidates(root)[0]

        self.assertEqual(candidate.active_issue, "0002")
        self.assertEqual(candidate.active_status, "In Progress: reviewer")

    def test_resume_candidate_recognizes_canonical_portable_issue_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(root, "active-v2", completed=(False,))
            state_path = artifacts.issues_index.with_name("README.loop.state.json")
            state_path.write_text(
                json.dumps(
                    {
                        "issues": {
                            "0001": {
                                "status": "IN_PROGRESS",
                                "current_step_instance_id": (
                                    "e7f9d3a2-1b64-48c5-9d20-6a7b8c9d0e02"
                                ),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            candidate = interactive_runner.find_resume_candidates(root)[0]

        self.assertEqual(candidate.active_issue, "0001")
        self.assertEqual(candidate.active_status, "IN_PROGRESS")

    def test_status_display_recognizes_canonical_portable_issue_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(
                root,
                "status-v2",
                completed=(False, False, False),
            )
            state_path = artifacts.issues_index.with_name("README.loop.state.json")
            state_path.write_text(
                json.dumps(
                    {
                        "issues": {
                            "0001": {"status": "COMPLETED"},
                            "0002": {"status": "BLOCKED"},
                            "0003": {"status": "IN_PROGRESS"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = StringIO()

            with redirect_stdout(output):
                interactive_runner.print_prd_status(artifacts)

        rendered = output.getvalue()
        self.assertIn("Completed issues: 0001", rendered)
        self.assertIn("Blocked issues: 0002", rendered)
        self.assertIn("In-progress issues: 0003", rendered)

    def test_discovers_a_flat_local_issue_pack(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issues_root = root / "issues"
            issues_root.mkdir()
            prd = issues_root / "flat-feature.md"
            index = issues_root / "flat-feature-issues.md"
            issue = issues_root / "0001-flat-feature.md"
            prd.write_text("# Flat feature\n", encoding="utf-8")
            issue.write_text("# Issue\n\nCompleted: [ ]\n", encoding="utf-8")
            index.write_text(f"[Issue 0001](./{issue.name})\n", encoding="utf-8")

            candidates = interactive_runner.find_resume_candidates(root)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].artifacts.prd_path, prd.resolve())
        self.assertEqual(candidates[0].artifacts.issues_index, index.resolve())
        self.assertEqual(interactive_runner.artifact_slug(candidates[0].artifacts), "flat-feature")

    def test_startup_resume_returns_the_selected_unfinished_prd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(root, "resume-me", completed=(False,))
            with mock.patch.object(
                interactive_runner,
                "ask_choice",
                side_effect=["2", "1"],
            ):
                selected = interactive_runner.choose_startup_artifacts(root)

        self.assertEqual(selected, artifacts)

    def test_startup_resume_skips_new_analysis_and_enters_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = self.make_prd_pair(root, "resume-me", completed=(False,))
            parser = interactive_runner.build_parser()
            args = parser.parse_args(["--repo", str(root)])
            bundle = mock.Mock(root=root)
            state_path = root / "devloop-plan.json"
            with mock.patch.object(
                interactive_runner.BundleContext,
                "from_file",
                return_value=bundle,
            ), mock.patch.object(
                interactive_runner,
                "plan_state_path",
                return_value=state_path,
            ), mock.patch.object(
                interactive_runner,
                "choose_target_repo",
                return_value=root,
            ), mock.patch.object(
                interactive_runner,
                "choose_startup_artifacts",
                return_value=artifacts,
            ), mock.patch.object(
                interactive_runner,
                "current_branch",
                return_value="main",
            ), mock.patch.object(
                interactive_runner,
                "print_prd_status",
            ), mock.patch.object(
                interactive_runner,
                "run_handoff",
                return_value=23,
            ) as run_handoff, mock.patch.object(
                interactive_runner,
                "apply_branch_strategy",
            ) as apply_branch_strategy, mock.patch.object(
                interactive_runner,
                "run_planning_chat",
            ) as run_planning_chat:
                result = interactive_runner._run_planning(parser, args)

        self.assertEqual(result, 23)
        apply_branch_strategy.assert_not_called()
        run_planning_chat.assert_not_called()
        run_handoff.assert_called_once()


class FindNewArtifactsTests(unittest.TestCase):
    def make_prd_pair(self, root: Path, name: str) -> None:
        prd = root / "prd" / name / f"{name}.md"
        prd.parent.mkdir(parents=True)
        prd.write_text("prd", encoding="utf-8")
        issues = root / "prd" / name / "issues" / "README.md"
        issues.parent.mkdir(parents=True)
        issues.write_text("issues", encoding="utf-8")

    def test_worktree_checkout_of_old_prd_is_not_detected(self) -> None:
        # Simulates `git worktree add` materializing a pre-existing PRD pair with
        # fresh (now) mtimes moments before started_at is captured. The snapshot is
        # taken first (mtimes are naturally >= started_at - slack), so the probe
        # must ignore it despite the fresh checkout mtimes.
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_prd_pair(root, "old-feature")
            baseline = interactive_runner.snapshot_artifacts(root)
            started_at = time.time()
            result = interactive_runner.find_new_artifacts(root, started_at, baseline)
        self.assertEqual(result, [])

    def test_new_pair_written_after_snapshot_is_detected(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline = interactive_runner.snapshot_artifacts(root)
            started_at = time.time() - 1
            self.make_prd_pair(root, "new-feature")
            result = interactive_runner.find_new_artifacts(root, started_at, baseline)
        self.assertEqual(len(result), 1)
        self.assertTrue(str(result[0].prd_path).endswith("new-feature.md"))

    def test_snapshotted_pair_retouched_forward_is_detected(self) -> None:
        import os
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_prd_pair(root, "edited-feature")
            baseline = interactive_runner.snapshot_artifacts(root)
            started_at = time.time()
            # Codex edits the snapshotted PRD after the chat begins: mtime advances.
            future = time.time() + 60
            prd = root / "prd" / "edited-feature" / "edited-feature.md"
            issues = root / "prd" / "edited-feature" / "issues" / "README.md"
            os.utime(prd, (future, future))
            os.utime(issues, (future, future))
            result = interactive_runner.find_new_artifacts(root, started_at, baseline)
        self.assertEqual(len(result), 1)
        self.assertTrue(str(result[0].prd_path).endswith("edited-feature.md"))

    def test_readme_fallback_index_is_ignored_by_probe(self) -> None:
        # A pair whose only index is prd/<name>/README.md (no issues/ dir) must be
        # ignored by the live probe, even though the --prd/manual paths accept it.
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prd_folder = root / "prd" / "fallback"
            prd_folder.mkdir(parents=True)
            (prd_folder / "fallback.md").write_text("prd", encoding="utf-8")
            (prd_folder / "README.md").write_text("index", encoding="utf-8")
            started_at = time.time() - 1
            result = interactive_runner.find_new_artifacts(root, started_at, {})
        self.assertEqual(result, [])

    def test_preexisting_old_prd_is_not_detected(self) -> None:
        # An old pair (stale mtimes, absent from a fresh worktree snapshot) that is
        # not fresh enough is filtered out by the freshness slack alone.
        import os
        import time

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.make_prd_pair(root, "old-feature")
            past = time.time() - 3600
            for path in (root / "prd").rglob("*"):
                os.utime(path, (past, past))
            os.utime(root / "prd" / "old-feature", (past, past))
            started_at = time.time()
            result = interactive_runner.find_new_artifacts(root, started_at, {})
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()

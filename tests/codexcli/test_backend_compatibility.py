from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from devloop.application.doctor import check_backend_compatibility
from devloop.domain.doctor import DoctorCheckStatus
from devloop.execution.compatibility import (
    COMPATIBILITY_PROFILE_SCHEMA,
    STANDARD_WORKFLOW_BACKEND_CONTRACT,
    CompatibilityCache,
    CompatibilityCacheKey,
    CompatibilityError,
    InstalledSchemaProfiler,
    ProtocolCapability,
    validate_strict_output_schema,
)


def _write_schema_bundle(
    root: Path,
    *,
    methods: dict[str, tuple[str, tuple[str, ...]]],
) -> None:
    client_variants: list[dict[str, object]] = []
    server_variants: list[dict[str, object]] = []
    for method, (params_name, fields) in methods.items():
        variants = server_variants if method.startswith("item/") else client_variants
        variants.append(
            {
                "type": "object",
                "properties": {
                    "method": {"enum": [method]},
                    "params": {"$ref": f"#/definitions/{params_name}"},
                },
            }
        )
        params_path = root / "v2" / f"{params_name}.json"
        params_path.parent.mkdir(parents=True, exist_ok=True)
        params_path.write_text(
            json.dumps(
                {
                    "type": "object",
                    "required": list(fields),
                    "properties": {field: {"type": "string"} for field in fields},
                }
            ),
            encoding="utf-8",
        )
    (root / "ClientRequest.json").write_text(
        json.dumps({"oneOf": client_variants}),
        encoding="utf-8",
    )
    (root / "ServerRequest.json").write_text(
        json.dumps({"oneOf": server_variants}),
        encoding="utf-8",
    )


def test_profile_reports_every_missing_required_method_and_field(tmp_path: Path) -> None:
    stable = tmp_path / "stable"
    experimental = tmp_path / "experimental"
    stable.mkdir()
    experimental.mkdir()
    methods = {
        "initialize": ("InitializeParams", ("clientInfo",)),
        "account/read": ("GetAccountParams", ("refreshToken",)),
    }
    _write_schema_bundle(stable, methods=methods)
    _write_schema_bundle(experimental, methods=methods)

    profile = InstalledSchemaProfiler(STANDARD_WORKFLOW_BACKEND_CONTRACT).profile(
        stable,
        experimental,
        codex_version="9.9.9",
    )

    assert profile.schema == COMPATIBILITY_PROFILE_SCHEMA
    assert profile.compatible is False
    codes = {finding.code for finding in profile.findings}
    assert "missing-method:thread/start" in codes
    assert "missing-field:initialize:capabilities" in codes
    assert "missing-method:turn/start" in codes


def test_profile_marks_a_required_experimental_field_as_negotiated(tmp_path: Path) -> None:
    stable = tmp_path / "stable"
    experimental = tmp_path / "experimental"
    stable.mkdir()
    experimental.mkdir()
    required = {
        **STANDARD_WORKFLOW_BACKEND_CONTRACT.required_client_fields,
        **STANDARD_WORKFLOW_BACKEND_CONTRACT.required_server_fields,
    }
    stable_methods: dict[str, tuple[str, tuple[str, ...]]] = {}
    experimental_methods: dict[str, tuple[str, tuple[str, ...]]] = {}
    for method, fields in required.items():
        params_name = "".join(part.title() for part in method.replace("/", " ").split()) + "Params"
        stable_fields = tuple(field for field in fields if field != "excludeTurns")
        stable_methods[method] = (params_name, stable_fields)
        experimental_methods[method] = (params_name, tuple(fields))
    _write_schema_bundle(stable, methods=stable_methods)
    _write_schema_bundle(experimental, methods=experimental_methods)

    profile = InstalledSchemaProfiler(STANDARD_WORKFLOW_BACKEND_CONTRACT).profile(
        stable,
        experimental,
        codex_version="9.9.9",
    )

    assert profile.compatible is True
    assert ProtocolCapability.EXPERIMENTAL_API in profile.capabilities


def test_cache_reuses_only_an_exact_profile_key(tmp_path: Path) -> None:
    cache = CompatibilityCache(tmp_path)
    profile = InstalledSchemaProfiler.empty_compatible_profile(
        codex_version="1.2.3",
        workflow_contract_version="contract/v1",
    )
    key = CompatibilityCacheKey(
        codex_version="1.2.3",
        operating_system="linux",
        filesystem="volume-7",
        permission_profile=":workspace",
        probe_version="probe/v1",
        workflow_contract_hash=profile.workflow_contract_hash,
    )
    cache.save(key, profile)

    assert cache.load(key) == profile
    assert cache.load(
        CompatibilityCacheKey(
            codex_version="1.2.4",
            operating_system="linux",
            filesystem="volume-7",
            permission_profile=":workspace",
            probe_version="probe/v1",
            workflow_contract_hash=profile.workflow_contract_hash,
        )
    ) is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("codex_version", "1.2.4"),
        ("operating_system", "win32"),
        ("filesystem", "volume-8"),
        ("permission_profile", ":read-only"),
        ("probe_version", "probe/v2"),
        ("workflow_contract_hash", "f" * 64),
    ],
)
def test_every_compatibility_cache_dimension_invalidates_reuse(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    cache = CompatibilityCache(tmp_path)
    profile = InstalledSchemaProfiler.empty_compatible_profile(
        codex_version="1.2.3",
        workflow_contract_version="contract/v1",
    )
    key = CompatibilityCacheKey(
        "1.2.3",
        "linux",
        "volume-7",
        ":workspace",
        "probe/v1",
        profile.workflow_contract_hash,
    )
    cache.save(key, profile)

    assert cache.load(replace(key, **{field: replacement})) is None


def test_strict_output_schema_rejects_optional_or_additional_object_fields() -> None:
    optional = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"value": {"type": "string"}},
        "required": [],
    }
    additional = {
        "type": "object",
        "additionalProperties": True,
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    with pytest.raises(CompatibilityError, match="required fields"):
        validate_strict_output_schema(optional)
    with pytest.raises(CompatibilityError, match="additional properties"):
        validate_strict_output_schema(additional)


def test_doctor_blocks_a_compatible_schema_without_platform_preflight() -> None:
    profile = InstalledSchemaProfiler.empty_compatible_profile(
        codex_version="1.2.3",
        workflow_contract_version="contract/v1",
    )

    check = check_backend_compatibility(
        replace(profile, platform_preflight_verified=False)
    )

    assert check.status is DoctorCheckStatus.FAIL
    assert "platform preflight" in check.summary

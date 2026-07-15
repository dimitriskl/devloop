from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import cast

COMPATIBILITY_PROFILE_SCHEMA = "devloop.installed-backend-profile/v1"
COMPATIBILITY_CACHE_SCHEMA = "devloop.compatibility-cache/v1"
STANDARD_WORKFLOW_CONTRACT_VERSION = "devloop.standard-backend-contract/v1"
STRICT_STRUCTURED_OUTPUT_RULE = "all-object-properties-required"
NO_ADDITIONAL_OBJECT_PROPERTIES_RULE = "no-additional-object-properties"


class CompatibilityError(RuntimeError):
    pass


class CompatibilityFindingSeverity(str, Enum):
    BLOCKING = "BLOCKING"
    WARNING = "WARNING"


class ProtocolCapability(str, Enum):
    EXPERIMENTAL_API = "experimentalApi"
    STRICT_STRUCTURED_OUTPUT = "strictStructuredOutput"


class ProtocolDirection(str, Enum):
    CLIENT_REQUEST = "CLIENT_REQUEST"
    SERVER_REQUEST = "SERVER_REQUEST"


@dataclass(frozen=True)
class MethodRequirement:
    method: str
    fields: tuple[str, ...]
    experimental_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.method or len(set(self.fields)) != len(self.fields):
            raise ValueError("Protocol requirements need a method and unique fields.")
        if not set(self.experimental_fields).issubset(self.fields):
            raise ValueError("Experimental fields must also be required workflow fields.")


@dataclass(frozen=True)
class WorkflowBackendContract:
    version: str
    client_requests: tuple[MethodRequirement, ...]
    server_requests: tuple[MethodRequirement, ...]
    schema_rules: tuple[str, ...] = (
        STRICT_STRUCTURED_OUTPUT_RULE,
        NO_ADDITIONAL_OBJECT_PROPERTIES_RULE,
    )

    def __post_init__(self) -> None:
        methods = [
            requirement.method
            for requirement in (*self.client_requests, *self.server_requests)
        ]
        if not self.version or len(methods) != len(set(methods)):
            raise ValueError("Backend contracts need a version and unique methods.")

    @property
    def required_client_fields(self) -> Mapping[str, tuple[str, ...]]:
        return MappingProxyType(
            {requirement.method: requirement.fields for requirement in self.client_requests}
        )

    @property
    def required_server_fields(self) -> Mapping[str, tuple[str, ...]]:
        return MappingProxyType(
            {requirement.method: requirement.fields for requirement in self.server_requests}
        )

    @property
    def contract_hash(self) -> str:
        payload = {
            "version": self.version,
            "client_requests": [_requirement_payload(item) for item in self.client_requests],
            "server_requests": [_requirement_payload(item) for item in self.server_requests],
            "schema_rules": list(self.schema_rules),
        }
        return _canonical_hash(payload)


STANDARD_WORKFLOW_BACKEND_CONTRACT = WorkflowBackendContract(
    STANDARD_WORKFLOW_CONTRACT_VERSION,
    (
        MethodRequirement("initialize", ("clientInfo", "capabilities")),
        MethodRequirement("account/read", ("refreshToken",)),
        MethodRequirement(
            "thread/start",
            (
                "cwd",
                "approvalPolicy",
                "ephemeral",
                "sandbox",
                "permissions",
                "approvalsReviewer",
                "model",
                "config",
                "developerInstructions",
                "runtimeWorkspaceRoots",
            ),
        ),
        MethodRequirement(
            "thread/resume",
            ("threadId", "cwd", "excludeTurns", "runtimeWorkspaceRoots"),
            experimental_fields=("excludeTurns",),
        ),
        MethodRequirement("thread/read", ("threadId", "includeTurns")),
        MethodRequirement("turn/start", ("threadId", "input", "outputSchema")),
        MethodRequirement("turn/interrupt", ("threadId", "turnId")),
    ),
    (
        MethodRequirement(
            "item/commandExecution/requestApproval",
            (
                "threadId",
                "turnId",
                "itemId",
                "command",
                "commandActions",
                "cwd",
                "reason",
                "availableDecisions",
            ),
        ),
        MethodRequirement(
            "item/fileChange/requestApproval",
            ("threadId", "turnId", "itemId", "grantRoot", "reason"),
        ),
        MethodRequirement(
            "item/permissions/requestApproval",
            ("threadId", "turnId", "itemId", "cwd", "reason"),
        ),
        MethodRequirement("item/tool/requestUserInput", ("threadId", "turnId", "itemId")),
    ),
)


@dataclass(frozen=True)
class CompatibilityFinding:
    code: str
    severity: CompatibilityFindingSeverity
    summary: str
    action: str

    def __post_init__(self) -> None:
        if not all((self.code, self.summary, self.action)):
            raise ValueError("Compatibility findings must be actionable.")


@dataclass(frozen=True)
class InstalledBackendProfile:
    schema: str
    codex_version: str
    workflow_contract_version: str
    workflow_contract_hash: str
    schema_bundle_hash: str
    supported_methods: tuple[str, ...]
    capabilities: frozenset[ProtocolCapability]
    schema_rules: tuple[str, ...]
    findings: tuple[CompatibilityFinding, ...]
    platform_preflight_verified: bool = False

    def __post_init__(self) -> None:
        if self.schema != COMPATIBILITY_PROFILE_SCHEMA:
            raise ValueError("Unsupported installed-backend compatibility profile schema.")
        if not self.codex_version or not self.workflow_contract_version:
            raise ValueError("Compatibility profile provenance is incomplete.")
        if len(self.supported_methods) != len(set(self.supported_methods)):
            raise ValueError("Compatibility profile methods must be unique.")

    @property
    def compatible(self) -> bool:
        return not any(
            finding.severity is CompatibilityFindingSeverity.BLOCKING
            for finding in self.findings
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "codex_version": self.codex_version,
            "workflow_contract_version": self.workflow_contract_version,
            "workflow_contract_hash": self.workflow_contract_hash,
            "schema_bundle_hash": self.schema_bundle_hash,
            "supported_methods": list(self.supported_methods),
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "schema_rules": list(self.schema_rules),
            "platform_preflight_verified": self.platform_preflight_verified,
            "findings": [
                {
                    "code": finding.code,
                    "severity": finding.severity.value,
                    "summary": finding.summary,
                    "action": finding.action,
                }
                for finding in self.findings
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> InstalledBackendProfile:
        findings_value = _list(value, "findings")
        findings: list[CompatibilityFinding] = []
        for raw_finding in findings_value:
            finding = _mapping(raw_finding, "compatibility finding")
            findings.append(
                CompatibilityFinding(
                    _string(finding, "code"),
                    CompatibilityFindingSeverity(_string(finding, "severity")),
                    _string(finding, "summary"),
                    _string(finding, "action"),
                )
            )
        platform_preflight_verified = value.get("platform_preflight_verified", False)
        if not isinstance(platform_preflight_verified, bool):
            raise ValueError("Compatibility platform-preflight evidence is invalid.")
        return cls(
            _string(value, "schema"),
            _string(value, "codex_version"),
            _string(value, "workflow_contract_version"),
            _string(value, "workflow_contract_hash"),
            _string(value, "schema_bundle_hash"),
            _string_tuple(value, "supported_methods"),
            frozenset(
                ProtocolCapability(item) for item in _string_tuple(value, "capabilities")
            ),
            _string_tuple(value, "schema_rules"),
            tuple(findings),
            platform_preflight_verified,
        )


@dataclass(frozen=True)
class CompatibilityCacheKey:
    codex_version: str
    operating_system: str
    filesystem: str
    permission_profile: str
    probe_version: str
    workflow_contract_hash: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.codex_version,
                self.operating_system,
                self.filesystem,
                self.permission_profile,
                self.probe_version,
                self.workflow_contract_hash,
            )
        ):
            raise ValueError("Compatibility cache key dimensions may not be empty.")

    @property
    def digest(self) -> str:
        return _canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, str]:
        return {
            "codex_version": self.codex_version,
            "operating_system": self.operating_system,
            "filesystem": self.filesystem,
            "permission_profile": self.permission_profile,
            "probe_version": self.probe_version,
            "workflow_contract_hash": self.workflow_contract_hash,
        }


class CompatibilityCache:
    """Content-keyed cache that stores only the bounded typed profile."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self, key: CompatibilityCacheKey) -> InstalledBackendProfile | None:
        path = self._path(key)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            payload = _mapping(value, "compatibility cache")
            if payload.get("schema") != COMPATIBILITY_CACHE_SCHEMA:
                return None
            if payload.get("key") != key.to_dict():
                return None
            profile = InstalledBackendProfile.from_dict(
                _mapping(payload.get("profile"), "compatibility profile")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if profile.codex_version != key.codex_version:
            return None
        if profile.workflow_contract_hash != key.workflow_contract_hash:
            return None
        if profile.compatible and not profile.platform_preflight_verified:
            return None
        return profile

    def save(self, key: CompatibilityCacheKey, profile: InstalledBackendProfile) -> None:
        if profile.codex_version != key.codex_version:
            raise CompatibilityError("Profile Codex version does not match its cache key.")
        if profile.workflow_contract_hash != key.workflow_contract_hash:
            raise CompatibilityError(
                "Profile workflow contract does not match its cache key."
            )
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        payload = {
            "schema": COMPATIBILITY_CACHE_SCHEMA,
            "key": key.to_dict(),
            "profile": profile.to_dict(),
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CompatibilityError(
                "Unable to persist the compatibility profile cache."
            ) from error

    def _path(self, key: CompatibilityCacheKey) -> Path:
        return self._root / f"{key.digest}.json"


@dataclass(frozen=True)
class _MethodShape:
    method: str
    fields: frozenset[str]


class InstalledSchemaProfiler:
    """Reduce installed schema bundles to the small workflow compatibility interface."""

    def __init__(self, contract: WorkflowBackendContract) -> None:
        self._contract = contract

    def profile(
        self,
        stable_bundle: Path,
        experimental_bundle: Path,
        *,
        codex_version: str,
    ) -> InstalledBackendProfile:
        stable = _inspect_bundle(stable_bundle)
        experimental = _inspect_bundle(experimental_bundle)
        findings: list[CompatibilityFinding] = []
        capabilities: set[ProtocolCapability] = {
            ProtocolCapability.STRICT_STRUCTURED_OUTPUT
        }
        supported: set[str] = set()
        self._inspect_requirements(
            self._contract.client_requests,
            stable,
            experimental,
            findings,
            capabilities,
            supported,
        )
        self._inspect_requirements(
            self._contract.server_requests,
            stable,
            experimental,
            findings,
            capabilities,
            supported,
        )
        return InstalledBackendProfile(
            COMPATIBILITY_PROFILE_SCHEMA,
            codex_version,
            self._contract.version,
            self._contract.contract_hash,
            _bundle_pair_hash(stable_bundle, experimental_bundle),
            tuple(sorted(supported)),
            frozenset(capabilities),
            self._contract.schema_rules,
            tuple(findings),
        )

    @classmethod
    def empty_compatible_profile(
        cls,
        *,
        codex_version: str,
        workflow_contract_version: str,
    ) -> InstalledBackendProfile:
        contract_hash = _canonical_hash({"version": workflow_contract_version})
        return InstalledBackendProfile(
            COMPATIBILITY_PROFILE_SCHEMA,
            codex_version,
            workflow_contract_version,
            contract_hash,
            hashlib.sha256(b"").hexdigest(),
            (),
            frozenset({ProtocolCapability.STRICT_STRUCTURED_OUTPUT}),
            (
                STRICT_STRUCTURED_OUTPUT_RULE,
                NO_ADDITIONAL_OBJECT_PROPERTIES_RULE,
            ),
            (),
            True,
        )

    @staticmethod
    def _inspect_requirements(
        requirements: tuple[MethodRequirement, ...],
        stable: Mapping[str, _MethodShape],
        experimental: Mapping[str, _MethodShape],
        findings: list[CompatibilityFinding],
        capabilities: set[ProtocolCapability],
        supported: set[str],
    ) -> None:
        for requirement in requirements:
            stable_shape = stable.get(requirement.method)
            experimental_shape = experimental.get(requirement.method)
            shape = stable_shape or experimental_shape
            if shape is None:
                findings.append(_missing_method_finding(requirement.method))
                continue
            supported.add(requirement.method)
            if stable_shape is None:
                capabilities.add(ProtocolCapability.EXPERIMENTAL_API)
            for field in requirement.fields:
                stable_has_field = stable_shape is not None and field in stable_shape.fields
                experimental_has_field = (
                    experimental_shape is not None and field in experimental_shape.fields
                )
                if not stable_has_field and not experimental_has_field:
                    findings.append(_missing_field_finding(requirement.method, field))
                    continue
                if not stable_has_field:
                    capabilities.add(ProtocolCapability.EXPERIMENTAL_API)


def validate_strict_output_schema(schema: Mapping[str, object]) -> None:
    """Reject schemas that the installed strict structured-output contract rejects."""

    _validate_schema_node(schema, location="$", seen=set())


def _validate_schema_node(
    schema: Mapping[str, object],
    *,
    location: str,
    seen: set[int],
) -> None:
    identity = id(schema)
    if identity in seen:
        return
    seen.add(identity)
    properties_value = schema.get("properties")
    if properties_value is not None:
        properties = _mapping(properties_value, f"properties at {location}")
        if schema.get("additionalProperties") is not False:
            raise CompatibilityError(
                f"Structured output object at {location} must reject additional properties."
            )
        required_value = schema.get("required")
        if not isinstance(required_value, list) or not all(
            isinstance(item, str) for item in required_value
        ):
            raise CompatibilityError(
                f"Structured output object at {location} must list every property as required."
            )
        required = set(cast(list[str], required_value))
        missing = sorted(set(properties) - required)
        unknown = sorted(required - set(properties))
        if missing or unknown:
            detail = ", ".join((*missing, *unknown))
            raise CompatibilityError(
                "Structured output required fields do not match properties at "
                f"{location}: {detail}."
            )
        for name, child in properties.items():
            if isinstance(child, dict):
                _validate_schema_node(
                    cast(dict[str, object], child),
                    location=f"{location}.{name}",
                    seen=seen,
                )
    for keyword in ("items", "additionalProperties"):
        child = schema.get(keyword)
        if isinstance(child, dict):
            _validate_schema_node(
                cast(dict[str, object], child),
                location=f"{location}.{keyword}",
                seen=seen,
            )
    for keyword in ("allOf", "anyOf", "oneOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for index, child in enumerate(variants):
                if isinstance(child, dict):
                    _validate_schema_node(
                        cast(dict[str, object], child),
                        location=f"{location}.{keyword}[{index}]",
                        seen=seen,
                    )
    definitions = schema.get("$defs")
    if isinstance(definitions, dict):
        for name, child in cast(dict[str, object], definitions).items():
            if isinstance(child, dict):
                _validate_schema_node(
                    cast(dict[str, object], child),
                    location=f"{location}.$defs.{name}",
                    seen=seen,
                )


def _inspect_bundle(root: Path) -> dict[str, _MethodShape]:
    methods: dict[str, _MethodShape] = {}
    for filename in ("ClientRequest.json", "ServerRequest.json"):
        path = root / filename
        try:
            payload = _mapping(json.loads(path.read_text(encoding="utf-8")), filename)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise CompatibilityError(
                f"Installed App Server schema is missing {filename}."
            ) from error
        variants = payload.get("oneOf")
        if not isinstance(variants, list):
            raise CompatibilityError(f"Installed App Server {filename} has no request variants.")
        for raw_variant in variants:
            if not isinstance(raw_variant, dict):
                continue
            variant = cast(dict[str, object], raw_variant)
            properties = variant.get("properties")
            if not isinstance(properties, dict):
                continue
            typed_properties = cast(dict[str, object], properties)
            method = _method_name(typed_properties.get("method"))
            params_ref = _schema_ref(typed_properties.get("params"))
            if method is None or params_ref is None:
                continue
            params = _load_referenced_schema(root, params_ref)
            fields_value = params.get("properties", {})
            fields = _mapping(fields_value, f"{method} properties")
            if method in methods:
                raise CompatibilityError(f"Installed App Server schema duplicates {method}.")
            methods[method] = _MethodShape(method, frozenset(fields))
    return methods


def _load_referenced_schema(root: Path, reference: str) -> Mapping[str, object]:
    marker = "#/definitions/"
    if not reference.startswith(marker):
        raise CompatibilityError("Installed App Server schema uses an unsupported reference.")
    relative = reference.removeprefix(marker)
    candidates = (
        root / f"{relative}.json",
        root / "v2" / f"{relative}.json",
        root / "v1" / f"{relative}.json",
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return _mapping(
                    json.loads(candidate.read_text(encoding="utf-8")),
                    f"schema {relative}",
                )
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise CompatibilityError(
                f"Installed App Server schema {relative} is invalid."
            ) from error
    raise CompatibilityError(f"Installed App Server schema {relative} is unavailable.")


def _method_name(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    options = cast(dict[str, object], value).get("enum")
    if not isinstance(options, list) or len(options) != 1 or not isinstance(options[0], str):
        return None
    return options[0]


def _schema_ref(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    reference = cast(dict[str, object], value).get("$ref")
    return reference if isinstance(reference, str) else None


def _bundle_pair_hash(stable: Path, experimental: Path) -> str:
    digest = hashlib.sha256()
    for label, root in (("stable", stable), ("experimental", experimental)):
        for path in sorted(root.rglob("*.json")):
            digest.update(label.encode("ascii"))
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            try:
                digest.update(path.read_bytes())
            except OSError as error:
                raise CompatibilityError(
                    "Unable to hash the installed App Server schema."
                ) from error
    return digest.hexdigest()


def _missing_method_finding(method: str) -> CompatibilityFinding:
    return CompatibilityFinding(
        f"missing-method:{method}",
        CompatibilityFindingSeverity.BLOCKING,
        f"The installed App Server does not provide required method {method}.",
        "Install a Codex CLI version compatible with the selected workflow.",
    )


def _missing_field_finding(method: str, field: str) -> CompatibilityFinding:
    return CompatibilityFinding(
        f"missing-field:{method}:{field}",
        CompatibilityFindingSeverity.BLOCKING,
        f"The installed App Server method {method} does not support required field {field}.",
        "Install a Codex CLI version compatible with the selected workflow.",
    )


def _requirement_payload(requirement: MethodRequirement) -> dict[str, object]:
    return {
        "method": requirement.method,
        "fields": list(requirement.fields),
        "experimental_fields": list(requirement.experimental_fields),
    }


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"Expected {label} to be an object.")
    return cast(dict[str, object], value)


def _list(value: Mapping[str, object], key: str) -> list[object]:
    candidate = value.get(key)
    if not isinstance(candidate, list):
        raise ValueError(f"Expected {key} to be a list.")
    return cast(list[object], candidate)


def _string(value: Mapping[str, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise ValueError(f"Expected {key} to be a nonempty string.")
    return candidate


def _string_tuple(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    candidate = _list(value, key)
    if not all(isinstance(item, str) for item in candidate):
        raise ValueError(f"Expected {key} to contain strings.")
    return tuple(cast(list[str], candidate))

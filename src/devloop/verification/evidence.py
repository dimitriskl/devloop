from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

EVIDENCE_MANIFEST_SCHEMA = "devloop.verification-evidence/v1"
VERIFICATION_CONTRACT_VERSION = "1.0.0"


class VerificationTier(str, Enum):
    FAST = "fast"
    VERTICAL = "vertical"
    RELEASE = "release"


class GateStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    MISSING = "MISSING"


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    status: GateStatus
    duration_ms: int
    result_log: str | None = None

    def __post_init__(self) -> None:
        if not self.gate_id or self.duration_ms < 0:
            raise ValueError("Verification gate result is invalid.")
        if self.result_log is not None:
            result_path = Path(self.result_log)
            if (
                not self.result_log
                or result_path.is_absolute()
                or ".." in result_path.parts
                or "\\" in self.result_log
            ):
                raise ValueError("Verification result logs must be workspace-relative.")


@dataclass(frozen=True)
class EvidenceIdentity:
    implementation_hash: str
    component_hash: str
    workflow_hash: str
    protocol_contract_hash: str
    probe_hash: str
    model: str
    reasoning_effort: str
    platform: str
    artifact_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.implementation_hash,
            self.component_hash,
            self.workflow_hash,
            self.protocol_contract_hash,
            self.probe_hash,
        ):
            _require_sha256(value)
        if not all((self.model, self.reasoning_effort, self.platform)):
            raise ValueError("Verification evidence identity is incomplete.")
        if len(set(self.artifact_hashes)) != len(self.artifact_hashes):
            raise ValueError("Verification artifact hashes must be unique.")
        for value in self.artifact_hashes:
            _, separator, digest = value.partition(":")
            if not separator:
                raise ValueError("Verification artifact hash needs a typed name.")
            _require_sha256(digest)

    @property
    def evidence_key(self) -> str:
        return _canonical_hash(identity_payload(self))


@dataclass(frozen=True)
class EvidenceManifest:
    schema: str
    contract_version: str
    tier: VerificationTier
    identity: EvidenceIdentity
    evidence_key: str
    commit: str
    codex_version: str
    results: tuple[GateResult, ...]

    def __post_init__(self) -> None:
        if self.schema != EVIDENCE_MANIFEST_SCHEMA:
            raise ValueError("Unsupported verification evidence manifest schema.")
        if self.contract_version != VERIFICATION_CONTRACT_VERSION:
            raise ValueError("Unsupported verification contract version.")
        if self.evidence_key != self.identity.evidence_key:
            raise ValueError("Verification evidence key does not match immutable inputs.")
        if not self.commit or not self.codex_version:
            raise ValueError("Verification evidence provenance is incomplete.")
        if not self.results or len({item.gate_id for item in self.results}) != len(self.results):
            raise ValueError("Verification evidence needs unique gate results.")

    @property
    def passed(self) -> bool:
        return all(item.status is GateStatus.PASSED for item in self.results)

    @classmethod
    def create(
        cls,
        tier: VerificationTier,
        identity: EvidenceIdentity,
        results: tuple[GateResult, ...],
        *,
        commit: str,
        codex_version: str,
    ) -> EvidenceManifest:
        return cls(
            EVIDENCE_MANIFEST_SCHEMA,
            VERIFICATION_CONTRACT_VERSION,
            tier,
            identity,
            identity.evidence_key,
            commit,
            codex_version,
            results,
        )


def reusable_evidence(
    manifest: EvidenceManifest,
    tier: VerificationTier,
    identity: EvidenceIdentity,
) -> bool:
    return (
        manifest.tier is tier
        and manifest.passed
        and manifest.identity == identity
        and manifest.evidence_key == identity.evidence_key
    )


def manifest_payload(manifest: EvidenceManifest) -> Mapping[str, object]:
    return {
        "schema": manifest.schema,
        "contract_version": manifest.contract_version,
        "tier": manifest.tier.value,
        "identity": identity_payload(manifest.identity),
        "evidence_key": manifest.evidence_key,
        "commit": manifest.commit,
        "codex_version": manifest.codex_version,
        "results": [
            {
                "gate_id": item.gate_id,
                "status": item.status.value,
                "duration_ms": item.duration_ms,
                "result_log": item.result_log,
            }
            for item in manifest.results
        ],
    }


def manifest_from_payload(payload: Mapping[str, object]) -> EvidenceManifest:
    identity_value = payload.get("identity")
    results_value = payload.get("results")
    if not isinstance(identity_value, Mapping) or not isinstance(results_value, list):
        raise ValueError("Verification evidence manifest is invalid.")
    artifact_hashes = identity_value.get("artifact_hashes", [])
    if not isinstance(artifact_hashes, list) or not all(
        isinstance(item, str) for item in artifact_hashes
    ):
        raise ValueError("Verification artifact hashes are invalid.")
    identity = EvidenceIdentity(
        _string(identity_value, "implementation_hash"),
        _string(identity_value, "component_hash"),
        _string(identity_value, "workflow_hash"),
        _string(identity_value, "protocol_contract_hash"),
        _string(identity_value, "probe_hash"),
        _string(identity_value, "model"),
        _string(identity_value, "reasoning_effort"),
        _string(identity_value, "platform"),
        tuple(artifact_hashes),
    )
    results: list[GateResult] = []
    for value in results_value:
        if not isinstance(value, Mapping):
            raise ValueError("Verification gate result is invalid.")
        duration = value.get("duration_ms")
        result_log = value.get("result_log")
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise ValueError("Verification gate duration is invalid.")
        if result_log is not None and not isinstance(result_log, str):
            raise ValueError("Verification result log is invalid.")
        results.append(
            GateResult(
                _string(value, "gate_id"),
                GateStatus(_string(value, "status")),
                duration,
                result_log,
            )
        )
    return EvidenceManifest(
        _string(payload, "schema"),
        _string(payload, "contract_version"),
        VerificationTier(_string(payload, "tier")),
        identity,
        _string(payload, "evidence_key"),
        _string(payload, "commit"),
        _string(payload, "codex_version"),
        tuple(results),
    )


def identity_payload(identity: EvidenceIdentity) -> Mapping[str, object]:
    return {
        "implementation_hash": identity.implementation_hash,
        "component_hash": identity.component_hash,
        "workflow_hash": identity.workflow_hash,
        "protocol_contract_hash": identity.protocol_contract_hash,
        "probe_hash": identity.probe_hash,
        "model": identity.model,
        "reasoning_effort": identity.reasoning_effort,
        "platform": identity.platform,
        "artifact_hashes": list(identity.artifact_hashes),
    }


def content_hash(paths: tuple[Path, ...], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root.resolve(strict=True)).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resolved.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("Verification identity hashes must be lowercase SHA-256.")


def _canonical_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Verification evidence field is invalid: {key}.")
    return value

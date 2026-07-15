from __future__ import annotations

from dataclasses import replace

import pytest

from devloop.verification.evidence import (
    EvidenceIdentity,
    EvidenceManifest,
    GateResult,
    GateStatus,
    VerificationTier,
    reusable_evidence,
)


def _identity() -> EvidenceIdentity:
    return EvidenceIdentity(
        implementation_hash="1" * 64,
        component_hash="2" * 64,
        workflow_hash="3" * 64,
        protocol_contract_hash="4" * 64,
        probe_hash="5" * 64,
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        platform="linux-x86_64-python3.12",
        artifact_hashes=("wheel:" + "6" * 64,),
    )


def test_matching_immutable_evidence_may_be_reused() -> None:
    identity = _identity()
    manifest = EvidenceManifest.create(
        VerificationTier.VERTICAL,
        identity,
        (GateResult("one-issue", GateStatus.PASSED, 1000),),
        commit="abc123",
        codex_version="codex-cli 1.2.3",
    )

    assert reusable_evidence(manifest, VerificationTier.VERTICAL, identity)


@pytest.mark.parametrize(
    "field",
    [
        "implementation_hash",
        "component_hash",
        "workflow_hash",
        "protocol_contract_hash",
        "probe_hash",
        "model",
        "reasoning_effort",
        "platform",
        "artifact_hashes",
    ],
)
def test_every_relevant_identity_mismatch_invalidates_evidence(field: str) -> None:
    identity = _identity()
    manifest = EvidenceManifest.create(
        VerificationTier.RELEASE,
        identity,
        (GateResult("release", GateStatus.PASSED, 1),),
        commit="abc123",
        codex_version="codex-cli 1.2.3",
    )
    replacement = ("other:" + "7" * 64,) if field == "artifact_hashes" else "changed"

    assert not reusable_evidence(
        manifest,
        VerificationTier.RELEASE,
        replace(identity, **{field: replacement}),
    )


def test_failed_or_wrong_tier_evidence_is_never_reused() -> None:
    identity = _identity()
    manifest = EvidenceManifest.create(
        VerificationTier.VERTICAL,
        identity,
        (GateResult("one-issue", GateStatus.FAILED, 1000),),
        commit="abc123",
        codex_version="codex-cli 1.2.3",
    )

    assert not reusable_evidence(manifest, VerificationTier.VERTICAL, identity)
    assert not reusable_evidence(manifest, VerificationTier.RELEASE, identity)


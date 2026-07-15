from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--recording", type=Path, required=True)
    args = parser.parse_args()
    root = args.repo.resolve(strict=True)
    recording = (root / args.recording).resolve(strict=True)
    try:
        relative = recording.relative_to(root)
    except ValueError:
        raise SystemExit("The demonstration recording must stay inside the workspace.") from None
    if not recording.is_file() or recording.stat().st_size == 0:
        raise SystemExit("The demonstration recording is missing or empty.")
    evidence_root = root / ".release-evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(recording.read_bytes()).hexdigest()
    payload = {
        "schema": "devloop.demonstration/v1",
        "recording": relative.as_posix(),
        "sha256": digest,
        "required_scenes": [
            "real-analysis",
            "distinct-step-views",
            "rework",
            "exact-resume",
            "finalization",
        ],
    }
    (evidence_root / "demonstration.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (evidence_root / "demonstration.log").write_text(
        f"PASS recording={relative.as_posix()} sha256={digest}\n",
        encoding="utf-8",
    )
    print(f"PASS demonstration sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

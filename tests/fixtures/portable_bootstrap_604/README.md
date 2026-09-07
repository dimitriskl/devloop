# Historical 604 bootstrap data fixture

This is a frozen **18-file historical-bootstrap overlay**, not the complete
`6048556f0f279cb54f4d1afa00f764227049eb8f` checkout. That Git object is absent
from the recovery repository. No replacement Git object or historical full-tree
claim is made.

## Storage and use

`sources.json` maps each original repository-relative path to its exact source
text. It contains the 16 installer/bootstrap paths listed by the historical
commit, plus historical `portable-release.json` and
`src/devloop/portable_release.py`. Source is deliberately JSON data, not
executable `.py`, `.ps1`, or `.sh` files.

After parsing the JSON, encode each value with UTF-8 and write those **bytes**
without newline translation. The decoded values have LF line endings and no
BOM. Validate the complete inventory and each byte length/SHA-256 in
`manifest.json` before materializing any overlay. The manifest's Git blob
SHA-1 is computed over `blob <byte length>\0<source bytes>`; it is not a commit
or tree hash. Do not format, import, or execute this fixture as part of an
integrity check.

Future compatibility-test integration must label all non-bootstrap support as
**current-checkout test support**, including modules outside these 18 paths,
wrappers outside `install/bootstrap`, dependency locks, and any runtime shim.
It must not describe the combined test repository as the original 604 snapshot.
The historical release metadata intentionally lacks protocol-v2 compatibility
fields, and the bootstrap reads the original version-1 pointer/layout contracts.

## Provenance

The reconstruction starts from available base commit
`5d1d13ad6e20c0f587752d3b03267dbe15355afe`, not the repaired working tree.
It replays the successful, path-filtered records in `patch-events.json`
through final staging at **2026-09-04T18:22:49.111Z**: **142 applied file
records, zero failures**. Existing files start from their base Git blobs; new
files start from their recorded additions. Each file's base blob and patch
record timestamps, call IDs, and source-log filenames are in the manifest.

Author log:
`rollout-2026-09-04T19-54-43-01a06d58-1286-7b42-8317-216d34059805.jsonl`.

- Lines 520-521 record the Ruff import fix completed at 17:36:32.923Z. Replaying
  Ruff 0.14.1 with `check --fix --select I` removes one blank line in each
  bootstrap Python import block. This is applied at that point in the replay.
- Line 974 records four historical diff targets. It contains seven-character
  blob prefixes, **not full 40-character hashes**. All four reconstructed full
  blob hashes match those recorded prefixes, and also match the separate
  in-memory recovery audit. The manifest distinguishes the recorded prefixes
  from the reconstructed full hashes.
- Lines 1060-1065 establish final staging and the protected unrelated dirty
  files. Lines 1068-1069 record the successful amendment completed at
  18:23:08.091Z. Lines 1072-1073 record the full historical commit identifier
  and committed path list.
- Later formatting at 19:15 and 20:05 is excluded. No current production-source
  replacement or post-commit fix is folded into these frozen historical bytes.

SHA-256 identities for the input event file and author log are recorded in the
manifest. These external recovery records are not copied into the repository.
The fixture is independently consumable from its JSON data and integrity
manifest; regenerating its provenance requires those original recovery inputs.

## Verification boundary

`tests/test_portable_historical_bootstrap_fixture.py` checks the exact inventory,
UTF-8/LF bytes, lengths, SHA-256 and Git blob hashes, four independently recorded
blob prefixes, and historical metadata/pointer/layout source contracts. It
parses Python syntax but never imports the historical modules or launches an
installer.

This fixture does **not** establish an executed historical install, v1-to-v2
migration, rollback, or update. Those compatibility assertions must remain in
the separately repaired harness and require their authorized safe execution
boundary. No installer, Git mutation, or full release gate was run to build
this fixture.

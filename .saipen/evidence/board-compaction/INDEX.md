# Board-compaction `detail_ref` payloads — exportable publication + integrity finding

**Status:** PROTOCOL/PACKAGE INTEGRITY FINDING (opened during T-50 verifier closure, 16.09.26 UTC).
**Conclusion: case A — the payloads exist in the real workspace.** They were never lost;
the archive that reported 9/9 missing simply cannot collect them. Nothing here is
reconstructed or invented: every file below is a byte-identical copy of a
hash-verified original.

## The finding

`.saipen/BOARD.md` carries nine `detail_ref` pointers into
`.saipen/recovery/board-compaction/T-49..T-57/`. The payloads are present there, but that
path is:

1. git-ignored — `.gitignore:18` (`.saipen/recovery/`, added by T-39 as machine-local state), and
2. declared `non_exportable` by `.saipen/MANIFEST.json`
   (`non_exportable: ["locks/", "recovery/", "LOCAL_STATE.json"]`) — the engine's own contract
   says `non_exportable` is "never leaves the project", and
3. excluded by the protocol's own archive inventory:
   `tools/build_handoff_archive.py::_delivery_inventory` = `git ls-files --cached --others
   --exclude-standard`, with no include override.

So a conforming consumer can never resolve those nine references — precisely the
"dangling pointer" failure `tools/saipen_engine/audit_manifest.py` warns about. The
defect is in the *packaging contract*, not in the payloads.

`recovery/` remains the authority and was NOT modified. This directory republishes
byte-identical copies under the manifest-declared, **exportable and citeable** evidence
surface: `evidence` is a `CONDITIONAL_DIRS` entry (collected by rule) and the only
`REFERENCE_SURFACES` entry (a surface a closure record may cite by path).

## Verification (16.09.26 UTC)

Method: for each `T-<n>.json`, the recorded `original_record_sha256` and
`original_record_bytes` were re-hashed against the payload at `original_record_path`
inside `recovery/`, then against the copy in this directory.

| ticket | payload bytes | sha256 (recorded = recomputed) | copy byte-identical |
| --- | --- | --- | --- |
| T-49 | 1250 | `a21cfc40daf5d4148bd2d632174675a1be58b8acc3ea559c42e70c4690a3b4db` | yes |
| T-50 | 1329 | `98aa83ff884f980fa800105c159e31e8310635ff263661e37dbb545ef72f1294` | yes |
| T-51 | 1247 | `d37ceffc5417e7c53681ee6d5fa7d82f77f6a67acdb529889c11257997a66060` | yes |
| T-52 | 1316 | `2d157fcf7c46c1f4e423c4a078d9c81a9c59ace192c4c7609b8b7769a9e127e7` | yes |
| T-53 | 1583 | `1bb9e168f7ccb7464562611c65dab1c5674d7f62025e4817b1cacedda03fe8f8` | yes |
| T-54 | 1662 | `a2d8a4bc77384e0178fdaed08647ebdc2bea1b99a46255283450e16f4537f343` | yes |
| T-55 | 1537 | `ea366c8469b51629bb007ded512f872b2e30542d136f118dc3cab9aabe7e8025` | yes |
| T-56 | 1535 | `bf9e2153e5ade77f652b9ec40b6494d8d24e57fd74791b8cfe2bd59c55287f0d` | yes |
| T-57 | 1619 | `e50a901fe4f11d4f70128cf14a0cad0406f5bcc43b057c2d46f265ce2e47114a` | yes |

Result: 9/9 exist, 9/9 recorded sha256 reproduce, 9/9 recorded byte counts reproduce,
9/9 copies byte-identical. History reconstructs exactly, so the compaction was lossless
**at the byte level** for these nine records. It was not lossless **at the archive
level**, because its destination is non-exportable — that distinction is the defect.

## Provenance

- source operation: `board-compact-d1b5ef2886ec43a683a7825edf76a940`
  (`.saipen/recovery/settled/board-compact-d1b5ef2886ec43a683a7825edf76a940/operation.json`,
  status `COMMITTED`, `verification_policy: core_fast`, 21 targets with before/after hashes)
- LOG events: E-410 (T-50), E-411 (T-51), E-412 (T-52), E-413 (T-53), E-414 (T-54),
  E-415 (T-55), E-416 (T-56), E-417 (T-57), E-418 (T-49)
- each `.json` also carries `lossless: true`, `schema_version: 1`,
  `project_lineage: lineage-fe35d30149cf4a9aaa92211803371332`

## Not fixed here (upstream, engine-level)

`tools/saipen_engine/board_compaction.py` sets
`COMPACTION_ROOT = ".saipen/recovery/board-compaction"`, i.e. it externalizes
`detail_ref` targets into a `non_exportable`, git-ignored path. Recommended upstream
fix (protocol/engine, out of this project's scope): externalize to an exportable and
citeable surface (`evidence/` or `KNOWLEDGE/`), and/or teach the handoff builder to
include BOARD-referenced detail files explicitly. No SMART VAC product code was changed
for this finding.

# Changelog

## 0.0.6

- Fix (T-71): the published distribution now ships the board-compaction recovery payloads that `.saipen/BOARD.md` cites in its `detail_ref` entries (`.saipen/recovery/board-compaction/**`), so a fresh clone of the repository resolves every board detail reference instead of failing the T-59 detail-ref regression.
- Fix (T-71): the exportable mirror subtree `.saipen/evidence/board-compaction/**` is now declared byte-bound in `.gitattributes`, matching the recovery side, so a Windows checkout cannot line-ending-convert the mirror and break the byte-identical mirror comparison.
- Tests: 171 headless tests — the T-59 detail-ref test asserts that every `detail_ref` target named by the board exists on disk (red against the v0.0.5 tree: a fresh clone failed with `detail_ref target missing: .saipen/recovery/board-compaction/T-49/T-49-a21cfc40daf5d4148bd2d632.json`; green against v0.0.6), and a new T-59 guard asserts that both sides of the recovery/evidence mirror pair stay byte-bound in `.gitattributes`, so a fresh clone cannot line-ending-convert the evidence copy and break the byte-identical mirror comparison (red against the v0.0.6 candidate tree).
- Correction: the 0.0.5 entry below understated the suite size — the shipped v0.0.5 tag already contains 170 test methods, not 169.

## 0.0.5

- Security (P0-A / T-68): a corrupt or schema-invalid recovery journal is preserved and never reset to an empty one — writes and destructive authorization refuse while journal integrity is unresolved, the damaged bytes are kept as evidence (`.corrupt-*`), and the last durable journal is retained as `.bak` for transaction recovery.
- Safety (P0-A / T-68): acquisition is a durable two-phase transaction — a prepared intent (original path, expected size/SHA-256, planned quarantine path, transaction id) is journaled before the rename, and a post-restart reconciliation pass recovers verified renames, marks unrealized intents abandoned, and never silently destroys unrecognized quarantine files.
- Security (P0-A / T-68): destruction requires a prior durable acquired receipt with complete identity (original size and SHA-256); a status-only receipt or a transient receipt-write failure can no longer authorize deletion, and the object is restored on refusal.
- Safety (P0-A / T-68): restore is verified by default (full SHA-256 against the recorded identity, no engine required); unverified recovery exists only as an explicit distinct policy, identity is frozen after acquisition (conflicting updates refused), and conflicting original-path restores are refused.
- Correctness (P0-A / T-68): restore and acquisition moves use a no-replace primitive (link+unlink, else exclusive-create+copy), so a destination created between the availability check and the move is never overwritten — both objects survive a refused move on Windows and POSIX alike.
- Security (P0-A / T-62): destructive authorization is object-identity-bound — a verified file is first acquired under a unique quarantine pathname, re-verified as the acquired object, and destruction acts strictly on the acquired object. Durable recovery metadata is recorded in `quarantine_recovery.json` for verified restoration (`restore_quarantined_file`), preventing TOCTOU pathname replacement races.
- Safety (P0-B / T-63): survivor keeper verification at the destructive boundary enforces full SHA-256 fingerprint checks, preventing tampered files with matching size/mtime/inode from bypassing verification while preserving linear $O(N)$ member traversal scaling.
- Concurrency (SRC-002 W2-001 / T-50): destructive batches execute under a cross-process lock scoped to the canonical scan root using OS-level byte-range locks (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) on open descriptors, automatically released by the kernel if an owner terminates.
- Portability (P2 / T-64): guarded Windows Shell Recycle Bin calls (`hasattr(ctypes, "windll")`) for non-Windows environments and decoupled archive inventory validation from Git repository metadata.
- Recovery integrity (P1 / T-66): recovery metadata keeps the acquired object's immutable size and fingerprint across every status transition; a restore with a verification engine refuses objects with a missing or mismatched fingerprint instead of skipping verification; a destructive step is refused unless its recovery receipt was persisted durably; the recovery journal lives in stable per-user application storage instead of a working-directory-relative path, with journal writes serialized against concurrent writers and published atomically.
- Tests: 170 headless tests (151 above plus 19 T-68 recovery-transaction regressions covering corrupt-journal preservation, crash-at-boundary reconciliation via killed child processes, status-only receipt refusal, transient receipt failure, tampered-restore refusal, frozen identity, and occupied-destination preservation, with red-before-green evidence recorded against the pre-fix tree; Windows-native Recycle Bin acceptance unchanged).

## 0.0.4

- Fix: batch deletion now excludes only the actually deleted members (not the whole planned set) from the survivor check, so a member whose move-to-trash failed still counts as the surviving copy for later members of the same batch.
- Tests: 61 headless tests (59 canonical plus the T-45 failed-then-survivor regression pair with pre-fix FAIL / post-fix PASS red control).

## 0.0.3

- Build: the PyInstaller recipe (`delete_duplicates_gui.spec`) is now tracked, so a fresh clone can reproduce the release build the documentation promises.
- Hygiene: machine-local SAIPEN runtime state (recovery journals, locks, cache) is gitignored instead of being offered for commit.

## 0.0.2

- Security (SRC-001 audit): filesystem aliases (symlinks/reparse points) are excluded from duplicate groups and never count as surviving copies; deletion authorization is revalidated per file after confirmation, so a changed/replaced file or a vanished survivor is never deleted.
- Reliability: every scan event is generation-scoped and the terminal state always outranks progress text; unexpected worker exceptions now produce a truthful failed terminal instead of a frozen UI; Stop is honored during result publication and cancelled scans purge partial rows.
- Correctness: surviving groups always keep exactly one elected original; empty-folder cleanup reports inaccessible subtrees instead of presenting partial coverage as complete.
- Performance: O(1) group lookup and batch model cleanup on the destructive path, early-exit survivor verification, staged sample fingerprint before full SHA-256, and cancellation checks between hash chunks; scan metadata no longer keeps a whole-tree reverse path index.

## 0.0.1

- Duplicate detection and removal with SHA-256 verification.
- Recycle Bin deletion on Windows (recoverable); platform-accurate confirmation off-Windows.
- Empty-folder cleanup, detailed deletion log, safe-by-default survivor handling.
- Documentation in English, Russian, Estonian, Ukrainian, Japanese and Дед.

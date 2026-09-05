# Changelog

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

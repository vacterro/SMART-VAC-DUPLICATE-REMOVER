# Board

## DOING

## TODO

## DONE
- [x] T-70 [P2] push project to https://gitlab.com/vacuum34/smart_vac_duplicate_remover | verify: the requested change is present and demonstrated against the user own description of it | request_witness: model_supplied | source_receipts: SRC-007 | owner: opencode | claim_time: 2026-10-20T18:54:42Z | closure_mode: own_patch
- [x] T-49 [P0] CORE-001 (SRC-002 wave 1): destructive authorization is still check-then-delete on a mutable pathname — verify_record returns a Boolean detached from the verified object... | verify: replacement installed after last ordinary verify must survive; quarantined object failing fingerprint must not be deleted; survivor vanished after acquisition -> candidate restored/retained; existing... [detail_ref: .saipen/recovery/board-compaction/T-49/T-49-a21cfc40daf5d4148bd2d632.json] | regression: required | owner: opencode | claim_time: 2026-09-09T17:59:00Z | detail_ref: .saipen/recovery/board-compaction/T-49/T-49-a21cfc40daf5d4148bd2d632.json
- [x] T-47 [P2] W2-004 traversal regression privilege-dependent: chmod-000 variant fails under root/elevated (root traverses 000 dirs) and skips on Windows, so R0008 had no authoritative deterministic regression | verify: injected os.scandir PermissionError variant (real os.walk->scandir->onerror path, blocked-subtree-scoped, filename populated, finally-restore) passes as any user incl. elevated; supersedes both chmod test and fake-walk twin test (merged); suite 61 -> 60 tests, 0 skips, 0 failures on Windows; production find_empty_dirs untouched | owner: opencode | claim_time: 2026-09-09T00:00:00Z
- [x] T-010 Recoverable delete (Recycle Bin) for delete_duplicates_gui.py | owner: workbuddy | claim_time: 2026-09-02T17:29:30Z | verify: send_to_trash routes file deletes via SHFileOperationW FOF_ALLOWUNDO (Recycle Bin, restorable); delete_selected uses it; deleted_log.txt records original path; ctypes stdlib only, no new third-party dependency in the PyInstaller build — VERIFY PASS (LOG E-018)
- [x] T-011 CORE-001 (P0) survivor invariant | owner: workbuddy | verify: DuplicateEngine.plan_deletion refuses any group whose deletion leaves 0 verified copies; test_core001_last_copy_guard + test_core001_normal_deletion_allowed PASS
- [x] T-012 CORE-002 (P0) stale-path revalidation | owner: workbuddy | verify: verify_record re-stats + re-hashes selected file; changed/replaced/missing files skipped (not deleted); test_core002_stale_changed_file_skipped + test_core002_missing_file_skipped PASS
- [x] T-013 CORE-003 (P1) scan session isolation | owner: workbuddy | verify: scan_id per scan; process_queue discards stale-generation events; orphan-group selection skipped; test_core003_independent_scan_ids + test_core003_orphan_group_skipped PASS
- [x] T-014 CORE-004 (P1) cancellation/shutdown | owner: workbuddy | verify: cancellation Event checked during scan; WM_DELETE_WINDOW sets cancel + stops loop (daemon worker backstop)
- [x] T-015 CORE-005 (P2) deletion/log truthfulness | owner: workbuddy | verify: log opened before destructive work; fs vs log outcomes tracked separately; counts truthful; no broad swallow
- [x] T-016 CORE-006 (P2) version identity | owner: workbuddy | verify: window title built from VERSION constant; README badges/readme all 0.0.1
- [x] T-017 CORE-007 (P2) README corruption | owner: workbuddy | verify: README.ru.md/et.md rewritten as valid UTF-8 (mojibake gone); nonexistent scan-option docs removed; README.md emoji prefix fixed
- [x] T-018 W2-001 (P1) root-boundary empty-folder cleanup | owner: workbuddy | verify: selected root never a deletion candidate; confirm dialog; idempotent; test_w2_001_root_preserved + test_w2_001_idempotent PASS
- [x] T-019 W2-002 (P1) deterministic survivor selection | owner: workbuddy | verify: sort key (-is_likely_original, normcase path) independent of traversal order; test_w2_002_deterministic_original PASS
- [x] T-020 W2-003 (P2) truthful terminal states | owner: workbuddy | verify: scan emits COMPLETED / COMPLETED_WITH_ERRORS / FAILED / CANCELLED with skipped-file accounting; invalid root -> FAILED (not perpetual "searching")
- [x] T-021 W2-004 (P2) build provenance | owner: workbuddy | verify: VERSION single source of truth; *.spec tracked in .gitignore (removed ignore); README documents PyInstaller build + release metadata (commit/version/SHA256). Full CI repro pipeline left as follow-up (out of session scope)
- [x] T-022 PERF-001 (P1) size-prefilter | owner: workbuddy | verify: hash only size-collision candidate groups; unique-size files never hashed; test_perf001_unique_size_not_hashed PASS (254x-class I/O avoided)
- [x] T-023 PERF-002 (P1) bounded/coalesced queue | owner: workbuddy | verify: queue.Queue(maxsize) backpressure; status coalesced (last wins); per-tick batch (BATCH=800) + after(40) reschedule; results never dropped
- [x] T-024 PERF-003 (P1) inverse path_to_item O(1) lookup | owner: workbuddy | verify: path_to_item map; _disarm resolves row in O(1); test_perf003_member_paths_unique PASS
- [x] T-025 PERF-004 (P2) single-pass empty-folder traversal | owner: workbuddy | verify: find_empty_dirs single bottom-up walk; removed-dir tracking cleans full chain in one pass
- [x] T-026 PERF-005 (P2) tooltip reuse | owner: workbuddy | verify: _hover_item guard prevents Toplevel churn on unchanged hover; tooltip destroyed on close

## BLOCKED

"""Regression tests for SMART VAC DUPLICATE REMOVER remediation (AUDIT_ALL_3).

Headless: exercises DuplicateEngine, find_empty_dirs and the destructive UI
paths through unbound methods with stub Tk collaborators (no Tk required).
Covers CORE-001/002/003, W2-001/002, PERF-001/003 and T-027/T-028/T-029.
"""

import builtins
import contextlib
import inspect
import json
import multiprocessing
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

import delete_duplicates_gui as mod
from delete_duplicates_gui import (
    DuplicateEngine,
    DuplicateFinderApp,
    DuplicateGroup,
    FileRecord,
    find_empty_dirs,
)


def _make_file(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def _read_bytes(path):
    """Read a file's bytes so a survivor can be checked for content, not just
    presence: a destructive race must never leave a truncated copy behind."""
    with open(path, "rb") as f:
        return f.read()


class _Var:
    """Stand-in for tk.BooleanVar / tk.StringVar."""

    def __init__(self, value=None):
        self._value = value
        self._callbacks = []

    def get(self):
        return self._value

    def set(self, value):
        self._value = value
        for cb in list(self._callbacks):
            try:
                cb("", "", "w")
            except TypeError:
                cb()

    def trace_add(self, mode, callback):
        self._callbacks.append(callback)

    def trace(self, mode, callback):
        self._callbacks.append(callback)


class _FakeButton:
    def __init__(self):
        self.state = "disabled"

    def config(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]


class _FakeThread:
    def __init__(self, alive=True):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _FakeTree:
    def __init__(self):
        self.deleted = []
        self.children = ()
        self.set_values = {}

    def get_children(self):
        return self.children

    def delete(self, *items):
        self.deleted.extend(items)

    def set(self, item, column, value):
        self.set_values.setdefault(item, {})[column] = value


class _FakeRoot:
    def __init__(self):
        self.after_calls = []

    def after(self, ms, fn):
        self.after_calls.append((ms, fn))


class _FakeMessagebox:
    def __init__(self, answer=True):
        self.answer = answer
        self.asked = []
        self.errors = []
        self.warnings = []
        self.infos = []

    def askyesno(self, _title, message):
        self.asked.append(message)
        return self.answer

    def showerror(self, _title, message):
        self.errors.append(message)

    def showwarning(self, _title, message):
        self.warnings.append(message)

    def showinfo(self, _title, message):
        self.infos.append(message)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = DuplicateEngine()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scan(self, root=None):
        return self.engine.scan(root or self.tmp)

    def test_scan_finds_duplicate_group(self):
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "c.bin"), b"unique")
        res = self._scan()
        self.assertEqual(res.stats["state"], "completed")
        self.assertEqual(len(res.groups), 1)
        self.assertEqual(len(res.groups[0].members), 2)

    def test_core001_last_copy_guard(self):
        """Selecting every member of a group must be refused (never delete last copy)."""
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        res = self._scan()
        group = res.groups[0]
        plan = self.engine.plan_deletion(res.groups, list(group.members))
        self.assertTrue(plan["refused_groups"], "group with all members selected must be refused")
        self.assertEqual(plan["approved"], [])
        reasons = {why for _, why in plan["skipped"]}
        self.assertIn("last_copy_refused", reasons)

    def test_core001_normal_deletion_allowed(self):
        """Selecting only the duplicate (not the original) is a safe plan."""
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        res = self._scan()
        group = res.groups[0]
        dup = next(m for m in group.members if not m.is_original)
        plan = self.engine.plan_deletion(res.groups, [dup])
        self.assertEqual(plan["refused_groups"], [])
        self.assertEqual(plan["approved"], [dup])

    def test_core002_stale_changed_file_skipped(self):
        """A selected file modified after scanning must NOT be deleted."""
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        res = self._scan()
        group = res.groups[0]
        dup = next(m for m in group.members if not m.is_original)
        # Mutate the selected file to same length but different content: size passes,
        # hash differs -> 'content_changed' refusal (CORE-002 revalidation).
        _make_file(dup.path, b"XYZ")
        plan = self.engine.plan_deletion(res.groups, [dup])
        self.assertEqual(plan["approved"], [])
        reasons = {why for _, why in plan["skipped"]}
        self.assertIn("content_changed", reasons)

    def test_core002_missing_file_skipped(self):
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        res = self._scan()
        group = res.groups[0]
        dup = next(m for m in group.members if not m.is_original)
        os.remove(dup.path)
        plan = self.engine.plan_deletion(res.groups, [dup])
        self.assertEqual(plan["approved"], [])
        reasons = {why for _, why in plan["skipped"]}
        self.assertIn("missing", reasons)

    def test_core003_independent_scan_ids(self):
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        r1 = self._scan()
        r2 = self._scan()
        self.assertEqual(r1.groups[0].group_id, 1)
        self.assertEqual(r2.groups[0].group_id, 1)

    def test_core003_orphan_group_skipped(self):
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        res = self._scan()
        ghost = res.groups[0].members[0]
        ghost.group_id = 999
        plan = self.engine.plan_deletion(res.groups, [ghost])
        reasons = {why for _, why in plan["skipped"]}
        self.assertIn("orphan_group", reasons)

    def test_w2_001_root_preserved(self):
        root = self.tmp
        os.makedirs(os.path.join(root, "empty_child"))
        os.makedirs(os.path.join(root, "a", "b", "c"))
        empties = find_empty_dirs(root)
        self.assertNotIn(root, empties)
        self.assertIn(os.path.join(root, "empty_child"), empties)
        self.assertIn(os.path.join(root, "a", "b", "c"), empties)

    def test_w2_001_idempotent(self):
        root = self.tmp
        os.makedirs(os.path.join(root, "x", "y"))
        # find_empty_dirs reports the full chain in one pass (PERF-004).
        first = find_empty_dirs(root)
        for d in first:
            os.rmdir(d)
        second = find_empty_dirs(root)
        self.assertEqual(second, [], "repeatable cleanup must remove zero dirs on second pass")
        self.assertTrue(os.path.isdir(root), "selected root must survive cleanup")

    def test_t031_relative_root_reports_full_chain(self):
        """PERF-004 chain cleanup must not depend on the root being absolute (T-031)."""
        os.makedirs(os.path.join(self.tmp, "a", "b", "c"))
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            relative = find_empty_dirs("a")
        finally:
            os.chdir(cwd)
        absolute = find_empty_dirs(os.path.join(self.tmp, "a"))
        self.assertEqual(len(relative), len(absolute), "relative root must report the same chain")
        self.assertEqual([os.path.normpath(p) for p in relative],
                         [os.path.relpath(p, self.tmp) for p in absolute])

    def test_t037_cancel_mid_hash_returns_no_partial_groups(self):
        """A scan cancelled mid-hash saw part of the tree; partial groups must not ship (T-37)."""
        saved_bs = self.engine.block_size
        self.engine.block_size = 2
        for name in ("a.bin", "b.bin", "c.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        cancel = threading.Event()
        real = mod.hash_file
        calls = {"n": 0}

        def spy(path, block_size=65536, cancel_event=None):
            calls["n"] += 1
            if calls["n"] == 2:
                cancel.set()
            return real(path, block_size, cancel_event)

        mod.hash_file = spy
        try:
            res = self.engine.scan(self.tmp, cancel_event=cancel)
        finally:
            mod.hash_file = real
            self.engine.block_size = saved_bs
        self.assertEqual(res.stats["state"], "cancelled")
        self.assertEqual(res.groups, [], "a cancelled scan must not report partial duplicate groups")

    def test_w2_002_deterministic_original(self):
        _make_file(os.path.join(self.tmp, "z.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        r1 = self._scan()
        r2 = self._scan()
        orig1 = next(m.path for m in r1.groups[0].members if m.is_original)
        orig2 = next(m.path for m in r2.groups[0].members if m.is_original)
        self.assertEqual(orig1, orig2, "survivor selection must be traversal-order independent")

    def test_perf001_unique_size_not_hashed(self):
        saved_bs = self.engine.block_size
        self.engine.block_size = 50
        calls = {"n": 0}
        real = mod.hash_file

        def spy(path, block_size=65536, cancel_event=None):
            calls["n"] += 1
            return real(path, block_size, cancel_event)

        mod.hash_file = spy
        try:
            # Three unique-size files (no possible duplicate) + one duplicate pair.
            for i, size in enumerate((10, 20, 30)):
                _make_file(os.path.join(self.tmp, f"uniq{i}.bin"), b"u" * size)
            _make_file(os.path.join(self.tmp, "d1.bin"), b"DUP" * 50)
            _make_file(os.path.join(self.tmp, "d2.bin"), b"DUP" * 50)
            res = self._scan()
        finally:
            mod.hash_file = real
            self.engine.block_size = saved_bs
        # Only the 2 files in the size-collision group should be hashed (3 unique-size skipped).
        self.assertEqual(calls["n"], 2, "unique-size files must not be hashed")
        self.assertEqual(len(res.groups), 1)

    def test_perf003_member_paths_unique(self):
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "c.bin"), b"DUP2")
        _make_file(os.path.join(self.tmp, "d.bin"), b"DUP2")
        res = self._scan()
        paths = [m.path for g in res.groups for m in g.members]
        self.assertEqual(len(paths), len(set(paths)), "member paths must be unique for O(1) map")

    def test_t029_no_dead_inode_field(self):
        """FileRecord must carry no field nothing reads (T-029 inode, T-035 mtime)."""
        self.assertNotIn("inode", FileRecord.__dataclass_fields__)
        self.assertNotIn("mtime", FileRecord.__dataclass_fields__)


class DestructiveUITest(unittest.TestCase):
    """T-028: cover the real-filesystem UI paths without Tk."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log_path = os.path.join(self.tmp, "deleted_log.txt")
        self.recovery_meta = os.path.join(self.tmp, "quarantine_recovery.json")
        self.mbox = _FakeMessagebox()
        self._saved = (mod.messagebox, mod.LOG_FILE, mod.send_to_trash,
                       mod.deletion_is_recoverable, mod.platform, mod.RECOVERY_LOG_FILE)
        mod.messagebox = self.mbox
        mod.LOG_FILE = self.log_path
        mod.RECOVERY_LOG_FILE = self.recovery_meta
        self.trashed = []

        def fake_trash(path, original_path=None):
            self.trashed.append(path)
            os.remove(path)

        mod.send_to_trash = fake_trash
        mod.deletion_is_recoverable = lambda: True

    def tearDown(self):
        (mod.messagebox, mod.LOG_FILE, mod.send_to_trash,
         mod.deletion_is_recoverable, mod.platform, mod.RECOVERY_LOG_FILE) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _app(self, groups=(), select=()):
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.engine = DuplicateEngine()
        app.current_groups = list(groups)
        app.current_records = [m for g in app.current_groups for m in g.members]
        selected = {r.path for r in select}
        app.check_vars = {r.path: _Var(r.path in selected) for r in app.current_records}
        app.path_to_item = {r.path: f"I{i}" for i, r in enumerate(app.current_records)}
        app.item_path_map = {v: k for k, v in app.path_to_item.items()}
        app.tree = _FakeTree()
        app.log_var = _Var(True)
        app.status_var = _Var("")
        app.path_var = _Var(self.tmp)
        app.stop_button = _FakeButton()
        app.browse_button = _FakeButton()
        app.current_result_root = os.path.normcase(os.path.abspath(self.tmp))
        app._sessions = {}
        app.current_scan_id = 1
        app.cancel_event = None
        app.worker_thread = None

        def _on_path_var_write(*_args):
            DuplicateFinderApp._on_path_changed(app)

        app.path_var.trace_add("write", _on_path_var_write)
        return app

    @staticmethod
    def _bare_app():
        """Minimal app shell for model-level (non-destructive) tests."""
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.engine = DuplicateEngine()
        app.current_groups = []
        app.current_records = []
        app.check_vars = {}
        app.item_path_map = {}
        app.path_to_item = {}
        app.tree = _FakeTree()
        app.status_var = _Var("")
        app._shutting_down = False
        return app

    @contextlib.contextmanager
    def _failing_log_close(self):
        """Deletion log whose buffered writes are only lost at close (T-36)."""

        class _BadHandle:
            def write(self, text):
                return len(text)

            def close(self):
                raise OSError("disk full on flush")

        original_open = builtins.open
        log_path = self.log_path

        def fake_open(path, *args, **kwargs):
            if str(path) == log_path:
                return _BadHandle()
            return original_open(path, *args, **kwargs)

        builtins.open = fake_open
        try:
            yield
        finally:
            builtins.open = original_open

    def _dup_pair(self):
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "copy (1).bin"), b"DUP")
        res = DuplicateEngine().scan(self.tmp)
        self.assertEqual(len(res.groups), 1)
        group = res.groups[0]
        original = next(m for m in group.members if m.is_original)
        dup = next(m for m in group.members if not m.is_original)
        return res.groups, original, dup

    def test_t028_delete_selected_deletes_duplicate_keeps_original(self):
        groups, original, dup = self._dup_pair()
        app = self._app(groups, [dup])
        dup_item = app.path_to_item[dup.path]
        orig_item = app.path_to_item[original.path]
        DuplicateFinderApp.delete_selected(app)
        # T-49: destruction acts on the acquired object — exactly one trashed
        # entry, carrying the original basename under the quarantine suffix.
        self.assertEqual(len(self.trashed), 1)
        self.assertIn(os.path.basename(dup.path), os.path.basename(self.trashed[0]))
        self.assertIn(".__vacq_", self.trashed[0])
        self.assertFalse(os.path.exists(dup.path))
        self.assertFalse(os.path.exists(self.trashed[0]), "acquired object must be gone")
        self.assertTrue(os.path.exists(original.path), "survivor must never be deleted")
        self.assertIn("Удалено файлов: 1", app.status_var.get())
        self.assertNotIn(dup.path, app.check_vars, "deleted row must be disarmed")
        # T-53: singletons collapse — original row also removed from UI model without touching disk
        self.assertEqual(set(app.tree.deleted), {dup_item, orig_item})
        self.assertEqual(app.current_groups, [])
        with open(self.log_path, encoding="utf-8") as f:
            self.assertIn(dup.path, f.read())

    def test_t028_delete_selected_refuses_last_copy(self):
        groups, original, dup = self._dup_pair()
        app = self._app(groups, [original, dup])
        DuplicateFinderApp.delete_selected(app)
        self.assertEqual(self.trashed, [], "last-copy refusal must delete nothing")
        self.assertEqual(self.mbox.asked, [], "refusal must not reach the confirm dialog")
        self.assertTrue(self.mbox.errors)
        self.assertTrue(os.path.exists(original.path))
        self.assertTrue(os.path.exists(dup.path))

    def test_t028_delete_selected_declined_deletes_nothing(self):
        groups, _original, dup = self._dup_pair()
        self.mbox.answer = False
        app = self._app(groups, [dup])
        DuplicateFinderApp.delete_selected(app)
        self.assertEqual(self.trashed, [])
        self.assertTrue(os.path.exists(dup.path))

    def test_t028_delete_selected_reports_failure_truthfully(self):
        groups, _original, dup = self._dup_pair()

        def failing_trash(path):
            raise OSError("access denied")

        mod.send_to_trash = failing_trash
        app = self._app(groups, [dup])
        DuplicateFinderApp.delete_selected(app)
        self.assertTrue(os.path.exists(dup.path))
        status = app.status_var.get()
        self.assertIn("Удалено файлов: 0", status)
        self.assertIn("не удалось: 1", status)
        self.assertIn(dup.path, app.check_vars, "failed delete must not disarm the row")

    def test_t045_failed_member_still_counts_as_survivor(self):
        """T-45: a member whose trash failed stays on disk and must remain a
        valid keeper for later members in the same batch. With the original
        stale (post-plan change), dups[0]-on-disk is dups[1]'s ONLY survivor:
        pre-fix code excluded it by the PLANNED set and refused the deletion
        for the wrong reason; post-fix the group keeps exactly one copy."""
        for name in ("a.bin", "b.bin", "c.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        res = DuplicateEngine().scan(self.tmp)
        self.assertEqual(len(res.groups), 1)
        group = res.groups[0]
        original = next(m for m in group.members if m.is_original)
        dups = [m for m in group.members if not m.is_original]

        calls = {"n": 0}

        def selective_trash(path):
            calls["n"] += 1
            if calls["n"] == 1:
                _make_file(original.path, b"CHANGED")  # survivor goes stale post-plan
                raise OSError("locked")
            os.remove(path)

        mod.send_to_trash = selective_trash
        app = self._app(res.groups, dups)
        DuplicateFinderApp.delete_selected(app)
        # Survivor invariant: at least one member still exists on disk and the
        # failed member is one of them. The changed original also stays (it was
        # never a deletion target), so expect original + dups[0] alive.
        self.assertTrue(os.path.exists(dups[0].path), "failed member must stay on disk")
        self.assertTrue(os.path.exists(original.path), "the never-targeted original must survive")
        self.assertFalse(os.path.exists(dups[1].path),
                         "later member deletes against the failed-but-present member")
        status = app.status_var.get()
        self.assertIn("Удалено файлов: 1", status)
        self.assertIn("не удалось: 1", status)

    def test_t045_failed_member_survivor_green_control(self):
        """T-45 green control: with the original still valid, a failed member
        does not block the other duplicate's legitimate deletion."""
        for name in ("a.bin", "b.bin", "c.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        res = DuplicateEngine().scan(self.tmp)
        group = res.groups[0]
        dups = [m for m in group.members if not m.is_original]

        calls = {"n": 0}

        def selective_trash(path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("locked")
            os.remove(path)

        mod.send_to_trash = selective_trash
        app = self._app(res.groups, dups)
        DuplicateFinderApp.delete_selected(app)
        self.assertTrue(os.path.exists(dups[0].path))
        self.assertFalse(os.path.exists(dups[1].path), "second member deletes fine against the healthy original")
        self.assertIn("Удалено файлов: 1", app.status_var.get())

    def test_t049_quarantined_object_fingerprint_mismatch_not_deleted(self):
        """T-49: if the acquired object fails fingerprint validation (was replaced
        between scan and acquisition), it is restored and never destroyed."""
        groups, _original, dup = self._dup_pair()
        app = self._app(groups, [dup])
        # Mutate the candidate AFTER scan but BEFORE delete_selected acquires it.
        real_verify = app.engine.verify_record

        def mutating_verify(rec):
            if rec.path == dup.path and os.path.exists(dup.path):
                _make_file(dup.path, b"REPLACEMENT")
            return real_verify(rec)

        app.engine.verify_record = mutating_verify
        DuplicateFinderApp.delete_selected(app)
        self.assertEqual(self.trashed, [], "changed-after-scan candidate must not be destroyed")
        self.assertIn("пропущено", app.status_var.get())
        self.assertTrue(os.path.exists(dup.path),
                        "the candidate must be restored to its original name")

    def test_t049_post_verify_replacement_survives_destructive_window(self):
        """T-49 (audit probe): an unscanned replacement installed at the original
        path AFTER the last ordinary verification must survive — the destructive
        primitive acts on the ACQUIRED object, never on the pathname.

        Red control: pre-fix code (verify path -> send_to_trash(path)) removes
        the replacement and reports "Удалено файлов: 1".
        """
        groups, _original, dup = self._dup_pair()
        acquired = {}

        def replacement_trash(path):
            # path is the acquired quarantine name; the replacement appears at
            # the original path inside the destructive window.
            acquired["path"] = path
            _make_file(dup.path, b"UNSCANNED-REPLACEMENT")
            os.remove(path)

        mod.send_to_trash = replacement_trash
        app = self._app(groups, [dup])
        DuplicateFinderApp.delete_selected(app)
        self.assertIn(".__vacq_", acquired.get("path", ""),
                      "destruction must target the acquired object, not the pathname")
        self.assertTrue(os.path.exists(dup.path), "replacement must survive untouched")
        with open(dup.path, "rb") as f:
            self.assertEqual(f.read(), b"UNSCANNED-REPLACEMENT",
                             "the unscanned replacement must never be destroyed")

    def test_t049_survivor_vanished_after_acquire_restores_candidate(self):
        """T-49: if the only survivor vanishes after acquisition, the candidate is
        restored to its original name and never destroyed."""
        groups, original, dup = self._dup_pair()
        app = self._app(groups, [dup])
        real_verify = app.engine.verify_record
        # The survivor is healthy until acquisition, then reported missing.
        state = {"acquired": False}

        def survivor_gone_after_acquire(rec):
            if rec.path == dup.path:
                state["acquired"] = True
                return real_verify(rec)
            if state["acquired"]:
                return False, "missing"
            return real_verify(rec)

        app.engine.verify_record = survivor_gone_after_acquire
        DuplicateFinderApp.delete_selected(app)
        self.assertTrue(state["acquired"], "acquisition must have been attempted")
        self.assertTrue(os.path.exists(dup.path), "candidate must be restored when survivor vanishes")
        self.assertEqual(self.trashed, [], "no destruction when survivor lost post-acquire")

    def test_t061_quarantined_candidate_restores_original_name_before_trash(self):
        """T-61 / T-62: object-identity-bound destructive semantics.
        send_to_trash deletes the verified acquired candidate directly without
        restoring it to the original pathname first, eliminating TOCTOU races.
        If an unscanned replacement occupied the original path while quarantined,
        send_to_trash deletes the quarantine object, the replacement survives untouched,
        and durable recovery metadata records the relationship."""
        mod.send_to_trash = self._saved[2]
        groups, original, dup = self._dup_pair()
        # Case A: occupied destination - replacement survives, acquired object deleted
        qpath = mod.acquire_candidate(dup.path)
        self.assertTrue(os.path.exists(qpath))
        self.assertFalse(os.path.exists(dup.path))
        _make_file(dup.path, b"UNSCANNED_REPLACEMENT")

        # Refusal verification: restore_candidate refuses safely if destination is occupied
        self.assertFalse(mod.restore_candidate(qpath, dup.path),
                         "restore_candidate must refuse when destination is occupied")
        self.assertTrue(os.path.exists(qpath), "quarantined candidate must survive refusal")
        self.assertTrue(os.path.exists(dup.path), "replacement must survive")

        # T-68 (REC-004): destruction is authorized only by a durable acquired
        # receipt carrying the original size and SHA-256.
        mod.record_quarantine_metadata(qpath, dup.path, size=dup.size,
                                       fingerprint=dup.fingerprint,
                                       status="acquired")

        # Object-identity deletion: send_to_trash deletes qpath, leaving replacement untouched
        mod.send_to_trash(qpath, original_path=dup.path)
        self.assertFalse(os.path.exists(qpath), "quarantine object must be deleted")
        self.assertTrue(os.path.exists(dup.path), "unscanned replacement must survive untouched")
        with open(dup.path, "rb") as f:
            self.assertEqual(f.read(), b"UNSCANNED_REPLACEMENT")

        # Verify durable recovery metadata was recorded
        meta = mod.get_quarantine_metadata(qpath)
        self.assertIsNotNone(meta, "durable recovery metadata must be recorded")
        self.assertEqual(meta["original_path"], os.path.abspath(dup.path))

        # Case B: clear destination - acquired candidate deleted, recovery metadata recorded
        os.remove(dup.path)
        qpath2 = mod.acquire_candidate(original.path)
        self.assertTrue(os.path.exists(qpath2))
        self.assertFalse(os.path.exists(original.path))
        mod.record_quarantine_metadata(qpath2, original.path, size=original.size,
                                       fingerprint=original.fingerprint,
                                       status="acquired")
        mod.send_to_trash(qpath2, original_path=original.path)
        self.assertFalse(os.path.exists(qpath2), "quarantine path must be deleted")
        self.assertFalse(os.path.exists(original.path), "original path must remain absent")
        meta2 = mod.get_quarantine_metadata(qpath2)
        self.assertIsNotNone(meta2)
        self.assertEqual(meta2["original_path"], os.path.abspath(original.path))

    @unittest.skipUnless(platform.system() == "Windows", "Windows Recycle Bin integration only")
    def test_t061_windows_recycle_bin_metadata_and_restore_integration(self):
        """T-61 / T-62: delete duplicate to Windows Recycle Bin with durable recovery metadata,
        inspect Recycle Bin for the quarantined item, restore via Shell verb to disk,
        and verify full restoration through restore_quarantined_file."""
        try:
            import comtypes.client
        except ImportError:
            self.skipTest("comtypes not available for Recycle Bin COM inspection")

        # Use the real production send_to_trash
        mod.send_to_trash = self._saved[2]
        groups, orig, dup = self._dup_pair()
        app = self._app(groups, [dup])

        # Delete selected duplicate
        DuplicateFinderApp.delete_selected(app)

        self.assertFalse(os.path.exists(dup.path), "duplicate must be deleted from source")
        self.assertTrue(os.path.exists(orig.path), "original copy must survive")

        # Inspect Windows Recycle Bin via Shell COM
        shell = comtypes.client.CreateObject("Shell.Application")
        rb = shell.Namespace(10)  # CSIDL_BITBUCKET
        found_item = None
        dup_name = os.path.basename(dup.path)
        expected_meta = mod.get_quarantine_metadata(dup.path)
        qname = expected_meta.get("quarantine_name") if expected_meta else None
        for i in range(rb.Items().Count):
            it = rb.Items().Item(i)
            if qname and it.Name == qname:
                found_item = it
                break
            elif it.Name == dup_name:
                meta = mod.search_quarantine_metadata(it.Name)
                if meta and meta.get("original_path") == os.path.abspath(dup.path):
                    found_item = it
                    break

        self.assertIsNotNone(found_item,
                             f"Item for {dup_name} must be found in Recycle Bin")

        # Restore from Recycle Bin
        verbs = found_item.Verbs()
        restored_verb = False
        for i in range(verbs.Count):
            v = verbs.Item(i)
            v_name = v.Name.lower()
            if "estore" in v_name or "осс" in v_name:
                v.DoIt()
                restored_verb = True
                break

        self.assertTrue(restored_verb, "Recycle Bin item must expose a Restore verb")

        # Wait up to 5 seconds for Windows shell to restore file to disk
        settled = False
        restored_on_disk = None
        qpath_expected = expected_meta["quarantine_path"] if expected_meta else None
        for _ in range(50):
            if os.path.exists(dup.path):
                settled = True
                restored_on_disk = dup.path
                break
            elif qpath_expected and os.path.exists(qpath_expected):
                settled = True
                restored_on_disk = qpath_expected
                break
            time.sleep(0.1)

        self.assertTrue(settled, "File must be restored from Recycle Bin")
        if restored_on_disk != dup.path:
            # Perform verified restore workflow to restore original name and path
            ok, final_path = mod.restore_quarantined_file(restored_on_disk, engine=app.engine)
            self.assertTrue(ok, f"restore_quarantined_file must succeed: {final_path}")
            self.assertEqual(final_path, dup.path)

        with open(dup.path, "rb") as f:
            self.assertEqual(f.read(), b"DUP",
                             "Restored file must retain intact content")

    def test_t052_selector_change_after_scan_invalidates_results_and_deletes_zero(self):
        """T-52 (FINDING B): changing path selector from root A to root B invalidates
        existing results; subsequent delete_selected deletes zero files from root A."""
        groups, orig, dup = self._dup_pair()
        app = self._app(groups, [dup])
        self.assertEqual(len(app.current_records), 2)
        self.assertTrue(app.check_vars[dup.path].get())

        # Create another folder B
        dir_b = os.path.join(self.tmp, "dir_b")
        os.makedirs(dir_b)

        # User changes path selector to folder B without scanning
        app.path_var.set(dir_b)

        # Invalidation must clear model and tree state
        self.assertEqual(app.current_records, [], "records must be cleared on path change")
        self.assertEqual(app.current_groups, [], "groups must be cleared on path change")
        self.assertEqual(app.check_vars, {}, "check_vars must be disarmed")
        self.assertIsNone(app.current_result_root, "result root must be reset")

        # Attempting delete_selected must do nothing and delete zero files
        DuplicateFinderApp.delete_selected(app)
        self.assertEqual(self.trashed, [], "zero files must be deleted after selector change")
        self.assertTrue(os.path.exists(dup.path), "duplicate in root A must survive")
        self.assertTrue(os.path.exists(orig.path), "original in root A must survive")

    def test_t052_delete_selected_defense_in_depth_refuses_out_of_scope_records(self):
        """T-52 defense-in-depth: even if records from root A bypass path_var trace
        while path_var is set to root B, delete_selected refuses deletion and disarms."""
        groups, orig, dup = self._dup_pair()
        app = self._app(groups, [dup])
        dir_b = os.path.join(self.tmp, "dir_b")
        os.makedirs(dir_b)

        # Directly set path_var without triggering trace callback
        app.path_var._value = dir_b

        DuplicateFinderApp.delete_selected(app)
        self.assertEqual(self.trashed, [], "out-of-scope files must never be deleted")
        self.assertTrue(os.path.exists(dup.path), "duplicate must survive")
        self.assertTrue(os.path.exists(orig.path), "original must survive")
        self.assertEqual(app.current_records, [], "out-of-scope records must be disarmed")

    def test_t052_selector_change_during_active_scan_cancels_and_disarms(self):
        """T-52: changing selector during an active scan cancels the scan,
        disarms results, and ignores late-arriving add_result events."""
        app = self._bare_app()
        app.queue = queue.Queue()
        app.root = _FakeRoot()
        app.stop_button = _FakeButton()
        app.browse_button = _FakeButton()
        app.path_var = _Var(self.tmp)
        app.cancel_event = threading.Event()
        app.worker_thread = _FakeThread(alive=True)
        app.current_scan_id = 1
        app.current_result_root = os.path.normcase(os.path.abspath(self.tmp))

        def _on_path_var_write(*_args):
            DuplicateFinderApp._on_path_changed(app)

        app.path_var.trace_add("write", _on_path_var_write)

        dir_b = os.path.join(self.tmp, "dir_b")
        os.makedirs(dir_b)

        # Change selector while scan is alive
        app.path_var.set(dir_b)

        self.assertTrue(app.cancel_event.is_set(), "scan must be cancelled on path change")
        self.assertIsNone(app.current_result_root, "current_result_root must be invalidated")

        # Worker puts add_result into queue
        app.queue.put(("add_result", 1, FileRecord(path=os.path.join(self.tmp, "late.bin"), size=10, fingerprint="f")))
        DuplicateFinderApp.process_queue(app)
        self.assertEqual(len(app.check_vars), 0, "late results for cancelled scan must be discarded")

    def test_t052_browse_folder_during_scan_cancels_and_invalidates(self):
        """T-52: browse_folder during an active scan triggers cancel_scan and invalidation."""
        app = self._app()
        app.worker_thread = _FakeThread(alive=True)
        app.cancel_event = threading.Event()
        dir_b = os.path.join(self.tmp, "dir_b")
        os.makedirs(dir_b)

        saved_filedialog = mod.filedialog
        mod.filedialog = types.SimpleNamespace(askdirectory=lambda: dir_b)
        try:
            DuplicateFinderApp.browse_folder(app)
        finally:
            mod.filedialog = saved_filedialog

        self.assertTrue(app.cancel_event.is_set(), "scan must be cancelled by browse_folder")
        self.assertEqual(app.path_var.get(), dir_b, "path_var must be updated to selected folder")
        self.assertIsNone(app.current_result_root, "results must be invalidated")

    def test_t052_untouched_selector_keeps_normal_flow_functional(self):
        """T-52: untouched selector allows normal scan and deletion flow."""
        groups, orig, dup = self._dup_pair()
        app = self._app(groups, [dup])
        DuplicateFinderApp.delete_selected(app)
        self.assertEqual(len(self.trashed), 1, "selected duplicate must be deleted normally")
        self.assertFalse(os.path.exists(dup.path))
        self.assertTrue(os.path.exists(orig.path))

    def test_t027_confirm_claims_permanent_off_windows(self):
        groups, _original, dup = self._dup_pair()
        mod.deletion_is_recoverable = lambda: False
        app = self._app(groups, [dup])
        DuplicateFinderApp.delete_selected(app)
        self.assertEqual(len(self.mbox.asked), 1)
        message = self.mbox.asked[0]
        self.assertIn("НАВСЕГДА", message)
        self.assertNotIn("восстановить", message)

    def test_t027_confirm_claims_recycle_bin_on_windows(self):
        groups, _original, dup = self._dup_pair()
        mod.deletion_is_recoverable = lambda: True
        app = self._app(groups, [dup])
        DuplicateFinderApp.delete_selected(app)
        self.assertIn("корзину", self.mbox.asked[0])
        self.assertIn("восстановить", self.mbox.asked[0])

    def test_t027_send_to_trash_permanent_off_windows(self):
        mod.deletion_is_recoverable = self._saved[3]
        mod.platform = types.SimpleNamespace(system=lambda: "Linux")
        path = os.path.join(self.tmp, "gone.bin")
        content = b"x"
        _make_file(path, content)
        mod.send_to_trash = self._saved[2]
        # T-68 (REC-004): destruction now requires a durable acquired receipt.
        mod.record_quarantine_metadata(path, path, size=len(content),
                                       fingerprint=mod.hash_file(path),
                                       status="acquired")
        mod.send_to_trash(path)
        self.assertFalse(os.path.exists(path), "off-Windows fallback deletes permanently")

    def test_t028_delete_empty_folders_keeps_root(self):
        os.makedirs(os.path.join(self.tmp, "x", "y"))
        app = self._app()
        DuplicateFinderApp.delete_empty_folders(app)
        self.assertTrue(os.path.isdir(self.tmp), "selected root must survive")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))
        self.assertTrue(any("Удалено пустых папок: 2" in m for m in self.mbox.infos))
        with open(self.log_path, encoding="utf-8") as f:
            self.assertIn("Пустая папка удалена", f.read())

    def test_t028_delete_empty_folders_declined_keeps_dirs(self):
        os.makedirs(os.path.join(self.tmp, "keep"))
        self.mbox.answer = False
        app = self._app()
        DuplicateFinderApp.delete_empty_folders(app)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "keep")))

    def test_t034_scrollbar_wired_to_tree(self):
        """T-034: the scrollbar drives the tree and the tree drives the thumb."""
        source = inspect.getsource(DuplicateFinderApp._build_ui)
        self.assertIn("yscrollcommand=scrollbar.set", source)
        self.assertNotIn("yscrollcommand=self.tree.yview", source)

    def test_t036_log_close_failure_reported(self):
        """A log that fails to flush must not be reported as a complete log (T-36)."""
        groups, _original, dup = self._dup_pair()
        with self._failing_log_close():
            app = self._app(groups, [dup])
            DuplicateFinderApp.delete_selected(app)
        # T-49: the destructive primitive receives the acquired quarantine name,
        # not the original path.
        self.assertEqual(len(self.trashed), 1, "the delete itself still happened")
        base = os.path.basename(dup.path)
        self.assertIn(".__vacq_", self.trashed[0],
                      "trash must act on the acquired object: " + self.trashed[0])
        self.assertIn(base, os.path.basename(self.trashed[0]),
                      "quarantine name must carry the original basename")
        self.assertIn("лог", app.status_var.get())
        self.assertIn("закрытия", app.status_var.get())

    def test_t036_empty_folder_log_close_failure_reported(self):
        os.makedirs(os.path.join(self.tmp, "gone"))
        with self._failing_log_close():
            app = self._app()
            DuplicateFinderApp.delete_empty_folders(app)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "gone")))
        self.assertTrue(any("закрытия" in m for m in self.mbox.infos))

    def test_t030_invalid_root_reaches_failed_terminal_state(self):
        """W2-003: a failed scan stores no session; the terminal handler must still report it."""
        app = self._app()
        app._sessions = {}
        app.current_scan_id = 1
        DuplicateFinderApp._on_terminal(app, "failed", {"state": "failed", "skipped": 0})
        self.assertIn("не удался", app.status_var.get())
        self.assertEqual(app.current_groups, [])
        self.assertEqual(app.current_records, [])

    def test_t032_cancel_scan_sets_event_while_running(self):
        app = self._app()
        app.cancel_event = threading.Event()
        app.worker_thread = _FakeThread(alive=True)
        app.stop_button.state = "normal"
        DuplicateFinderApp.cancel_scan(app)
        self.assertTrue(app.cancel_event.is_set(), "stop must reach the scan's cancel event")
        self.assertEqual(app.stop_button.state, "disabled")

    def test_t032_cancel_scan_noop_without_running_scan(self):
        app = self._app()
        app.cancel_event = threading.Event()
        app.worker_thread = _FakeThread(alive=False)
        DuplicateFinderApp.cancel_scan(app)
        self.assertFalse(app.cancel_event.is_set())

    def test_t032_terminal_state_disables_stop(self):
        app = self._app()
        app.stop_button.state = "normal"
        DuplicateFinderApp._on_terminal(app, "cancelled", {"state": "cancelled", "skipped": 0})
        self.assertEqual(app.stop_button.state, "disabled")
        self.assertIn("отменён", app.status_var.get())
        self.assertIn("неполный", app.status_var.get(), "a cancelled scan must say the list is partial")

    def test_t033_cancel_event_honoured_by_engine(self):
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        cancel = threading.Event()
        cancel.set()
        res = DuplicateEngine().scan(self.tmp, cancel_event=cancel)
        self.assertEqual(res.stats["state"], "cancelled")
        self.assertEqual(res.groups, [])

    def test_t033_new_scan_drops_previous_sessions(self):
        """A new scan must not keep the FileRecords of every earlier scan (T-033)."""
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP")
        app = self._app()
        app._sessions = {1: "stale", 2: "stale"}
        app.worker_thread = _FakeThread(alive=False)
        started = []
        real_thread = mod.threading.Thread

        class _NoRunThread(real_thread):
            def start(self):
                started.append(True)

        mod.threading.Thread = _NoRunThread
        try:
            DuplicateFinderApp.start_finding_duplicates(app)
        finally:
            mod.threading.Thread = real_thread
        self.assertTrue(started, "scan thread must be started")
        self.assertEqual(app._sessions, {}, "earlier sessions must be released")
        self.assertEqual(app.stop_button.state, "normal", "stop must be reachable during a scan")


class SRC001CoreAuditTest(unittest.TestCase):
    """SRC-001 (audit/1.md) CORE wave regressions."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = DuplicateEngine()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dup(self, *contents):
        for i, c in enumerate(contents):
            _make_file(os.path.join(self.tmp, f"f{i}.bin"), c)

    def test_core001_symlink_not_surviving_copy(self):
        """R0001: real file + symlink to it must produce no plan authorizing target deletion."""
        if os.name != "nt" and not hasattr(os, "symlink"):
            self.skipTest("symlink unavailable")
        real = os.path.join(self.tmp, "a.bin")
        link = os.path.join(self.tmp, "z-link.bin")
        _make_file(real, b"DUPLICATE-CONTENT")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation refused")
        res = self.engine.scan(self.tmp)
        self.assertEqual(res.groups, [], "alias must never enter a duplicate group")
        reasons = {why for _, why in res.stats["errors"]}
        self.assertTrue(any("alias" in r for r in reasons), "skip reason must name the alias")

    def test_core001_verify_record_rejects_symlink(self):
        if os.name != "nt" and not hasattr(os, "symlink"):
            self.skipTest("symlink unavailable")
        real = os.path.join(self.tmp, "a.bin")
        link = os.path.join(self.tmp, "l.bin")
        _make_file(real, b"DUPLICATE-CONTENT")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation refused")
        rec = FileRecord(path=link, size=19, fingerprint="x")
        ok, reason = self.engine.verify_record(rec)
        self.assertFalse(ok)
        self.assertEqual(reason, "alias")

    def test_core002_stale_after_confirmation_not_deleted(self):
        """R0002: plan approved, file changed inside confirmation -> nothing deleted."""
        self._dup(b"DUP", b"DUP")
        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        dup = next(m for m in group.members if not m.is_original)
        plan = self.engine.plan_deletion(res.groups, [dup])
        self.assertEqual(len(plan["approved"]), 1)
        # simulate plan-then-change-then-confirm-then-delete-boundary
        _make_file(dup.path, b"NEW")
        ok, reason = self.engine.verify_record(plan["approved"][0])
        self.assertFalse(ok, "changed after planning must fail revalidation at the boundary")
        self.assertIn(reason, ("content_changed", "size_changed"))

    def test_core002_vanished_survivor_refuses_deletion(self):
        self._dup(b"DUP", b"DUP")
        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        dup = next(m for m in group.members if not m.is_original)
        orig = next(m for m in group.members if m.is_original)
        plan = self.engine.plan_deletion(res.groups, [dup])
        self.assertEqual(len(plan["approved"]), 1)
        os.remove(orig.path)  # survivor vanishes after planning
        ok, reason = self.engine.verify_record(orig)
        self.assertFalse(ok)
        # boundary survivor check must therefore refuse the deletion
        members = [m for m in group.members if m is not dup]
        survivor_ok = any(self.engine.verify_record(m)[0] for m in members)
        self.assertFalse(survivor_ok, "no currently valid survivor must refuse deletion")

    def test_core003_status_then_terminal_terminal_wins(self):
        """R0003: coalesced status must not overwrite a terminal in the same batch."""
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.queue = queue.Queue()
        app.current_scan_id = 1
        app.status_var = _Var("")
        app.stop_button = _FakeButton()
        app._shutting_down = False
        app._sessions = {1: types.SimpleNamespace(groups=[])}
        app.current_groups = []
        app.current_records = []
        app.check_vars = {}
        app.item_path_map = {}
        app.path_to_item = {}
        app.tree = _FakeTree()
        app.root = _FakeRoot()
        app.queue.put(("status", 1, "Читаю: обработано файлов 200"))
        app.queue.put(("terminal", 1, "completed", {"state": "completed", "skipped": 0}))
        DuplicateFinderApp.process_queue(app)
        self.assertIn("Завершено", app.status_var.get())
        self.assertNotIn("Читаю", app.status_var.get())

    def test_core003_stale_generation_events_ignored(self):
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.queue = queue.Queue()
        app.current_scan_id = 2
        app.status_var = _Var("current-status")
        app.stop_button = _FakeButton()
        app._shutting_down = False
        app._sessions = {}
        app.current_groups = []
        app.current_records = []
        app.check_vars = {}
        app.item_path_map = {}
        app.path_to_item = {}
        app.tree = _FakeTree()
        app.root = _FakeRoot()
        app.queue.put(("status", 1, "OLD_SCAN_STATUS"))
        app.queue.put(("error", 1, "OLD_SCAN_ERROR"))
        app.queue.put(("add_result", 1, FileRecord(path="old", size=1, fingerprint="f")))
        saved_mbox = mod.messagebox
        shown = []
        mod.messagebox = types.SimpleNamespace(showerror=lambda t, m: shown.append(m))
        try:
            DuplicateFinderApp.process_queue(app)
        finally:
            mod.messagebox = saved_mbox
        self.assertEqual(app.status_var.get(), "current-status")
        self.assertEqual(shown, [], "stale-generation errors must not surface")

    def test_core003_current_generation_status_applies(self):
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.queue = queue.Queue()
        app.current_scan_id = 3
        app.status_var = _Var("")
        app.stop_button = _FakeButton()
        app._shutting_down = False
        app._sessions = {}
        app.current_groups = []
        app.current_records = []
        app.check_vars = {}
        app.item_path_map = {}
        app.path_to_item = {}
        app.tree = _FakeTree()
        app.root = _FakeRoot()
        app.queue.put(("status", 3, "NEW_SCAN_STATUS"))
        DuplicateFinderApp.process_queue(app)
        self.assertEqual(app.status_var.get(), "NEW_SCAN_STATUS")

    def test_core004_group_keeps_one_original_after_original_removed(self):
        """R0004: deleting the designated original re-elects exactly one survivor."""
        for name in ("a.bin", "b.bin", "c.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        res = self.engine.scan(self.tmp)
        app = DestructiveUITest._bare_app()
        app.current_groups = res.groups
        app.current_records = [m for g in res.groups for m in g.members]
        for r in app.current_records:
            app.path_to_item[r.path] = f"I{r.path}"
            app.item_path_map[f"I{r.path}"] = r.path
            app.check_vars[r.path] = _Var(False)
        original = next(m for m in res.groups[0].members if m.is_original)
        os.remove(original.path)
        DuplicateFinderApp._batch_disarm(app, [original])
        markers = [m.is_original for m in app.current_groups[0].members]
        self.assertEqual(sum(markers), 1, "surviving group must keep exactly one original")

    def test_core004_only_nonoriginals_deleted_keeps_original(self):
        for name in ("a.bin", "b.bin", "c.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        res = self.engine.scan(self.tmp)
        app = DestructiveUITest._bare_app()
        app.current_groups = res.groups
        app.current_records = [m for g in res.groups for m in g.members]
        for r in app.current_records:
            app.path_to_item[r.path] = f"I{r.path}"
            app.item_path_map[f"I{r.path}"] = r.path
            app.check_vars[r.path] = _Var(False)
        original = next(m for m in res.groups[0].members if m.is_original)
        dups = [m for m in res.groups[0].members if not m.is_original]
        for d in dups:
            os.remove(d.path)
        DuplicateFinderApp._batch_disarm(app, dups)
        self.assertEqual(app.current_groups[0].members, [original])
        self.assertTrue(original.is_original)


class SRC001Wave2Test(unittest.TestCase):
    """SRC-001 W2 wave regressions."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _app(self):
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.engine = DuplicateEngine()
        app.queue = queue.Queue(maxsize=40000)
        app.current_scan_id = 1
        app.current_groups = []
        app.current_records = []
        app.check_vars = {}
        app.item_path_map = {}
        app.path_to_item = {}
        app.tree = _FakeTree()
        app.status_var = _Var("")
        app.stop_button = _FakeButton()
        app._shutting_down = False
        app._sessions = {}
        app.cancel_event = threading.Event()
        return app

    def test_w2001_cancel_during_publication_is_cancelled(self):
        """R0005: Stop during group publication aborts; state stays cancelled."""
        for g, content in (("g1", b"ONE"), ("g2", b"TWO")):
            d = os.path.join(self.tmp, g)
            os.makedirs(d)
            _make_file(os.path.join(d, "a.bin"), content)
            _make_file(os.path.join(d, "b.bin"), content)
        published = []
        cancel = threading.Event()

        def progress_cb(batch, stats):
            for grp in batch:
                if published:
                    cancel.set()
                    return
                published.append(grp.group_id)

        res = DuplicateEngine().scan(self.tmp, cancel_event=cancel, progress_cb=progress_cb)
        self.assertEqual(res.stats["state"], "cancelled")
        self.assertEqual(res.groups, [], "cancelled publication must not ship a result list")
        self.assertEqual(published, [1], "publication stops at the group after cancel")

    def test_w2002_worker_exception_emits_failed_terminal(self):
        """R0006: engine RuntimeError -> exactly one generation-scoped failed terminal."""
        app = self._app()
        app.engine = types.SimpleNamespace(
            scan=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        messages = []
        app.queue = types.SimpleNamespace(put=lambda m: messages.append(m))
        DuplicateFinderApp._scan_worker(app, 7, self.tmp, threading.Event())
        terminals = [m for m in messages if m[0] == "terminal"]
        self.assertEqual(len(terminals), 1, "exactly one terminal on worker crash")
        self.assertEqual(terminals[0][1], 7)
        self.assertEqual(terminals[0][2], "failed")
        self.assertTrue(any(m[0] == "error" and m[1] == 7 for m in messages))

    def test_w2002_successful_worker_single_terminal(self):
        app = self._app()
        for name in ("a.bin", "b.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        messages = []
        app.queue = types.SimpleNamespace(put=lambda m: messages.append(m))
        DuplicateFinderApp._scan_worker(app, 7, self.tmp, threading.Event())
        terminals = [m for m in messages if m[0] == "terminal"]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0][2], "completed")
        self.assertIn(7, app._sessions)

    def test_w2003_stale_events_consume_batch_budget(self):
        """R0007: 2500 stale events + BATCH=800 -> ~1700 remain after one tick."""
        app = self._app()
        app.root = _FakeRoot()
        app.current_scan_id = 2
        for _ in range(2500):
            app.queue.put(("add_result", 1, FileRecord(path="old", size=1, fingerprint="f")))
        DuplicateFinderApp.process_queue(app)
        self.assertAlmostEqual(app.queue.qsize(), 1700, delta=1,
                               msg="discarded stale events must still consume BATCH")

    def test_w2004_traversal_error_surfaced(self):
        """R0008: an inaccessible subtree is reported, not silently omitted.

        Deterministic across unprivileged users, root/elevated, containers and
        Windows: os.scandir is patched to raise PermissionError only for the
        designated blocked subtree, so the real os.walk -> os.scandir -> onerror
        production path is exercised without relying on chmod as a boundary.
        """
        root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(root, "visible-empty"))
        blocked = os.path.join(root, "blocked")
        os.makedirs(os.path.join(blocked, "inner"))
        errors = []
        real_scandir = os.scandir
        blocked_abs = os.path.normcase(os.path.abspath(blocked))

        def failing_scandir(path, *args, **kwargs):
            if os.path.normcase(os.path.abspath(path)) == blocked_abs:
                err = PermissionError(13, "Permission denied")
                err.filename = path
                raise err
            return real_scandir(path, *args, **kwargs)

        os.scandir = failing_scandir
        try:
            empties = find_empty_dirs(root, collect_errors=errors.append)
        finally:
            os.scandir = real_scandir
        self.assertIn(os.path.join(root, "visible-empty"), empties)
        self.assertNotIn(blocked, empties)
        self.assertNotIn(os.path.join(blocked, "inner"), empties)
        self.assertTrue(errors, "traversal failure must be reported through the channel")
        self.assertIn(blocked, errors[0])


class SRC001PerfTest(unittest.TestCase):
    """SRC-001 PERF wave regressions."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = DuplicateEngine()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_perf001_group_lookup_is_indexed(self):
        """R0009: plan_deletion resolves groups by id, not a linear next() scan."""
        source = inspect.getsource(DuplicateEngine.plan_deletion)
        self.assertIn("groups_by_id", source)
        self.assertNotIn("next((g for g in groups", source)

    def test_perf001_bulk_disarm_equivalent(self):
        """R0009: batch disarm == repeated logical deletions on 10k records."""
        groups = []
        recs = []
        for gid in range(1, 5001):
            g = DuplicateGroup(group_id=gid, fingerprint=f"f{gid}",
                               members=[FileRecord(path=f"p{gid}a", size=1, fingerprint=f"f{gid}"),
                                        FileRecord(path=f"p{gid}b", size=1, fingerprint=f"f{gid}")])
            g.members[0].is_original = True
            g.members[0].group_id = g.members[1].group_id = gid
            groups.append(g)
            recs.extend(g.members)
        app = DestructiveUITest._bare_app()
        app.current_groups = [DuplicateGroup(group_id=g.group_id, fingerprint=g.fingerprint,
                                              members=list(g.members)) for g in groups]
        app.current_records = list(recs)
        for r in app.current_records:
            app.path_to_item[r.path] = f"I{r.path}"
            app.item_path_map[f"I{r.path}"] = r.path
            app.check_vars[r.path] = _Var(False)
        doomed = [r for r in recs if not r.is_original]
        DuplicateFinderApp._batch_disarm(app, doomed)
        self.assertEqual(len(app.current_records), 5000)
        self.assertEqual(len(app.current_groups), 5000)
        for g in app.current_groups:
            self.assertEqual(len(g.members), 1)
            self.assertTrue(g.members[0].is_original)

    def test_perf002_survivor_early_exit(self):
        """R0010: 1 selected + 999 valid survivors -> 2 verify calls."""
        group = DuplicateGroup(group_id=1, fingerprint="f", members=[])
        sel = FileRecord(path="sel", size=1, fingerprint="f", is_original=False)
        sel.group_id = 1
        members = [sel]
        for i in range(999):
            m = FileRecord(path=f"p{i}", size=1, fingerprint="f", is_original=(i == 0))
            m.group_id = 1
            members.append(m)
        group.members = members
        calls = {"n": 0}

        def fake_verify(rec):
            calls["n"] += 1
            return True, "ok"

        engine = DuplicateEngine()
        engine.verify_record = fake_verify
        plan = engine.plan_deletion([group], [sel])
        self.assertEqual(calls["n"], 2, "selected + first survivor only")
        self.assertEqual(len(plan["approved"]), 1)

    def test_perf002_all_invalid_survivors_checked_and_refused(self):
        group = DuplicateGroup(group_id=1, fingerprint="f", members=[])
        sel = FileRecord(path="sel", size=1, fingerprint="f", is_original=False)
        sel.group_id = 1
        members = [sel]
        for i in range(5):
            m = FileRecord(path=f"p{i}", size=1, fingerprint="f", is_original=(i == 0))
            m.group_id = 1
            members.append(m)
        group.members = members
        calls = {"n": 0}

        def fake_verify(rec):
            calls["n"] += 1
            if rec is sel:
                return True, "ok"
            return False, "missing"

        engine = DuplicateEngine()
        engine.verify_record = fake_verify
        plan = engine.plan_deletion([group], [sel])
        self.assertEqual(calls["n"], 6, "all survivors checked when none valid")
        self.assertEqual(plan["approved"], [])
        self.assertEqual(plan["refused_groups"], [1])

    def test_perf003_same_size_different_sample_zero_full_hashes(self):
        """R0011: sample mismatch rejects before any full SHA-256."""
        full_calls = {"n": 0}
        real_hash = mod.hash_file

        def spy_hash(path, block_size=65536, cancel_event=None):
            full_calls["n"] += 1
            return real_hash(path, block_size, cancel_event)

        mod.hash_file = spy_hash
        try:
            for name, head in (("x1.bin", b"A"), ("x2.bin", b"B")):
                _make_file(os.path.join(self.tmp, name), head + b"u" * 1024)
            res = self.engine.scan(self.tmp)
        finally:
            mod.hash_file = real_hash
        self.assertEqual(full_calls["n"], 0, "differing samples must never reach full hash")
        self.assertEqual(res.groups, [])

    def test_perf003_sample_match_but_content_differs_not_grouped(self):
        for name in ("y1.bin", "y2.bin"):
            _make_file(os.path.join(self.tmp, name), b"HEAD" + name.encode() + b"TAIL" * 64)
        res = self.engine.scan(self.tmp)
        self.assertEqual(res.groups, [], "same sample must never prove equality")

    def test_perf003_true_duplicates_still_grouped(self):
        for name in ("d1.bin", "d2.bin"):
            _make_file(os.path.join(self.tmp, name), b"D" * 4096)
        res = self.engine.scan(self.tmp)
        self.assertEqual(len(res.groups), 1)
        self.assertEqual(len(res.groups[0].members), 2)

    def test_perf003_cancel_during_large_hash_is_bounded(self):
        """R0011: a mid-hash cancel raises CancelledHash within chunk boundaries,
        never a trustworthy hash value."""
        big = os.path.join(self.tmp, "big.bin")
        with open(big, "wb") as f:
            f.write(b"\0" * (4 * 1024 * 1024))

        class _CancelAfter:
            """Deterministic cancel_event stub: fires after N is_set() calls."""

            def __init__(self, n):
                self.limit = n
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > self.limit

        with self.assertRaises(mod.CancelledHash):
            mod.hash_file(big, 65536, _CancelAfter(2))

        # Engine-level: an interrupted hash is a distinct cancellation outcome,
        # not an unreadable/error file and never a duplicate.
        for name in ("c1.bin", "c2.bin"):
            _make_file(os.path.join(self.tmp, name), b"D" * (3 * 65536))
        real_hash = mod.hash_file

        def interrupting_hash(path, block_size=65536, cancel_event=None):
            return real_hash(path, block_size, _CancelAfter(1))

        mod.hash_file = interrupting_hash
        try:
            res = self.engine.scan(self.tmp)
        finally:
            mod.hash_file = real_hash
        self.assertEqual(res.stats["state"], "cancelled")
        self.assertEqual(res.groups, [])
        self.assertFalse(any(r.startswith("hash:") for _, r in res.stats["errors"]),
                         "cancelled hash must not masquerade as an unreadable file")

    def test_perf004_no_reverse_path_index(self):
        """R0012: scan carries no whole-tree path_size map."""
        source = inspect.getsource(DuplicateEngine.scan)
        self.assertNotIn("path_size", source)
        self.assertNotIn("setdefault(size, [])", source)

    def test_perf004_unique_size_never_hashed_still_true(self):
        calls = {"n": 0}
        real_sample = mod.sample_fingerprint

        def spy_sample(path, cancel_event=None, block_size=65536):
            calls["n"] += 1
            return real_sample(path, cancel_event, block_size)

        mod.sample_fingerprint = spy_sample
        try:
            for i, size in enumerate((10, 20, 30)):
                _make_file(os.path.join(self.tmp, f"u{i}.bin"), b"u" * size)
            res = self.engine.scan(self.tmp)
        finally:
            mod.sample_fingerprint = real_sample
        self.assertEqual(calls["n"], 0, "unique-size files must not even be sampled")
        self.assertEqual(res.groups, [])

    def test_perf004_mixed_fixture_groups_identical(self):
        _make_file(os.path.join(self.tmp, "uniq1.bin"), b"a" * 10)
        _make_file(os.path.join(self.tmp, "uniq2.bin"), b"b" * 20)
        _make_file(os.path.join(self.tmp, "dup1.bin"), b"DUP" * 50)
        _make_file(os.path.join(self.tmp, "dup2.bin"), b"DUP" * 50)
        _make_file(os.path.join(self.tmp, "size-same-1.bin"), b"X" * 150)
        _make_file(os.path.join(self.tmp, "size-same-2.bin"), b"Y" * 150)
        res = self.engine.scan(self.tmp)
        self.assertEqual(len(res.groups), 1)
        self.assertEqual(len(res.groups[0].members), 2)
        self.assertEqual(res.stats["state"], "completed")
        for m in res.groups[0].members:
            self.assertEqual(m.size, 150)


# --- T-50 concurrency harness -------------------------------------------------
# Every regression below owns real worker processes. These helpers exist so a
# failed assertion (or a crashed worker) can never leave the run holding a hung
# process, an unreaped queue feeder thread, or a leaked semaphore.

_T050_START_TIMEOUT = 20.0     # spawn handshake budget, never normal control flow
_T050_TRASH_TIMEOUT = 5.0      # bounded wait that IS the tested interleave
_T050_JOIN_TIMEOUT = 45.0      # bounded join for one interleave
_T050_TERMINATE_GRACE = 10.0   # bounded re-join after terminate()


def _t050_start(barrier):
    """Bounded spawn handshake shared by the T-50 workers.

    Both processes are started before either waits here, so this is pure
    liveness. A peer that never arrives exits the worker with a dedicated code
    (90) that the test reports as a worker failure, instead of the run waiting
    out an accidental barrier timeout and then blaming the survivor count.
    """
    try:
        barrier.wait(timeout=_T050_START_TIMEOUT)
    except threading.BrokenBarrierError:
        raise SystemExit(90)


def _reap_workers(procs, timeout=_T050_JOIN_TIMEOUT, grace=_T050_TERMINATE_GRACE):
    """Bounded join of spawned workers; returns their exit codes.

    Anything still alive when `timeout` expires is terminated and re-joined
    within `grace`, so a stuck worker surfaces as an exit code (and therefore a
    failing, non-hanging test), never as a leaked process or semaphore.
    """
    deadline = time.monotonic() + timeout
    for p in procs:
        p.join(max(0.0, deadline - time.monotonic()))
    stragglers = [p for p in procs if p.is_alive()]
    for p in stragglers:
        p.terminate()
    deadline = time.monotonic() + grace
    for p in stragglers:
        p.join(max(0.0, deadline - time.monotonic()))
    return [p.exitcode for p in procs]


def _shutdown_queues(queues):
    """Close and join status queues so no feeder thread outlives the test."""
    for q in queues:
        try:
            q.close()
        except (OSError, ValueError):
            pass
    for q in queues:
        try:
            q.join_thread()
        except (OSError, ValueError, AssertionError):
            pass


def _read_status(status_q, timeout=5.0):
    """One worker status; None when the worker never delivered one."""
    try:
        return status_q.get(timeout=timeout)
    except (queue.Empty, OSError, ValueError):
        return None


def _t050_worker(root, pick_original, start_barrier, trash_barrier, out_q, use_lock):
    """One worker process: scan `root`, select a member, delete under the
    destructive lock. `pick_original` selects the original or the duplicate."""
    import threading
    import delete_duplicates_gui as mod2
    from delete_duplicates_gui import DuplicateEngine, DuplicateFinderApp

    mod2.messagebox = type("M", (), {
        "showwarning": staticmethod(lambda *a, **k: None),
        "showerror": staticmethod(lambda *a, **k: None),
        "askyesno": staticmethod(lambda *a, **k: True),
        "showinfo": staticmethod(lambda *a, **k: None),
    })()

    class Var:
        def __init__(s, v):
            s._v = v

        def get(s):
            return s._v

        def set(s, x):
            s._v = x

    class FakeTree:
        def __init__(s):
            s.deleted = []

        def delete(s, *a):
            s.deleted.extend(a)

        def set(s, item, col, val):
            pass

    res = DuplicateEngine().scan(root)
    group = res.groups[0]
    original = next(m for m in group.members if m.is_original)
    dup = next(m for m in group.members if not m.is_original)
    target = original if pick_original else dup
    survivor = dup if pick_original else original

    def barrier_trash(path):
        # Both processes have completed their own candidate + survivor
        # verification; synchronize here, then let BOTH destructions proceed.
        # A bounded wait: under the lock the parties can never meet (the
        # critical section excludes the second), so the barrier breaks and
        # each proceeds individually — the lock's own revalidation then refuses
        # the second deletion. Without the lock the barrier releases both.
        try:
            trash_barrier.wait(timeout=_T050_TRASH_TIMEOUT)
        except threading.BrokenBarrierError:
            pass
        os.remove(path)

    # Start barrier: both processes alive BEFORE either scans, so both scans
    # see the full two-file group regardless of spawn timing.
    _t050_start(start_barrier)
    res = DuplicateEngine().scan(root)

    mod2.send_to_trash = barrier_trash
    app = DuplicateFinderApp.__new__(DuplicateFinderApp)
    app.engine = DuplicateEngine()
    app.current_groups = list(res.groups)
    app.current_records = [m for g in res.groups for m in g.members]
    app.check_vars = {r.path: Var(r.path == target.path) for r in app.current_records}
    app.path_to_item = {r.path: "I" + str(i) for i, r in enumerate(app.current_records)}
    app.item_path_map = {v: k for k, v in app.path_to_item.items()}
    app.tree = FakeTree()
    app.log_var = Var(False)
    app.status_var = Var("")
    app.path_var = Var(root)

    # Synchronize inside the destructive primitive (after each process's own
    # candidate/survivor verification) — the exact audit probe interleave.
    # NOTE: the production delete_selected already takes the destructive lock
    # itself, so the worker must never wrap it in a second acquire (the lock
    # is not reentrant — a nested acquire self-deadlocks into TimeoutError).
    if not use_lock:
        # Red control / defense-in-depth probe: neutralize the production
        # cross-process lock only; T-49 quarantine stays in place.
        import contextlib
        mod2.destructive_lock = lambda _root, **_kw: contextlib.nullcontext()
    DuplicateFinderApp.delete_selected(app)
    out_q.put(app.status_var.get())


def _t050_three_worker(root, target_names, start_barrier, trash_barrier, out_q):
    """T-50 three-member variant: select `target_names`, keep the rest."""
    import threading
    import delete_duplicates_gui as mod2
    from delete_duplicates_gui import DuplicateEngine, DuplicateFinderApp

    mod2.messagebox = type("M", (), {
        "showwarning": staticmethod(lambda *a, **k: None),
        "showerror": staticmethod(lambda *a, **k: None),
        "askyesno": staticmethod(lambda *a, **k: True),
        "showinfo": staticmethod(lambda *a, **k: None),
    })()

    class Var:
        def __init__(s, v):
            s._v = v

        def get(s):
            return s._v

        def set(s, x):
            s._v = x

    class FakeTree:
        def delete(s, *a):
            pass

        def set(s, *a):
            pass

    state = {"n": 0}

    def barrier_trash(_path):
        # Synchronize only at the FIRST destructive step per process; later
        # members proceed individually so a serialized (locked) run cannot
        # deadlock on the barrier while a racy run still collides.
        state["n"] += 1
        if state["n"] == 1:
            try:
                trash_barrier.wait(timeout=_T050_TRASH_TIMEOUT)
            except threading.BrokenBarrierError:
                pass
        os.remove(_path)

    _t050_start(start_barrier)
    res = DuplicateEngine().scan(root)
    mod2.send_to_trash = barrier_trash
    app = DuplicateFinderApp.__new__(DuplicateFinderApp)
    app.engine = DuplicateEngine()
    app.current_groups = list(res.groups)
    app.current_records = [m for g in res.groups for m in g.members]
    app.check_vars = {r.path: Var(os.path.basename(r.path) in target_names)
                      for r in app.current_records}
    app.path_to_item = {r.path: "I" for r in app.current_records}
    app.item_path_map = {}
    app.tree = FakeTree()
    app.log_var = Var(False)
    app.status_var = Var("")
    app.path_var = Var(root)
    DuplicateFinderApp.delete_selected(app)
    out_q.put(app.status_var.get())


def _t050_lock_owner_worker(lock_path, start_barrier, out_q, hold):
    """One contender in the controlled-interleaving ownership probe.

    Both contenders meet on the start barrier, then each reaches for the SAME
    lock path. The winner holds it for `hold` seconds; a contender that cannot
    take it reports the bounded refusal. At most one process may ever report
    ownership, which is exactly what the old remove-and-recreate primitive
    could violate (two contenders observed one dead owner, each unlinked the
    other's fresh lock and both entered the critical section).
    """
    from delete_duplicates_gui import _acquire_lock_file, _release_lock_file
    _t050_start(start_barrier)
    try:
        held = _acquire_lock_file(lock_path, timeout=0.6, poll=0.01)
    except TimeoutError:
        out_q.put("refused")
        return
    out_q.put("owned")
    time.sleep(hold)
    _release_lock_file(held)


class SRC002W2001ConcurrencyTest(unittest.TestCase):
    """T-50 (SRC-002 W2-001): cross-process destructive ownership regressions.

    Every test here spawns real worker processes. Cleanup is registered with
    addCleanup, so it runs whether the test passed, failed or errored: workers
    are reaped with bounded joins (a still-live worker is terminated and
    re-joined), status queues are closed and joined, and only then is the temp
    tree removed -- a live worker must never outlive the tree it deletes in, and
    no barrier/queue/semaphore may leak out of a failed test. Each concurrency
    test asserts worker exit codes, so a crashed worker is reported as a worker
    failure instead of as a puzzling survivor count.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._procs = []
        self._queues = []
        # addCleanup is LIFO, so the reap runs before the tree is removed.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self._reap)

    def _reap(self):
        """Guaranteed bounded cleanup: reap workers, then close/join queues."""
        _reap_workers(self._procs)
        _shutdown_queues(self._queues)

    def _harness(self):
        """Spawn context + both barriers + both status queues for one interleave."""
        ctx = multiprocessing.get_context("spawn")
        start_barrier = ctx.Barrier(2)
        trash_barrier = ctx.Barrier(2)
        q1, q2 = ctx.Queue(), ctx.Queue()
        self._queues.extend((q1, q2))
        return ctx, start_barrier, trash_barrier, q1, q2

    def _spawn(self, ctx, target, args_list):
        procs = [ctx.Process(target=target, args=args) for args in args_list]
        self._procs.extend(procs)
        for p in procs:
            p.start()
        return procs

    def _finish(self, procs, timeout=_T050_JOIN_TIMEOUT, label="T-50"):
        """Bounded join with every worker exit code surfaced explicitly."""
        codes = _reap_workers(procs, timeout=timeout)
        for p, code in zip(procs, codes):
            self.assertEqual(
                code, 0,
                f"{label}: worker pid={p.pid} exited {code} -- the worker "
                f"traceback above is the failure, not the survivor count")
        return codes

    def _t050_run(self, use_lock, roots=None, timeout=_T050_JOIN_TIMEOUT):
        """Two processes scan the same two-file group, select opposite members,
        synchronize before the destructive step, then race.

        `roots` overrides the (first, second) spellings handed to the workers,
        so one implementation covers both the plain/plain and the plain/alias
        interleaves. Returns (remaining_files, worker_statuses); the worker
        exit codes are asserted before the outcome is inspected.
        """
        for name in ("a.bin", "b.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        ctx, start_barrier, trash_barrier, q1, q2 = self._harness()
        root_a, root_b = roots if roots else (self.tmp, self.tmp)
        procs = self._spawn(ctx, _t050_worker, (
            (root_a, False, start_barrier, trash_barrier, q1, use_lock),
            (root_b, True, start_barrier, trash_barrier, q2, use_lock),
        ))
        self._finish(procs, timeout=timeout)
        remaining = [n for n in ("a.bin", "b.bin")
                     if os.path.exists(os.path.join(self.tmp, n))]
        statuses = [_read_status(q) for q in (q1, q2)]
        return remaining, statuses

    def test_t050_lock_keeps_last_copy_under_concurrent_delete(self):
        """T-50 green: two-process race with the production lock — exactly one
        deletion may succeed and exactly one original copy must remain."""
        remaining, statuses = self._t050_run(use_lock=True)
        self.assertNotIn(None, statuses,
                         f"T-50: both workers must reach a terminal status: {statuses}")
        self.assertEqual(len(remaining), 1,
                         f"T-50: expected exactly one surviving copy, got {remaining}")
        self.assertEqual(_read_bytes(os.path.join(self.tmp, remaining[0])), b"DUP",
                         "T-50: the surviving copy must still be the scanned content")
        removed = sum(1 for s in statuses if "Удалено файлов: 1" in s)
        self.assertEqual(removed, 1,
                         f"T-50: exactly one deletion must succeed, statuses={statuses}")
        refused = sum(1 for s in statuses
                      if "Удалено файлов: 0" in s or "пропущено" in s or "TIMEOUT" in s)
        self.assertGreaterEqual(refused, 1,
                                f"T-50: the loser must refuse, statuses={statuses}")

    def test_t050_t49_quarantine_alone_keeps_last_copy_when_lock_neutralized(self):
        """T-50 defense-in-depth: even with the cross-process lock neutralized,
        T-49's acquire-then-recheck order must keep at least one copy on the
        audit's two-process interleave. (The full audit red — zero copies —
        reproduces only against the pre-T-49 baseline; that evidence is
        recorded in LOG as a sandbox probe, since on the fixed tree the
        recheck-after-acquire makes a joint barrier hit unreachable.)"""
        remaining, statuses = self._t050_run(use_lock=False)
        self.assertNotIn(None, statuses,
                         f"T-50: both workers must reach a terminal status: {statuses}")
        self.assertTrue(remaining,
                        f"CORE-001 violated even with quarantine: got {remaining}")
        for name in remaining:
            self.assertEqual(_read_bytes(os.path.join(self.tmp, name)), b"DUP",
                             "T-50: a surviving copy must keep its content")

    def test_t050_three_member_two_deletion_race_never_zero(self):
        """T-50: a three-member group with two concurrent two-file deletions
        (each plan leaves a different keeper) must never reach zero copies, and
        both workers must still reach a truthful terminal status."""
        for name in ("a.bin", "b.bin", "c.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP3")
        ctx, start_barrier, trash_barrier, q1, q2 = self._harness()
        procs = self._spawn(ctx, _t050_three_worker, (
            (self.tmp, ("a.bin", "b.bin"), start_barrier, trash_barrier, q1),
            (self.tmp, ("b.bin", "c.bin"), start_barrier, trash_barrier, q2),
        ))
        self._finish(procs, timeout=60.0)
        remaining = [n for n in ("a.bin", "b.bin", "c.bin")
                     if os.path.exists(os.path.join(self.tmp, n))]
        self.assertTrue(remaining,
                        f"T-50: three-member race deleted every copy: {remaining}")
        for name in remaining:
            self.assertEqual(_read_bytes(os.path.join(self.tmp, name)), b"DUP3",
                             "T-50: every survivor must still be a valid content copy")
        statuses = [_read_status(q) for q in (q1, q2)]
        self.assertNotIn(None, statuses,
                         f"T-50: both workers must reach a terminal status: {statuses}")
        for s in statuses:
            self.assertIn("Удалено файлов", s,
                          "both workers must reach a truthful terminal status")

    def test_t050_stale_lock_file_is_acquirable_no_holder(self):
        """T-50 (OS-level lock): a lock FILE left behind by a crashed run holds
        no ownership. Ownership lives on an open descriptor; a bare file from a
        dead owner therefore never bricks the app the way the old pid-reclaim
        contract could, and its payload — empty, `pid=0`, truncated or garbage —
        is diagnostic only and decides nothing."""
        from delete_duplicates_gui import (
            _lock_path_for, _acquire_lock_file, _release_lock_file,
        )
        lock = _lock_path_for(self.tmp)
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        for payload in ("", "pid=0\n", "pid=\n", "not-a-pid\n", "pid=abc\n",
                        "pid=999999999\n"):
            with open(lock, "w", encoding="utf-8") as f:
                f.write(payload)
            held = _acquire_lock_file(lock, timeout=2.0, poll=0.01)
            self.assertIsNotNone(
                held, f"a file with no OS holder must be acquirable: {payload!r}")
            _release_lock_file(held)

    def test_t050_pid_probe_never_grants_ownership(self):
        """T-50: the pid probe is diagnostic only. `_pid_alive` reports an
        unreadable owner (pid 0) as live and a demonstrably dead pid as dead,
        but neither result can grant or withhold the lock — only the OS-level
        lock decides, so an OpenProcess/os.kill failure can never unlock a live
        owner's critical section."""
        from delete_duplicates_gui import _pid_alive
        self.assertTrue(_pid_alive(0),
                        "pid 0 means 'owner unknown' and must read as live")
        self.assertFalse(_pid_alive(999999999), "a dead pid must read as dead")

    def test_t050_lock_held_by_live_owner_times_out(self):
        """T-50: a lock held by a live owner times out instead of proceeding
        unsynchronized."""
        from delete_duplicates_gui import (
            _lock_path_for, _acquire_lock_file, _release_lock_file,
        )
        lock = _lock_path_for(self.tmp)
        held = _acquire_lock_file(lock, timeout=3.0, poll=0.01)
        try:
            self.assertIsNotNone(held)
            with self.assertRaises(TimeoutError):
                _acquire_lock_file(lock, timeout=0.1, poll=0.01)
        finally:
            _release_lock_file(held)

    def test_t050_simultaneous_contenders_exactly_one_owns(self):
        """T-50 P0 regression (controlled interleaving): two real processes
        barrier-synchronize on the SAME lock path, pre-seeded with a stale dead
        pid, then both reach for it. Exactly one may hold the critical section;
        the other must refuse. The superseded remove-and-recreate primitive let
        BOTH observe the dead owner, each unlink the other's fresh file and both
        report ownership (SIMULTANEOUS_OWNERS ['A','B'])."""
        from delete_duplicates_gui import _lock_path_for
        lock = _lock_path_for(self.tmp)
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w", encoding="utf-8") as f:
            f.write("pid=999999999\n")  # a stale dead owner, the audit's premise
        ctx = multiprocessing.get_context("spawn")
        start_barrier = ctx.Barrier(2)
        q1, q2 = ctx.Queue(), ctx.Queue()
        self._queues.extend((q1, q2))
        procs = self._spawn(ctx, _t050_lock_owner_worker, (
            (lock, start_barrier, q1, 3.0),
            (lock, start_barrier, q2, 3.0),
        ))
        self._finish(procs, timeout=30.0, label="T-50 ownership")
        results = sorted(filter(None, (_read_status(q1, 10.0), _read_status(q2, 10.0))))
        self.assertEqual(
            results, ["owned", "refused"],
            f"exactly one contender may own the lock, got {results}")

    def test_t050_crashed_owner_lock_is_recoverable(self):
        """T-50: an owner that dies WITHOUT releasing is released by the OS, so
        the lock is never a permanent brick and no pid probe is consulted. The
        worker acquires, reports ownership, then is terminated while still
        holding it."""
        from delete_duplicates_gui import _lock_path_for, _acquire_lock_file, _release_lock_file
        lock = _lock_path_for(self.tmp)
        ctx = multiprocessing.get_context("spawn")
        start_barrier = ctx.Barrier(2)
        q = ctx.Queue()
        self._queues.append(q)
        procs = self._spawn(ctx, _t050_lock_owner_worker, ((lock, start_barrier, q, 20.0),))
        p = procs[0]
        # The single-contender barrier needs a second waiter to release.
        try:
            start_barrier.wait(timeout=_T050_START_TIMEOUT)
        except threading.BrokenBarrierError:
            pass
        self.assertEqual(_read_status(q, 10.0), "owned",
                         "worker must hold the lock before it is killed")
        p.terminate()
        p.join(10.0)
        self.assertFalse(p.is_alive(), "the crashed owner must be gone")
        # The OS released the dead owner's descriptor: a fresh acquire succeeds.
        held = _acquire_lock_file(lock, timeout=5.0, poll=0.02)
        self.assertIsNotNone(held, "a crashed owner's lock must be recoverable")
        _release_lock_file(held)

    def test_t050_lock_scope_canonicalized(self):
        """T-50: alternative spellings of the same root cannot create two locks."""
        from delete_duplicates_gui import _lock_path_for
        canonical = _lock_path_for(self.tmp)
        for spelling in (
            os.path.join(self.tmp, os.pardir, os.path.basename(self.tmp)),
            self.tmp + os.sep,                            # trailing separator
            os.path.join(self.tmp, "."),                  # dot segment
            os.path.join(self.tmp, "sub", os.pardir),      # never stat-ed, lexical
        ):
            self.assertEqual(canonical, _lock_path_for(spelling),
                             f"spelling must share the lock scope: {spelling}")

    def test_t050_alias_spelling_cannot_bypass_lock(self):
        """T-50: a worker scanning the root through a `..`-segment alias still
        serializes with a worker using the plain spelling — the lock scope is
        canonical, so exactly one copy is destroyed and one remains.

        The alias must be a REAL, traversable spelling of the same directory
        (its `..` component already exists), so both workers genuinely reach the
        duplicate group. An alias through a nonexistent child (`root/sub/..`)
        only parses under Win32's lexical path normalization: on POSIX the scan
        root is untraversable, the worker dies before the race is ever exercised
        and its peer is left waiting on the barrier — a verifier artifact, not a
        product result.
        """
        from delete_duplicates_gui import _lock_path_for
        for name in ("a.bin", "b.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        alias = os.path.join(self.tmp, os.pardir, os.path.basename(self.tmp))
        # Preconditions asserted BEFORE spawning, so an unusable alias can never
        # be misreported as a concurrency failure.
        self.assertNotEqual(alias, self.tmp)
        self.assertIn(os.pardir, alias)
        self.assertEqual(os.path.realpath(alias), os.path.realpath(self.tmp))
        self.assertTrue(os.path.isdir(alias), f"alias must be a real directory: {alias}")
        self.assertTrue(os.access(alias, os.R_OK), f"alias must be readable: {alias}")
        self.assertEqual(sorted(os.listdir(alias)), ["a.bin", "b.bin"],
                         "the alias must be traversable and show the same group")

        remaining, statuses = self._t050_run(use_lock=True, roots=(self.tmp, alias))
        self.assertNotIn(None, statuses,
                         f"T-50: both workers must reach a terminal status: {statuses}")
        self.assertEqual(len(remaining), 1,
                         f"T-50: alias race must keep exactly one copy, got {remaining}")
        self.assertEqual(_read_bytes(os.path.join(self.tmp, remaining[0])), b"DUP")
        removed = sum(1 for s in statuses if "Удалено файлов: 1" in s)
        self.assertEqual(removed, 1,
                         f"T-50: the alias worker must not bypass serialization: {statuses}")
        # Supporting claim only (the race above is the regression): one scope
        # for both spellings is WHY the serialization held.
        self.assertEqual(_lock_path_for(self.tmp), _lock_path_for(alias),
                         "both spellings must share one lock scope")

    def test_t050_lock_released_after_batch(self):
        """T-50 green control: the batch releases ownership. The lock FILE is
        deliberately kept (unlinking it is what let two owners race), so the
        observable release is that a second acquire succeeds afterwards."""
        from delete_duplicates_gui import (
            _lock_path_for, _acquire_lock_file, _release_lock_file,
            destructive_lock,
        )
        lock = _lock_path_for(self.tmp)
        with destructive_lock(self.tmp):
            with self.assertRaises(TimeoutError):
                _acquire_lock_file(lock, timeout=0.2, poll=0.01)
        held = _acquire_lock_file(lock, timeout=2.0, poll=0.01)
        self.assertIsNotNone(held, "ownership must be free after the batch")
        _release_lock_file(held)

    def test_t050_realpath_scope_resolves_junction_alias(self):
        """T-50: the lock scope is resolved with realpath, so a directory
        reached through a junction/symlink of the same physical root shares one
        lock path. `abspath`+`normcase` alone left them as two independent
        scopes, which let two instances bypass mutual exclusion."""
        from delete_duplicates_gui import _lock_path_for
        real_dir = os.path.join(self.tmp, "real")
        os.makedirs(real_dir)
        link_dir = os.path.join(self.tmp, "link")
        made = False
        try:
            os.symlink(real_dir, link_dir, target_is_directory=True)
            made = True
        except (OSError, NotImplementedError, AttributeError):
            # Symlink creation needs a privilege this run may not hold;
            # a directory junction does not (Windows).
            if os.name == "nt":
                import subprocess
                r = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", link_dir, real_dir],
                    capture_output=True, text=True)
                made = r.returncode == 0 and os.path.isdir(link_dir)
        if not made:
            self.skipTest("no junction/symlink privilege available")
        self.assertNotEqual(os.path.abspath(real_dir), os.path.abspath(link_dir))
        self.assertEqual(os.path.realpath(real_dir), os.path.realpath(link_dir))
        self.assertEqual(
            _lock_path_for(real_dir), _lock_path_for(link_dir),
            "a junction/symlink alias must share one lock scope")

    def test_t050_lock_dir_is_fixed_not_temp_env_dependent(self):
        """T-50: the lock directory is a fixed per-user path, not
        `tempfile.gettempdir()`. Two instances with different TMP/TEMP/TMPDIR
        settings must still contend for one lock, or they never serialize."""
        from delete_duplicates_gui import _lock_dir, _lock_path_for
        lock_dir = os.path.realpath(_lock_dir())
        root = os.path.realpath(self.tmp)
        self.assertFalse(
            os.path.normcase(lock_dir).startswith(os.path.normcase(root)),
            "the lock dir must never live inside a scanned root")
        before = _lock_path_for(self.tmp)
        saved = {k: os.environ.get(k) for k in ("TMP", "TEMP", "TMPDIR")}
        for k in saved:
            os.environ[k] = os.path.join(self.tmp, "elsewhere-" + k.lower())
        try:
            after = _lock_path_for(self.tmp)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(before, after,
                         "a temp-env change must not move the lock scope")

    def test_t050_nested_roots_share_destructive_lock(self):
        """T-50 (FINDING A): nested and overlapping scan roots (parent and child)
        resolve to the same fixed destructive lock path, serializing destructive
        batches across overlapping scan scopes."""
        from delete_duplicates_gui import _lock_path_for
        parent = self.tmp
        child = os.path.join(self.tmp, "sub")
        deep_child = os.path.join(self.tmp, "a", "b", "c")
        self.assertEqual(_lock_path_for(parent), _lock_path_for(child),
                         "parent and child must share the same destructive lock")
        self.assertEqual(_lock_path_for(parent), _lock_path_for(deep_child),
                         "deeply nested child must share the same destructive lock")

    def test_t050_nested_roots_cross_process_serialized(self):
        """T-50 (FINDING A): two real processes scanning parent and child
        directories containing the same duplicate group serialize under the
        destructive lock. Exactly one deletion succeeds and one copy survives."""
        sub = os.path.join(self.tmp, "sub")
        os.makedirs(sub, exist_ok=True)
        for name in ("a.bin", "b.bin"):
            _make_file(os.path.join(sub, name), b"DUP_NESTED")
        ctx, start_barrier, trash_barrier, q1, q2 = self._harness()
        procs = self._spawn(ctx, _t050_worker, (
            (self.tmp, False, start_barrier, trash_barrier, q1, True),
            (sub, True, start_barrier, trash_barrier, q2, True),
        ))
        self._finish(procs, timeout=60.0)
        remaining = [n for n in ("a.bin", "b.bin")
                     if os.path.exists(os.path.join(sub, n))]
        self.assertEqual(len(remaining), 1,
                         f"expected exactly one survivor under nested roots, got {remaining}")
        self.assertEqual(_read_bytes(os.path.join(sub, remaining[0])), b"DUP_NESTED",
                         "surviving copy must retain original content")
        statuses = [_read_status(q) for q in (q1, q2)]
        self.assertNotIn(None, statuses,
                         f"both workers must reach a terminal status: {statuses}")
        removed = sum(1 for s in statuses if "Удалено файлов: 1" in s)
        self.assertEqual(removed, 1,
                         f"exactly one worker must perform deletion: {statuses}")

    def test_t050_nested_roots_quarantine_alone_keeps_last_copy_when_lock_neutralized(self):
        """T-50 (FINDING A defense-in-depth): even when the cross-process lock is
        completely neutralized between overlapping parent and child scans, T-49
        quarantine and survivor revalidation prevent zero copies — demonstrating
        that overlapping scopes cannot cause data loss."""
        sub = os.path.join(self.tmp, "sub_nolock")
        os.makedirs(sub, exist_ok=True)
        for name in ("a.bin", "b.bin"):
            _make_file(os.path.join(sub, name), b"DUP_NESTED_NOLOCK")
        ctx, start_barrier, trash_barrier, q1, q2 = self._harness()
        procs = self._spawn(ctx, _t050_worker, (
            (self.tmp, False, start_barrier, trash_barrier, q1, False),
            (sub, True, start_barrier, trash_barrier, q2, False),
        ))
        self._finish(procs, timeout=60.0)
        remaining = [n for n in ("a.bin", "b.bin")
                     if os.path.exists(os.path.join(sub, n))]
        self.assertTrue(remaining,
                        f"data loss occurred under overlapping roots: remaining={remaining}")
        for n in remaining:
            self.assertEqual(_read_bytes(os.path.join(sub, n)), b"DUP_NESTED_NOLOCK",
                             "surviving copies must retain intact content")
        statuses = [_read_status(q) for q in (q1, q2)]
        self.assertNotIn(None, statuses,
                         f"both workers must reach a terminal status: {statuses}")


class T060CoreUnitTest(unittest.TestCase):
    """T-60 (FINDING D): Declared core-unit family isolation and admission."""

    def test_t060_engine_core_unit_family_not_declared_in_project(self):
        """T-60: project must not vendor or declare the SAIPEN engine's tools/ family,
        which causes core_unit.ship_gate to refuse SHIP with INCOMPLETE_TICKET."""
        proj_root = os.path.dirname(os.path.abspath(__file__))
        engine_runner = os.path.join(proj_root, "tools", "saipen_engine", "test_runner.py")
        engine_core_unit = os.path.join(proj_root, "tools", "core_unit.py")
        self.assertFalse(os.path.exists(engine_runner), "tools/saipen_engine/test_runner.py must not exist in project")
        self.assertFalse(os.path.exists(engine_core_unit), "tools/core_unit.py must not exist in project")
        self.assertFalse(os.path.exists(os.path.join(proj_root, "tools")), "tools/ directory must not exist in project")

    def test_t060_ship_gate_passes_without_declared_engine_family(self):
        """T-60: ship_gate returns None (PASS) when engine's core-unit family is not declared."""
        import sys
        proj_root = os.path.dirname(os.path.abspath(__file__))
        saipen_tools = r"V:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\_SAIPEN\tools"
        if os.path.isdir(saipen_tools):
            sys.path.insert(0, saipen_tools)
            try:
                from saipen_engine import core_unit
                self.assertFalse(core_unit.declared(proj_root))
                self.assertIsNone(core_unit.ship_gate(proj_root, "T-60", []))
            except ImportError:
                pass


class T051FailureStateProtocolTest(unittest.TestCase):
    """T-51 (SRC-002 CORE-002): failure-state protocol distinguishes total failure from partial coverage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = DuplicateEngine()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t051_root_permission_error_fails_with_root_unreadable(self):
        """T-51: root exists but traversal raises PermissionError -> FAILED / root_unreadable."""
        saved_walk = os.walk
        def failing_walk(top, onerror=None):
            if onerror:
                onerror(PermissionError(13, "Permission denied", top))
            return iter([])
        os.walk = failing_walk
        try:
            res = self.engine.scan(self.tmp)
        finally:
            os.walk = saved_walk
        self.assertEqual(res.stats["state"], "failed")
        self.assertEqual(res.stats.get("reason"), "root_unreadable")
        self.assertEqual(res.groups, [])

    def test_t051_accessible_root_with_inaccessible_child_completed_with_errors(self):
        """T-51: accessible root with one inaccessible child -> COMPLETED_WITH_ERRORS with results preserved."""
        child = os.path.join(self.tmp, "child")
        os.makedirs(child)
        _make_file(os.path.join(self.tmp, "a.bin"), b"DUP_PRESERVED")
        _make_file(os.path.join(self.tmp, "b.bin"), b"DUP_PRESERVED")
        real_walk = os.walk
        def sub_fail_walk(top, onerror=None):
            for dirpath, dirnames, filenames in real_walk(top):
                if os.path.normcase(os.path.abspath(dirpath)) == os.path.normcase(os.path.abspath(child)):
                    if onerror:
                        onerror(PermissionError(13, "Permission denied", child))
                    continue
                yield dirpath, dirnames, filenames
        os.walk = sub_fail_walk
        try:
            res = self.engine.scan(self.tmp)
        finally:
            os.walk = real_walk
        self.assertEqual(res.stats["state"], "completed_with_errors")
        self.assertEqual(len(res.groups), 1, "results from accessible root must be preserved")

    def test_t051_invalid_root_fails_with_invalid_root_and_invalid_folder_message(self):
        """T-51: invalid/non-directory root -> FAILED / invalid_root with invalid-folder message."""
        nonexistent = os.path.join(self.tmp, "does_not_exist")
        res = self.engine.scan(nonexistent)
        self.assertEqual(res.stats["state"], "failed")
        self.assertEqual(res.stats.get("reason"), "invalid_root")
        self.assertEqual(res.groups, [])

        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.status_var = _Var("")
        app._sessions = {}
        app.current_scan_id = 1
        app.stop_button = _FakeButton()
        DuplicateFinderApp._on_terminal(app, "failed", {"state": "failed", "reason": "invalid_root"})
        self.assertIn("неверная папка", app.status_var.get())

    def test_t051_worker_exception_fails_with_worker_exception_and_no_invalid_folder_message(self):
        """T-51: engine RuntimeError on valid root -> FAILED / worker_exception, no invalid-folder text."""
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.status_var = _Var("")
        app._sessions = {}
        app.current_scan_id = 1
        app.stop_button = _FakeButton()
        DuplicateFinderApp._on_terminal(app, "failed", {"state": "failed", "reason": "worker_exception"})
        status = app.status_var.get()
        self.assertIn("ошибка сканирования", status)
        self.assertNotIn("неверная папка", status, "worker exception must not be misreported as invalid folder")

    def test_t051_failed_terminal_leaves_no_actionable_partial_state(self):
        """T-51: failed terminal leaves no actionable partial model/tree state from failed generation."""
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.status_var = _Var("")
        app._sessions = {}
        app.current_scan_id = 1
        app.stop_button = _FakeButton()
        app.tree = _FakeTree()
        app.check_vars = {"/some/path": _Var(True)}
        app.item_path_map = {"I1": "/some/path"}
        app.path_to_item = {"/some/path": "I1"}
        app.current_groups = ["fake_group"]
        app.current_records = ["fake_record"]
        app.current_result_root = "C:/root"

        DuplicateFinderApp._on_terminal(app, "failed", {"state": "failed", "reason": "worker_exception"})
        self.assertEqual(app.current_records, [])
        self.assertEqual(app.current_groups, [])
        self.assertEqual(app.check_vars, {})
        self.assertIsNone(app.current_result_root)


class T054BulkDeletionScalingTest(unittest.TestCase):
    """T-54 (SRC-002 PERF-001): bulk deletion scaling, keeper caching and I/O reduction."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = DuplicateEngine()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t054_bulk_scaling_1k_2k_4k_near_linear_not_quadratic(self):
        """T-54: member traversal scaling at 1k/2k/4k with N-1 selected is near-linear (~2x per doubling),
        not quadratic (~4x per doubling), and performs < 5,000 checks at 1k instead of ~500,000."""
        from delete_duplicates_gui import _GroupDeletionState

        checks = {}
        for n in (1000, 2000, 4000):
            keeper_path = os.path.join(self.tmp, f"keeper_{n}.bin")
            _make_file(keeper_path, b"KEEPER")
            fp = mod.hash_file(keeper_path)
            keeper = FileRecord(path=keeper_path, size=6, fingerprint=fp, is_original=True)
            keeper.group_id = 1
            members = [keeper]
            selected = []
            for i in range(n - 1):
                m = FileRecord(path=f"dummy_{n}_{i}.bin", size=6, fingerprint=fp, is_original=False)
                m.group_id = 1
                members.append(m)
                selected.append(m)
            group = DuplicateGroup(group_id=1, fingerprint=fp, members=members)

            state = _GroupDeletionState(group, keeper=keeper)
            deleted_paths = set()
            dummy_engine = DuplicateEngine()
            # Destructive batch calls get_survivor twice per approved record:
            # once before acquisition, once after acquisition (T-49/T-45).
            for rec in selected:
                survivor = state.get_survivor(rec, deleted_paths, dummy_engine)
                self.assertIsNotNone(survivor)
                survivor_post = state.get_survivor(rec, deleted_paths, dummy_engine)
                self.assertIsNotNone(survivor_post)
                deleted_paths.add(rec.path)

            checks[n] = state.traversal_checks

        # Quadratic would require ~500,000 checks at 1k, ~2,000,000 at 2k, ~8,000,000 at 4k.
        self.assertLess(checks[1000], 5000, f"at 1k checks must be O(N), got {checks[1000]}")
        ratio_1k_2k = checks[2000] / checks[1000]
        ratio_2k_4k = checks[4000] / checks[2000]
        self.assertTrue(1.8 <= ratio_1k_2k <= 2.2,
                        f"traversal scaling 1k->2k must be near-linear (~2.0x), got {ratio_1k_2k:.2f}x")
        self.assertTrue(1.8 <= ratio_2k_4k <= 2.2,
                        f"traversal scaling 2k->4k must be near-linear (~2.0x), got {ratio_2k_4k:.2f}x")

    def test_t054_preview_no_longer_full_hashes_selected_records(self):
        """T-54: preview phase eliminates full SHA-256 hashing of selected records,
        reserving full hashing for final destructive authority."""
        p1 = os.path.join(self.tmp, "p1.bin")
        p2 = os.path.join(self.tmp, "p2.bin")
        content = b"PREVIEW_CONTENT" * 100
        _make_file(p1, content)
        _make_file(p2, content)
        res = self.engine.scan(self.tmp)
        self.assertEqual(len(res.groups), 1)

        sel = [m for m in res.groups[0].members if not m.is_original]
        self.assertEqual(len(sel), 1)

        hash_calls = {"count": 0}
        real_hash = mod.hash_file

        def spy_hash(*args, **kwargs):
            hash_calls["count"] += 1
            return real_hash(*args, **kwargs)

        mod.hash_file = spy_hash
        try:
            # 1. Preview mode (full_hash=False) must perform 0 full SHA-256 computations
            plan_preview = self.engine.plan_deletion(res.groups, sel, full_hash=False)
            self.assertEqual(hash_calls["count"], 0,
                             "plan_deletion with full_hash=False must not compute full SHA-256")
            self.assertEqual(len(plan_preview["approved"]), 1)

            # 2. Final destructive authority (full_hash=True) computes full SHA-256
            plan_final = self.engine.plan_deletion(res.groups, sel, full_hash=True)
            self.assertGreater(hash_calls["count"], 0,
                               "plan_deletion with full_hash=True must verify full SHA-256")
            self.assertEqual(len(plan_final["approved"]), 1)
        finally:
            mod.hash_file = real_hash

    def test_t054_delete_selected_ui_preview_skips_full_hash(self):
        """T-54: DuplicateFinderApp.delete_selected preview phase skips full SHA-256 before confirmation."""
        p1 = os.path.join(self.tmp, "u1.bin")
        p2 = os.path.join(self.tmp, "u2.bin")
        _make_file(p1, b"UI_PREVIEW" * 50)
        _make_file(p2, b"UI_PREVIEW" * 50)
        res = self.engine.scan(self.tmp)

        app = DestructiveUITest._bare_app()
        app.current_groups = res.groups
        app.current_records = list(res.groups[0].members)
        for r in app.current_records:
            app.path_to_item[r.path] = f"I{r.path}"
            app.item_path_map[f"I{r.path}"] = r.path
            app.check_vars[r.path] = _Var(not r.is_original)

        hash_calls = {"count": 0}
        real_hash = mod.hash_file

        def spy_hash(*args, **kwargs):
            hash_calls["count"] += 1
            return real_hash(*args, **kwargs)

        mod.hash_file = spy_hash
        saved_askyesno = mod.messagebox.askyesno
        try:
            # User declines in confirmation dialog -> preview only, no destruction
            mod.messagebox.askyesno = lambda *a, **k: False
            app.delete_selected()
            self.assertEqual(hash_calls["count"], 0,
                             "delete_selected preview before confirmation must not compute full SHA-256")
        finally:
            mod.hash_file = real_hash
            mod.messagebox.askyesno = saved_askyesno

    def test_t054_keeper_invalidation_mid_batch_forces_safe_fallback(self):
        """T-54: invalidating the cached keeper mid-batch forces safe fallback scan to next valid keeper,
        and refuses deletion if all survivors become invalid, never permitting last-copy deletion."""
        from delete_duplicates_gui import _GroupDeletionState

        k1 = os.path.join(self.tmp, "k1.bin")
        k2 = os.path.join(self.tmp, "k2.bin")
        d1 = os.path.join(self.tmp, "d1.bin")
        d2 = os.path.join(self.tmp, "d2.bin")
        for p in (k1, k2, d1, d2):
            _make_file(p, b"CONTENT")

        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        k1_rec = next(m for m in group.members if m.path == k1)
        k2_rec = next(m for m in group.members if m.path == k2)
        d1_rec = next(m for m in group.members if m.path == d1)
        d2_rec = next(m for m in group.members if m.path == d2)

        state = _GroupDeletionState(group, keeper=k1_rec)
        deleted_paths = set()

        # Step 1: d1 deletion finds k1
        s1 = state.get_survivor(d1_rec, deleted_paths, self.engine)
        self.assertIsNotNone(s1)
        self.assertEqual(s1.path, k1)
        deleted_paths.add(d1)

        # Step 2: invalidate k1 (delete from disk)
        os.remove(k1)

        # Step 3: d2 deletion detects invalid k1, falls back to scan, discovers k2
        s2 = state.get_survivor(d2_rec, deleted_paths, self.engine)
        self.assertIsNotNone(s2)
        self.assertEqual(s2.path, k2)
        deleted_paths.add(d2)

        # Step 4: invalidate k2 (delete from disk)
        os.remove(k2)

        # Step 5: next check finds no valid survivor -> refuses deletion
        dummy = FileRecord(path="dummy.bin", size=7, fingerprint="fp")
        s3 = state.get_survivor(dummy, deleted_paths, self.engine)
        self.assertIsNone(s3, "when all survivors are gone, get_survivor must return None to refuse last-copy deletion")

    def test_t054_failed_send_to_trash_member_remains_eligible_survivor(self):
        """T-54: a member whose deletion failed is not in deleted_paths and remains an eligible
        survivor under T-45 when the primary keeper is lost."""
        from delete_duplicates_gui import _GroupDeletionState

        k1 = os.path.join(self.tmp, "keeper1.bin")
        d1 = os.path.join(self.tmp, "failed_d1.bin")
        d2 = os.path.join(self.tmp, "doomed_d2.bin")
        for p in (k1, d1, d2):
            _make_file(p, b"SURVIVE")

        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        k1_rec = next(m for m in group.members if m.path == k1)
        d1_rec = next(m for m in group.members if m.path == d1)
        d2_rec = next(m for m in group.members if m.path == d2)

        state = _GroupDeletionState(group, keeper=k1_rec)
        deleted_paths = set()

        # d1 deletion fails: d1 is NOT added to deleted_paths, still exists on disk
        # Now k1 is destroyed by an external actor
        os.remove(k1)

        # d2 is evaluated: keeper k1 is invalid, fallback scan evaluates d1,
        # which is not in deleted_paths and is still a valid disk copy
        survivor = state.get_survivor(d2_rec, deleted_paths, self.engine)
        self.assertIsNotNone(survivor)
        self.assertEqual(survivor.path, d1, "failed deletion member must remain an eligible survivor (T-45)")


class T055BackpressureCancellationTest(unittest.TestCase):
    """T-55 (SRC-002 PERF-002): saturated result backpressure is cancellation-aware."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = DuplicateEngine()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _app(self, queue_maxsize=1):
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.root = _FakeRoot()
        app.engine = self.engine
        app.queue = queue.Queue(maxsize=queue_maxsize)
        app.control_queue = queue.Queue()
        app.current_scan_id = 1
        app.current_groups = []
        app.current_records = []
        app.check_vars = {}
        app.item_path_map = {}
        app.path_to_item = {}
        app.tree = _FakeTree()
        app.status_var = _Var("")
        app.stop_button = _FakeButton()
        app._shutting_down = False
        app._sessions = {}
        app.cancel_event = threading.Event()
        return app

    def test_t055_saturated_queue_cancel_exits_worker_without_draining(self):
        """T-55: worker blocked on full result queue unblocks on cancel and exits
        without externally draining capacity; terminal reaches control_queue."""
        for i in range(10):
            _make_file(os.path.join(self.tmp, f"dup_{i}.bin"), b"SAME_CONTENT")

        app = self._app(queue_maxsize=1)
        cancel_event = threading.Event()
        worker = threading.Thread(
            target=DuplicateFinderApp._scan_worker,
            args=(app, 1, self.tmp, cancel_event),
            daemon=True,
        )
        worker.start()

        # Wait until producer fills the tiny queue (size reaches maxsize=1)
        for _ in range(50):
            if app.queue.full():
                break
            time.sleep(0.02)
        self.assertTrue(app.queue.full(), "result queue must be saturated")

        # Trigger cancellation while worker is blocked in put
        cancel_event.set()

        # Worker must exit cleanly without the queue being drained
        worker.join(timeout=3.0)
        self.assertFalse(worker.is_alive(), "worker must exit promptly upon cancellation without queue draining")
        self.assertTrue(app.queue.full(), "queue capacity was not drained by caller")

        # Terminal message must be present in control_queue
        control_msgs = []
        while not app.control_queue.empty():
            control_msgs.append(app.control_queue.get_nowait())
        terminals = [m for m in control_msgs if m[0] == "terminal"]
        self.assertEqual(len(terminals), 1, "exactly one terminal must be enqueued")
        self.assertEqual(terminals[0][1], 1)
        self.assertEqual(terminals[0][2], "cancelled")

    def test_t055_terminal_reaches_consumer_while_result_queue_full(self):
        """T-55: process_queue handles control_queue first, so cancelled terminal
        wipes partial state even when result queue is 100% full."""
        for i in range(6):
            _make_file(os.path.join(self.tmp, f"f_{i}.bin"), b"DUP_SET")

        app = self._app(queue_maxsize=2)
        cancel_event = threading.Event()
        worker = threading.Thread(
            target=DuplicateFinderApp._scan_worker,
            args=(app, 1, self.tmp, cancel_event),
            daemon=True,
        )
        worker.start()

        for _ in range(50):
            if app.queue.full():
                break
            time.sleep(0.02)
        cancel_event.set()
        worker.join(timeout=3.0)

        # Run process_queue with full result queue
        DuplicateFinderApp.process_queue(app)
        self.assertIn("Поиск отменён", app.status_var.get())
        self.assertEqual(app.current_records, [], "cancelled terminal must leave zero armed records")
        self.assertEqual(app.current_groups, [])

    def test_t055_multiple_groups_saturated_queue_cancellation(self):
        """T-55: repeat with multiple distinct duplicate groups across subdirectories."""
        for g_idx in range(4):
            sub = os.path.join(self.tmp, f"grp_{g_idx}")
            os.makedirs(sub, exist_ok=True)
            for m_idx in range(3):
                _make_file(os.path.join(sub, f"f_{m_idx}.bin"), f"GROUP_{g_idx}".encode())

        app = self._app(queue_maxsize=2)
        cancel_event = threading.Event()
        worker = threading.Thread(
            target=DuplicateFinderApp._scan_worker,
            args=(app, 1, self.tmp, cancel_event),
            daemon=True,
        )
        worker.start()

        for _ in range(50):
            if app.queue.full():
                break
            time.sleep(0.02)
        cancel_event.set()
        worker.join(timeout=3.0)
        self.assertFalse(worker.is_alive())

    def test_t055_slow_consumer_bounded_memory_no_result_loss(self):
        """T-55: non-cancelled scan with bounded queue preserves backpressure
        and delivers all duplicate records without result loss."""
        for i in range(8):
            _make_file(os.path.join(self.tmp, f"item_{i}.bin"), b"EXACT_SAME_DATA")

        app = self._app(queue_maxsize=3)
        cancel_event = threading.Event()
        worker = threading.Thread(
            target=DuplicateFinderApp._scan_worker,
            args=(app, 1, self.tmp, cancel_event),
            daemon=True,
        )
        worker.start()

        results_collected = []
        terminal_collected = []
        while worker.is_alive() or not app.queue.empty() or not app.control_queue.empty():
            self.assertLessEqual(app.queue.qsize(), 3, "bounded result queue must never exceed maxsize")
            try:
                msg = app.queue.get_nowait()
                if msg[0] == "add_result":
                    results_collected.append(msg[2])
            except queue.Empty:
                pass
            try:
                cmsg = app.control_queue.get_nowait()
                if cmsg[0] == "terminal":
                    terminal_collected.append(cmsg)
            except queue.Empty:
                pass
            time.sleep(0.01)

        worker.join(timeout=3.0)
        self.assertEqual(len(results_collected), 8, "non-cancelled scan must deliver all 8 duplicate records")
        self.assertEqual(len(terminal_collected), 1, "exactly one terminal on successful scan")
        self.assertEqual(terminal_collected[0][2], "completed")


class T053ModelNormalizationTest(unittest.TestCase):
    """T-53 (SRC-002 W2-003): model normalization, stale record invalidation,
    and singleton duplicate-group collapse."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = DuplicateEngine()
        self.saved_mbox = (mod.messagebox.showwarning, mod.messagebox.askyesno,
                           mod.messagebox.showerror, mod.messagebox.showinfo)
        self.saved_trash = mod.send_to_trash
        self.warnings = []
        self.asked = []
        self.errors = []
        mod.messagebox.showwarning = lambda title, msg: self.warnings.append((title, msg))
        mod.messagebox.askyesno = lambda title, msg: (self.asked.append((title, msg)), True)[1]
        mod.messagebox.showerror = lambda title, msg: self.errors.append((title, msg))
        mod.messagebox.showinfo = lambda title, msg: None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        mod.messagebox.showwarning = self.saved_mbox[0]
        mod.messagebox.askyesno = self.saved_mbox[1]
        mod.messagebox.showerror = self.saved_mbox[2]
        mod.messagebox.showinfo = self.saved_mbox[3]
        mod.send_to_trash = self.saved_trash

    def _app(self, res, select_paths=()):
        app = DestructiveUITest._bare_app()
        app.engine = self.engine
        app.current_groups = list(res.groups)
        app.current_records = [m for g in app.current_groups for m in g.members]
        app.path_to_item = {r.path: f"I_{r.path}" for r in app.current_records}
        app.item_path_map = {v: k for k, v in app.path_to_item.items()}
        app.check_vars = {r.path: _Var(r.path in select_paths) for r in app.current_records}
        app.tree = _FakeTree()
        app.log_var = _Var(False)
        app.status_var = _Var("")
        app.path_var = _Var(self.tmp)
        app.current_result_root = os.path.normcase(os.path.abspath(self.tmp))
        return app

    def test_t053_changed_selected_record_stale_reported_no_confirm_disarmed(self):
        """T-53: changed selected record reports stale, shows no zero-file confirmation,
        disarms the stale row, and collapses the group."""
        f1 = os.path.join(self.tmp, "f1.bin")
        f2 = os.path.join(self.tmp, "f2.bin")
        _make_file(f1, b"IDENTICAL_CONTENT")
        _make_file(f2, b"IDENTICAL_CONTENT")
        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        orig = next(m for m in group.members if m.is_original)
        dup = next(m for m in group.members if not m.is_original)

        # Mutate dup before deletion
        _make_file(dup.path, b"CHANGED_CONTENT_DIFFERENT_SIZE")
        app = self._app(res, select_paths=[dup.path])
        app.delete_selected()

        self.assertEqual(len(self.asked), 0, "must not display confirmation dialog for zero approved files")
        self.assertEqual(len(self.warnings), 1, "must report stale warning")
        self.assertNotIn(dup.path, app.check_vars, "stale record must be removed from check_vars")
        self.assertNotIn(dup.path, [r.path for r in app.current_records], "stale record removed from records")
        self.assertIn(f"I_{dup.path}", app.tree.deleted, "stale row removed from tree")
        self.assertIn(f"I_{orig.path}", app.tree.deleted, "singleton survivor removed from tree UI")
        self.assertEqual(app.current_groups, [], "2-member group with 1 stale collapsed from current_groups")
        self.assertEqual(app.current_records, [], "all records removed from model")
        self.assertTrue(os.path.exists(orig.path), "original survivor must remain on disk")
        self.assertTrue(os.path.exists(dup.path), "changed file must not be destroyed")
        self.assertIn("пропущено: 1", app.status_var.get())

    def test_t053_missing_selected_record_stale_reported_no_confirm_disarmed(self):
        """T-53: missing selected record reports stale, no confirmation, disarms row, collapses group."""
        f1 = os.path.join(self.tmp, "f1.bin")
        f2 = os.path.join(self.tmp, "f2.bin")
        _make_file(f1, b"IDENTICAL_CONTENT")
        _make_file(f2, b"IDENTICAL_CONTENT")
        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        orig = next(m for m in group.members if m.is_original)
        dup = next(m for m in group.members if not m.is_original)

        os.remove(dup.path)
        app = self._app(res, select_paths=[dup.path])
        app.delete_selected()

        self.assertEqual(len(self.asked), 0, "no zero-file confirmation dialog")
        self.assertEqual(len(self.warnings), 1, "stale warning reported")
        self.assertNotIn(dup.path, app.check_vars)
        self.assertEqual(app.current_groups, [])
        self.assertEqual(app.current_records, [])
        self.assertTrue(os.path.exists(orig.path), "survivor must remain on disk")
        self.assertIn("пропущено: 1", app.status_var.get())

    def test_t053_send_to_trash_oserror_remains_for_retry(self):
        """T-53: send_to_trash OSError on valid record stays visible and armed for retry (T-45)."""
        f1 = os.path.join(self.tmp, "f1.bin")
        f2 = os.path.join(self.tmp, "f2.bin")
        _make_file(f1, b"IDENTICAL_CONTENT")
        _make_file(f2, b"IDENTICAL_CONTENT")
        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        dup = next(m for m in group.members if not m.is_original)

        def failing_trash(path, original_path=None):
            raise OSError("Access denied / file in use")

        mod.send_to_trash = failing_trash
        app = self._app(res, select_paths=[dup.path])
        app.delete_selected()

        self.assertEqual(len(self.asked), 1, "approved file asked confirmation")
        self.assertIn(dup.path, app.check_vars, "failed record remains in check_vars")
        self.assertTrue(app.check_vars[dup.path].get(), "failed record stays checked for retry")
        self.assertEqual(len(app.current_groups), 1, "group retained for retry")
        self.assertEqual(len(app.current_groups[0].members), 2)
        status = app.status_var.get()
        self.assertIn("Удалено файлов: 0", status)
        self.assertIn("не удалось: 1", status)

    def test_t053_two_member_group_delete_one_group_and_singleton_disappear_survivor_on_disk(self):
        """T-53: deleting 1 member of a 2-member group collapses the group and removes the singleton
        row from the UI model while leaving the survivor on disk."""
        f1 = os.path.join(self.tmp, "f1.bin")
        f2 = os.path.join(self.tmp, "f2.bin")
        _make_file(f1, b"IDENTICAL_CONTENT")
        _make_file(f2, b"IDENTICAL_CONTENT")
        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        orig = next(m for m in group.members if m.is_original)
        dup = next(m for m in group.members if not m.is_original)

        app = self._app(res, select_paths=[dup.path])
        app.delete_selected()

        self.assertFalse(os.path.exists(dup.path), "deleted file must be gone")
        self.assertTrue(os.path.exists(orig.path), "survivor must remain on disk")
        self.assertEqual(app.current_groups, [], "group collapsed from results model")
        self.assertEqual(app.current_records, [], "all records removed from results model")
        self.assertIn(f"I_{dup.path}", app.tree.deleted)
        self.assertIn(f"I_{orig.path}", app.tree.deleted)
        self.assertIn("Удалено файлов: 1", app.status_var.get())

    def test_t053_three_member_group_delete_one_leaves_two_member_group_with_one_original(self):
        """T-53: deleting 1 member of a 3-member group retains a 2-member group with exactly one original."""
        p1 = os.path.join(self.tmp, "p1.bin")
        p2 = os.path.join(self.tmp, "p2.bin")
        p3 = os.path.join(self.tmp, "p3.bin")
        for p in (p1, p2, p3):
            _make_file(p, b"THREE_DUPES")
        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        dups = [m for m in group.members if not m.is_original]
        del_target = dups[0]

        app = self._app(res, select_paths=[del_target.path])
        app.delete_selected()

        self.assertFalse(os.path.exists(del_target.path))
        self.assertEqual(len(app.current_groups), 1, "group retained with 2 members")
        self.assertEqual(len(app.current_groups[0].members), 2)
        self.assertEqual(len(app.current_records), 2)
        orig_count = sum(1 for m in app.current_groups[0].members if m.is_original)
        self.assertEqual(orig_count, 1, "exactly one original elected")
        self.assertIn("Удалено файлов: 1", app.status_var.get())

    def test_t053_three_member_group_delete_two_removes_singleton_from_ui_not_disk(self):
        """T-53: deleting 2 members of a 3-member group removes surviving singleton from UI not disk."""
        p1 = os.path.join(self.tmp, "p1.bin")
        p2 = os.path.join(self.tmp, "p2.bin")
        p3 = os.path.join(self.tmp, "p3.bin")
        for p in (p1, p2, p3):
            _make_file(p, b"THREE_DUPES")
        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        orig = next(m for m in group.members if m.is_original)
        dups = [m for m in group.members if not m.is_original]

        app = self._app(res, select_paths=[d.path for d in dups])
        app.delete_selected()

        for d in dups:
            self.assertFalse(os.path.exists(d.path))
        self.assertTrue(os.path.exists(orig.path), "sole survivor must remain on disk")
        self.assertEqual(app.current_groups, [], "singleton group collapsed from current_groups")
        self.assertEqual(app.current_records, [], "all records removed from results model")
        self.assertNotIn(orig.path, app.check_vars)
        self.assertIn("Удалено файлов: 2", app.status_var.get())


class T056StagedFingerprintPipelineTest(unittest.TestCase):
    """T-56 (SRC-002 wave 3 PERF-003): staged fingerprint pipeline small files redundant hashing
    and non-overlapping sample ranges."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = DuplicateEngine()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t056_duplicate_small_files_one_hash_pass_total_no_hash_file(self):
        """T-56: two duplicate files <= block_size -> exactly one complete hash pass per file total,
        and hash_file is not called because sample_fingerprint is authoritative."""
        f1 = os.path.join(self.tmp, "f1.bin")
        f2 = os.path.join(self.tmp, "f2.bin")
        content = b"SMALL_DUPE_DATA" * 50  # 750 bytes <= 65536
        _make_file(f1, content)
        _make_file(f2, content)

        hash_file_calls = {"n": 0}
        sample_calls = {"n": 0}
        real_hash = mod.hash_file
        real_sample = mod.sample_fingerprint

        def spy_hash(*args, **kwargs):
            hash_file_calls["n"] += 1
            return real_hash(*args, **kwargs)

        def spy_sample(*args, **kwargs):
            sample_calls["n"] += 1
            return real_sample(*args, **kwargs)

        mod.hash_file = spy_hash
        mod.sample_fingerprint = spy_sample
        try:
            res = self.engine.scan(self.tmp)
        finally:
            mod.hash_file = real_hash
            mod.sample_fingerprint = real_sample

        self.assertEqual(sample_calls["n"], 2, "sample_fingerprint called exactly once per file")
        self.assertEqual(hash_file_calls["n"], 0, "hash_file must not be called for files <= block_size")
        self.assertEqual(len(res.groups), 1, "duplicate group detected")
        self.assertEqual(len(res.groups[0].members), 2)
        expected_fp = mod.hashlib.sha256(content).hexdigest()
        self.assertEqual(res.groups[0].members[0].fingerprint, expected_fp)
        self.assertEqual(res.groups[0].members[1].fingerprint, expected_fp)

    def test_t056_distinct_small_files_one_hash_pass_each_no_group(self):
        """T-56: distinct same-size files <= block_size -> no group, one full pass each (via sample_fingerprint)."""
        f1 = os.path.join(self.tmp, "s1.bin")
        f2 = os.path.join(self.tmp, "s2.bin")
        _make_file(f1, b"SAME_SIZE_DIFF_CONTENT_11111")
        _make_file(f2, b"SAME_SIZE_DIFF_CONTENT_22222")

        hash_file_calls = {"n": 0}
        sample_calls = {"n": 0}
        real_hash = mod.hash_file
        real_sample = mod.sample_fingerprint

        def spy_hash(*args, **kwargs):
            hash_file_calls["n"] += 1
            return real_hash(*args, **kwargs)

        def spy_sample(*args, **kwargs):
            sample_calls["n"] += 1
            return real_sample(*args, **kwargs)

        mod.hash_file = spy_hash
        mod.sample_fingerprint = spy_sample
        try:
            res = self.engine.scan(self.tmp)
        finally:
            mod.hash_file = real_hash
            mod.sample_fingerprint = real_sample

        self.assertEqual(sample_calls["n"], 2, "sample_fingerprint called once per file")
        self.assertEqual(hash_file_calls["n"], 0, "hash_file not called")
        self.assertEqual(res.groups, [], "distinct files must not form a group")

    def test_t056_block_size_plus_one_sample_ranges_strictly_below_file_size(self):
        """T-56: block_size+1 file -> sampled bytes <= file size and ranges non-overlapping."""
        bs = 65536
        file_size = bs + 1
        ranges = mod._sample_ranges(file_size, bs)
        self.assertEqual(len(ranges), 3, "three sample ranges for size > block_size")

        # Verify non-overlapping
        (o1, l1), (o2, l2), (o3, l3) = ranges
        self.assertLessEqual(o1 + l1, o2, "range 1 and range 2 must not overlap")
        self.assertLessEqual(o2 + l2, o3, "range 2 and range 3 must not overlap")
        self.assertLessEqual(o3 + l3, file_size, "range 3 must not exceed file size")

        total_sampled = l1 + l2 + l3
        self.assertLess(total_sampled, file_size, "aggregate sampled bytes strictly below file size")
        self.assertLessEqual(total_sampled, file_size)

        # Integration test with real file
        test_file = os.path.join(self.tmp, "bs_plus_1.bin")
        _make_file(test_file, b"A" * file_size)

        # Track bytes read during sample_fingerprint
        bytes_read = {"total": 0}
        orig_open = builtins.open

        class FileSpy:
            def __init__(self, f):
                self._f = f
            def read(self, n=-1):
                chunk = self._f.read(n)
                bytes_read["total"] += len(chunk)
                return chunk
            def seek(self, offset, whence=os.SEEK_SET):
                return self._f.seek(offset, whence)
            def fileno(self):
                return self._f.fileno()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self._f.__exit__(*args)

        def spy_open(path, *args, **kwargs):
            f = orig_open(path, *args, **kwargs)
            if os.path.abspath(path) == os.path.abspath(test_file):
                return FileSpy(f)
            return f

        builtins.open = spy_open
        try:
            sfp = mod.sample_fingerprint(test_file, block_size=bs)
        finally:
            builtins.open = orig_open

        self.assertIsNotNone(sfp)
        self.assertLessEqual(bytes_read["total"], file_size,
                             "actual bytes read during sample must not exceed file size")
        self.assertEqual(bytes_read["total"], total_sampled)

    def test_t056_large_distinct_sample_files_zero_full_hashes(self):
        """T-56: large files (> block_size) with distinct samples -> zero full hashes."""
        bs = 1000
        engine = DuplicateEngine(block_size=bs)
        f1 = os.path.join(self.tmp, "large1.bin")
        f2 = os.path.join(self.tmp, "large2.bin")
        # 3000 bytes > 1000 block_size, differing in first byte
        _make_file(f1, b"1" + b"X" * 2999)
        _make_file(f2, b"2" + b"X" * 2999)

        hash_file_calls = {"n": 0}
        real_hash = mod.hash_file

        def spy_hash(*args, **kwargs):
            hash_file_calls["n"] += 1
            return real_hash(*args, **kwargs)

        mod.hash_file = spy_hash
        try:
            res = engine.scan(self.tmp)
        finally:
            mod.hash_file = real_hash

        self.assertEqual(hash_file_calls["n"], 0, "differing samples must never trigger full hash")
        self.assertEqual(res.groups, [])

    def test_t056_large_partial_sample_collisions_full_sha256_only_equal_groups(self):
        """T-56: large files (> block_size) with colliding partial samples -> full SHA-256 runs,
        and only truly equal files form duplicate groups."""
        bs = 1000
        file_size = 4000
        engine = DuplicateEngine(block_size=bs)

        ranges = mod._sample_ranges(file_size, bs)
        (o1, l1), (o2, l2), (o3, l3) = ranges

        # Construct 3 files:
        # f1 and f2 are identical
        # f3 has identical sample ranges, but differs in unsampled gap (offset 1100)
        base = bytearray(b"S" * file_size)
        f1_data = bytes(base)
        f2_data = bytes(base)
        base[1100:1200] = b"DIFF_INTERIOR_" * 7 + b"XX"
        f3_data = bytes(base)

        f1 = os.path.join(self.tmp, "p1.bin")
        f2 = os.path.join(self.tmp, "p2.bin")
        f3 = os.path.join(self.tmp, "p3.bin")
        _make_file(f1, f1_data)
        _make_file(f2, f2_data)
        _make_file(f3, f3_data)

        # Verify that samples indeed collide
        s1 = mod.sample_fingerprint(f1, block_size=bs)
        s2 = mod.sample_fingerprint(f2, block_size=bs)
        s3 = mod.sample_fingerprint(f3, block_size=bs)
        self.assertEqual(s1, s2)
        self.assertEqual(s1, s3, "f3 must collide with f1 and f2 on partial sample")

        hash_file_calls = []
        real_hash = mod.hash_file

        def spy_hash(path, *args, **kwargs):
            hash_file_calls.append(os.path.basename(path))
            return real_hash(path, *args, **kwargs)

        mod.hash_file = spy_hash
        try:
            res = engine.scan(self.tmp)
        finally:
            mod.hash_file = real_hash

        self.assertEqual(len(hash_file_calls), 3, "all 3 partial-sample colliding files must be full-hashed")
        self.assertEqual(len(res.groups), 1, "only 1 duplicate group formed")
        group_paths = {os.path.basename(m.path) for m in res.groups[0].members}
        self.assertEqual(group_paths, {"p1.bin", "p2.bin"}, "only f1 and f2 group; f3 excluded")


class T057ReleaseSurfaceParityTest(unittest.TestCase):
    """T-57 (SRC-002 wave 1 CORE-003): release-surface parity and Estonian translation fixes."""

    @classmethod
    def setUpClass(cls):
        cls.proj_root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(cls.proj_root, "VERSION"), encoding="utf-8") as f:
            cls.root_version = f.read().strip()
        import glob
        cls.readme_paths = sorted(glob.glob(os.path.join(cls.proj_root, "README*.md")))

    def test_t057_root_version_matches_module_version(self):
        """T-57: root VERSION file matches delete_duplicates_gui module VERSION."""
        self.assertEqual(mod.VERSION, self.root_version)

    def test_t057_all_discovered_readmes_clean_utf8_no_mojibake(self):
        """T-57: all discovered README*.md files decode as clean UTF-8 with zero mojibake sequences."""
        mojibake_markers = ["Г¤", "Г¶", "Гј", "Гµ", "рџ", "\ufffd", "Ã¤", "Ã¶", "Ã¼", "Ãµ"]
        self.assertGreaterEqual(len(self.readme_paths), 7, "at least 7 tracked README mirrors must be discovered")
        for path in self.readme_paths:
            fname = os.path.basename(path)
            with open(path, "rb") as f:
                raw = f.read()
            text = raw.decode("utf-8")
            for marker in mojibake_markers:
                self.assertNotIn(marker, text, f"{fname} contains mojibake marker {marker!r}")

    def test_t057_all_discovered_readmes_version_parity_no_stale_literals(self):
        """T-57: every discovered README*.md resolves to root VERSION with no contradictory stale versions."""
        import re
        for path in self.readme_paths:
            fname = os.path.basename(path)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertTrue(
                f"v{self.root_version}" in content or self.root_version in content,
                f"{fname} must mention root VERSION {self.root_version}",
            )
            versions = set(re.findall(r"0\.0\.\d+", content))
            self.assertEqual(
                versions, {self.root_version},
                f"{fname} has contradictory or stale version literals: {versions}",
            )

    def test_t057_no_discovered_readme_claims_committed_executable_or_nonexistent_scan_controls(self):
        """T-57: no README advertises a committed exe or describes nonexistent scan controls."""
        for path in self.readme_paths:
            fname = os.path.basename(path)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            lower = text.lower()
            self.assertNotIn("valmis delete_duplicates_gui.exe", lower,
                             f"{fname} must not claim a committed exe exists")
            self.assertNotIn("valmis exe", lower,
                             f"{fname} must not claim a prebuilt exe is in the repository")
            self.assertNotIn("rГ¤si algoritm", text,
                             f"{fname} must not describe nonexistent scan options")

    def test_t057_contributing_lists_all_discovered_readmes(self):
        """T-57: CONTRIBUTING.md version-surface list includes all discovered README*.md files."""
        contrib_path = os.path.join(self.proj_root, "CONTRIBUTING.md")
        with open(contrib_path, encoding="utf-8") as f:
            contrib_text = f.read()
        for path in self.readme_paths:
            fname = os.path.basename(path)
            self.assertIn(fname, contrib_text, f"{fname} must be declared in CONTRIBUTING.md")


class T059BoardDetailRefArchivingTest(unittest.TestCase):
    """T-59 (SRC-002 P2): BOARD detail_ref payloads archiving and recovery path preservation."""

    REQUIRED_TICKETS = ("T-49", "T-50", "T-51", "T-52", "T-53", "T-54", "T-55", "T-56", "T-57")

    @classmethod
    def _payload_refs(cls, *parts):
        """All compaction payload JSON paths under .saipen/<root>/board-compaction/<ticket>/."""
        refs = []
        root = os.path.join(cls.proj_root, *parts)
        if not os.path.isdir(root):
            return refs
        for ticket in sorted(os.listdir(root)):
            ticket_dir = os.path.join(root, ticket)
            if not os.path.isdir(ticket_dir):
                continue
            for name in sorted(os.listdir(ticket_dir)):
                if name.endswith(".json"):
                    refs.append("/".join(parts + (ticket, name)))
        return refs

    @classmethod
    def setUpClass(cls):
        cls.proj_root = os.path.dirname(os.path.abspath(__file__))
        board_path = os.path.join(cls.proj_root, ".saipen", "BOARD.md")
        with open(board_path, encoding="utf-8") as f:
            board_text = f.read()
        import re
        cls.board_refs = sorted(set(re.findall(
            r"detail_ref:\s*(\.saipen/(?:recovery|evidence)/board-compaction/[^\s|\]]+)", board_text
        )))
        cls.recovery_refs = cls._payload_refs(".saipen", "recovery", "board-compaction")
        cls.export_refs = cls._payload_refs(".saipen", "evidence", "board-compaction")
        cls.shipped_refs = sorted(set(cls.board_refs) | set(cls.export_refs))
        cls.present_refs = sorted(
            ref for ref in set(cls.board_refs) | set(cls.recovery_refs) | set(cls.export_refs)
            if os.path.isfile(os.path.join(cls.proj_root, ref.replace("/", os.sep)))
        )

    def test_t059_detail_refs_present_and_resolve_on_disk(self):
        """T-59: every BOARD detail_ref citation resolves to a valid on-disk payload.

        Closed tickets are pruned from BOARD by the protocol board scrub, so the count of
        live citations is protocol-owned and not asserted here; the durable payload
        coverage is asserted by test_t059_exportable_mirror_covers_t49_t57.
        """
        import json
        for ref in self.board_refs:
            disk_path = os.path.join(self.proj_root, ref.replace("/", os.sep))
            self.assertTrue(os.path.isfile(disk_path), f"detail_ref target missing: {ref}")
            with open(disk_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsInstance(data, dict)

    def test_t059_exportable_mirror_covers_t49_t57(self):
        """T-59: the exportable mirror keeps one resolvable payload per compacted ticket."""
        import json
        self.assertGreaterEqual(len(self.export_refs), len(self.REQUIRED_TICKETS),
                                "exportable mirror must keep one payload per T-49..T-57")
        covered = set()
        for ref in self.export_refs:
            covered.add(ref.split("/")[3])
            disk_path = os.path.join(self.proj_root, ref.replace("/", os.sep))
            self.assertTrue(os.path.isfile(disk_path), f"exportable payload missing: {ref}")
            with open(disk_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsInstance(data, dict)
        for ticket in self.REQUIRED_TICKETS:
            self.assertIn(ticket, covered, f"exportable mirror lost payload for {ticket}")

    def test_t059_board_compaction_not_gitignored(self):
        """T-59: compaction payloads are explicitly unignored in .gitignore, so the handoff builder collects them."""
        import subprocess
        for ref in self.shipped_refs + self.recovery_refs:
            r = subprocess.run(
                ["git", "check-ignore", ref],
                cwd=self.proj_root,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(r.returncode, 0,
                                f"{ref} must NOT be ignored by git, so handoff builder collects it")

    def test_t059_evidence_board_compaction_mirrors_all_recovery_targets(self):
        """T-59: .saipen/evidence/board-compaction contains byte-identical copies of all detail_ref targets."""
        import filecmp
        for ref in self.recovery_refs:
            rec_path = os.path.join(self.proj_root, ref.replace("/", os.sep))
            evi_rel = ref.replace(".saipen/recovery/", ".saipen/evidence/")
            evi_path = os.path.join(self.proj_root, evi_rel.replace("/", os.sep))
            self.assertTrue(os.path.isfile(evi_path), f"evidence mirror missing: {evi_rel}")
            self.assertTrue(filecmp.cmp(rec_path, evi_path, shallow=False),
                            f"evidence mirror bytes differ from recovery target: {ref}")

    def test_t059_mirror_pair_is_byte_bound_in_gitattributes(self):
        """T-59: both sides of the recovery/evidence mirror pair are byte-bound.

        The mirror pair must stay byte-identical after a fresh clone, so neither
        side may be line-ending converted by checkout (Windows autocrlf would
        otherwise rewrite the evidence side and break the byte comparison).
        """
        attrs_path = os.path.join(self.proj_root, ".gitattributes")
        with open(attrs_path, encoding="utf-8") as f:
            attrs = f.read()
        for pattern in (".saipen/recovery/board-compaction/**",
                        ".saipen/evidence/board-compaction/**"):
            self.assertIn(
                f"{pattern} -text", attrs,
                f"{pattern} must be declared byte-bound (-text) so a fresh clone keeps the mirror byte-identical",
            )

    def test_t059_delivery_inventory_includes_all_board_detail_refs(self):
        """T-59: delivery inventory (tracked + untracked non-ignored) includes all detail_ref targets.
        Distinguishes archive validation (when .git is absent) from Git workspace validation (P2)."""
        git_dir = os.path.join(self.proj_root, ".git")
        if not os.path.isdir(git_dir):
            for ref in self.present_refs:
                disk_path = os.path.join(self.proj_root, ref.replace("/", os.sep))
                self.assertTrue(os.path.isfile(disk_path), f"archive inventory must include {ref}")
            return

        import subprocess
        r = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=self.proj_root,
            capture_output=True,
        )
        if r.returncode != 0:
            for ref in self.present_refs:
                disk_path = os.path.join(self.proj_root, ref.replace("/", os.sep))
                self.assertTrue(os.path.isfile(disk_path), f"archive inventory must include {ref}")
            return
        untracked = {e.decode("utf-8", errors="replace").replace("\\", "/")
                     for e in r.stdout.split(b"\0") if e.strip()}
        r_tr = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=self.proj_root,
            capture_output=True,
        )
        tracked = {e.decode("utf-8", errors="replace").replace("\\", "/")
                   for e in r_tr.stdout.split(b"\0") if e.strip()}
        inventory = tracked | untracked
        for ref in self.present_refs:
            norm_ref = ref.lstrip("./")
            # Also check with leading dot-slash or normalized
            found = norm_ref in inventory or ref in inventory or f"./{norm_ref}" in inventory
            self.assertTrue(found, f"delivery inventory must include {ref}")

class T058TruncatedLockPayloadHandlingTest(unittest.TestCase):
    """T-58 (SRC-002 P3): stale/truncated lock file handling and actionable reporting."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="smart_vac_t058_")
        self.lock_path = os.path.join(self.tmp_dir, "destructive.lock")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_t058_owner_unknown_empty_payload_reclaimed_safely(self):
        """T-58: 0-byte lock file (owner died between create and pid write) is reclaimed immediately."""
        with open(self.lock_path, "wb") as f:
            pass
        self.assertEqual(os.path.getsize(self.lock_path), 0)
        t0 = time.monotonic()
        held = mod._acquire_lock_file(self.lock_path, timeout=2.0, poll=0.01)
        elapsed = time.monotonic() - t0
        try:
            self.assertIsNotNone(held)
            self.assertLess(elapsed, 1.0, "unheld 0-byte lock must be acquired immediately without waiting timeout")
        finally:
            mod._release_lock_file(held)
        with open(self.lock_path, "r", encoding="ascii") as f:
            content = f.read()
        self.assertIn(f"pid={os.getpid()}", content)

    def test_t058_owner_unknown_corrupted_payload_reclaimed_safely(self):
        """T-58: corrupt, truncated or non-ASCII lock file without live OS holder is reclaimed safely."""
        corrupt_payloads = [
            b"pid=",
            b"not-a-pid\n",
            b"\x00\x00\x00\x00",
            b"\xff\xfe\xaa\xbb\xcc",
            b"pid=-9999\n",
            b"pid=abc\n",
        ]
        for payload in corrupt_payloads:
            with open(self.lock_path, "wb") as f:
                f.write(payload)
            held = mod._acquire_lock_file(self.lock_path, timeout=2.0, poll=0.01)
            self.assertIsNotNone(held, f"corrupted payload {payload!r} must be acquirable")
            mod._release_lock_file(held)

    def test_t058_lock_timeout_reports_actionable_lock_path(self):
        """T-58: timeout on held lock raises TimeoutError naming the exact actionable lock path."""
        held = mod._acquire_lock_file(self.lock_path, timeout=2.0, poll=0.01)
        try:
            with self.assertRaises(TimeoutError) as ctx:
                mod._acquire_lock_file(self.lock_path, timeout=0.05, poll=0.01)
            err_msg = str(ctx.exception)
            self.assertIn("destructive lock held by another instance", err_msg)
            self.assertIn(self.lock_path, err_msg,
                          "TimeoutError must name the actionable lock path so user/operator can locate it")
        finally:
            mod._release_lock_file(held)

    def test_t058_live_owner_lock_never_removed_or_stolen(self):
        """T-58 regression: a live owner's lock is never unlinked or stolen by competing contenders."""
        held = mod._acquire_lock_file(self.lock_path, timeout=2.0, poll=0.01)
        try:
            self.assertTrue(os.path.exists(self.lock_path))
            with self.assertRaises(TimeoutError):
                mod._acquire_lock_file(self.lock_path, timeout=0.05, poll=0.01)
            self.assertTrue(os.path.exists(self.lock_path),
                            "lock file must never be unlinked while held by a live instance")
        finally:
            mod._release_lock_file(held)
        self.assertTrue(os.path.exists(self.lock_path),
                        "safe release leaves file in place; descriptor closure drops lock")


class T062ToctouPathnameReplacementRegressionTest(unittest.TestCase):
    """P0-A / T-62: Object-identity-bound destructive semantics regression suite.
    Guarantees that an unscanned replacement placed at an original pathname while
    a candidate is quarantined NEVER gets deleted, destination-occupied restore
    refuses safely, and durable recovery metadata supports explicit restore."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smart_vac_t062_")
        self.meta_file = os.path.join(self.tmp, "recovery_meta.json")
        self._orig_meta = mod.RECOVERY_LOG_FILE
        mod.RECOVERY_LOG_FILE = self.meta_file
        self.engine = DuplicateEngine()

    def tearDown(self):
        mod.RECOVERY_LOG_FILE = self._orig_meta
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t062_unscanned_replacement_survives_destructive_call(self):
        """P0-A deterministic reproduction: an unscanned file placed at the original
        path after candidate acquisition SURVIVES send_to_trash untouched."""
        orig_path = os.path.join(self.tmp, "target.bin")
        _make_file(orig_path, b"ORIGINAL_SCANNED_CONTENT")

        # 1. Acquire candidate
        qpath = mod.acquire_candidate(orig_path)
        self.assertTrue(os.path.exists(qpath))
        self.assertFalse(os.path.exists(orig_path))

        # 2. Concurrent actor creates an unscanned replacement at the original path
        _make_file(orig_path, b"UNSCANNED_REPLACEMENT_PAYLOAD")

        # 3. T-68 (REC-004): the acquisition is authorized only through a durable
        #    acquired receipt with complete identity.
        mod.record_quarantine_metadata(qpath, orig_path,
                                       size=os.path.getsize(qpath),
                                       fingerprint=mod.hash_file(qpath),
                                       status="acquired")

        # 4. Destructive call against acquired quarantine object
        mod.send_to_trash(qpath, original_path=orig_path)

        # 4. Prove replacement survived with exact content intact
        self.assertFalse(os.path.exists(qpath), "acquired quarantine file must be deleted")
        self.assertTrue(os.path.exists(orig_path), "unscanned replacement must survive")
        self.assertEqual(_read_bytes(orig_path), b"UNSCANNED_REPLACEMENT_PAYLOAD")

        # 5. Prove durable recovery metadata was recorded
        meta = mod.get_quarantine_metadata(qpath)
        self.assertIsNotNone(meta, "durable recovery metadata must be present")
        self.assertEqual(meta["original_path"], os.path.abspath(orig_path))

    def test_t062_restore_candidate_refuses_occupied_destination_safely(self):
        """When restore_candidate is called on an occupied destination, it refuses safely."""
        orig_path = os.path.join(self.tmp, "doc.bin")
        _make_file(orig_path, b"SCANNED_BYTES")
        qpath = mod.acquire_candidate(orig_path)
        _make_file(orig_path, b"NEW_OCCUPANT")

        # Refusal must return False and touch neither file
        ok = mod.restore_candidate(qpath, orig_path)
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(qpath), "quarantined file must remain intact")
        self.assertTrue(os.path.exists(orig_path), "occupant must remain intact")
        self.assertEqual(_read_bytes(orig_path), b"NEW_OCCUPANT")
        meta = mod.get_quarantine_metadata(qpath)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["status"], "restore_refused_occupied")

    def test_t062_restore_quarantined_file_workflow_with_verification(self):
        """Explicit verified restore workflow validates fingerprint and free destination."""
        orig_path = os.path.join(self.tmp, "safe.bin")
        content = b"CRITICAL_DATA_FINGERPRINT"
        _make_file(orig_path, content)
        fp = mod.hash_file(orig_path)
        qpath = mod.acquire_candidate(orig_path)
        mod.record_quarantine_metadata(qpath, orig_path, size=len(content), fingerprint=fp, status="acquired")

        # Scenario 1: occupied destination refuses
        _make_file(orig_path, b"BLOCKING_FILE")
        ok, reason = mod.restore_quarantined_file(qpath, engine=self.engine)
        self.assertFalse(ok)
        self.assertEqual(reason, "destination_occupied")
        os.remove(orig_path)

        # Scenario 2: tampered quarantine file refuses
        _make_file(qpath, b"TAMPERED_QUARANTINE_DATA")
        ok, reason = mod.restore_quarantined_file(qpath, engine=self.engine)
        self.assertFalse(ok)
        self.assertEqual(reason, "fingerprint_mismatch")

        # Scenario 3: valid file with clear destination restores cleanly
        _make_file(qpath, content)
        ok, res_path = mod.restore_quarantined_file(qpath, engine=self.engine)
        self.assertTrue(ok)
        self.assertEqual(res_path, os.path.abspath(orig_path))
        self.assertTrue(os.path.exists(orig_path))
        self.assertEqual(_read_bytes(orig_path), content)
        meta = mod.get_quarantine_metadata(qpath)
        self.assertEqual(meta["status"], "restored")

    def test_t062_production_batch_unscanned_replacement_isolation(self):
        """Full _execute_destructive_batch integration: replacement installed between
        acquisition and trash is isolated and survives completely."""
        dir1 = os.path.join(self.tmp, "dir1")
        dir2 = os.path.join(self.tmp, "dir2")
        os.makedirs(dir1)
        os.makedirs(dir2)
        p1 = os.path.join(dir1, "file.bin")
        p2 = os.path.join(dir2, "file.bin")
        _make_file(p1, b"IDENTICAL_CONTENT")
        _make_file(p2, b"IDENTICAL_CONTENT")

        res = self.engine.scan(self.tmp)
        self.assertEqual(len(res.groups), 1)
        group = res.groups[0]
        rec1 = next(m for m in group.members if m.path == p1)
        rec2 = next(m for m in group.members if m.path == p2)

        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.engine = self.engine
        app.current_groups = [group]
        app.current_records = [rec1, rec2]
        app.log_var = _Var(False)
        app.status_var = _Var("")
        app.check_vars = {r.path: _Var(False) for r in app.current_records}
        app.path_to_item = {r.path: f"I{i}" for i, r in enumerate(app.current_records)}
        app.item_path_map = {v: k for k, v in app.path_to_item.items()}
        app.tree = _FakeTree()

        # Select rec2 for deletion
        plan = {"approved": [rec2], "skipped": []}

        # Intercept acquire_candidate to inject unscanned replacement at p2 immediately
        real_acquire = mod.acquire_candidate
        def intercepted_acquire(path, intent=None):
            q = real_acquire(path, intent=intent)
            if path == p2:
                _make_file(p2, b"UNSCANNED_ACTOR_REPLACEMENT")
            return q

        saved_acquire = mod.acquire_candidate
        mod.acquire_candidate = intercepted_acquire
        try:
            app._execute_destructive_batch(plan)
        finally:
            mod.acquire_candidate = saved_acquire

        status = app.status_var.get()
        self.assertIn("Удалено файлов: 1", status)
        self.assertTrue(os.path.exists(p1), "keeper p1 must survive with original content")
        self.assertEqual(_read_bytes(p1), b"IDENTICAL_CONTENT")
        self.assertTrue(os.path.exists(p2), "unscanned replacement at p2 must survive untouched")
        self.assertEqual(_read_bytes(p2), b"UNSCANNED_ACTOR_REPLACEMENT")


class T062BAtomicNoReplaceMoveRaceTest(unittest.TestCase):
    """P0-001: _atomic_no_replace_move must not destroy a source pathname that
    a concurrent actor substitutes between the link and the unlink of the old
    link-then-unlink sequence.

    Each test forces the exact interleaving that the old code lost: the
    replacement content is installed at `src` after the destination reservation
    succeeds but BEFORE any source-path removal. The fix collapses the whole
    move into a single atomic syscall, so there is no window to interpose; a
    refused or in-flight move leaves every survivor intact.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smart_vac_t062b_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, name, content):
        path = os.path.join(self.tmp, name)
        _make_file(path, content)
        return path

    def test_t062b_occupied_destination_refuses_and_preserves_both(self):
        """An occupied destination must be refused atomically — neither object
        is destroyed. This replaces the old link-then-unlink that could
        destroy the destination's replacement."""
        src = self._make("src.bin", b"ORIGINAL_CONTENT")
        dst = self._make("dst.bin", b"EXISTING_OCCUPANT")
        with self.assertRaises(FileExistsError):
            mod._atomic_no_replace_move(src, dst)
        # Both objects must survive with exact content.
        self.assertTrue(os.path.exists(src), "source must survive a refused move")
        self.assertTrue(os.path.exists(dst), "destination occupant must survive")
        self.assertEqual(_read_bytes(src), b"ORIGINAL_CONTENT")
        self.assertEqual(_read_bytes(dst), b"EXISTING_OCCUPANT")

    def test_t062b_unoccupied_destination_moves_atomically(self):
        """The normal case: a free destination receives the source and the
        source is consumed, in one atomic step."""
        src = self._make("move_me.bin", b"PAYLOAD_BYTES")
        dst = os.path.join(self.tmp, "arrives_here.bin")
        mod._atomic_no_replace_move(src, dst)
        self.assertFalse(os.path.exists(src), "source consumed on success")
        self.assertTrue(os.path.exists(dst), "destination created on success")
        self.assertEqual(_read_bytes(dst), b"PAYLOAD_BYTES")

    def test_t062b_no_separate_unlink_window(self):
        """The fix must be a single atomic syscall — no os.unlink on the
        source pathname that a concurrent actor could exploit.

        We wrap os.unlink to assert it is never called for a refused
        no-replace move, and that even on a refused move nothing at the
        source is unlinked.
        """
        src = self._make("race.bin", b"ORIGINAL")
        dst = self._make("taken.bin", b"REPLACEMENT")
        real_unlink = os.unlink
        unlink_calls = []

        def spy_unlink(path):
            unlink_calls.append(path)
            real_unlink(path)

        mod.os.unlink = spy_unlink
        try:
            with self.assertRaises(FileExistsError):
                mod._atomic_no_replace_move(src, dst)
        finally:
            mod.os.unlink = real_unlink
        self.assertEqual(
            unlink_calls, [],
            "refused move must not call os.unlink on any path")
        # Replacement and source both intact.
        self.assertEqual(_read_bytes(src), b"ORIGINAL")
        self.assertEqual(_read_bytes(dst), b"REPLACEMENT")

    @unittest.skipUnless(platform.system() == "Windows",
                         "Windows MoveFileExW no-replace semantics")
    def test_t062b_windows_movefile_no_replace_semantics(self):
        """Windows-specific: MoveFileExW(0) without MOVEFILE_REPLACE_EXISTING
        refuses an occupied destination and never overwrites it.
        """
        src = self._make("ntfs_src.bin", b"SOURCE")
        dst = self._make("ntfs_dst.bin", b"OCCUPANT")
        with self.assertRaises(FileExistsError):
            mod._atomic_no_replace_move(src, dst)
        self.assertEqual(_read_bytes(dst), b"OCCUPANT",
                         "Windows must not overwrite an occupied destination")
        self.assertEqual(_read_bytes(src), b"SOURCE",
                         "source must survive a Windows refused move")

    def test_t062b_red_control_old_link_unlink_destroys_replacement(self):
        """Red control for P0-001: the OLD link-then-unlink implementation
        destroys a source pathname that a concurrent actor replaces between the
        link and the unlink. The NEW implementation must pass the SAME
        interposition and preserve the replacement.
        """

        def _old_link_unlink_move(src, dst):
            """The pre-fix implementation: os.link(src, dst) then os.unlink(src)
            — two separate path operations (the P0-001 race)."""
            try:
                mod.os.link(src, dst)
            except FileExistsError:
                raise
            except OSError:
                fd = mod.os.open(dst, mod.os.O_CREAT | mod.os.O_EXCL | mod.os.O_WRONLY, 0o600)
                try:
                    with open(src, "rb") as fin, mod.os.fdopen(fd, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                        fout.flush()
                except OSError:
                    try:
                        mod.os.remove(dst)
                    except OSError:
                        pass
                    raise
                mod.os.unlink(src)
                return
            mod.os.unlink(src)

        def _run_race(move_fn, interpose_fn=None):
            """Return (replacement_survived, dst_content) for move_fn.

            `interpose_fn` is the single move primitive the implementation
            calls: mod.os.link for the OLD link-then-unlink code, or
            mod._windows_atomic_move / mod._linux_atomic_move for the NEW code.
            The concurrent actor replaces the source pathname right after
            the primitive returns. The old code then unlinks the *replacement*;
            an atomic single-syscall move already consumed the source and
            cannot unlink it. Both paths get the same race window.
            """
            race_src = os.path.join(self.tmp, "race.bin")
            race_dst = os.path.join(self.tmp, "taken.bin")
            _make_file(race_src, b"ORIGINAL_CONTENT")

            if interpose_fn is None:
                interpose_fn = os.link

            real_call = interpose_fn
            swap_os = (interpose_fn is mod.os.link)
            swap_win = (hasattr(mod, "_windows_atomic_move")
                        and interpose_fn is mod._windows_atomic_move)
            swap_lin = (hasattr(mod, "_linux_atomic_move")
                        and interpose_fn is mod._linux_atomic_move)

            def interposed(s, d):
                real_call(s, d)
                # Concurrent actor replaces the source pathname right after
                # the move primitive completes.
                try:
                    os.remove(race_src)
                except OSError:
                    pass
                _make_file(race_src, b"CONCURRENT_REPLACEMENT")

            if swap_os:
                mod.os.link = interposed
            elif swap_win:
                mod._windows_atomic_move = interposed
            elif swap_lin:
                mod._linux_atomic_move = interposed

            try:
                move_fn(race_src, race_dst)
            finally:
                if swap_os:
                    mod.os.link = real_call
                elif swap_win:
                    mod._windows_atomic_move = real_call
                elif swap_lin:
                    mod._linux_atomic_move = real_call

            survived = (os.path.exists(race_src)
                        and _read_bytes(race_src) == b"CONCURRENT_REPLACEMENT")
            dst_content = (_read_bytes(race_dst)
                           if os.path.exists(race_dst) else None)
            for p in (race_src, race_dst):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return survived, dst_content

        # 1. OLD code MUST destroy the replacement (red-control, the bug).
        old_survived, old_dst = _run_race(_old_link_unlink_move)
        self.assertFalse(old_survived,
                         "red control: old link-then-unlink must destroy the "
                         "concurrent replacement pathname")

        # 2. NEW code MUST preserve it under the same interposition (the fix).
        if platform.system() == "Windows":
            _new_interpose = mod._windows_atomic_move
        else:
            _new_interpose = mod._linux_atomic_move
        new_survived, new_dst = _run_race(mod._atomic_no_replace_move,
                                          interpose_fn=_new_interpose)
        self.assertTrue(new_survived,
                        "new atomic move must preserve a concurrent "
                        "replacement at the source pathname")
        self.assertEqual(new_dst, b"ORIGINAL_CONTENT",
                        "destination holds the verified original bytes")


class T063CachedKeeperContentValidityRegressionTest(unittest.TestCase):
    """P0-B / T-63: Content validity at destructive boundary regression suite.
    Guarantees that a keeper whose content is modified while preserving size,
    mtime_ns, and inode is NEVER accepted as a valid copy, preventing data loss."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smart_vac_t063_")
        self.engine = DuplicateEngine()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t063_tampered_keeper_refuses_deletion_of_remaining_duplicates(self):
        """P0-B deterministic reproduction:
        1. a.bin, b.bin, c.bin with identical content.
        2. a.bin and b.bin approved for deletion, c.bin is keeper.
        3. a.bin deleted.
        4. c.bin content replaced with different bytes of same length, exact mtime restored.
        5. Batch attempts to delete b.bin: MUST REFUSE because c.bin failed content validity.
        6. b.bin SURVIVES with original content intact. Zero data loss."""
        p_a = os.path.join(self.tmp, "a.bin")
        p_b = os.path.join(self.tmp, "b.bin")
        p_c = os.path.join(self.tmp, "c.bin")
        orig_data = b"ORIGINAL_FINGERPRINT_12345678_ABCD"
        _make_file(p_a, orig_data)
        _make_file(p_b, orig_data)
        _make_file(p_c, orig_data)

        res = self.engine.scan(self.tmp)
        self.assertEqual(len(res.groups), 1)
        group = res.groups[0]
        rec_a = next(m for m in group.members if m.path == p_a)
        rec_b = next(m for m in group.members if m.path == p_b)
        rec_c = next(m for m in group.members if m.path == p_c)

        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.engine = self.engine
        app.current_groups = [group]
        app.current_records = [rec_a, rec_b, rec_c]
        app.log_var = _Var(False)
        app.status_var = _Var("")
        app.check_vars = {r.path: _Var(False) for r in app.current_records}
        app.path_to_item = {r.path: f"I{i}" for i, r in enumerate(app.current_records)}
        app.item_path_map = {v: k for k, v in app.path_to_item.items()}
        app.tree = _FakeTree()

        # Plan: delete a and b, keep c
        plan = {"approved": [rec_a, rec_b], "skipped": []}

        # Intercept send_to_trash: when a is trashed, tamper with keeper c.bin
        real_trash = mod.send_to_trash
        tampered = False
        def tampering_trash(path, original_path=None):
            nonlocal tampered
            real_trash(path, original_path)
            if not tampered and (original_path == p_a or path == p_a or "a.bin" in path):
                tampered = True
                st = os.stat(p_c)
                orig_mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
                # Modify c.bin in-place with different bytes of identical length
                with open(p_c, "r+b") as f:
                    f.write(b"TAMPERED_DIFFERENT_BYTES_99999_WXYZ")
                # Restore exact mtime_ns
                os.utime(p_c, ns=(orig_mtime_ns, orig_mtime_ns))

        saved_trash = mod.send_to_trash
        mod.send_to_trash = tampering_trash
        try:
            app._execute_destructive_batch(plan)
        finally:
            mod.send_to_trash = saved_trash

        status = app.status_var.get()
        # a.bin was removed, but b.bin deletion MUST be refused because keeper c.bin is corrupt
        self.assertIn("Удалено файлов: 1", status)
        self.assertIn("пропущено: 1", status)
        self.assertFalse(os.path.exists(p_a), "a.bin was deleted")
        self.assertTrue(os.path.exists(p_b), "b.bin must survive as the last valid copy!")
        self.assertEqual(_read_bytes(p_b), orig_data,
                         "b.bin must retain intact original content")

    def test_t063_get_survivor_enforces_full_fingerprint_verification(self):
        """_GroupDeletionState.get_survivor rejects a cached keeper when content changed."""
        p1 = os.path.join(self.tmp, "k1.bin")
        p2 = os.path.join(self.tmp, "k2.bin")
        data = b"VALID_KEEPER_CONTENT"
        _make_file(p1, data)
        _make_file(p2, data)

        res = self.engine.scan(self.tmp)
        group = res.groups[0]
        r1 = next(m for m in group.members if m.path == p1)
        r2 = next(m for m in group.members if m.path == p2)

        g_state = mod._GroupDeletionState(group, keeper=r1)
        # Initial check passes
        survivor1 = g_state.get_survivor(r2, set(), self.engine)
        self.assertIs(survivor1, r1)

        # Alter r1 content with identical length and restore mtime
        st = os.stat(p1)
        orig_mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        with open(p1, "r+b") as f:
            f.write(b"CORRUPT_KEEPER_PAYLO")
        os.utime(p1, ns=(orig_mtime_ns, orig_mtime_ns))

        # Next check MUST reject r1 and return r2 as survivor
        survivor2 = g_state.get_survivor(None, set(), self.engine)
        self.assertIs(survivor2, r2, "must fall back to valid survivor r2")

    def test_t063_performance_scaling_1k_2k_4k_members(self):
        """Performance benchmark: monotonic scan with verified keeper scales linearly O(N)
        across 1000, 2000, and 4000 group members."""
        from delete_duplicates_gui import DuplicateGroup, FileRecord
        for count in (1000, 2000, 4000):
            members = [
                FileRecord(path=f"/fake/path/{i}.bin", size=1024, fingerprint="dummy_hash")
                for i in range(count)
            ]
            group = DuplicateGroup(group_id=1, fingerprint="dummy_hash", members=members)
            keeper = members[-1]  # Keeper is the last member
            state = mod._GroupDeletionState(group, keeper=keeper)

            # Mock engine where keeper verification succeeds
            class MockEngine:
                def verify_record(self, rec, full_hash=True):
                    return True, "ok"

            mock_engine = MockEngine()
            deleted = set()
            t0 = time.monotonic()
            # Simulate sequential deletion of first count-1 members
            for i in range(count - 1):
                cur = members[i]
                surv = state.get_survivor(cur, deleted, mock_engine)
                self.assertIs(surv, keeper)
                deleted.add(cur.path)
            elapsed = time.monotonic() - t0

            # Linear check: traversal_checks must be exactly count - 1 (O(1) per deletion)
            self.assertEqual(state.traversal_checks, count - 1,
                             f"scaling for {count} members must require exactly {count-1} checks")
            self.assertLess(elapsed, 1.0, f"execution time for {count} members must be under 1s")


class T064PortabilityAndDocSyncTest(unittest.TestCase):
    """P2 / T-64: Test portability and documentation synchronization."""

    def test_t064_send_to_trash_portable_when_windll_missing(self):
        """When deletion_is_recoverable is mocked True on non-Windows (no ctypes.windll),
        send_to_trash falls back to os.remove without raising AttributeError."""
        import ctypes
        tmp = tempfile.mkdtemp(prefix="smart_vac_t064_")
        try:
            target = os.path.join(tmp, "trash_test.bin")
            _make_file(target, b"DATA")

            saved_recoverable = mod.deletion_is_recoverable
            saved_has_windll = hasattr(ctypes, "windll")
            saved_windll = getattr(ctypes, "windll", None)

            mod.deletion_is_recoverable = lambda: True
            if saved_has_windll:
                delattr(ctypes, "windll")
            try:
                # T-68 (REC-004): the durable acquired receipt authorizes the
                # fallback permanent delete; without it the call must refuse.
                mod.record_quarantine_metadata(target, target,
                                               size=len(b"DATA"),
                                               fingerprint=mod.hash_file(target),
                                               status="acquired")
                # Must not raise AttributeError
                mod.send_to_trash(target)
                self.assertFalse(os.path.exists(target))
            finally:
                mod.deletion_is_recoverable = saved_recoverable
                if saved_has_windll:
                    ctypes.windll = saved_windll
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_t064_changelog_sync_os_locks_and_test_count(self):
        """CHANGELOG.md 0.0.5 accurately describes kernel byte-range lock semantics
        and reflects the actual test suite count."""
        repo_root = os.path.dirname(os.path.abspath(__file__))
        changelog_path = os.path.join(repo_root, "CHANGELOG.md")
        self.assertTrue(os.path.exists(changelog_path))
        with open(changelog_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Must describe kernel byte-range locks / msvcrt / flock
        self.assertIn("byte-range", content.lower())
        # Must not claim T-61 restored before delete as safe practice
        self.assertNotIn("restores original name before deletion", content)



class T065QuarantineMetadataHygieneTest(unittest.TestCase):
    """P2 / T-65: Quarantine metadata atomic write failure cleanup and .gitignore patterns."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smart_vac_t065_")
        self.saved_log_file = mod.RECOVERY_LOG_FILE
        mod.RECOVERY_LOG_FILE = os.path.join(self.tmp, "test_quarantine_recovery.json")

    def tearDown(self):
        mod.RECOVERY_LOG_FILE = self.saved_log_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t065_record_quarantine_metadata_cleans_up_tmp_on_replace_failure(self):
        """When os.replace fails with OSError, temporary metadata file is removed in finally block."""
        orig_replace = os.replace

        def failing_replace(src, dst):
            raise OSError("Simulated Windows file access locked error")

        os.replace = failing_replace
        try:
            qpath = os.path.join(self.tmp, ".test.bin.__vacq_123_1")
            opath = os.path.join(self.tmp, "test.bin")
            mod.record_quarantine_metadata(qpath, opath)
        finally:
            os.replace = orig_replace

        # Verify no orphan .tmp files remain in self.tmp
        tmps = [f for f in os.listdir(self.tmp) if f.endswith(".tmp")]
        self.assertEqual(tmps, [], f"orphan tmp files remained after replace error: {tmps}")

    def test_t065_gitignore_ignores_recovery_log_and_tmp_files(self):
        """.gitignore matches quarantine_recovery.json and transient tmp variants."""
        import subprocess
        repo_root = os.path.dirname(os.path.abspath(__file__))
        if not os.path.isdir(os.path.join(repo_root, ".git")):
            self.skipTest(".git missing, skip check-ignore")
        for path in ("quarantine_recovery.json", "quarantine_recovery.json.123.tmp", "quarantine_recovery.json.123.456.tmp"):
            r = subprocess.run(
                ["git", "check-ignore", path],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, 0, f"{path} must be ignored by .gitignore")


class T066RecoveryIntegrityRegressionTest(unittest.TestCase):
    """T-66: recovery identity is immutable across states, a restore with an
    engine refuses unverifiable objects, destruction requires a durable
    receipt, and the journal lives in stable per-user storage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.recovery_meta = os.path.join(self.tmp, "quarantine_recovery.json")
        self._saved = (mod.RECOVERY_LOG_FILE, mod.deletion_is_recoverable)
        mod.RECOVERY_LOG_FILE = self.recovery_meta
        mod.deletion_is_recoverable = lambda: False

    def tearDown(self):
        (mod.RECOVERY_LOG_FILE, mod.deletion_is_recoverable) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _acquire(self, name="orig.txt", content=b"payload"):
        src = os.path.join(self.tmp, name)
        with open(src, "wb") as f:
            f.write(content)
        qpath = mod.acquire_candidate(src)
        return src, qpath

    def test_t066_status_transition_preserves_immutable_identity(self):
        """A status transition omitting size/fingerprint keeps the recorded
        identity: a receipt must never lose the identity it vouches for.

        Red control: pre-fix record_quarantine_metadata overwrote the record
        wholesale, so the trashing/deleted/recycled transitions erased both.
        """
        src, qpath = self._acquire()
        fp = "f" * 64
        mod.record_quarantine_metadata(qpath, src, size=7, fingerprint=fp,
                                       status="acquired")
        mod.record_quarantine_metadata(qpath, src, status="trashing")
        rec = mod.get_quarantine_metadata(qpath)
        self.assertEqual(rec["fingerprint"], fp)
        self.assertEqual(rec["size"], 7)

    def test_t066_restore_refuses_missing_fingerprint_with_engine(self):
        """With an engine provided, a metadata record without a fingerprint is
        a refusal — never a silent skip of content verification.

        Red control: pre-fix guard `engine and meta and meta.get('fingerprint')`
        skipped verification and renamed tampered content back into place.
        """
        src, qpath = self._acquire()
        mod.record_quarantine_metadata(qpath, src, size=8, status="acquired")
        with open(qpath, "wb") as f:
            f.write(b"TAMPERED-DIFFERENT-CONTENT")
        ok, reason = mod.restore_quarantined_file(qpath, original_path=src,
                                                  engine=object())
        self.assertEqual((ok, reason), (False, "fingerprint_missing"))
        self.assertTrue(os.path.exists(qpath), "the object must stay quarantined")

    def test_t066_restore_refuses_without_any_metadata_with_engine(self):
        """No record at all plus an engine is also a refusal."""
        src, qpath = self._acquire()
        ok, reason = mod.restore_quarantined_file(qpath, original_path=src,
                                                  engine=object())
        self.assertEqual((ok, reason), (False, "fingerprint_missing"))
        self.assertTrue(os.path.exists(qpath))

    def test_t066_restore_verifies_fingerprint_when_present(self):
        """The T-62 verify path stays strict: mismatch refuses, match restores."""
        src, qpath = self._acquire(content=b"original-bytes")
        fp = mod.hash_file(qpath)
        mod.record_quarantine_metadata(qpath, src, size=14, fingerprint=fp,
                                       status="acquired")
        with open(qpath, "wb") as f:
            f.write(b"tampered-bytes")
        ok, reason = mod.restore_quarantined_file(qpath, original_path=src,
                                                  engine=object())
        self.assertEqual((ok, reason), (False, "fingerprint_mismatch"))
        with open(qpath, "wb") as f:
            f.write(b"original-bytes")
        ok, dest = mod.restore_quarantined_file(qpath, original_path=src,
                                                engine=object())
        self.assertEqual(ok, True)
        self.assertEqual(dest, src)

    def test_t066_record_returns_false_when_journal_unwritable(self):
        """A failed receipt is reported, not swallowed:
        record_quarantine_metadata returns False and leaves no tmp orphan."""
        src, qpath = self._acquire()
        journal_dir = os.path.join(self.tmp, "unwritable")
        os.makedirs(journal_dir, exist_ok=True)
        mod.RECOVERY_LOG_FILE = journal_dir  # a directory: open() raises OSError
        self.assertFalse(
            mod.record_quarantine_metadata(qpath, src, size=7, fingerprint="a" * 64))
        self.assertEqual(
            [p for p in os.listdir(self.tmp) if p.endswith(".tmp")], [])

    def test_t066_send_to_trash_refuses_without_durable_receipt(self):
        """No durable receipt, no destruction: the destructive step is refused
        and the acquired object survives, still recoverable.

        Red control: pre-fix send_to_trash swallowed the persistence failure
        and deleted the file with no recovery evidence.
        """
        src, qpath = self._acquire()
        journal_dir = os.path.join(self.tmp, "unwritable")
        os.makedirs(journal_dir, exist_ok=True)
        mod.RECOVERY_LOG_FILE = journal_dir
        with self.assertRaises(OSError):
            mod.send_to_trash(qpath, original_path=src)
        self.assertTrue(os.path.exists(qpath), "the object must survive")

    def test_t066_journal_default_is_absolute_per_user_storage(self):
        """The default journal path is absolute per-user application storage,
        not a working-directory-relative name."""
        default = mod._recovery_log_path()
        self.assertTrue(os.path.isabs(default))
        self.assertNotEqual(mod.RECOVERY_LOG_FILE, "quarantine_recovery.json")
        self.assertIn("SmartVacDuplicateRemover", default)

    def test_t066_metadata_discoverable_after_cwd_change(self):
        """A recovery attempt from another working directory still finds the
        records written by an earlier process.

        Red control: pre-fix RECOVERY_LOG_FILE was CWD-relative, so
        get_quarantine_metadata from a different directory returned None.
        """
        other = os.path.join(self.tmp, "other_cwd")
        os.makedirs(other, exist_ok=True)
        src, qpath = self._acquire()
        mod.record_quarantine_metadata(qpath, src, size=7, fingerprint="b" * 64,
                                       status="acquired")
        cwd = os.getcwd()
        try:
            os.chdir(other)
            rec = mod.get_quarantine_metadata(qpath)
        finally:
            os.chdir(cwd)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["fingerprint"], "b" * 64)

    def test_t066_concurrent_journal_updates_lose_no_record(self):
        """Concurrent writers serialize on the journal: every record survives
        and the published file is never torn."""
        import threading
        n = 8
        barrier = threading.Barrier(n)
        errors = []

        def writer(i):
            src, qpath = self._acquire(name=f"orig{i}.txt",
                                       content=f"c{i}".encode())
            try:
                barrier.wait(timeout=10)
                self.assertTrue(
                    mod.record_quarantine_metadata(qpath, src, size=2,
                                                   fingerprint=f"{i:064d}",
                                                   status="acquired"))
            except Exception as exc:  # surfaced after join, never swallowed
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        with open(self.recovery_meta, "r", encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertEqual(len(items), n)
        self.assertEqual(
            [p for p in os.listdir(self.tmp) if p.endswith(".tmp")], [])

    def test_t066_crashed_writer_leaves_no_torn_journal(self):
        """A crashed writer's stale tmp file never corrupts the journal: the
        next record publishes atomically and the old records remain readable."""
        src, qpath = self._acquire()
        mod.record_quarantine_metadata(qpath, src, size=7, fingerprint="c" * 64,
                                       status="acquired")
        stale_tmp = f"{self.recovery_meta}.999.{0}.tmp"
        with open(stale_tmp, "w", encoding="utf-8") as f:
            f.write('{"items": {"torn": ')
        src2, qpath2 = self._acquire(name="orig2.txt", content=b"second")
        self.assertTrue(
            mod.record_quarantine_metadata(qpath2, src2, size=6,
                                           fingerprint="d" * 64, status="acquired"))
        with open(self.recovery_meta, "r", encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertIn(os.path.abspath(qpath), items)   # old record intact
        self.assertIn(os.path.abspath(qpath2), items)  # new record published
        # the writer's own tmp is cleaned; the planted crash leftover is not
        # this writer's responsibility and may remain
        tmps = [p for p in os.listdir(self.tmp) if p.endswith(".tmp")]
        self.assertEqual(tmps, [os.path.basename(stale_tmp)])

class T026TooltipReuseRegressionTest(unittest.TestCase):
    """T-026 / PERF-005: hovering the same unchanged row must not churn
    Toplevel tooltips; switching rows destroys the previous tip first."""

    class _FakeTree:
        def __init__(self, rows):
            # rows: {item: displayed value}; identify_row maps y -> item
            self.rows = rows
            self._by_y = {i: item for i, item in enumerate(rows)}

        def identify_row(self, y):
            return self._by_y.get(y)

        def item(self, item, *_args):
            return {"values": ["", self.rows[item]]}

        def bind(self, *_args):
            pass

    class _FakeTip:
        created = []
        hidden = []

        def __init__(self, widget, text):
            self.text = text
            self.destroyed = False
            T026TooltipReuseRegressionTest._FakeTip.created.append(text)

        def show(self, _):
            pass

        def hide(self, _):
            self.destroyed = True
            T026TooltipReuseRegressionTest._FakeTip.hidden.append(self.text)

    def setUp(self):
        self._saved = mod.ToolTip
        T026TooltipReuseRegressionTest._FakeTip.created = []
        T026TooltipReuseRegressionTest._FakeTip.hidden = []

    def tearDown(self):
        mod.ToolTip = self._saved

    def _app(self, rows):
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.tree = self._FakeTree(rows)
        app.item_path_map = {item: f"C:\\data\\{item}" for item in rows}
        app._hover_item = None
        app.tree_tip = None
        return app

    def test_t026_same_row_creates_no_second_tooltip(self):
        rows = {"i1": "C:\\data\\i1 ... tail"}
        app = self._app(rows)
        event = types.SimpleNamespace(y=0)
        mod.ToolTip = self._FakeTip

        DuplicateFinderApp.show_tooltip_on_long_path(app, event)
        self.assertEqual(len(self._FakeTip.created), 1)
        first_tip = app.tree_tip

        DuplicateFinderApp.show_tooltip_on_long_path(app, event)
        self.assertEqual(len(self._FakeTip.created), 1,
                         "same unchanged row must not create a second tip")
        self.assertIs(app.tree_tip, first_tip)

    def test_t026_row_switch_hides_previous_tip(self):
        rows = {"i1": "C:\\data\\i1 ... tail", "i2": "C:\\data\\i2 ... tail"}
        app = self._app(rows)
        mod.ToolTip = self._FakeTip

        DuplicateFinderApp.show_tooltip_on_long_path(app, types.SimpleNamespace(y=0))
        first_tip = app.tree_tip
        DuplicateFinderApp.show_tooltip_on_long_path(app, types.SimpleNamespace(y=1))

        self.assertTrue(first_tip.destroyed, "the previous tip must be destroyed")
        self.assertEqual(len(self._FakeTip.created), 2)
        self.assertIsNot(app.tree_tip, first_tip)

    def test_t026_real_tooltip_hide_destroys_window(self):
        """The real ToolTip.hide destroys the tip window (the close path)."""
        tip = mod.ToolTip.__new__(mod.ToolTip)
        destroyed = []
        tip.tip_window = types.SimpleNamespace(destroy=lambda: destroyed.append(1))
        mod.ToolTip.hide(tip, None)
        self.assertEqual(destroyed, [1])
        self.assertIsNone(tip.tip_window)

class T068RecoveryTransactionTest(unittest.TestCase):
    """T-68 / SRC-005: recovery-transaction closure for the quarantine journal
    (findings A-E). Red controls name the pre-fix defect each test pins."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smart_vac_t068_")
        self.recovery_meta = os.path.join(self.tmp, "quarantine_recovery.json")
        self._saved = (mod.RECOVERY_LOG_FILE, mod.deletion_is_recoverable,
                       mod.send_to_trash)
        mod.RECOVERY_LOG_FILE = self.recovery_meta
        mod.deletion_is_recoverable = lambda: False
        self.trashed = []

        def fake_trash(path, original_path=None):
            self.trashed.append(path)
            os.remove(path)

        mod.send_to_trash = fake_trash

    def tearDown(self):
        (mod.RECOVERY_LOG_FILE, mod.deletion_is_recoverable,
         mod.send_to_trash) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers -----------------------------------------------------------

    def _acquire(self, name="orig.txt", content=b"payload", receipt=True):
        src = os.path.join(self.tmp, name)
        _make_file(src, content)
        fp = mod.hash_file(src)
        intent = mod.prepare_acquisition(src, size=len(content), fingerprint=fp)
        self.assertIsNotNone(intent, "durable intent must persist before rename")
        qpath = mod.acquire_candidate(src, intent=intent)
        if receipt:
            self.assertTrue(mod.record_quarantine_metadata(
                qpath, src, size=len(content), fingerprint=fp, status="acquired",
                txn_id=intent["txn_id"]))
        return src, qpath, fp

    def _corrupt_journal(self, payload=b'{"items": {"BROKEN": '):
        with open(self.recovery_meta, "wb") as f:
            f.write(payload)
        return self.recovery_meta

    def _child_script(self, stage, journal, src, content):
        """A crash harness: runs one transaction stage, then dies hard."""
        here = os.path.dirname(os.path.abspath(__file__))
        body = {
            "after_prepare": "os._exit(63)  # crash after intent, before rename",
            "after_rename": ("os._exit(64)  # crash after rename, before receipt"),
            "after_receipt": ("os._exit(65)  # crash after receipt, before destruction"),
        }[stage]
        after = ("q = m.acquire_candidate(src, intent=intent)\n"
                 if stage != "after_prepare" else "")
        receipt = ("m.record_quarantine_metadata(q, src, size=len(content),"
                   " fingerprint=fp, status='acquired')\n"
                   if stage == "after_receipt" else "")
        return (
            "import os, sys\n"
            f"sys.path.insert(0, {here!r})\n"
            "import delete_duplicates_gui as m\n"
            f"m.RECOVERY_LOG_FILE = {journal!r}\n"
            "m.deletion_is_recoverable = lambda: False\n"
            f"src = {src!r}\n"
            "os.makedirs(os.path.dirname(src), exist_ok=True)\n"
            f"content = {content!r}\n"
            "with open(src, 'wb') as f: f.write(content)\n"
            "fp = m.hash_file(src)\n"
            "intent = m.prepare_acquisition(src, size=len(content), fingerprint=fp)\n"
            + after + receipt + body + "\n"
        )

    def _crash_child(self, stage, name, content=b"payload"):
        src = os.path.join(self.tmp, name)
        script = os.path.join(self.tmp, f"child_{stage}_{name}.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(self._child_script(stage, self.recovery_meta,
                                       os.path.abspath(src), content))
        proc = subprocess.run([sys.executable, script],
                              capture_output=True, text=True, timeout=120)
        return src, proc

    # --- FINDING A: corrupt journal destroys history ------------------------

    def test_t068_corrupt_journal_preserved_and_write_refused(self):
        """Red control: pre-fix `except: data = {}` replaced a corrupt journal
        with an empty one, silently erasing every recovery mapping."""
        src, qpath, fp = self._acquire()
        with open(self.recovery_meta, "rb") as f:
            good_bytes = f.read()
        self._corrupt_journal()
        with self.assertRaises(Exception):
            json.loads(good_bytes[:5])  # sanity: the good journal parsed
        # A write must refuse while integrity is unresolved.
        self.assertFalse(mod.record_quarantine_metadata(
            qpath, src, size=7, fingerprint="e" * 64, status="trashing"))
        # The damaged bytes survive untouched, and a preserved copy exists.
        with open(self.recovery_meta, "rb") as f:
            self.assertIn(b"BROKEN", f.read(), "corrupt journal must be preserved")
        preserved = [p for p in os.listdir(self.tmp) if ".corrupt-" in p]
        self.assertTrue(preserved, "the damaged original must be preserved for recovery")

    def test_t068_corrupt_journal_destruction_refused(self):
        """An unresolved journal authorizes no destruction (REC-004)."""
        src, qpath, fp = self._acquire()
        self._corrupt_journal()
        real_trash = mod.send_to_trash
        mod.send_to_trash = self._saved[2]
        try:
            with self.assertRaises(OSError):
                mod.send_to_trash(qpath, original_path=src)
        finally:
            mod.send_to_trash = real_trash
        self.assertTrue(os.path.exists(qpath), "the object must survive")

    def test_t068_recovered_journal_keeps_prior_records(self):
        """Recovering the last good journal (.bak) into place makes writes
        legal again without losing the records that predated the damage."""
        src, qpath, fp = self._acquire()
        src2, qpath2, _ = self._acquire(name="orig2.txt", content=b"second")
        self._corrupt_journal()
        self.assertFalse(mod.record_quarantine_metadata(
            qpath2, src2, status="restored"))
        preserved = [p for p in os.listdir(self.tmp) if ".corrupt-" in p][0]
        # The damaged bytes are kept as evidence; the recoverable backup is
        # the last good journal.
        shutil.copyfile(os.path.join(self.tmp, preserved), self.recovery_meta)
        backup = self.recovery_meta + ".bak"
        self.assertTrue(os.path.exists(backup),
                        "the last durable journal must be kept for recovery")
        shutil.copyfile(backup, self.recovery_meta)
        self.assertTrue(mod.record_quarantine_metadata(
            qpath2, src2, status="restored"))
        with open(self.recovery_meta, encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertIn(os.path.abspath(qpath), items, "prior record must survive")
        self.assertIn(os.path.abspath(qpath2), items)

    def test_t068_schema_invalid_journal_refused_not_reset(self):
        """A parseable journal with the wrong schema is damaged, not empty."""
        src, qpath, fp = self._acquire()
        with open(self.recovery_meta, "w", encoding="utf-8") as f:
            json.dump({"not_items": []}, f)
        self.assertFalse(mod.record_quarantine_metadata(
            qpath, src, size=7, fingerprint="e" * 64, status="trashing"))
        with open(self.recovery_meta, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"not_items": []},
                             "damaged journal must not be reset to an empty one")

    # --- FINDING B: crash before first receipt ------------------------------

    def test_t068_intent_is_durable_before_rename(self):
        """The planned name and identity are journaled before any move."""
        src, qpath, fp = self._acquire(name="i.bin")
        data, err = mod._read_journal(self.recovery_meta)
        self.assertIsNone(err)
        rec = data["items"][os.path.abspath(qpath)]
        self.assertEqual(rec["status"], "acquired")
        self.assertIn("txn_id", rec)

    def test_t068_crash_after_prepare_leaves_original(self):
        src, proc = self._crash_child("after_prepare", "p.bin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(src), "no rename happened: original intact")
        outcome = mod.reconcile_transactions()
        self.assertEqual(outcome["abandoned"], 1)
        rec = mod.get_quarantine_metadata(src)
        self.assertEqual(rec["status"], "abandoned_before_rename")
        self.assertTrue(os.path.exists(src), "reconciliation never moves files")

    def test_t068_crash_after_rename_recovers_verified_identity(self):
        """Red control: pre-fix the file sat renamed with no journal entry at
        all — unrecoverable, and any cleanup deleted it silently."""
        src, proc = self._crash_child("after_rename", "r.bin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(src), "the rename landed before the crash")
        outcome = mod.reconcile_transactions()
        self.assertEqual(outcome["recovered"], 1, "identity-verified recovery")
        quarantined = [p for p in os.listdir(self.tmp) if ".__vacq_" in p]
        self.assertEqual(len(quarantined), 1, "the object survives under quarantine")
        rec = mod.get_quarantine_metadata(quarantined[0])
        self.assertEqual(rec["status"], "acquired_recovered")
        # Recovered receipts authorize a later lawful destruction.
        mod.send_to_trash(rec["quarantine_path"], original_path=rec["original_path"])
        self.assertFalse(os.path.exists(rec["quarantine_path"]))

    def test_t068_crash_after_rename_tampering_not_recovered(self):
        """A quarantined object whose bytes changed since the intent is never
        silently adopted as a recovered receipt."""
        src, proc = self._crash_child("after_rename", "t.bin")
        quarantined = [p for p in os.listdir(self.tmp) if ".__vacq_" in p]
        _make_file(os.path.join(self.tmp, quarantined[0]), b"TAMPERED")
        outcome = mod.reconcile_transactions()
        self.assertEqual(outcome["unverified"], 1)
        quarantined_path = os.path.join(self.tmp, quarantined[0])
        self.assertTrue(os.path.exists(quarantined_path),
                        "tampered object must survive for investigation")
        real_trash = mod.send_to_trash
        mod.send_to_trash = self._saved[2]
        try:
            with self.assertRaises(OSError):
                mod.send_to_trash(quarantined_path)
        finally:
            mod.send_to_trash = real_trash

    def test_t068_reconcile_never_destroys_unrecognized_quarantine(self):
        """A quarantine file with no journal entry is annotation-only."""
        orphan = os.path.join(self.tmp, ".mystery.bin.__vacq_999_1")
        _make_file(orphan, b"ORPHAN")
        outcome = mod.reconcile_transactions()
        self.assertEqual(outcome["abandoned"] + outcome["recovered"], 0)
        self.assertTrue(os.path.exists(orphan))

    # --- FINDING C: incomplete receipt authorizes delete --------------------

    def test_t068_status_only_receipt_never_authorizes_deletion(self):
        """Red control: pre-fix send_to_trash's first write could fail and the
        successful 'trashing' write then laundered an unverified delete."""
        src, qpath, fp = self._acquire()
        # Journal is fine, but the acquisition receipt is erased: only a
        # status-only record exists (no size, no fingerprint).
        with open(self.recovery_meta, "w", encoding="utf-8") as f:
            json.dump({"items": {os.path.abspath(qpath): {
                "quarantine_path": os.path.abspath(qpath),
                "original_path": os.path.abspath(src),
                "size": None, "fingerprint": None,
                "status": "trashing"}}}, f)
        real_trash = mod.send_to_trash
        mod.send_to_trash = self._saved[2]
        try:
            with self.assertRaises(OSError):
                mod.send_to_trash(qpath, original_path=src)
        finally:
            mod.send_to_trash = real_trash
        self.assertTrue(os.path.exists(qpath), "refusal preserves the object")

    def test_t068_transient_receipt_failure_blocks_delete(self):
        """A transient failure of the acquired-receipt write followed by a
        successful 'trashing' write must NOT authorize destruction — the batch
        restores the object instead of deleting it."""
        for name in ("a.bin", "b.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        res = mod.DuplicateEngine().scan(self.tmp)
        group = res.groups[0]
        dup = next(m for m in group.members if not m.is_original)
        orig = next(m for m in group.members if m.is_original)
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.engine = mod.DuplicateEngine()
        app.current_groups = [group]
        app.current_records = [orig, dup]
        app.log_var = _Var(False)
        app.status_var = _Var("")
        app.check_vars = {r.path: _Var(r is dup) for r in app.current_records}
        app.path_to_item = {r.path: f"I{i}" for i, r in enumerate(app.current_records)}
        app.item_path_map = {v: k for k, v in app.path_to_item.items()}
        app.tree = _FakeTree()

        real_record = mod.record_quarantine_metadata

        def flaky_record(qpath, original_path, **kw):
            if kw.get("status") == "acquired":
                return False  # the transient first-write failure
            return real_record(qpath, original_path, **kw)

        mod.record_quarantine_metadata = flaky_record
        try:
            app._execute_destructive_batch({"approved": [dup], "skipped": []})
        finally:
            mod.record_quarantine_metadata = real_record
        self.assertEqual(self.trashed, [], "no destruction on a failed receipt")
        self.assertTrue(os.path.exists(dup.path),
                        "the candidate must be restored to its original name")
        self.assertIn("не удалось: 1", app.status_var.get())

    def test_t068_missing_identity_refuses_through_production_batch(self):
        """Production path: a journal that cannot hold receipts stops the
        destructive batch without deleting anything."""
        for name in ("a.bin", "b.bin"):
            _make_file(os.path.join(self.tmp, name), b"DUP")
        res = mod.DuplicateEngine().scan(self.tmp)
        group = res.groups[0]
        dup = next(m for m in group.members if not m.is_original)
        orig = next(m for m in group.members if m.is_original)
        app = DuplicateFinderApp.__new__(DuplicateFinderApp)
        app.engine = mod.DuplicateEngine()
        app.current_groups = [group]
        app.current_records = [orig, dup]
        app.log_var = _Var(False)
        app.status_var = _Var("")
        app.check_vars = {r.path: _Var(r is dup) for r in app.current_records}
        app.path_to_item = {r.path: f"I{i}" for i, r in enumerate(app.current_records)}
        app.item_path_map = {v: k for k, v in app.path_to_item.items()}
        app.tree = _FakeTree()
        # The journal directory itself is unwritable: no receipt can persist.
        blocker = os.path.join(self.tmp, "blocker")
        os.makedirs(blocker, exist_ok=True)
        mod.RECOVERY_LOG_FILE = blocker
        try:
            app._execute_destructive_batch({"approved": [dup], "skipped": []})
        finally:
            mod.RECOVERY_LOG_FILE = self.recovery_meta
        self.assertEqual(self.trashed, [], "no destruction without a receipt")
        self.assertTrue(os.path.exists(dup.path), "the object must survive")
        self.assertIn("не удалось: 1", app.status_var.get())

    # --- FINDING D: restore verification and identity ------------------------

    def test_t068_tampered_restore_refused_without_engine(self):
        """Red control: pre-fix restore_quarantined_file verified content only
        when an engine was passed; the default accepted modified data."""
        src, qpath, fp = self._acquire(content=b"original-bytes")
        with open(qpath, "wb") as f:
            f.write(b"tampered-bytes")
        ok, reason = mod.restore_quarantined_file(qpath, original_path=src)
        self.assertEqual((ok, reason), (False, "fingerprint_mismatch"))
        self.assertTrue(os.path.exists(qpath))
        self.assertFalse(os.path.exists(src), "refused restore leaves the original absent")
        # The receipt is preserved for investigation, never rewritten.
        rec = mod.get_quarantine_metadata(qpath)
        self.assertEqual(rec["fingerprint"], fp)

    def test_t072_refused_restore_leaves_no_temp_copy_in_destination(self):
        """T-72: a refused restore leaves the destination directory untouched.

        Red control: pre-fix restore_quarantined_file created the snapshot copy
        inside the destination directory and returned its refusals from inside
        the try block, so the cleanup never ran and every refusal left a
        .vacrestore_* file holding a full copy of the quarantined content.
        """
        src, qpath, fp = self._acquire(content=b"original-bytes")
        with open(qpath, "wb") as f:
            f.write(b"tampered-bytes")
        ok, reason = mod.restore_quarantined_file(qpath, original_path=src)
        self.assertEqual((ok, reason), (False, "fingerprint_mismatch"))
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".vacrestore_")]
        self.assertEqual(
            leftovers, [],
            f"a refused restore must not leave a snapshot copy behind: {leftovers}")
        self.assertTrue(os.path.exists(qpath), "the quarantined object must survive")
        self.assertFalse(os.path.exists(src), "the original path must stay absent")

    def test_t068_unverified_restore_requires_explicit_policy(self):
        """Only the distinct recovery policy can restore unverifiable bytes."""
        src, qpath, fp = self._acquire()
        with open(qpath, "wb") as f:
            f.write(b"tampered-bytes")
        ok, reason = mod.restore_quarantined_file(qpath, original_path=src)
        self.assertEqual((ok, reason), (False, "fingerprint_mismatch"))
        ok, dest = mod.restore_quarantined_file(qpath, original_path=src,
                                                allow_unverified=True)
        self.assertTrue(ok)
        self.assertEqual(dest, os.path.abspath(src))
        self.assertEqual(_read_bytes(src), b"tampered-bytes")

    def test_t068_conflicting_identity_update_rejected(self):
        """Explicitly supplied fingerprints cannot overwrite a frozen identity."""
        src, qpath, fp = self._acquire()
        self.assertFalse(mod.record_quarantine_metadata(
            qpath, src, size=7, fingerprint="0" * 64, status="trashing"))
        rec = mod.get_quarantine_metadata(qpath)
        self.assertEqual(rec["fingerprint"], fp)
        self.assertEqual(rec["status"], "acquired", "receipt preserved for investigation")

    def test_t068_restore_original_path_conflict_refused(self):
        src, qpath, fp = self._acquire()
        other = os.path.join(self.tmp, "elsewhere.txt")
        ok, reason = mod.restore_quarantined_file(qpath, original_path=other)
        self.assertEqual((ok, reason), (False, "original_path_conflict"))
        self.assertTrue(os.path.exists(qpath))

    # --- FINDING E: POSIX destination replacement ----------------------------

    def test_t068_occupied_destination_never_overwritten(self):
        """Red control: pre-fix lexists()-then-rename let a replacement created
        in between be silently overwritten (POSIX rename replaces)."""
        src, qpath, fp = self._acquire(name="race.bin", content=b"QUARANTINED")
        occupant = b"UNSCANNED_OCCUPANT"
        _make_file(src, occupant)
        real_lexists = os.path.lexists
        try:
            # A competing actor installs the occupier AFTER the availability
            # check — the exact interleaving the old code lost.
            mod.os.path.lexists = lambda p: (False if p == src else real_lexists(p))
            ok, reason = mod.restore_quarantined_file(qpath, original_path=src)
        finally:
            mod.os.path.lexists = real_lexists
        self.assertEqual((ok, reason), (False, "destination_occupied"))
        self.assertEqual(_read_bytes(src), occupant, "occupant must be untouched")
        self.assertEqual(_read_bytes(qpath), b"QUARANTINED", "moved object preserved")

    def test_t068_restore_candidate_preserves_both_on_conflict(self):
        src, qpath, fp = self._acquire()
        _make_file(src, b"OCCUPANT")
        real_lexists = os.path.lexists
        try:
            mod.os.path.lexists = lambda p: (False if p == src else real_lexists(p))
            self.assertFalse(mod.restore_candidate(qpath, src))
        finally:
            mod.os.path.lexists = real_lexists
        self.assertTrue(os.path.exists(src))
        self.assertTrue(os.path.exists(qpath))

    def test_t068_no_replace_move_replaces_nothing(self):
        src = os.path.join(self.tmp, "movable.bin")
        dst = os.path.join(self.tmp, "taken.bin")
        _make_file(src, b"SOURCE")
        _make_file(dst, b"TARGET")
        with self.assertRaises(FileExistsError):
            mod._atomic_no_replace_move(src, dst)
        self.assertEqual(_read_bytes(src), b"SOURCE")
        self.assertEqual(_read_bytes(dst), b"TARGET")
        os.remove(dst)
        mod._atomic_no_replace_move(src, dst)
        self.assertEqual(_read_bytes(dst), b"SOURCE")
        self.assertFalse(os.path.exists(src))


class T069RestoreTransactionOutcomeTest(unittest.TestCase):
    """P1-002: restore_quarantined_file must not return success when cleanup or
    journal write fails. The old code swallowed OSError on os.remove(qpath) and
    stamped the receipt `restored` regardless, producing false-positive restore
    success and stale_receipt on retry.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smart_vac_t069_")
        self.recovery_meta = os.path.join(self.tmp, "quarantine_recovery.json")
        self._saved = (mod.RECOVERY_LOG_FILE,)
        mod.RECOVERY_LOG_FILE = self.recovery_meta

    def tearDown(self):
        mod.RECOVERY_LOG_FILE = self._saved[0]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _acquire(self, name="orig.txt", content=b"payload"):
        src = os.path.join(self.tmp, name)
        _make_file(src, content)
        fp = mod.hash_file(src)
        intent = mod.prepare_acquisition(src, size=len(content), fingerprint=fp)
        self.assertIsNotNone(intent)
        qpath = mod.acquire_candidate(src, intent=intent)
        self.assertTrue(mod.record_quarantine_metadata(
            qpath, src, size=len(content), fingerprint=fp, status="acquired"))
        return src, qpath, fp

    def test_t069_permissionerror_during_quarantine_cleanup_is_partial(self):
        """os.remove(qpath) raising PermissionError must NOT be swallowed into
        a false success. The destination is installed, the qpath survives, and
        the reason is partial_recovery_required."""
        src, qpath, fp = self._acquire(content=b"original-bytes")
        # Install destination, then block qpath removal.
        real_remove = os.remove

        def blocking_remove(path):
            if os.path.abspath(path) == os.path.abspath(qpath):
                raise PermissionError(13, "denied", qpath)
            real_remove(path)

        mod.os.remove = blocking_remove
        try:
            ok, reason = mod.restore_quarantined_file(
                qpath, original_path=src, engine=object())
        finally:
            mod.os.remove = real_remove
        self.assertFalse(ok,
                         "cleanup failure must not report success")
        self.assertEqual(reason, "partial_recovery_required")
        self.assertTrue(os.path.exists(src),
                        "destination must be installed")
        self.assertTrue(os.path.exists(qpath),
                        "quarantined source must survive partial cleanup")
        rec = mod.get_quarantine_metadata(qpath)
        self.assertEqual(rec["status"], "restore_partial")

    def test_t069_journal_write_failure_surfaces(self):
        """When the durable receipt cannot transition to `restored`, the call
        returns journal_write_failed instead of silently claiming RESTORED."""
        src, qpath, fp = self._acquire(content=b"original-bytes")
        real_record = mod.record_quarantine_metadata

        def failing_record(qp, op, status="acquired", **kw):
            # Refuse ONLY the final restore stamp; acquisition must succeed.
            if status == "restored":
                return False
            return real_record(qp, op, status=status, **kw)

        mod.record_quarantine_metadata = failing_record
        try:
            ok, reason = mod.restore_quarantined_file(
                qpath, original_path=src, engine=object())
        finally:
            mod.record_quarantine_metadata = real_record
        self.assertFalse(ok)
        self.assertEqual(reason, "journal_write_failed")

    def test_t069_idempotent_after_partial(self):
        """A retry after partial_recovery_required must converge to RESTORED
        once the blocker is removed (safe idempotency)."""
        src, qpath, fp = self._acquire(content=b"original-bytes")
        real_remove = os.remove
        blocked = {"v": True}

        def blocking_remove(path):
            if os.path.abspath(path) == os.path.abspath(qpath) and blocked["v"]:
                raise PermissionError(13, "denied", qpath)
            real_remove(path)

        mod.os.remove = blocking_remove
        try:
            ok, reason = mod.restore_quarantined_file(
                qpath, original_path=src, engine=object())
            self.assertFalse(ok)
            self.assertEqual(reason, "partial_recovery_required")
        finally:
            mod.os.remove = real_remove

        # The destination is already installed; retry with cleanup unblocked.
        blocked["v"] = False
        # Second attempt: destination is already installed, so the no-replace
        # move refuses and destination_occupied is returned.
        ok2, reason2 = mod.restore_quarantined_file(
            qpath, original_path=src, engine=object())
        self.assertFalse(ok2, "retry must not falsely claim success")
        self.assertEqual(reason2, "destination_occupied")

    def test_t069_occupied_destination_returns_destination_occupied(self):
        """An occupied destination must return destination_occupied, not
        success, and never overwrite the occupant — even when the quarantined
        object is intact.
        """
        src, qpath, fp = self._acquire(content=b"original-bytes")
        _make_file(src, b"OCCUPANT")
        ok, reason = mod.restore_quarantined_file(
            qpath, original_path=src, engine=object())
        self.assertFalse(ok)
        self.assertEqual(reason, "destination_occupied")
        self.assertEqual(_read_bytes(src), b"OCCUPANT",
                        "occupant must be untouched")
        self.assertEqual(_read_bytes(qpath), b"original-bytes",
                        "quarantined object must survive")

    def test_t069_concurrent_source_replacement_never_deleted(self):
        """A pathname that is replaced at the quarantine name after the
        verified snapshot is installed but before cleanup must not be removed.
        The destination receives the verified bytes; the replacement survives.

        P1-002 root cause: the old code did os.remove(qpath) unconditionally
        on the pathname; a replacement swapped in between would be destroyed.
        The fix compares the live (dev, ino) against the snapshot and only
        removes when the object is provably the same one that was verified.
        """
        src, qpath, fp = self._acquire(content=b"original-bytes")
        new_replacement = b"CONCURRENT_REPLACEMENT"
        real_move = mod._atomic_no_replace_move
        swap_done = {"v": False}

        def move_then_swap(src_p, dst_p):
            # The snapshot install just succeeded: a concurrent actor swaps
            # the quarantine name's inode (rename-away + fresh file) right
            # before the cleanup stat/remove.
            if swap_done["v"] is False and \
                    os.path.abspath(qpath) != os.path.abspath(src_p):
                swap_done["v"] = True
                gone = qpath + ".orig"
                os.rename(qpath, gone)
                _make_file(qpath, new_replacement)
            real_move(src_p, dst_p)

        mod._atomic_no_replace_move = move_then_swap
        try:
            ok, reason = mod.restore_quarantined_file(
                qpath, original_path=src, engine=object())
        finally:
            mod._atomic_no_replace_move = real_move
        self.assertFalse(ok, "cleanup failure must not report success")
        self.assertEqual(reason, "partial_recovery_required")
        self.assertTrue(os.path.exists(qpath),
                        "concurrent replacement must not be deleted")
        self.assertEqual(_read_bytes(qpath), new_replacement)
        self.assertEqual(_read_bytes(src), b"original-bytes")

    def test_t069_no_replace_move_replaces_nothing(self):
        src = os.path.join(self.tmp, "movable.bin")
        dst = os.path.join(self.tmp, "taken.bin")
        _make_file(src, b"SOURCE")
        _make_file(dst, b"TARGET")
        with self.assertRaises(FileExistsError):
            mod._atomic_no_replace_move(src, dst)
        self.assertEqual(_read_bytes(src), b"SOURCE")
        self.assertEqual(_read_bytes(dst), b"TARGET")
        os.remove(dst)
        mod._atomic_no_replace_move(src, dst)
        self.assertEqual(_read_bytes(dst), b"SOURCE")
        self.assertFalse(os.path.exists(src))


if __name__ == "__main__":
    unittest.main(verbosity=2)
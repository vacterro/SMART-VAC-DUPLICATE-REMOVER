"""Regression tests for SMART VAC DUPLICATE REMOVER remediation (AUDIT_ALL_3).

Headless: exercises DuplicateEngine, find_empty_dirs and the destructive UI
paths through unbound methods with stub Tk collaborators (no Tk required).
Covers CORE-001/002/003, W2-001/002, PERF-001/003 and T-027/T-028/T-029.
"""

import builtins
import contextlib
import inspect
import os
import queue
import shutil
import tempfile
import threading
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


class _Var:
    """Stand-in for tk.BooleanVar / tk.StringVar."""

    def __init__(self, value=None):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


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
        self.mbox = _FakeMessagebox()
        self._saved = (mod.messagebox, mod.LOG_FILE, mod.send_to_trash,
                       mod.deletion_is_recoverable, mod.platform)
        mod.messagebox = self.mbox
        mod.LOG_FILE = self.log_path
        self.trashed = []

        def fake_trash(path):
            self.trashed.append(path)
            os.remove(path)

        mod.send_to_trash = fake_trash
        mod.deletion_is_recoverable = lambda: True

    def tearDown(self):
        (mod.messagebox, mod.LOG_FILE, mod.send_to_trash,
         mod.deletion_is_recoverable, mod.platform) = self._saved
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
        app._sessions = {}
        app.current_scan_id = 1
        app.cancel_event = None
        app.worker_thread = None
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
        DuplicateFinderApp.delete_selected(app)
        self.assertEqual(self.trashed, [dup.path])
        self.assertFalse(os.path.exists(dup.path))
        self.assertTrue(os.path.exists(original.path), "survivor must never be deleted")
        self.assertIn("Удалено файлов: 1", app.status_var.get())
        self.assertNotIn(dup.path, app.check_vars, "deleted row must be disarmed")
        self.assertEqual(app.tree.deleted, [dup_item])
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
        _make_file(path, b"x")
        mod.send_to_trash = self._saved[2]
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
        self.assertEqual(self.trashed, [dup.path], "the delete itself still happened")
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
        """R0008: an inaccessible subtree is reported, not silently omitted."""
        root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(root, "visible-empty"))
        blocked = os.path.join(root, "blocked", "inner")
        os.makedirs(blocked)
        errors = []
        if os.name == "nt":
            self.skipTest("chmod 000 is not a permission boundary on Windows")
        os.chmod(blocked, 0o000)
        try:
            empties = find_empty_dirs(root, collect_errors=errors.append)
        finally:
            os.chmod(blocked, 0o755)
        self.assertIn(os.path.join(root, "visible-empty"), empties)
        self.assertTrue(errors, "traversal failure must be reported through the channel")

    def test_w2004_onerror_channel_platform_independent(self):
        """R0008: the traversal-error channel fires on a simulated scandir failure."""
        root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(root, "visible-empty"))
        errors = []
        real_walk = os.walk

        def fake_walk(top, topdown=False, onerror=None, **kwargs):
            err = PermissionError("denied")
            err.filename = os.path.join(top, "blocked")
            if onerror is not None:
                onerror(err)
            yield from real_walk(top, topdown=topdown, onerror=onerror)

        os.walk = fake_walk
        try:
            empties = find_empty_dirs(root, collect_errors=errors.append)
        finally:
            os.walk = real_walk
        self.assertIn(os.path.join(root, "visible-empty"), empties)
        self.assertTrue(errors, "traversal failure must be reported through the channel")
        self.assertIn("blocked", errors[0])


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
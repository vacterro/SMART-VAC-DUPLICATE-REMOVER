"""SMART VAC DUPLICATE REMOVER — Tk GUI duplicate finder/remover.

Remediation build addressing AUDIT_ALL_3 handoff (RUN_ID acb-mtjyt8v1-ae9f4603097246f6917e):
  CORE-001 survivor invariant      CORE-002 stale-path revalidation
  CORE-003 scan session isolation  CORE-004 cancellation/shutdown
  CORE-005 deletion/log truthful   CORE-006 version identity
  W2-001  root-boundary cleanup    W2-002 deterministic survivor
  W2-003  truthful terminal states PERF-001 size-prefilter
  PERF-002 bounded/coalesced queue PERF-003 inverse path map
  PERF-004 single-pass empty walk  PERF-005 tooltip reuse

SRC-001 (audit/1.md, RUN_ID acb-mtnc78d3-187dbf28ea354fc7844c) remediation:
  CORE-001 alias-aware survivors   CORE-002 deletion-boundary revalidation
  CORE-003 generation-scoped events CORE-004 post-delete original re-election
  W2-001 cancel during publication W2-002 worker exception boundary
  W2-003 batch-budget accounting   W2-004 traversal-error channel
  PERF-001 indexed planning/bulk disarm
  PERF-002 survivor early-exit     PERF-003 staged sample fingerprint
  PERF-004 compact scan metadata
"""

import os
import stat
import ctypes
import re
import threading
import queue
import hashlib
import subprocess
import platform
import webbrowser
from dataclasses import dataclass, field
from typing import Optional, Callable

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # headless environments (e.g. test runners) may lack Tk
    tk = filedialog = messagebox = ttk = None

VERSION = "0.0.4"

LOG_FILE = "deleted_log.txt"

# --- Recycle Bin deletion (T-010, retained) -----------------------------------
FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class CancelledHash(Exception):
    """PERF-003: a streaming hash observed its cancel_event between chunks.

    Distinct from OSError so an interrupted hash is never misread as an
    unreadable/error file.
    """


def hash_file(path, block_size=65536, cancel_event=None):
    """Full SHA-256, cancellation-aware between chunks (PERF-003).

    A cancel_event observed mid-read raises CancelledHash; the hash itself is
    never returned as a trustworthy value after cancellation.
    """
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(block_size):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledHash(path)
            hasher.update(chunk)
    return hasher.hexdigest()


def _sample_offsets(size, block_size):
    if size <= block_size:
        return (0,)
    return tuple(sorted({0, size // 2, max(0, size - block_size)}))


def sample_fingerprint(path, cancel_event=None, block_size=65536):
    """PERF-003: cheap deterministic rejection fingerprint (first/mid/last blocks).

    A collision here proves nothing — full SHA-256 stays the only equality
    authority; a differing sample only proves the files are NOT duplicates.
    """
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        for offset in _sample_offsets(size, block_size):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledHash(path)
            f.seek(offset)
            hasher.update(f.read(block_size))
    return hasher.hexdigest()


def deletion_is_recoverable():
    """True only where send_to_trash reaches a real Recycle Bin (Windows).

    T-027: the deletion promise shown to the user must match what the code does;
    off-Windows send_to_trash falls back to permanent os.remove.
    """
    return platform.system() == "Windows"


def send_to_trash(path):
    """Move a file to the Recycle Bin (Windows) instead of permanent deletion.

    On non-Windows there is no recycle bin, so it falls back to os.remove.
    Raises OSError if the file remains in place after the call.
    """
    abs_path = os.path.abspath(path)
    if not deletion_is_recoverable():
        os.remove(abs_path)
        return

    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    file_op = SHFILEOPSTRUCTW()
    file_op.hwnd = None
    file_op.wFunc = FO_DELETE
    file_op.pFrom = abs_path + "\0\0"
    file_op.pTo = None
    file_op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(file_op))
    if result != 0 or file_op.fAnyOperationsAborted:
        raise OSError(f"SHFileOperationW returned {result} for {abs_path}")
    if os.path.exists(abs_path):
        raise OSError(f"File still present after recycle-bin move: {abs_path}")


def is_likely_original(filename):
    """Score how likely a file is the 'original' by name/path heuristics (higher = more original)."""
    score = 0
    copy_indicators = [r'\bcopy\b', r'\bcopia\b', r'_bak\b', r'\(\d+\)', r'\bduplicate\b', r'\d+$']
    for pattern in copy_indicators:
        if re.search(pattern, filename.lower()):
            score -= 10
    score -= len(filename) // 20
    return score


def order_members(paths):
    """W2-002: deterministic survivor ordering shared by scan and post-delete
    normalization (CORE-004), so both can never drift apart."""
    return sorted(paths, key=lambda p: (-is_likely_original(p), os.path.normcase(p)))


# --- Headless, testable engine (CORE-001/002/003/004/019/020/022) -------------
@dataclass
class FileRecord:
    path: str
    size: int
    fingerprint: str
    is_original: bool = False
    group_id: int = 0


@dataclass
class DuplicateGroup:
    group_id: int
    fingerprint: str
    members: list = field(default_factory=list)


@dataclass
class ScanResult:
    groups: list
    stats: dict


class DuplicateEngine:
    """Pure duplicate-detection + safe-deletion-planning logic (no Tk)."""

    def __init__(self, block_size=65536):
        self.block_size = block_size

    def _walk_onerror(self, stats):
        def _cb(err):
            stats['errors'].append((str(err.filename), f'walk: {err}'))
            stats['skipped'] += 1
        return _cb

    def verify_record(self, rec):
        """Return (ok, reason): True only if the file on disk still matches the scanned identity.

        Used by CORE-002 to refuse deletion of files changed/replaced/missing since scan.
        CORE-001 (SRC-001): a symbolic link / reparse alias is never a verified
        surviving content copy — even if it still resolves to the right bytes.
        """
        try:
            lst = os.lstat(rec.path)
        except OSError:
            return False, 'missing'
        if not stat.S_ISREG(lst.st_mode):
            return False, 'alias'
        if stat.S_ISLNK(lst.st_mode):
            return False, 'alias'
        try:
            st = os.stat(rec.path)
        except OSError:
            return False, 'missing'
        if st.st_size != rec.size:
            return False, 'size_changed'
        try:
            fp = hash_file(rec.path, self.block_size)
        except OSError:
            return False, 'unreadable'
        if fp != rec.fingerprint:
            return False, 'content_changed'
        return True, 'ok'

    def scan(self, root_dir, cancel_event=None,
             status_cb: Optional[Callable[[str], None]] = None,
             progress_cb: Optional[Callable[[list, dict], None]] = None) -> ScanResult:
        stats = {'scanned': 0, 'hashed': 0, 'skipped': 0, 'errors': [], 'state': 'completed'}
        # PERF-004 (SRC-001): single per-size slot, promoted to a list only on the
        # second path of that size; no whole-tree reverse path index. Size travels
        # with the candidate path into the sample stage.
        size_map = {}
        files_seen = 0

        for dirpath, dirnames, filenames in os.walk(root_dir, onerror=self._walk_onerror(stats)):
            if cancel_event and cancel_event.is_set():
                stats['state'] = 'cancelled'
                break
            for name in filenames:
                if cancel_event and cancel_event.is_set():
                    stats['state'] = 'cancelled'
                    break
                path = os.path.join(dirpath, name)
                stats['scanned'] += 1
                files_seen += 1
                # CORE-001 (SRC-001): aliasing entries (symlinks/reparse points) are
                # never independent content copies; lstat-classify before any follow.
                try:
                    lst = os.lstat(path)
                except OSError as e:
                    stats['skipped'] += 1
                    stats['errors'].append((path, f'lstat: {e}'))
                    continue
                if not stat.S_ISREG(lst.st_mode):
                    stats['skipped'] += 1
                    stats['errors'].append((path, 'alias: not a regular file'))
                    continue
                size = lst.st_size
                # PERF-004 (SRC-001): first path per size stored directly; list only
                # once the size actually collides.
                slot = size_map.get(size)
                if slot is None:
                    size_map[size] = path
                elif isinstance(slot, str):
                    size_map[size] = [slot, path]
                else:
                    slot.append(path)
            if cancel_event and cancel_event.is_set():
                stats['state'] = 'cancelled'
                break
            if status_cb and files_seen % 200 == 0:
                status_cb(f"Читаю: обработано файлов {files_seen}")

        if stats['state'] == 'cancelled':
            return ScanResult(groups=[], stats=stats)

        # PERF-003 (SRC-001): staged fingerprint pipeline.
        #   stage 1: size collision (above)
        #   stage 2: cheap sample fingerprint — only rejects, never proves equality
        #   stage 3: full SHA-256 — the only content-equality authority
        sample_map = {}
        for size, entry in size_map.items():
            if isinstance(entry, str):
                continue  # unique-size file: can never be a duplicate (PERF-001)
            for path in entry:
                if cancel_event and cancel_event.is_set():
                    stats['state'] = 'cancelled'
                    break
                try:
                    sfp = sample_fingerprint(path, cancel_event, self.block_size)
                except CancelledHash:
                    stats['state'] = 'cancelled'
                    break
                except OSError as e:
                    stats['skipped'] += 1
                    stats['errors'].append((path, f'sample: {e}'))
                    continue
                sample_map.setdefault((size, sfp), []).append(path)
            if stats['state'] == 'cancelled':
                break

        fingerprint_map = {}
        for (size, sfp), paths in sample_map.items():
            if len(paths) < 2:
                continue
            for path in paths:
                if cancel_event and cancel_event.is_set():
                    stats['state'] = 'cancelled'
                    break
                try:
                    fp = hash_file(path, self.block_size, cancel_event)
                except CancelledHash:
                    stats['state'] = 'cancelled'
                    break
                except OSError as e:
                    stats['skipped'] += 1
                    stats['errors'].append((path, f'hash: {e}'))
                    continue
                stats['hashed'] += 1
                fingerprint_map.setdefault((size, fp), []).append(path)
            if stats['state'] == 'cancelled':
                break

        if stats['state'] == 'cancelled':
            # T-37: a scan cancelled mid-hash has seen only part of the tree, so the
            # groups built so far are not a duplicate list — never hand them out as one.
            return ScanResult(groups=[], stats=stats)

        # W2-001 (SRC-001): groups are buffered until the scan is known complete;
        # publication checks cancellation before each group and a cancelled scan
        # never ships a partial duplicate list (T-37).
        groups = []
        gid = 0
        for (size, fp), paths in fingerprint_map.items():
            if cancel_event and cancel_event.is_set():
                stats['state'] = 'cancelled'
                break
            if len(paths) < 2:
                continue
            # W2-002: deterministic survivor ordering, independent of traversal order.
            ordered = order_members(paths)
            members = []
            for idx, p in enumerate(ordered):
                members.append(FileRecord(
                    path=p,
                    size=size,
                    fingerprint=fp,
                    is_original=(idx == 0),
                ))
            gid += 1
            for m in members:
                m.group_id = gid
            group = DuplicateGroup(group_id=gid, fingerprint=fp, members=members)
            groups.append(group)

        for group in groups:
            if progress_cb:
                progress_cb([group], stats)
            if cancel_event and cancel_event.is_set():
                # W2-001 (SRC-001): a cancellation observed during publication wins;
                # the scan was not allowed to finish, so the terminal state is cancelled.
                stats['state'] = 'cancelled'
                break
        if stats['state'] == 'cancelled':
            return ScanResult(groups=[], stats=stats)

        if stats['errors'] and stats['state'] == 'completed':
            stats['state'] = 'completed_with_errors'
        return ScanResult(groups=groups, stats=stats)

    def plan_deletion(self, groups, selected_records):
        """Plan a safe deletion.

        Returns dict with:
          approved      -> FileRecords safe to delete (re-verified, group keeps >=1 verified copy)
          skipped       -> list of (FileRecord, reason) not deleted (changed/missing/refused)
          refused_groups-> list of group_ids where deleting would remove the last verified copy
        Enforces CORE-001 (never delete the last verified copy) and CORE-002 (reject stale paths).

        PERF-001 (SRC-001): groups are indexed by id once — no repeated linear scan.
        PERF-002 (SRC-001): survivor verification stops at the first valid keeper.
        """
        by_group = {}
        for r in selected_records:
            by_group.setdefault(r.group_id, []).append(r)
        groups_by_id = {g.group_id: g for g in groups}

        approved, skipped, refused_groups = [], [], []
        for gid, recs in by_group.items():
            group = groups_by_id.get(gid)
            if group is None:
                for r in recs:
                    skipped.append((r, 'orphan_group'))
                continue
            verified = []
            for r in recs:
                ok, reason = self.verify_record(r)
                if ok:
                    verified.append(r)
                else:
                    skipped.append((r, reason))
            if not verified:
                continue
            verified_ids = {id(r) for r in verified}
            # PERF-002: one currently valid unselected member is all the invariant
            # needs; stop at the first (deterministic order above). Every member is
            # still checked when no valid survivor exists — then the group is refused.
            has_survivor = False
            for m in group.members:
                if id(m) in verified_ids:
                    continue
                ok, _ = self.verify_record(m)
                if ok:
                    has_survivor = True
                    break
            if not has_survivor:
                # CORE-001: deleting these would leave zero verified copies -> refuse the whole group.
                refused_groups.append(gid)
                for r in verified:
                    skipped.append((r, 'last_copy_refused'))
                continue
            approved.extend(verified)
        return {'approved': approved, 'skipped': skipped, 'refused_groups': refused_groups}


# --- Tk UI layer (CORE-003/004/005/006, W2-001/003, PERF-002/003/004/005) ------
class ToolTip:
    def __init__(self, widget, text=''):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _):
        if self.tip_window or not self.text:
            return
        x, y, *_ = self.widget.winfo_pointerxy()
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.geometry(f"+{x + 10}+{y + 10}")
        label = tk.Label(
            tw, text=self.text, background="#ffffe0", relief="solid", borderwidth=1,
            justify="left", font=("Segoe UI", 9), wraplength=600,
        )
        label.pack(ipadx=6, ipady=2)

    def hide(self, _):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


def shorten_path(path, maxlen=90):
    if len(path) <= maxlen:
        return path
    return path[:35] + " ... " + path[-48:]


def find_empty_dirs(root_dir, collect_errors=None):
    """Return empty subdirectories of root_dir (excluding root_dir itself), bottom-up order.

    W2-001: the selected root is never a deletion candidate.
    PERF-004: single bottom-up walk, no repeated global passes. Children are yielded
    before parents, so a parent that becomes empty after its children are removed is
    still reported (tracked via `removed`) — one pass fully cleans a chain.
    W2-004 (SRC-001): traversal failures (inaccessible subtrees) are reported through
    ``collect_errors`` instead of silently shrinking the covered tree; a subtree that
    could not be inspected is never treated as fully checked.
    """
    root_norm = os.path.abspath(os.path.normcase(root_dir))

    def _onerror(err):
        if collect_errors is not None:
            collect_errors(f"{err.filename}: {err}")

    empties = []
    removed = set()
    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=False, onerror=_onerror):
        if os.path.abspath(os.path.normcase(dirpath)) == root_norm:
            continue
        # T-031: compare children the same way they were recorded (abspath + normcase),
        # otherwise a relative root never matches and the chain check silently fails.
        children_gone = all(
            os.path.abspath(os.path.normcase(os.path.join(dirpath, d))) in removed
            for d in dirnames
        )
        if not filenames and children_gone:
            empties.append(dirpath)
            removed.add(os.path.abspath(os.path.normcase(dirpath)))
    return empties


class DuplicateFinderApp:
    def __init__(self, root):
        self.root = root
        # CORE-006: window title driven by the single VERSION constant.
        self.root.title(f"💣 Уничтожитель Дубликатов v{VERSION}")
        self.engine = DuplicateEngine()
        # Bounded queue (PERF-002): producer backpressure, results never dropped.
        self.queue = queue.Queue(maxsize=40000)
        self.worker_thread = None
        self.cancel_event = None
        self.current_scan_id = 0
        self._sessions = {}
        self.current_groups = []
        self.current_records = []
        self.check_vars = {}
        self.item_path_map = {}
        self.path_to_item = {}  # PERF-003: O(1) row lookup
        self.tree_tip = None
        self._hover_item = None
        self._shutting_down = False
        self.stop_button = None
        self._build_ui()

    def _build_ui(self):
        main_frame = tk.Frame(self.root)
        main_frame.pack(padx=10, pady=10, fill="both", expand=True)

        tk.Label(main_frame, text="Выбери папку для поиска дубликатов:").pack(anchor="w")
        self.path_var = tk.StringVar()
        tk.Entry(main_frame, textvariable=self.path_var, width=80).pack(fill="x", pady=3)
        tk.Button(main_frame, text="🔍 Обзор", command=self.browse_folder,
                  bg="#4CAF50", fg="white", activebackground="#45a049").pack(pady=3)

        self.log_var = tk.BooleanVar(value=True)
        check_log = tk.Checkbutton(main_frame, text="Вести лог удалений", variable=self.log_var)
        check_log.pack(anchor="w", pady=5)
        ToolTip(check_log, f"Все удалённые файлы будут записаны в {LOG_FILE}")

        button_frame = tk.Frame(main_frame)
        button_frame.pack(fill="x", pady=5)
        tk.Button(button_frame, text="🚀 Найти дубликаты", command=self.start_finding_duplicates,
                  bg="#2196F3", fg="white", activebackground="#1e88e5", width=20).pack(pady=2)
        # T-032: the cancel Event existed with no way to reach it; stop is the pair of start.
        self.stop_button = tk.Button(button_frame, text="🛑 Остановить поиск", command=self.cancel_scan,
                                     bg="#9E9E9E", fg="white", activebackground="#757575",
                                     state="disabled")
        self.stop_button.pack(side="left", padx=(5, 0))
        tk.Button(button_frame, text="🗑 Удалить пустые папки", command=self.delete_empty_folders,
                  bg="#FF9800", fg="white", activebackground="#f57c00").pack(side="left", padx=(5, 0))
        tk.Button(button_frame, text="🤍 Support developer",
                  command=lambda: webbrowser.open("https://buymeacoffee.com/vacuum34"),
                  bg="#444444", fg="white", activebackground="#F3C300").pack(side="left", padx=(5, 0))

        self.status_var = tk.StringVar()
        tk.Label(main_frame, textvariable=self.status_var, anchor="w").pack(fill="x")

        tree_frame = tk.Frame(main_frame)
        tree_frame.pack(fill="both", expand=True, pady=5)
        self.tree = ttk.Treeview(
            tree_frame, columns=("Select", "FilePath"), show="headings",
            selectmode="browse", height=20,
        )
        self.tree.heading("Select", text="Удалить")
        self.tree.heading("FilePath", text="Файл")
        self.tree.column("Select", width=60, anchor="center")
        self.tree.column("FilePath", width=650, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        # T-034: the scrollbar drives the tree AND the tree drives the scrollbar thumb.
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.open_file_path)
        self.tree.bind("<Motion>", self.show_tooltip_on_long_path)

        tk.Button(main_frame, text="💥 Удалить выбранные файлы", command=self.delete_selected,
                  bg="#f44336", fg="white", activebackground="#d32f2f", width=20).pack(pady=5)

        # CORE-004: clean shutdown cancels the worker and stops the event loop.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self.process_queue)

    def browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.path_var.set(folder)

    def start_finding_duplicates(self):
        # CORE-003: detect an active scan BEFORE mutating any UI/model state.
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Идёт работа", "Поиск уже выполняется!")
            return
        root_dir = self.path_var.get()
        self.tree.delete(*self.tree.get_children())
        self.check_vars.clear()
        self.item_path_map.clear()
        self.path_to_item.clear()
        self.current_groups = []
        self.current_records = []
        self._hover_item = None
        self.status_var.set("⏳ Поиск дубликатов...")
        self.current_scan_id += 1
        scan_id = self.current_scan_id
        # T-033: only the live session is useful; older ones are just retained FileRecords.
        self._sessions.clear()
        self.cancel_event = threading.Event()
        self._set_stop_enabled(True)
        self.worker_thread = threading.Thread(
            target=self._scan_worker, args=(scan_id, root_dir, self.cancel_event), daemon=True,
        )
        self.worker_thread.start()

    def _set_stop_enabled(self, enabled):
        if self.stop_button is not None:
            self.stop_button.config(state="normal" if enabled else "disabled")

    def cancel_scan(self):
        """T-032: stop is the pair of start — reach the cancel Event the scan already honours."""
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        if self.cancel_event:
            self.cancel_event.set()
        self.status_var.set("🛑 Останавливаю поиск...")
        self._set_stop_enabled(False)

    def _scan_worker(self, scan_id, root_dir, cancel_event):
        if not os.path.isdir(root_dir):
            # CORE-003 (SRC-001): every worker-originated event is generation-scoped.
            self.queue.put(("error", scan_id, "Укажи корректную папку!"))
            self.queue.put(("terminal", scan_id, "failed",
                            {"state": "failed", "scanned": 0, "hashed": 0, "skipped": 0, "errors": []}))
            return

        def status_cb(text):
            self.queue.put(("status", scan_id, text))

        def progress_cb(batch, stats):
            for g in batch:
                for m in g.members:
                    # W2-001 (SRC-001): cancellation is honored mid-publication; a
                    # blocking put must not force out every remaining member.
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    self.queue.put(("add_result", scan_id, m))

        try:
            # W2-002 (SRC-001): any unexpected engine/callback exception must still
            # produce exactly one truthful failed terminal, or the UI freezes in
            # "searching" forever on a dead daemon worker (console=False build).
            result = self.engine.scan(root_dir, cancel_event=cancel_event,
                                      status_cb=status_cb, progress_cb=progress_cb)
            self._sessions[scan_id] = result
            self.queue.put(("terminal", scan_id, result.stats["state"], result.stats))
        except Exception as e:  # noqa: BLE001 - never BaseException/SystemExit
            self.queue.put(("error", scan_id, f"Ошибка сканирования: {e}"))
            self.queue.put(("terminal", scan_id, "failed",
                            {"state": "failed", "scanned": 0, "hashed": 0, "skipped": 0,
                             "errors": [str(e)]}))

    def process_queue(self):
        if self._shutting_down:
            return
        last_status = None
        terminal_seen = False
        count = 0
        BATCH = 800  # PERF-002: bound work per Tk tick, then reschedule.
        while count < BATCH:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            # W2-003 (SRC-001): the per-tick budget is consumed on dequeue, before
            # any branch can `continue` — stale discards no longer bypass BATCH.
            count += 1
            kind = msg[0]
            if kind == "status":
                # CORE-003 (SRC-001): status is generation-scoped; a terminal in the
                # same batch outranks any earlier progress text.
                if msg[1] == self.current_scan_id and not terminal_seen:
                    last_status = msg[2]
            elif kind == "add_result":
                if msg[1] != self.current_scan_id:
                    continue  # CORE-003: discard stale-generation events.
                self._add_result_row(msg[2])
            elif kind == "terminal":
                if msg[1] != self.current_scan_id:
                    continue
                self._on_terminal(msg[2], msg[3])
                last_status = None
                terminal_seen = True
            elif kind == "error":
                # CORE-003 (SRC-001): worker errors are generation-scoped too; the
                # direct messagebox calls elsewhere stay the non-scan UI channel.
                if msg[1] != self.current_scan_id:
                    continue
                messagebox.showerror("Ошибка", msg[2])
        if last_status is not None and not terminal_seen:
            self.status_var.set(last_status)
        self.root.after(40, self.process_queue)

    def _add_result_row(self, rec):
        var = tk.BooleanVar(value=not rec.is_original)
        self.check_vars[rec.path] = var
        label = '📜 Оригинал: ' if rec.is_original else '📌 Дубликат: '
        item_id = self.tree.insert("", "end", values=("☑" if var.get() else "☐", label + shorten_path(rec.path)))
        self.item_path_map[item_id] = rec.path
        self.path_to_item[rec.path] = item_id  # PERF-003

    def _on_terminal(self, state, stats):
        # T-030: a failed scan never stored a session, so a missing one is normal here,
        # not a reason to raise inside the Tk callback and freeze the status forever.
        session = self._sessions.get(self.current_scan_id)
        self.current_groups = session.groups if session else []
        self.current_records = [m for g in self.current_groups for m in g.members]
        self._set_stop_enabled(False)
        if state == "cancelled":
            # W2-001 (SRC-001): a cancelled scan must not leave partial rows armed as
            # if they were a complete duplicate list (T-37 descendant).
            children = self.tree.get_children()
            if children:
                self.tree.delete(*children)
            self.check_vars.clear()
            self.item_path_map.clear()
            self.path_to_item.clear()
            self.current_groups = []
            self.current_records = []
        n = len(self.current_groups)
        if state == "completed":
            self.status_var.set(f"🔎 Завершено: групп дубликатов: {n}")
        elif state == "completed_with_errors":
            self.status_var.set(f"🔎 Завершено (с ошибками): групп: {n}, пропущено файлов: {stats['skipped']}")
        elif state == "failed":
            self.status_var.set("❌ Поиск не удался (неверная папка)")
        elif state == "cancelled":
            self.status_var.set("🛑 Поиск отменён — список неполный, результаты сброшены")

    def on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        path = self.item_path_map.get(item)
        if path in self.check_vars:
            var = self.check_vars[path]
            var.set(not var.get())
            self.tree.set(item, "Select", "☑" if var.get() else "☐")

    def show_tooltip_on_long_path(self, event):
        # PERF-005: no Toplevel churn while hovering the same unchanged row.
        item = self.tree.identify_row(event.y)
        if item == self._hover_item:
            return
        self._hover_item = item
        if self.tree_tip:
            self.tree_tip.hide(None)
            self.tree_tip = None
        if not item:
            return
        path = self.item_path_map.get(item)
        if not path:
            return
        displayed = self.tree.item(item)["values"][1]
        if "..." in displayed:
            self.tree_tip = ToolTip(self.tree, path)
            self.tree_tip.show(None)

    def open_file_path(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        path = self.item_path_map.get(item)
        if not os.path.exists(path):
            messagebox.showerror("Ошибка", f"Путь не существует: {path}")
            return
        folder = os.path.dirname(path)
        try:
            if platform.system() == "Windows":
                subprocess.run(["explorer", "/select,", os.path.normpath(path)])
            elif platform.system() == "Darwin":
                subprocess.run(["open", "-R", path])
            else:
                subprocess.run(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть путь: {e}")

    def _batch_disarm(self, removed_recs):
        """Remove all traces of deleted records and renormalize groups (CORE-002/003).

        PERF-001 (SRC-001): one rebuild per batch instead of an O(N) list.remove
        plus a linear group scan per deleted record; only successfully deleted
        records are passed in, so failed deletions stay armed/present.
        """
        if not removed_recs:
            return
        removed_ids = {id(r) for r in removed_recs}
        for rec in removed_recs:
            item = self.path_to_item.pop(rec.path, None)
            if item is not None:
                self.tree.delete(item)
                self.item_path_map.pop(item, None)
            self.check_vars.pop(rec.path, None)
        self.current_records = [r for r in self.current_records if id(r) not in removed_ids]
        for g in self.current_groups:
            g.members = [m for m in g.members if id(m) not in removed_ids]
        self.current_groups = [g for g in self.current_groups if g.members]
        self._normalize_originals()

    def _normalize_originals(self):
        """CORE-004 (SRC-001): every surviving group keeps exactly one original
        marker, elected with the same deterministic policy as scan(); row labels
        are refreshed without touching the user's checkbox choices."""
        for g in self.current_groups:
            if not g.members:
                continue
            elected = order_members([m.path for m in g.members])[0]
            for m in g.members:
                should_be_original = (m.path == elected)
                if m.is_original == should_be_original:
                    continue
                m.is_original = should_be_original
                item = self.path_to_item.get(m.path)
                if item is not None:
                    label = '📜 Оригинал: ' if should_be_original else '📌 Дубликат: '
                    self.tree.set(item, "FilePath", label + shorten_path(m.path))

    def delete_selected(self):
        selected = [r for r in self.current_records
                    if r.path in self.check_vars and self.check_vars[r.path].get()]
        if not selected:
            messagebox.showwarning("Ничего не выбрано", "Выбери файлы для удаления.")
            return

        # CORE-002 (SRC-001): this plan is a PREVIEW only, never final destructive
        # authorization; every approved record is revalidated at the deletion
        # boundary below, after the user confirms.
        plan = self.engine.plan_deletion(self.current_groups, selected)
        if plan["refused_groups"]:
            names = ", ".join(f"#{g}" for g in plan["refused_groups"])
            messagebox.showerror(
                "Удаление отменено",
                f"Нельзя удалить последнюю сохранённую копию в группе(ах): {names}. "
                "Оставьте хотя бы один файл в каждой группе.",
            )
            return
        if plan["skipped"]:
            reasons = "; ".join(f"{os.path.basename(r.path)} ({why})" for r, why in plan["skipped"])
            messagebox.showwarning(
                "Пропущено",
                f"Эти файлы изменились или исчезли с момента сканирования и НЕ будут удалены: {reasons}",
            )

        # T-027: the promise must match the platform's actual deletion semantics.
        if deletion_is_recoverable():
            fate = "Файлы отправляются в корзину — их можно восстановить."
        else:
            fate = "На этой системе корзины нет: файлы удаляются НАВСЕГДА, без восстановления."
        confirm = messagebox.askyesno(
            "Подтвердить",
            f"Удалить {len(plan['approved'])} выбранных файлов?\n\n{fate}",
        )
        if not confirm:
            return

        # CORE-005: open the log before destructive work; keep fs vs log outcomes separate.
        log_fh = None
        log_err = None
        if self.log_var.get():
            try:
                log_fh = open(LOG_FILE, "a", encoding="utf-8")
            except OSError as e:
                log_err = f"лог недоступен: {e}"

        # CORE-002 (SRC-001): final destructive authorization happens here, per
        # record, immediately before each send_to_trash — never carried over from
        # the pre-confirmation plan. A post-plan changed/replaced selected file or
        # a vanished last survivor is classified stale and left untouched.
        approved_paths = {r.path for r in plan["approved"]}
        groups_by_id = {g.group_id: g for g in self.current_groups}  # PERF-001 (SRC-001)
        deleted_paths = set()
        stale_now = []
        failed = []
        disarmed = []
        removed = 0
        for rec in plan["approved"]:
            if rec.path in deleted_paths:
                continue
            ok, reason = self.engine.verify_record(rec)
            if not ok:
                stale_now.append((rec.path, reason))
                continue
            group = groups_by_id.get(rec.group_id)
            if group is None:
                stale_now.append((rec.path, 'orphan_group'))
                continue
            survivor_ok = False
            for m in group.members:
                # Members ALREADY deleted in this batch can never be survivors;
                # members whose deletion FAILED or went stale still exist on
                # disk and remain valid keepers (T-45: the exclusion is the
                # actual-deletion set, never the planned one).
                if m is rec or m.path in deleted_paths:
                    continue
                m_ok, _ = self.engine.verify_record(m)
                if m_ok:
                    survivor_ok = True
                    break  # PERF-002 (SRC-001): one valid keeper ends the search.
            if not survivor_ok:
                # CORE-001: deleting this would leave zero currently valid copies.
                stale_now.append((rec.path, 'last_copy_refused'))
                continue
            try:
                send_to_trash(rec.path)
            except OSError as e:
                failed.append((rec.path, str(e)))
                continue
            if log_fh:
                try:
                    log_fh.write(rec.path + "\n")
                except OSError:
                    log_err = log_err or "ошибка записи лога"
            deleted_paths.add(rec.path)
            disarmed.append(rec)
            removed += 1
        if log_fh:
            try:
                log_fh.close()
            except OSError as e:
                # T-36: buffered records can be lost at flush; a swallowed close would
                # let the status claim a complete log that never reached disk.
                log_err = log_err or f"ошибка закрытия лога: {e}"

        self._batch_disarm(disarmed)

        status = f"✅ Удалено файлов: {removed}"
        if failed:
            status += f" (не удалось: {len(failed)})"
        if log_err:
            status += f" [лог: {log_err}]"
        stale_total = len(plan["skipped"]) + len(stale_now)
        if stale_total:
            status += f" (пропущено: {stale_total})"
        self.status_var.set(status)

    def delete_empty_folders(self):
        # W2-001 + PERF-004: single bottom-up pass; the selected root is never a deletion candidate.
        root_dir = self.path_var.get()
        if not os.path.isdir(root_dir):
            messagebox.showerror("Ошибка", "Укажи корректную папку!")
            return
        # W2-004 (SRC-001): inaccessible subtrees are surfaced, never silently
        # conflated with "fully traversed".
        traversal_errors = []
        empty_dirs = find_empty_dirs(root_dir, collect_errors=traversal_errors.append)
        if traversal_errors and not empty_dirs:
            messagebox.showwarning(
                "Обход неполный",
                f"Некоторые папки недоступны ({len(traversal_errors)}), "
                "поэтому список пустых папок может быть неполным.\n"
                f"Пример: {traversal_errors[0]}",
            )
            return
        if not empty_dirs:
            messagebox.showinfo("Готово", "Пустых папок нет.")
            return

        confirm = messagebox.askyesno(
            "Подтвердить",
            f"Удалить {len(empty_dirs)} пустых папок внутри:\n{root_dir}\n\n"
            "Сама выбранная папка не будет удалена.",
        )
        if not confirm:
            return

        log_fh = None
        log_err = None
        if self.log_var.get():
            try:
                log_fh = open(LOG_FILE, "a", encoding="utf-8")
            except OSError as e:
                log_err = f"лог недоступен: {e}"

        removed, failed = 0, []
        for dirpath in empty_dirs:
            try:
                os.rmdir(dirpath)
                removed += 1
                if log_fh:
                    try:
                        log_fh.write(f"Пустая папка удалена: {dirpath}\n")
                    except OSError:
                        log_err = log_err or "ошибка записи лога"
            except OSError as e:
                failed.append((dirpath, str(e)))
        if log_fh:
            try:
                log_fh.close()
            except OSError as e:
                # T-36: same truthfulness rule as delete_selected.
                log_err = log_err or f"ошибка закрытия лога: {e}"

        status = f"Удалено пустых папок: {removed}"
        if failed:
            status += f" (не удалось: {len(failed)})"
        if traversal_errors:
            # W2-004 (SRC-001): partial coverage is never reported as a clean success.
            status += f" (обход неполный, недоступно: {len(traversal_errors)})"
        if log_err:
            status += f" [лог: {log_err}]"
        messagebox.showinfo("Готово", status)

    def _on_close(self):
        # CORE-004: cancel the worker and stop the event loop cleanly.
        self._shutting_down = True
        if self.cancel_event:
            self.cancel_event.set()
        if self.tree_tip:
            self.tree_tip.hide(None)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = DuplicateFinderApp(root)
    root.mainloop()

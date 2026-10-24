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

T-68 (SRC-005) recovery-transaction closure:
  REC-001 corrupt journal preserved, never reset      REC-002 durable pre-rename intent
  REC-003 restart reconciliation                     REC-004 destruction needs acquired identity
  REC-005 verified restore by default                REC-006 frozen identity, no-replace moves
"""

import os
import stat
import ctypes
import errno
import re
import shutil
import threading
import uuid
import queue
import hashlib
import subprocess
import platform
import webbrowser
import tempfile
import time
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional, Callable

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # headless environments (e.g. test runners) may lack Tk
    tk = filedialog = messagebox = ttk = None

VERSION = "0.0.7"

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


def _sample_ranges(size, block_size):
    """PERF-003: non-overlapping sample ranges with aggregate bytes <= file size.

    Files <= block_size are sampled completely from byte 0 to size, producing the
    authoritative full SHA-256 digest. Files > block_size sample three non-overlapping
    ranges (first, middle, last) whose aggregate sampled bytes are strictly below
    the full file size.
    """
    if size <= block_size:
        return ((0, size),)
    chunk_len = max(1, min(block_size, (size - 1) // 3))
    first = (0, chunk_len)
    mid = ((size - chunk_len) // 2, chunk_len)
    last = (size - chunk_len, chunk_len)
    return (first, mid, last)


def _sample_offsets(size, block_size):
    """Offsets of sample ranges for backward compatibility."""
    return tuple(offset for offset, _ in _sample_ranges(size, block_size))


def sample_fingerprint(path, cancel_event=None, block_size=65536):
    """PERF-003: cheap deterministic rejection fingerprint (first/mid/last blocks).

    For files <= block_size, this hashes the entire content and produces the
    authoritative full SHA-256 digest. For larger files, it samples non-overlapping
    first/middle/last ranges strictly smaller than the full file size; collisions
    on partial samples fall through to full SHA-256 (hash_file).
    """
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        for offset, length in _sample_ranges(size, block_size):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledHash(path)
            f.seek(offset)
            remaining = length
            while remaining > 0:
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledHash(path)
                to_read = min(remaining, block_size)
                chunk = f.read(to_read)
                if not chunk:
                    break
                hasher.update(chunk)
                remaining -= len(chunk)
    return hasher.hexdigest()


def deletion_is_recoverable():
    """True only where send_to_trash reaches a real Recycle Bin (Windows).

    T-027: the deletion promise shown to the user must match what the code does;
    off-Windows send_to_trash falls back to permanent os.remove.
    """
    return platform.system() == "Windows"


# T-49 (SRC-002): quarantine-before-authorization counter. A per-process,
# monotonic suffix keeps acquired names collision-proof against any other
# actor that might create a like-named path between attempts.
_acquire_seq = 0


# T-68 (REC-002): durable pre-rename intent. The quarantine name is decided and
# journaled BEFORE the object moves, so a crash between the rename and the
# receipt leaves a reconcilable prepared transaction instead of an orphan file
# with no recovery evidence.
_TXN_OPEN_STATUSES = ("prepared",)


def _quarantine_name(base):
    """Deterministic-within-process quarantine name for `base` (T-49/T-68)."""
    global _acquire_seq
    _acquire_seq += 1
    return f".{base}.__vacq_{os.getpid()}_{_acquire_seq}"


# REC-006 (P0-001): the move MUST be one atomic syscall that refuses an occupied
# destination by itself. link-then-unlink is forbidden: os.unlink acts on the
# pathname, not on the object the link created, so a replacement installed
# between the two calls is destroyed.
_RENAME_NOREPLACE = 0x1  # Linux >= 3.15 (btrfs, xfs, ext4 >= 5.12, overlayfs)
_AT_FDCWD = -100


def _windows_atomic_move(src, dst):
    """Windows no-replace move via MoveFileExW(0) — one syscall.

    Without MOVEFILE_REPLACE_EXISTING the call FAILS when dst exists and leaves
    src intact; cross-volume fails with ERROR_NOT_SAME_DEVICE. Neither outcome
    destroys anything.
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    ctypes.c_uint]
    kernel32.MoveFileExW.restype = ctypes.c_int
    if not kernel32.MoveFileExW(src, dst, 0):
        raise ctypes.WinError()


def _linux_atomic_move(src, dst):
    """Linux atomic no-replace rename: renameat2(AT_FDCWD, src, AT_FDCWD, dst,
    RENAME_NOREPLACE).

    RENAME_NOREPLACE is the single atomic step — the destination is created or
    refused as an indivisible syscall, and the source is consumed only on
    success. Raises when the kernel or filesystem does not implement the flag,
    so the caller can refuse instead of falling back to a destructive rename.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "renameat2"):
        raise OSError(errno.ENOSYS, "renameat2 unavailable")
    rc = libc.renameat2(_AT_FDCWD, src.encode(), _AT_FDCWD,
                        dst.encode(), _RENAME_NOREPLACE)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), src)


def _source_inode_unchanged(path, st_before):
    """True when `path` still names the same object `st_before` described.

    This is the guard that makes a reserve-copy move safe: the copy is taken
    from a stable open handle, so a pathname substituted afterwards is detected
    here and left alone instead of being unlinked.
    """
    try:
        st_now = os.stat(path)
    except OSError:
        return False
    return (st_now.st_dev, st_now.st_ino) == (st_before.st_dev, st_before.st_ino)


def _atomic_no_replace_move(src, dst):
    """Move `src` to `dst` atomically, refusing — never overwriting — an occupied `dst`.

    P0-001 / REC-006: the whole operation is one atomic syscall that reserves or
    refuses the destination name itself. There is no separate unlink acting on a
    mutable pathname, so a concurrent replacement installed at `src` can never be
    destroyed by this move: either the object moves (src is consumed by the same
    syscall), or the destination is occupied (src is preserved), or the operation
    is refused (both survive).

      * Windows: MoveFileExW without MOVEFILE_REPLACE_EXISTING.
      * Linux:   renameat2 with RENAME_NOREPLACE.
      * Otherwise: refuse. A filesystem that cannot provide the guarantee gets a
        raised OSError, never a destructive link-then-unlink or a plain rename.
    """
    src = os.fspath(src)
    dst = os.fspath(dst)
    if platform.system() == "Windows":
        _windows_atomic_move(src, dst)
        return
    if hasattr(ctypes, "CDLL"):
        _linux_atomic_move(src, dst)
        return
    raise OSError(errno.ENOTSUP,
                  "no atomic no-replace move on this platform")


def prepare_acquisition(path, size=None, fingerprint=None):
    """Durable intent to acquire `path` (REC-002), journaled before any rename.

    Records the original path, the expected identity (size and SHA-256 when the
    caller has it), the planned quarantine path and a transaction ID. Returns
    the transaction dict, or None when the intent could not be made durable —
    in which case the caller must not touch the file.
    """
    directory = os.path.dirname(os.path.abspath(path))
    planned = os.path.join(directory, _quarantine_name(os.path.basename(path)))
    txn = {
        "quarantine_path": os.path.abspath(planned),
        "quarantine_name": os.path.basename(planned),
        "original_path": os.path.abspath(path),
        "original_name": os.path.basename(path),
        "size": size,
        "fingerprint": fingerprint,
        "status": "prepared",
        "txn_id": uuid.uuid4().hex,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not record_quarantine_metadata(planned, path, size=size,
                                      fingerprint=fingerprint,
                                      status="prepared", txn_id=txn["txn_id"]):
        return None
    return txn


def _abandon_intent(intent, status="abandoned_before_rename"):
    """Annotate an unrealized intent. Never deletes or overwrites anything."""
    if not intent:
        return
    record_quarantine_metadata(intent["quarantine_path"], intent["original_path"],
                               size=intent.get("size"),
                               fingerprint=intent.get("fingerprint"),
                               status=status)


def acquire_candidate(path, intent=None):
    """T-49/T-68: atomically move the candidate to a unique quarantine
    name on the same directory, binding the verified object to the object the
    destructive step later touches.

    The move is the acquisition: after it returns, no concurrent replace can
    sit between "verified" and "deleted", because the destructive step acts on
    the acquired name, never on the original path again. Returns the new
    quarantine path. Raises OSError when the file cannot be moved (locked,
    vanished, replaced by a directory, occupied quarantine name, ...) — the
    original is then untouched and the caller must not authorize deletion.

    REC-002: with a prepared `intent` the move goes to the journaled planned
    name through the no-replace primitive, so the prepared transaction and the
    rename cannot diverge, and an occupied planned name refuses the move
    instead of silently destroying the occupier.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if intent is not None:
        qpath = intent["quarantine_path"]
        # os.rename would silently overwrite; the no-replace primitive refuses.
        _atomic_no_replace_move(path, qpath)
        return qpath
    base = os.path.basename(path)
    while True:
        qpath = os.path.join(directory, _quarantine_name(base))
        try:
            _atomic_no_replace_move(path, qpath)
            return qpath
        except FileExistsError:
            continue
        except OSError:
            raise


def _recovery_log_path():
    """Stable per-user storage for recovery records (T-66).

    A journal path relative to the working directory is lost the moment the
    process changes directory, or when a later recovery attempt starts
    elsewhere; per-user application storage is discoverable by any process of
    the same user. Falls back to the home directory when the platform gives no
    application-data root.
    """
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base or os.path.expanduser("~"),
                        "SmartVacDuplicateRemover", "quarantine_recovery.json")


RECOVERY_LOG_FILE = _recovery_log_path()

# T-66: serializes journal read-modify-write inside one process; the OS-level
# lock below serializes across processes. Acquired in this order only.
_recovery_journal_lock = threading.Lock()


# REC-001 (FINDING A): a corrupt journal is evidence, not an empty page.
# Reading returns a hard error instead of a reset `{}`, writing refuses while
# integrity is unresolved, and the damaged bytes are preserved for recovery.
_JOURNAL_REQUIRED_ITEM_KEYS = ("quarantine_path", "original_path", "status")


def _journal_schema_ok(data):
    """True only for a journal this code may extend in place."""
    if not isinstance(data, dict):
        return False
    items = data.get("items")
    if not isinstance(items, dict):
        return False
    for rec in items.values():
        if not isinstance(rec, dict):
            return False
        if any(not isinstance(rec.get(k), str) or not rec.get(k)
               for k in _JOURNAL_REQUIRED_ITEM_KEYS):
            return False
    return True


def _read_journal(path):
    """Return (data, error). Never fabricates a reset journal (REC-001)."""
    if not os.path.exists(path):
        return {"items": {}}, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # unreadable, torn or non-JSON bytes
        return None, f"unreadable: {e}"
    if not _journal_schema_ok(data):
        return None, "schema_invalid"
    return data, None


def _preserve_corrupt_journal(path):
    """Keep the damaged journal bytes for recovery (REC-001).

    The canonical path is deliberately left untouched: overwriting it with a
    valid-but-depleted journal is the destruction this exists to prevent, and
    an operator repair needs the original bytes to repair from.
    """
    try:
        dst = f"{path}.corrupt-{time.time_ns()}.json"
        shutil.copyfile(path, dst)
        return dst
    except OSError:
        return None


def _with_journal(mutate):
    """Serialized read-modify-write of RECOVERY_LOG_FILE (REC-001/REC-006).

    `mutate(data)` returns True to authorize the write. Returns True only when
    the new journal is durable. A corrupt or schema-invalid journal is
    preserved and False is returned: no update, no reset, no destructive
    authorization can ride on an unresolved journal. The previous good journal
    is kept as `.bak` before every publish, so a failed or mid-way transaction
    is recoverable from the last durable state.
    """
    with _recovery_journal_lock:
        try:
            held = _acquire_lock_file(RECOVERY_LOG_FILE + ".lock")
        except (OSError, TimeoutError):
            return False
        tmp_meta = None
        try:
            os.makedirs(os.path.dirname(os.path.abspath(RECOVERY_LOG_FILE)),
                        exist_ok=True)
            data, err = _read_journal(RECOVERY_LOG_FILE)
            if err is not None:
                _preserve_corrupt_journal(RECOVERY_LOG_FILE)
                return False
            if not mutate(data):
                return False
            tmp_meta = f"{RECOVERY_LOG_FILE}.{os.getpid()}.{time.time_ns()}.tmp"
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(RECOVERY_LOG_FILE):
                shutil.copyfile(RECOVERY_LOG_FILE, RECOVERY_LOG_FILE + ".bak")
            # Atomic publish: a reader observes either the whole old journal or
            # the whole new one, never a torn file.
            os.replace(tmp_meta, RECOVERY_LOG_FILE)
            tmp_meta = None
            return True
        except OSError:
            return False
        finally:
            if tmp_meta and os.path.exists(tmp_meta):
                try:
                    os.remove(tmp_meta)
                except OSError:
                    pass
            _release_lock_file(held)


def record_quarantine_metadata(qpath, original_path, size=None, fingerprint=None, status="acquired", txn_id=None):
    """Durable recovery metadata for acquired quarantine objects (P0-A / T-62 / T-69)."""
    key = os.path.abspath(qpath)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = {
        "quarantine_path": key,
        "quarantine_name": os.path.basename(qpath),
        "original_path": os.path.abspath(original_path),
        "original_name": os.path.basename(original_path),
        "size": size,
        "fingerprint": fingerprint,
        "status": status,
        "timestamp": now,
    }
    if txn_id is not None:
        record["txn_id"] = txn_id

    _STATUS_TRANSITIONS = {
        "prepared": {"prepared", "acquired", "acquired_recovered",
                     "abandoned_before_rename", "identity_unverified",
                     "object_missing", "restored", "restore_refused_occupied"},
        "acquired": {"acquired", "trashing", "restored",
                     "restore_refused_occupied", "acquired_recovered",
                     "restore_partial"},
        "acquired_recovered": {"acquired_recovered", "trashing", "restored",
                               "restore_refused_occupied", "restore_partial"},
        "trashing": {"trashing", "deleted", "recycled", "restored",
                     "restore_refused_occupied", "restore_partial"},
        # P1-002: a restore whose cleanup or journal write failed. The
        # destination may already hold the verified bytes, so the receipt must
        # stay retryable rather than terminal.
        "restore_partial": {"restore_partial", "restored",
                            "restore_refused_occupied", "trashing",
                            "acquired", "acquired_recovered"},
        "restore_refused_occupied": {"restore_refused_occupied", "restored",
                                    "trashing", "acquired", "acquired_recovered",
                                    "prepared", "restore_partial"},
        "restored": {"restored"},
        "deleted": {"deleted"},
        # A user restoring an item out of the real Recycle Bin lands here; the
        # object is back on disk, so the receipt must be able to close as
        # restored (P1-002). Otherwise a successful restore is forced to report
        # a journal failure it did not have.
        "recycled": {"recycled", "restored", "restore_partial",
                     "restore_refused_occupied"},
        "abandoned_before_rename": {"abandoned_before_rename"},
        "identity_unverified": {"identity_unverified"},
        "object_missing": {"object_missing"},
    }

    def _mutate(data):
        items = data.setdefault("items", {})
        prior = items.get(key)
        if prior:
            for field_name in ("txn_id", "original_path", "quarantine_path",
                               "size", "fingerprint"):
                frozen = prior.get(field_name)
                if frozen is not None and record.get(field_name) is not None \
                        and record.get(field_name) != frozen:
                    return False
            if prior.get("quarantine_path") is not None \
                    and prior.get("quarantine_path") != key:
                return False
            if record.get("original_path") is None:
                record["original_path"] = prior.get("original_path")
            if record.get("size") is None:
                record["size"] = prior.get("size")
            if record.get("fingerprint") is None:
                record["fingerprint"] = prior.get("fingerprint")
            if record.get("txn_id") is None:
                record["txn_id"] = prior.get("txn_id")
            prior_status = prior.get("status")
            if prior_status is not None and prior_status != record["status"]:
                allowed = _STATUS_TRANSITIONS.get(prior_status)
                if allowed is None or record["status"] not in allowed:
                    return False
            record["quarantine_path"] = key
            if prior.get("original_name") is not None and \
                    record.get("original_name") is None:
                record["original_name"] = prior.get("original_name")
            if prior.get("quarantine_name") is not None and \
                    record.get("quarantine_name") is None:
                record["quarantine_name"] = prior.get("quarantine_name")
            transitioned = (prior.get("status") != record["status"])
            status_ts = now
            if not transitioned:
                for k in ("status_timestamp", "status_changed_at",
                          "transitioned_at", "status_transition_timestamp",
                          "status_time"):
                    if prior.get(k) is not None:
                        status_ts = prior.get(k)
                        break
            for k in ("status_timestamp", "status_changed_at",
                      "transitioned_at", "status_transition_timestamp",
                      "status_time"):
                record[k] = status_ts
            history = list(prior.get("history", prior.get("transitions", [])) or [])
            if transitioned or not history:
                history.append({"status": record["status"],
                                "timestamp": status_ts})
            record["history"] = history
            record["transitions"] = list(history)
        else:
            for k in ("status_timestamp", "status_changed_at",
                      "transitioned_at", "status_transition_timestamp",
                      "status_time"):
                record[k] = now
            record["history"] = [{"status": record["status"], "timestamp": now}]
            record["transitions"] = list(record["history"])
        items[key] = record
        return True

    return _with_journal(_mutate)



def get_quarantine_metadata(qpath_or_name):
    """Canonical quarantine-object identity lookup (FINDING A).

    The authoritative source is an exact quarantine_path match. A non-authoritative
    original_path convenience match is retained for discovery/tests, but basename
    and original-name fallbacks are removed so an unrelated object sharing a
    name can never borrow another object's receipt.
    """
    data, err = _read_journal(RECOVERY_LOG_FILE)
    if err is not None:
        return None
    items = data.get("items", {})
    key = os.path.abspath(qpath_or_name)
    rec = items.get(key)
    if isinstance(rec, dict) and rec.get("quarantine_path") == key:
        return rec
    # Discovery-only convenience: original path -> its quarantine receipt.
    for r in items.values():
        if isinstance(r, dict) and r.get("original_path") == key                 and r.get("quarantine_path"):
            return r
    base = os.path.basename(qpath_or_name)
    query_dir = os.path.dirname(key)
    # Cross-directory aliasing guard: a basename-only query may resolve only
    # when exactly one record in the whole journal carries that basename.
    dir_scoped = os.path.isabs(qpath_or_name) or os.path.dirname(qpath_or_name) not in ("", ".")
    matches = []
    for r in items.values():
        if not isinstance(r, dict):
            continue
        qpath = r.get("quarantine_path")
        if not qpath:
            continue
        if os.path.basename(qpath) != base:
            continue
        if dir_scoped and os.path.dirname(os.path.abspath(qpath)) != query_dir:
            continue
        matches.append(r)
    if len(matches) == 1:
        return matches[0]
    # Ambiguous basename: refuse to guess.
    return None


def _exact_quarantine_record(qpath):
    """Authoritative receipt lookup: exact quarantine_path identity only."""
    data, err = _read_journal(RECOVERY_LOG_FILE)
    if err is not None:
        return None
    items = data.get("items", {})
    key = os.path.abspath(qpath)
    rec = items.get(key)
    if not isinstance(rec, dict):
        return None
    if rec.get("quarantine_path") != key:
        return None
    return rec


def search_quarantine_metadata(qpath_or_name):
    """Non-authoritative discovery helper for UI convenience only.

    Fuzzy matches by basename / original name / original path are allowed here
    because this is explicitly NOT an authorization source. Never use its result
    to authorize a restore or deletion.
    """
    data, err = _read_journal(RECOVERY_LOG_FILE)
    if err is not None:
        return None
    try:
        items = data.get("items", {})
        norm = os.path.abspath(qpath_or_name)
        if norm in items and items[norm].get("quarantine_path") == norm:
            return items[norm]
        for rec in items.values():
            if rec.get("original_path") == norm:
                return rec
        base = os.path.basename(qpath_or_name)
        for rec in items.values():
            if rec.get("quarantine_name") == base:
                return rec
        for rec in items.values():
            if rec.get("original_name") == base:
                return rec
    except Exception:
        pass
    return None


# Backward/alias names for discovery and exact lookup.
find_quarantine_metadata = search_quarantine_metadata
discover_quarantine_metadata = search_quarantine_metadata
browse_quarantine_metadata = search_quarantine_metadata
get_quarantine_receipt = get_quarantine_metadata
lookup_quarantine_metadata = get_quarantine_metadata
get_quarantine_record = get_quarantine_metadata


def _acquired_receipt(abs_path):
    """The durable acquired receipt authorizing destruction of `abs_path`.

    REC-004 (FINDING C): destruction requires a receipt that proves a durable
    acquisition with COMPLETE identity — status acquired (or reconciled from a
    prepared intent) plus the original size and SHA-256. A status-only receipt
    created by a later update can never authorize deletion: an identity the
    journal never recorded cannot be verified against the object, and a
    transient first-write failure must not be laundered into authority by a
    successful second write. Returns None when the journal is unreadable too,
    so an unresolved journal refuses destruction exactly like a missing record.
    """
    data, err = _read_journal(RECOVERY_LOG_FILE)
    if err is not None:
        return None
    rec = data["items"].get(abs_path)
    if not rec:
        return None
    if rec.get("status") not in ("acquired", "acquired_recovered"):
        return None
    if rec.get("size") is None or not rec.get("fingerprint"):
        return None
    return rec


def reconcile_transactions():
    """REC-003: reconcile prepared/acquired transactions after a restart.

    A process killed at any acquisition boundary leaves one of three observable
    truths, and reconciliation records the truth — it never destroys and never
    invents. For each open intent:

    * renamed but never receipted (original gone, quarantine present): the
      identity journaled in the intent is checked against the quarantined
      object; a match is committed as `acquired_recovered` (so a lawful,
      previously authorized acquisition keeps its recovery evidence and a
      later explicit delete/restore can proceed against verified identity),
      a mismatch is annotated `identity_unverified` and left untouched.
    * not renamed (original present): `abandoned_before_rename`, no file moved.
    * both missing: `object_missing`, no recovery claim made.

    Returns the per-class counts. Acquired objects with live receipts are left
    exactly as they are — silently destroying or re-animating them is what this
    pass must never do.
    """
    data, err = _read_journal(RECOVERY_LOG_FILE)
    if err is not None:
        _preserve_corrupt_journal(RECOVERY_LOG_FILE)
        return {"recovered": 0, "unverified": 0, "abandoned": 0,
                "ambiguous": 0, "error": err}
    items = data.get("items", {})
    open_items = [(k, r) for k, r in items.items()
                  if r.get("status") in ("prepared", "acquired", "acquired_recovered")]
    if not open_items:
        return {"recovered": 0, "unverified": 0, "abandoned": 0, "ambiguous": 0}

    outcomes = {"recovered": 0, "unverified": 0, "abandoned": 0, "ambiguous": 0}
    updates = {}

    def _classify(key, rec):
        """Return (status_or_None, outcome_key_or_None) for one open intent."""
        qpath, orig = rec["quarantine_path"], rec["original_path"]
        q_present = os.path.lexists(qpath)
        o_present = os.path.lexists(orig)
        if not q_present and o_present:
            return "abandoned_before_rename", "abandoned"
        if q_present and not o_present:
            # The rename landed, the receipt write was lost to the crash.
            try:
                fp = hash_file(qpath)
                actual_size = os.path.getsize(qpath)
            except OSError:
                return "identity_unverified", "unverified"
            if (rec.get("fingerprint") and fp == rec["fingerprint"]
                    and rec.get("size") == actual_size):
                return "acquired_recovered", "recovered"
            return "identity_unverified", "unverified"
        if not q_present and not o_present:
            return "object_missing", "ambiguous"
        # Both names present (or a live acquired receipt with the original
        # still there): observable but not safely classifiable here.
        return ("unresolved_both_present", "ambiguous") if rec.get("status") == "prepared" else (None, None)

    for key, rec in open_items:
        try:
            status, outcome_key = _classify(key, rec)
        except Exception:
            continue
        if status is None:
            continue
        updates[key] = status
        if outcome_key:
            outcomes[outcome_key] += 1

    if updates:
        def _mutate(data):
            # Applied onto the view _with_journal re-read under the lock.
            items = data.get("items", {})
            for key, status in updates.items():
                if not status:
                    continue
                rec = items.get(key)
                if rec is None or rec.get("status") == status:
                    continue
                rec["status"] = status
                rec["reconciled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return True

        _with_journal(_mutate)
    return outcomes


def restore_quarantined_file(qpath, original_path=None, engine=None,
                             allow_unverified=False):
    """Explicit restore workflow for quarantined files (P0-A / T-62 / T-68 / T-69).

    Verified by default (REC-005): the quarantined object's full SHA-256 must
    equal the recorded fingerprint, a receipt must exist, the destination must
    be free and the original path must match the receipt when one is given.
    Returns (True, destination_path) on success, or (False, reason) on refusal.

    FINDING A: authorization uses the exact canonical quarantine-object identity
    only; basename / original-name fallback can never grant a restore.

    FINDING B: verification and installation are bound to one consistent
    content snapshot. The bytes that are hashed are the bytes that are installed;
    a concurrent writer cannot substitute EVIL between verification and the
    final move. The destination is still reserved with the no-replace primitive.
    """
    if not os.path.lexists(qpath):
        return False, "quarantine_file_missing"
    meta = _exact_quarantine_record(qpath)
    if meta is None:
        if not allow_unverified:
            return False, "fingerprint_missing"
        return False, "receipt_missing"
    # Stale receipts do not authorize a restore. `restore_partial` is retried:
    # the destination may already hold the verified bytes, so the caller must
    # be able to converge rather than be stranded (P1-002 idempotency).
    if meta.get("status") not in ("acquired", "acquired_recovered", "prepared",
                                "trashing", "recycled",
                                "restore_refused_occupied",
                                "restore_partial"):
        return False, "stale_receipt"
    dest = original_path or meta.get("original_path")
    if not dest:
        return False, "destination_unknown"
    dest = os.path.abspath(dest)
    if original_path:
        if meta.get("original_path") != dest:
            return False, "original_path_conflict"
    expected_fp = meta.get("fingerprint")
    if expected_fp and not allow_unverified:
        pass
    elif (not expected_fp) and (not allow_unverified):
        return False, "fingerprint_missing"

    # Capture a consistent, verified content snapshot.
    try:
        st_before = os.stat(qpath)
    except OSError as e:
        return False, f"hash_error: {e}"
    identity_before = (getattr(st_before, "st_dev", None),
                       getattr(st_before, "st_ino", None),
                       st_before.st_size,
                       getattr(st_before, "st_mtime_ns", int(st_before.st_mtime * 1e9)))
    dest_dir = os.path.dirname(dest) or "."
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        pass
    fd, tmp_path = tempfile.mkstemp(prefix=".vacrestore_", dir=dest_dir)
    snapshot_fp = None
    refusal = None
    try:
        hasher = hashlib.sha256()
        with os.fdopen(fd, "wb") as out:
            with open(qpath, "rb") as srcf:
                while True:
                    chunk = srcf.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        snapshot_fp = hasher.hexdigest()
        try:
            st_after = os.stat(qpath)
        except OSError as e:
            refusal = f"hash_error: {e}"
        else:
            identity_after = (getattr(st_after, "st_dev", None),
                              getattr(st_after, "st_ino", None),
                              st_after.st_size,
                              getattr(st_after, "st_mtime_ns", int(st_after.st_mtime * 1e9)))
            if identity_after != identity_before:
                refusal = "fingerprint_mismatch"
            elif (not allow_unverified) and expected_fp and snapshot_fp != expected_fp:
                refusal = "fingerprint_mismatch"
    except OSError as e:
        refusal = f"hash_error: {e}"
    if refusal is not None:
        # Every refusal removes the snapshot copy: it lives in the DESTINATION
        # directory, so a leftover would be a full copy of the quarantined
        # content the user never asked to keep (T-72).
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False, refusal

    # Install the verified snapshot without overwriting an occupied destination.
    try:
        _atomic_no_replace_move(tmp_path, dest)
    except FileExistsError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False, "destination_occupied"
    except OSError as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False, f"rename_error: {e}"
    tmp_path = None

    # Remove the original quarantine object only when its identity is unchanged.
    # A failure here is a PARTIAL restore: the destination is installed, the
    # quarantined source still exists and still requires recovery, so the
    # transaction can NOT be reported as fully restored (P1-002).
    cleanup_error = None
    source_replaced = False
    try:
        st_now = os.stat(qpath)
        identity_now = (getattr(st_now, "st_dev", None),
                        getattr(st_now, "st_ino", None),
                        st_now.st_size,
                        getattr(st_now, "st_mtime_ns", int(st_now.st_mtime * 1e9)))
        if identity_now == identity_before:
            os.remove(qpath)
        else:
            # A newer pathname replacement occupies the quarantine name: it is
            # NOT our object, so it is never removed (P1-002). The destination
            # holds the verified bytes, so this is still a recovery-required
            # outcome: the receipt must name the live object that remains.
            source_replaced = True
    except OSError as e:
        cleanup_error = e

    if cleanup_error is not None:
        # P1-002: the destination is installed but the quarantine source is
        # still on disk. Stamp the durable receipt as partial, then report the
        # exact class so a retry converges instead of returning a false success.
        if not record_quarantine_metadata(qpath, dest,
                                          status="restore_partial"):
            return False, "journal_write_failed"
        return False, "partial_recovery_required"

    if source_replaced:
        # The quarantine name now holds a different object. It must survive
        # (it is not ours to delete), so the transaction cannot complete.
        if not record_quarantine_metadata(qpath, dest,
                                          status="restore_refused_occupied"):
            return False, "journal_write_failed"
        return False, "partial_recovery_required"

    if not record_quarantine_metadata(qpath, dest, status="restored"):
        # The receipt could not be made durable. The filesystem already agrees
        # with the filesystem truth (destination installed, source gone), but
        # the journal no longer matches it — never claim success.
        return False, "journal_write_failed"
    # Full success: the destination holds the verified bytes, the quarantine
    # source is gone, and the receipt durably records the restore. The success
    # payload stays the restored destination path so existing callers can use
    # it directly.
    return True, dest



def restore_candidate(qpath, original_path):
    """T-49/T-62/T-68: best-effort restore of an acquired candidate after a refusal.

    Restoring the original name is the clean half; when the original path is
    now occupied (concurrent actor recreated it), the acquired object is kept
    under its quarantine name and the caller must report a recovery-required
    outcome instead of silently dropping or deleting it. The move is the
    no-replace primitive (REC-006), so an occupier created between the check
    and the move survives, on POSIX exactly as on Windows.
    """
    try:
        if not os.path.lexists(original_path):
            _atomic_no_replace_move(qpath, original_path)
            record_quarantine_metadata(qpath, original_path, status="restored")
            return True
    except OSError:
        pass
    record_quarantine_metadata(qpath, original_path, status="restore_refused_occupied")
    return False


def _windows_send_to_recycle_bin(abs_path):
    """Windows-specific SHFileOperationW call to send a file to the Recycle Bin."""
    if not hasattr(ctypes, "windll"):
        raise OSError("ctypes.windll is not available on non-Windows platform")

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


def send_to_trash(path, original_path=None):
    """Move a file to the Recycle Bin (Windows) instead of permanent deletion.

    On non-Windows there is no recycle bin, so it falls back to os.remove.
    Raises OSError if the file remains in place after the call.

    Identity-bound destructive safety (T-49 / T-62 / T-68):
    Destruction acts strictly on the verified acquired object `path`.
    `original_path` (if provided) is recorded in durable recovery metadata,
    and is NEVER restored immediately before deletion to eliminate TOCTOU
    pathname replacement races.

    REC-004 (FINDING C): destruction requires a prior durable ACQUIRED receipt
    for this exact object — one carrying its original size and SHA-256. A
    status update may never create an implicitly verified acquisition receipt,
    and an unreadable journal refuses exactly like a missing record. When the
    gate refuses, the object is left in place for the caller to restore or
    report; it is never deleted.
    """
    abs_path = os.path.abspath(path)
    receipt = _acquired_receipt(abs_path)
    if receipt is None:
        raise OSError(
            f"no durable acquired receipt with complete identity; "
            f"refusing to delete {abs_path}")
    if original_path and receipt.get("original_path") != os.path.abspath(original_path):
        raise OSError(
            f"receipt original_path mismatch; refusing to delete {abs_path}")

    if not record_quarantine_metadata(abs_path, original_path or receipt["original_path"],
                                      status="trashing"):
        raise OSError(
            f"recovery receipt not durable; refusing to delete {abs_path}")

    if not deletion_is_recoverable() or not hasattr(ctypes, "windll"):
        os.remove(abs_path)
        record_quarantine_metadata(abs_path, original_path or receipt["original_path"],
                                   status="deleted")
        return

    try:
        _windows_send_to_recycle_bin(abs_path)
    except OSError:
        raise
    record_quarantine_metadata(abs_path, original_path or receipt["original_path"],
                               status="recycled")


# --- Cross-process destructive ownership (T-50) --------------------------------
# W2-001 (SRC-002): two concurrent instances each authorizing against the
# other's still-present candidate can delete every copy of a group. The fix is
# a cross-process critical section over the canonical scan root, held from
# before final acquisition until the destructive outcome settles.
#
# Stale-owner recovery: no pid is consulted to decide ownership. The lock is an
# OS-level byte-range lock held for the lifetime of an open descriptor
# (msvcrt.locking on Windows, fcntl.flock elsewhere), so a crashed owner is
# released by the kernel the moment its descriptor closes — there is no stale
# file to reclaim, no remove-and-recreate window in which two contenders each
# delete the other's lock, and no PID-reuse ambiguity. The pid written into the
# file is a diagnostic for operators only and is never read back to grant a
# lock. Scope is canonicalized with realpath before hashing, so path spelling,
# case and junction/`..` aliases cannot create two independent locks for one
# physical root.
#
# The lock file lives in a fixed per-user lock directory (LOCALAPPDATA on
# Windows, XDG_STATE_HOME/~/.local/state elsewhere), never inside the scanned
# tree and never under the volatile temp dir: a lock inside the root would
# pollute scan results and be unwritable on read-only roots, and a TMP/TEMP-
# keyed directory would give two instances with different environment settings
# two different locks for the same root.

def _lock_dir():
    """Fixed per-user directory for destructive-lock files.

    Deliberately independent of `tempfile.gettempdir()`: that value follows
    TMP/TEMP/TMPDIR, so two instances with different environment settings would
    hash the same root to two different lock files and never serialize.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_STATE_HOME")
                or os.path.join(os.path.expanduser("~"), ".local", "state"))
    return os.path.join(base, "smart-vac-locks")


def _lock_path_for(root=None):
    """Fixed per-user destructive-lock path (T-50 overlapping-roots contract).

    All destructive batches executed by this user serialize on one OS-level
    lock in the fixed per-user lock directory, eliminating simultaneous critical
    sections between overlapping, nested (parent/child), aliased or concurrent
    scan roots without brittle path hierarchy walking or deadlock risk.
    """
    return os.path.join(_lock_dir(), "destructive.lock")


def _os_lock_file(lock_path, timeout=30.0, poll=0.05):
    """Acquire an OS-level exclusive lock on `lock_path`, creating it if needed.

    The lock is held on an open descriptor, not merely verified by creation, so
    the operating system — not a re-read pid — decides who owns it:

    * Windows: msvcrt.locking on one byte of a dedicated lock fd. O_CREAT without
      O_EXCL makes the descriptor itself the exclusive resource, and the nsleep
      callers loop until the byte is free. O_EXCL is not used: the file may
      survive from an earlier run, and removing-then-recreating it is exactly
      the TOCTOU the old code could lose.
    * POSIX: fcntl.flock on the same descriptor, which the kernel releases on
      process death.

    Returns the locked file descriptor; the caller MUST _os_unlock_file it.

    A live holder is waited out for `timeout` and then raises TimeoutError. An
    inspection failure (an unreadable or unwritable path) is never treated as a
    dead owner: if the path cannot be opened at all the acquire fails closed
    with the underlying error rather than assuming the owner is gone, so a
    permission problem cannot hand the critical section to a second instance.
    """
    deadline = time.monotonic() + timeout
    # The fd is opened once and outlives the poll loop. A lock whose creator
    # died is released by the OS (Windows: handle close on exit; POSIX: flock
    # release on last close), so the loop simply picks it up — no pid probe, no
    # remove-and-recreate race, no PID-reuse ambiguity.
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"destructive lock held by another instance: {lock_path}")
                time.sleep(poll)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _os_unlock_file(fd):
    """Release an fd acquired by _os_lock_file and close it.

    Ownership lives on the descriptor, not the filename: unlinking the file is
    deliberately NOT done, because removing a lock file another instance is
    about to open is precisely how two owners could both hold the critical
    section. A byte-range lock survives the unlink until every descriptor that
    holds it is closed.
    """
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        # The close below releases whatever this fd still holds; an unlock
        # failure never turns into a second owner.
        pass
    try:
        os.close(fd)
    except OSError:
        pass


class _DestructiveLock:
    """A held OS-level destructive lock, releasable exactly once.

    Holding the fd is the ownership; the payload pid inside the file is written
    for operators only and never read back to decide a reclamation, so no
    PID-reuse or stale-payload probe can manufacture a second owner.
    """

    def __init__(self, lock_path, fd):
        self.lock_path = lock_path
        self.fd = fd
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        _os_unlock_file(self.fd)


def _acquire_lock_file(lock_path, timeout=30.0, poll=0.05, root=None):
    """Own `lock_path` at the OS level, waiting up to `timeout` for the holder.

    Returns a `_DestructiveLock`. Raises TimeoutError when another live
    instance holds the lock for the whole timeout — the caller must then refuse
    the destructive operation rather than proceed unsynchronized.
    """
    fd = _os_lock_file(lock_path, timeout=timeout, poll=poll)
    held = _DestructiveLock(lock_path, fd)
    # The recorded pid and root are diagnostic only: acquisition was decided
    # by the OS, and it is never read back to reclaim, so a reused or stale
    # payload cannot grant a second owner. Writing it inside the lock keeps
    # a reader from ever observing a half-payload from a live owner created
    # between the open and the lock.
    try:
        os.ftruncate(fd, 0)
        diag = f"pid={os.getpid()}"
        if root:
            diag += f" root={os.path.normcase(os.path.realpath(root))}"
        os.write(fd, f"{diag}\n".encode("ascii"))
        os.fsync(fd)
    except OSError:
        pass
    return held


def _release_lock_file(lock):
    """Release a `_DestructiveLock` (or, tolerantly, a bare path/fd).

    Accepting a path keeps the previous signature usable for callers that only
    knew the name; a bare path can only be used to delete, which is not a safe
    ownership release, so a stale file is left in place for its own holder to
    release rather than being unlinked on someone else's behalf.
    """
    if isinstance(lock, _DestructiveLock):
        lock.release()
        return
    if isinstance(lock, int):
        _os_unlock_file(lock)
        return
    # A path holds no ownership: the OS-level lock lives on a descriptor this
    # object never had. Leaving the file is the safe outcome.
    return


@contextmanager
def destructive_lock(root=None, timeout=30.0, poll=0.05):
    """T-50: cross-process ownership around the destructive sequence.

    Serializes acquisition/revalidation/destruction across cooperating
    instances (same root or overlapping/nested roots). Yields True when ownership
    was taken. A TimeoutError propagates when a live owner never releases —
    the caller must refuse, never fall through to an unsynchronized delete.
    """
    lock_path = _lock_path_for(root)
    held = _acquire_lock_file(lock_path, timeout=timeout, poll=poll, root=root)
    try:
        yield True
    finally:
        if held:
            _release_lock_file(held)


def _pid_alive(pid):
    """Cross-platform liveness probe for the recorded owner pid.

    pid 0 means "no readable owner" — no payload line, an unparsable one, or an
    explicit 0 — and is reported alive on purpose: it is indistinguishable from
    a live owner that has created the lock but not yet written its pid, and
    reclaiming it could delete a lock a live instance owns. Our own pid reads as
    alive too, which is what makes a nested acquire refuse (bounded
    TimeoutError) instead of self-deleting the lock this process holds.

    This probe is diagnostic only. It is never the authority for taking a lock:
    the OS-level lock in _os_lock_file decides ownership, so an OpenProcess or
    os.kill failure can never unlock a live owner's critical section. It remains
    for logging and for tests that assert the old contract is not relied upon.
    """
    if not pid or pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes as _ct
            process = _ct.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if process:
                _ct.windll.kernel32.CloseHandle(process)
                return True
            err = _ct.windll.kernel32.GetLastError()
            # ERROR_ACCESS_DENIED: the process exists but this user may not
            # inspect it. Absence of permission is not absence of process.
            if err == 5:
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except OSError as e:
        # EPERM means "it is there, you may not signal it" — permission, not
        # absence. Only ESRCH proves the process is gone.
        import errno
        if e.errno == errno.EPERM:
            return True
        return False
    except Exception:
        return True
    return True


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

    def _walk_onerror(self, stats, root_dir=None):
        def _cb(err):
            stats['errors'].append((str(err.filename), f'walk: {err}'))
            stats['skipped'] += 1
            if root_dir is not None:
                err_file = getattr(err, 'filename', None)
                is_root = False
                if err_file:
                    try:
                        is_root = os.path.samefile(err_file, root_dir)
                    except (OSError, ValueError, AttributeError, TypeError):
                        is_root = (os.path.normcase(os.path.abspath(str(err_file))) ==
                                   os.path.normcase(os.path.abspath(str(root_dir))))
                elif stats['scanned'] == 0:
                    is_root = True
                if is_root:
                    stats['root_error'] = True
                    stats['root_error_obj'] = err
        return _cb

    def verify_record(self, rec, full_hash=True):
        """Return (ok, reason): True only if the file on disk still matches the scanned identity.

        Used by CORE-002 to refuse deletion of files changed/replaced/missing since scan.
        CORE-001 (SRC-001): a symbolic link / reparse alias is never a verified
        surviving content copy — even if it still resolves to the right bytes.
        PERF-001 (SRC-002): full_hash=False skips full SHA-256 computation for cheap
        pre-confirmation preview, while final destructive authority keeps full_hash=True.
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
        if not full_hash:
            return True, 'ok'
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
        stats = {'scanned': 0, 'hashed': 0, 'skipped': 0, 'errors': [], 'state': 'completed', 'reason': None}
        if not os.path.isdir(root_dir):
            stats['state'] = 'failed'
            stats['reason'] = 'invalid_root'
            stats['errors'].append((str(root_dir), 'invalid or nonexistent directory'))
            return ScanResult(groups=[], stats=stats)

        # PERF-004 (SRC-001): single per-size slot, promoted to a list only on the
        # second path of that size; no whole-tree reverse path index. Size travels
        # with the candidate path into the sample stage.
        size_map = {}
        files_seen = 0

        for dirpath, dirnames, filenames in os.walk(root_dir, onerror=self._walk_onerror(stats, root_dir)):
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

        if stats.get('root_error'):
            stats['state'] = 'failed'
            stats['reason'] = 'root_unreadable'
            return ScanResult(groups=[], stats=stats)

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
            if cancel_event and cancel_event.is_set():
                stats['state'] = 'cancelled'
                break
            if len(paths) < 2:
                continue
            if size <= self.block_size:
                # T-56 (PERF-003): sample covers entire file; sfp is authoritative full SHA-256
                for path in paths:
                    stats['hashed'] += 1
                    fingerprint_map.setdefault((size, sfp), []).append(path)
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

    def plan_deletion(self, groups, selected_records, full_hash=True):
        """Plan a safe deletion.

        Returns dict with:
          approved      -> FileRecords safe to delete (re-verified, group keeps >=1 verified copy)
          skipped       -> list of (FileRecord, reason) not deleted (changed/missing/refused)
          refused_groups-> list of group_ids where deleting would remove the last verified copy
          keepers       -> dict mapping group_id to a verified unselected keeper candidate (PERF-001)
        Enforces CORE-001 (never delete the last verified copy) and CORE-002 (reject stale paths).

        PERF-001 (SRC-001): groups are indexed by id once — no repeated linear scan.
        PERF-002 (SRC-001): survivor verification stops at the first valid keeper.
        PERF-001 (SRC-002): carry explicit unselected keeper candidates to avoid
        O(N^2) member rediscovery in the final destructive phase; support full_hash=False
        for preview phase to eliminate redundant SHA-256 I/O before confirmation.
        """
        by_group = {}
        for r in selected_records:
            by_group.setdefault(r.group_id, []).append(r)
        groups_by_id = {g.group_id: g for g in groups}

        approved, skipped, refused_groups = [], [], []
        keepers = {}

        def _verify(rec):
            try:
                return self.verify_record(rec, full_hash=full_hash)
            except TypeError:
                return self.verify_record(rec)

        for gid, recs in by_group.items():
            group = groups_by_id.get(gid)
            if group is None:
                for r in recs:
                    skipped.append((r, 'orphan_group'))
                continue
            verified = []
            for r in recs:
                ok, reason = _verify(r)
                if ok:
                    verified.append(r)
                else:
                    skipped.append((r, reason))
            if not verified:
                continue
            verified_ids = {id(r) for r in verified}
            # PERF-001 (SRC-002): prefer an unselected member as the candidate keeper
            # so the final phase does not rely on records planned for deletion.
            keeper = None
            for m in group.members:
                if id(m) in verified_ids:
                    continue
                ok, _ = _verify(m)
                if ok:
                    keeper = m
                    break
            if keeper is None:
                # CORE-001: deleting these would leave zero verified copies -> refuse the whole group.
                refused_groups.append(gid)
                for r in verified:
                    skipped.append((r, 'last_copy_refused'))
                continue
            keepers[gid] = keeper
            approved.extend(verified)
        return {'approved': approved, 'skipped': skipped, 'refused_groups': refused_groups, 'keepers': keepers}


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


class _GroupDeletionState:
    """T-54 / T-63: stateful survivor tracking for bulk deletion in DuplicateFinderApp.delete_selected.

    Eliminates quadratic member traversal while strictly enforcing content validity at the
    destructive boundary (P0-B / T-63: never accepts a keeper based on cached metadata alone)."""

    def __init__(self, group, keeper=None):
        self.group = group
        self.keeper = keeper
        self.keeper_verified = False
        self.member_idx = 0
        self.traversal_checks = 0

    def get_survivor(self, current_rec, deleted_paths, engine, full_hash=True):
        """Return an eligible, verified survivor FileRecord, or None if none remain.
        Preserves T-45: members whose deletion failed are not in deleted_paths and
        remain eligible as survivors.
        P0-B / T-63: Never accepts a keeper merely because cached metadata matches.
        Enforces content verification at the destructive boundary."""
        # 1. Preferred keeper check in O(1)
        if self.keeper is not None:
            self.traversal_checks += 1
            if self.keeper is not current_rec and self.keeper.path not in deleted_paths:
                ok, _ = engine.verify_record(self.keeper, full_hash=full_hash)
                if ok:
                    self.keeper_verified = True
                    return self.keeper
                else:
                    self.keeper = None
                    self.keeper_verified = False

        # 2. Fallback: monotonic scan over group.members
        members = self.group.members
        n = len(members)
        while self.member_idx < n and members[self.member_idx].path in deleted_paths:
            self.member_idx += 1

        idx = self.member_idx
        while idx < n:
            self.traversal_checks += 1
            m = members[idx]
            if m is not current_rec and m.path not in deleted_paths:
                ok, _ = engine.verify_record(m, full_hash=full_hash)
                if ok:
                    self.keeper = m
                    self.keeper_verified = True
                    self.member_idx = idx
                    return m
            idx += 1

        return None


class DuplicateFinderApp:
    def __init__(self, root):
        self.root = root
        # CORE-006: window title driven by the single VERSION constant.
        self.root.title(f"💣 Уничтожитель Дубликатов v{VERSION}")
        self.engine = DuplicateEngine()
        # Bounded queue (PERF-002): producer backpressure, results never dropped.
        self.queue = queue.Queue(maxsize=40000)
        # Control queue (PERF-002 T-55): separate channel for terminal/error/status so
        # control messages never block behind saturated result backpressure.
        self.control_queue = queue.Queue()
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
        self.browse_button = None
        self.current_result_root = None
        # REC-003: a previous process killed at an acquisition boundary left
        # prepared/acquired transactions; reconcile them (evidence-only, no
        # file is ever destroyed) before any new destructive work can start.
        reconcile_transactions()
        self._build_ui()

    def _build_ui(self):
        main_frame = tk.Frame(self.root)
        main_frame.pack(padx=10, pady=10, fill="both", expand=True)

        tk.Label(main_frame, text="Выбери папку для поиска дубликатов:").pack(anchor="w")
        self.path_var = tk.StringVar()
        tk.Entry(main_frame, textvariable=self.path_var, width=80).pack(fill="x", pady=3)
        self.browse_button = tk.Button(main_frame, text="🔍 Обзор", command=self.browse_folder,
                                       bg="#4CAF50", fg="white", activebackground="#45a049")
        self.browse_button.pack(pady=3)

        def _on_path_var_write(*_args):
            self._on_path_changed()

        try:
            self.path_var.trace_add("write", _on_path_var_write)
        except AttributeError:
            self.path_var.trace("w", _on_path_var_write)

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

    def _invalidate_results(self):
        """T-52: disarm and clear all scan results when scan root changes or is invalidated."""
        tree = getattr(self, "tree", None)
        if tree is not None and hasattr(tree, "get_children"):
            children = tree.get_children()
            if children:
                tree.delete(*children)
        elif tree is not None and hasattr(tree, "delete"):
            try:
                tree.delete()
            except TypeError:
                pass
        if hasattr(self, "check_vars"):
            self.check_vars.clear()
        if hasattr(self, "item_path_map"):
            self.item_path_map.clear()
        if hasattr(self, "path_to_item"):
            self.path_to_item.clear()
        self.current_groups = []
        self.current_records = []
        if hasattr(self, "_sessions"):
            self._sessions.clear()
        self.current_result_root = None
        status_var = getattr(self, "status_var", None)
        if status_var is not None:
            status_var.set("Папка изменена — результаты сброшены. Выполните повторный поиск.")

    def _on_path_changed(self):
        """T-52: react to path selector changes. Disarm stale records and cancel active scans."""
        path_var = getattr(self, "path_var", None)
        new_val = path_var.get() if path_var is not None else ""
        norm_new = os.path.normcase(os.path.abspath(new_val)) if new_val and new_val.strip() else ""

        worker = getattr(self, "worker_thread", None)
        if worker and worker.is_alive():
            if self.current_result_root is None or norm_new != self.current_result_root:
                self.cancel_scan()
                self._invalidate_results()
                return

        if self.current_result_root is not None and norm_new != self.current_result_root:
            self._invalidate_results()

    def browse_folder(self):
        worker = getattr(self, "worker_thread", None)
        if worker and worker.is_alive():
            self.cancel_scan()
            self._invalidate_results()
        folder = filedialog.askdirectory()
        if folder:
            self.path_var.set(folder)

    def start_finding_duplicates(self):
        # CORE-003: detect an active scan BEFORE mutating any UI/model state.
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Идёт работа", "Поиск уже выполняется!")
            return
        root_dir = self.path_var.get()
        tree = getattr(self, "tree", None)
        if tree is not None:
            tree.delete(*tree.get_children())
        self.check_vars.clear()
        self.item_path_map.clear()
        self.path_to_item.clear()
        self.current_groups = []
        self.current_records = []
        self._hover_item = None
        self.status_var.set("⏳ Поиск дубликатов...")
        self.current_scan_id += 1
        scan_id = self.current_scan_id
        # T-52: bind session to normalized canonical scan root
        self.current_result_root = os.path.normcase(os.path.abspath(root_dir)) if root_dir and root_dir.strip() else None
        # T-033: only the live session is useful; older ones are just retained FileRecords.
        self._sessions.clear()
        self.cancel_event = threading.Event()
        self._set_stop_enabled(True)
        browse_button = getattr(self, "browse_button", None)
        if browse_button is not None:
            browse_button.config(state="disabled")
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
        browse_button = getattr(self, "browse_button", None)
        if browse_button is not None:
            browse_button.config(state="normal")

    def _scan_worker(self, scan_id, root_dir, cancel_event):
        def _put_control(msg):
            # PERF-002 (SRC-002 T-55): control traffic is separated from bulk results
            # so terminal/error/status never block on a saturated result queue.
            cq = getattr(self, "control_queue", None)
            if cq is not None:
                cq.put(msg)
            else:
                self.queue.put(msg)

        if not os.path.isdir(root_dir):
            # CORE-003 (SRC-001): every worker-originated event is generation-scoped.
            _put_control(("error", scan_id, "Укажи корректную папку!"))
            _put_control(("terminal", scan_id, "failed",
                          {"state": "failed", "reason": "invalid_root", "scanned": 0, "hashed": 0, "skipped": 0, "errors": []}))
            return

        def status_cb(text):
            _put_control(("status", scan_id, text))

        def progress_cb(batch, stats):
            for g in batch:
                for m in g.members:
                    # PERF-002 (SRC-002 T-55): cancellation-aware bounded backpressure.
                    # Attempt put with a short timeout; on queue.Full, re-check cancel_event.
                    # When cancelled, stop publishing immediately so worker is never trapped.
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        try:
                            try:
                                self.queue.put(("add_result", scan_id, m), timeout=0.05)
                            except TypeError:
                                self.queue.put(("add_result", scan_id, m))
                            break
                        except queue.Full:
                            continue

        try:
            # W2-002 (SRC-001): any unexpected engine/callback exception must still
            # produce exactly one truthful failed terminal, or the UI freezes in
            # "searching" forever on a dead daemon worker (console=False build).
            result = self.engine.scan(root_dir, cancel_event=cancel_event,
                                      status_cb=status_cb, progress_cb=progress_cb)
            self._sessions[scan_id] = result
            _put_control(("terminal", scan_id, result.stats["state"], result.stats))
        except Exception as e:  # noqa: BLE001 - never BaseException/SystemExit
            _put_control(("error", scan_id, f"Ошибка сканирования: {e}"))
            _put_control(("terminal", scan_id, "failed",
                          {"state": "failed", "reason": "worker_exception", "scanned": 0, "hashed": 0, "skipped": 0,
                           "errors": [str(e)]}))

    def process_queue(self):
        if getattr(self, "_shutting_down", False):
            return
        last_status = None
        terminal_seen = False
        count = 0
        BATCH = 800  # PERF-002: bound work per Tk tick, then reschedule.

        # Control messages processed first (PERF-002 T-55): terminal/error/status
        # are never starved or delayed behind bulk add_result batches.
        cq = getattr(self, "control_queue", None)
        if cq is not None:
            while True:
                try:
                    cmsg = cq.get_nowait()
                except queue.Empty:
                    break
                kind = cmsg[0]
                if kind == "status":
                    if cmsg[1] == self.current_scan_id and not terminal_seen:
                        last_status = cmsg[2]
                elif kind == "terminal":
                    if cmsg[1] == self.current_scan_id:
                        self._on_terminal(cmsg[2], cmsg[3])
                        last_status = None
                        terminal_seen = True
                elif kind == "error":
                    if cmsg[1] == self.current_scan_id:
                        messagebox.showerror("Ошибка", cmsg[2])

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
                if getattr(self, "current_result_root", None) is None:
                    continue  # T-52: discard results if root was invalidated
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
        browse_button = getattr(self, "browse_button", None)
        if browse_button is not None:
            browse_button.config(state="normal")
        if state in ("cancelled", "failed"):
            # W2-001 (SRC-001): a cancelled scan must not leave partial rows armed as
            # if they were a complete duplicate list (T-37 descendant).
            # T-51: failed terminal leaves no actionable partial model/tree state.
            tree = getattr(self, "tree", None)
            if tree is not None and hasattr(tree, "get_children"):
                children = tree.get_children()
                if children:
                    tree.delete(*children)
            elif tree is not None and hasattr(tree, "delete"):
                try:
                    tree.delete()
                except TypeError:
                    pass
            if hasattr(self, "check_vars"):
                self.check_vars.clear()
            if hasattr(self, "item_path_map"):
                self.item_path_map.clear()
            if hasattr(self, "path_to_item"):
                self.path_to_item.clear()
            self.current_groups = []
            self.current_records = []
            self.current_result_root = None
        else:
            path_var = getattr(self, "path_var", None)
            root_dir = path_var.get() if path_var is not None else ""
            self.current_result_root = os.path.normcase(os.path.abspath(root_dir)) if root_dir and root_dir.strip() else None
        n = len(self.current_groups)
        if state == "completed":
            self.status_var.set(f"🔎 Завершено: групп дубликатов: {n}")
        elif state == "completed_with_errors":
            self.status_var.set(f"🔎 Завершено (с ошибками): групп: {n}, пропущено файлов: {stats.get('skipped', 0) if isinstance(stats, dict) else 0}")
        elif state == "failed":
            reason = stats.get("reason") if isinstance(stats, dict) else None
            if reason == "invalid_root":
                self.status_var.set("❌ Поиск не удался (неверная папка)")
            elif reason == "root_unreadable":
                self.status_var.set("❌ Поиск не удался (папка недоступна для чтения)")
            elif reason == "worker_exception":
                self.status_var.set("❌ Поиск не удался (ошибка сканирования)")
            else:
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

    def _disarm_records(self, recs):
        """Remove records from UI indexes (tree, check_vars, path_to_item, item_path_map, current_records)."""
        if not recs:
            return
        rec_ids = {id(r) for r in recs}
        for rec in recs:
            item = self.path_to_item.pop(rec.path, None)
            if item is not None:
                self.tree.delete(item)
                self.item_path_map.pop(item, None)
            self.check_vars.pop(rec.path, None)
        self.current_records = [r for r in self.current_records if id(r) not in rec_ids]

    def _normalize_groups_after_change(self, removed_recs=None, stale_recs=None):
        """T-53 (W2-003): settle model and UI after deletion / stale-record invalidation.

        1. Disarm removed (deleted) and terminal-stale records.
        2. Remove disarmed records from their groups.
        3. For each group:
           - >= 2 members: retain group and elect exactly one original.
           - < 2 members: remove group from current_groups and remove any surviving
             singleton member from the duplicate-results UI without touching disk.
        """
        all_to_remove = []
        if removed_recs:
            all_to_remove.extend(removed_recs)
        if stale_recs:
            all_to_remove.extend(stale_recs)

        if all_to_remove:
            remove_ids = {id(r) for r in all_to_remove}
            self._disarm_records(all_to_remove)
            for g in self.current_groups:
                g.members = [m for m in g.members if id(m) not in remove_ids]

        surviving_groups = []
        singletons_to_disarm = []
        for g in self.current_groups:
            if len(g.members) >= 2:
                surviving_groups.append(g)
            elif len(g.members) == 1:
                singletons_to_disarm.extend(g.members)

        if singletons_to_disarm:
            self._disarm_records(singletons_to_disarm)

        self.current_groups = surviving_groups
        self._normalize_originals()

    def _batch_disarm(self, removed_recs, collapse_singletons=False):
        """Remove all traces of deleted records and renormalize groups (CORE-002/003).

        PERF-001 (SRC-001): one rebuild per batch instead of an O(N) list.remove
        plus a linear group scan per deleted record; only successfully deleted
        records are passed in, so failed deletions stay armed/present.
        """
        if collapse_singletons:
            self._normalize_groups_after_change(removed_recs=removed_recs)
        else:
            if not removed_recs:
                return
            removed_ids = {id(r) for r in removed_recs}
            self._disarm_records(removed_recs)
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
        path_var = getattr(self, "path_var", None)
        current_root = os.path.normcase(os.path.abspath(path_var.get())) if path_var and path_var.get().strip() else ""
        result_root = getattr(self, "current_result_root", None)
        if result_root is None and current_root:
            result_root = current_root

        if result_root is not None and current_root != result_root:
            self._invalidate_results()
            messagebox.showwarning("Папка изменена", "Выбранная папка изменилась. Выполните повторный поиск дубликатов.")
            return

        selected = [r for r in self.current_records
                    if r.path in self.check_vars and self.check_vars[r.path].get()]
        if not selected:
            messagebox.showwarning("Ничего не выбрано", "Выбери файлы для удаления.")
            return

        # T-52 defense-in-depth: verify all selected records belong to current_root
        if current_root:
            stale_scope = False
            for r in selected:
                r_norm = os.path.normcase(os.path.abspath(r.path))
                try:
                    if os.path.commonpath([r_norm, current_root]) != current_root:
                        stale_scope = True
                        break
                except (ValueError, OSError):
                    stale_scope = True
                    break
            if stale_scope:
                self._invalidate_results()
                messagebox.showwarning("Несоответствие папки", "Файлы не принадлежат выбранной папке. Результаты сброшены.")
                return

        # CORE-002 (SRC-001): this plan is a PREVIEW only, never final destructive
        # authorization; every approved record is revalidated at the deletion
        # boundary below, after the user confirms.
        # PERF-001 (SRC-002): full_hash=False eliminates redundant SHA-256 during preview.
        plan = self.engine.plan_deletion(self.current_groups, selected, full_hash=False)
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

        # T-53 (W2-003): no destructive confirmation for zero approved records.
        # Terminal-stale records are invalidated and disarmed immediately.
        if not plan["approved"]:
            stale_recs = []
            path_to_rec = {r.path: r for r in self.current_records}
            for item in plan.get("skipped", []):
                r = item[0] if isinstance(item, (tuple, list)) else item
                if isinstance(r, FileRecord) and r not in stale_recs:
                    stale_recs.append(r)
                elif isinstance(r, str) and r in path_to_rec and path_to_rec[r] not in stale_recs:
                    stale_recs.append(path_to_rec[r])
            if stale_recs:
                self._normalize_groups_after_change(stale_recs=stale_recs)
            stale_total = len(plan.get("skipped", []))
            self.status_var.set(f"✅ Удалено файлов: 0 (пропущено: {stale_total})")
            return

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

        # T-50: the whole final-boundary sequence runs under cross-process
        # destructive ownership for the scan root, so two instances can never
        # both authorize against each other's still-present candidate.
        try:
            with destructive_lock(self.path_var.get()):
                self._execute_destructive_batch(plan)
        except TimeoutError as e:
            messagebox.showerror(
                "Удаление отложено",
                f"Другой экземпляр программы сейчас удаляет файлы в этой папке.\n"
                f"Повтори попытку через несколько секунд.\n\n{e}",
            )

    def _execute_destructive_batch(self, plan):
        """T-49/T-50: the final-boundary destructive sequence. The caller holds
        the cross-process destructive lock for the scan root."""
        # CORE-005: open the log before destructive work; keep fs vs log outcomes separate.
        log_fh = None
        log_err = None
        if self.log_var.get():
            try:
                log_fh = open(LOG_FILE, "a", encoding="utf-8")
            except OSError as e:
                log_err = f"лог недоступен: {e}"
        # CORE-002 (SRC-001): final destructive authorization happens here, per
        # record, immediately before the destructive step — never carried over
        # from the pre-confirmation plan. A post-plan changed/replaced selected
        # file or a vanished last survivor is classified stale and left untouched.
        # T-49 (SRC-002): authorization is identity-bound — verify_record checks
        # the object at the path, then acquire_candidate moves THAT object to a
        # unique quarantine name; the acquired object is re-verified after the
        # move and only the acquired object is ever destroyed. No concurrent
        # replacement can sit between the verification and the deletion.
        groups_by_id = {g.group_id: g for g in self.current_groups}  # PERF-001 (SRC-001)
        plan_keepers = plan.get("keepers", {}) if isinstance(plan, dict) else {}
        group_states = {gid: _GroupDeletionState(g, keeper=plan_keepers.get(gid))
                        for gid, g in groups_by_id.items()}  # PERF-001 (SRC-002)
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
            g_state = group_states.get(rec.group_id)
            survivor = g_state.get_survivor(rec, deleted_paths, self.engine) if g_state else None
            if survivor is None:
                stale_now.append((rec.path, 'last_copy_refused'))
                continue
            # T-49/T-68: the durable intent is journaled BEFORE the rename
            # (REC-002), the acquired receipt must be durable and complete
            # (REC-004), and only an identity-bearing receipt authorizes the
            # destructive step that follows.
            intent = prepare_acquisition(rec.path, size=rec.size,
                                         fingerprint=rec.fingerprint)
            if intent is None:
                failed.append((rec.path, 'recovery intent not durable'))
                continue
            try:
                qpath = acquire_candidate(rec.path, intent=intent)
            except OSError as e:
                _abandon_intent(intent)
                failed.append((rec.path, str(e)))
                continue
            if not record_quarantine_metadata(qpath, rec.path, size=rec.size,
                                              fingerprint=rec.fingerprint,
                                              status="acquired",
                                              txn_id=intent["txn_id"]):
                # No durable receipt -> no recovery evidence -> no destruction.
                failed.append((rec.path, 'receipt_not_durable'))
                if not restore_candidate(qpath, rec.path):
                    failed.append((rec.path, 'restore_required (kept as ' + qpath + ')'))
                continue
            # The acquired object must still be the scanned identity: verify
            # through the quarantine name (size + full fingerprint).
            acquired = FileRecord(path=qpath, size=rec.size,
                                  fingerprint=rec.fingerprint)
            a_ok, a_reason = self.engine.verify_record(acquired)
            if not a_ok:
                stale_now.append((rec.path, f'acquisition_{a_reason}'))
                if not restore_candidate(qpath, rec.path):
                    failed.append((rec.path, 'restore_required (kept as ' + qpath + ')'))
                continue
            # T-45: re-check the survivor against the post-acquisition disk
            # state (the acquisition itself cannot invalidate a distinct
            # file, but a concurrent actor may have moved between checks).
            survivor = g_state.get_survivor(rec, deleted_paths, self.engine) if g_state else None
            if survivor is None:
                stale_now.append((rec.path, 'last_copy_refused'))
                if not restore_candidate(qpath, rec.path):
                    failed.append((rec.path, 'restore_required (kept as ' + qpath + ')'))
                continue
            try:
                try:
                    send_to_trash(qpath, original_path=rec.path)
                except TypeError:
                    send_to_trash(qpath)
            except OSError as e:
                failed.append((rec.path, str(e)))
                if os.path.lexists(qpath) and not restore_candidate(qpath, rec.path):
                    failed.append((rec.path, 'restore_required (kept as ' + qpath + ')'))
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

        # Collect terminal-stale records to invalidate and disarm (T-53 W2-003)
        stale_recs = []
        path_to_rec = {r.path: r for r in self.current_records}
        for item in plan.get("skipped", []):
            r = item[0] if isinstance(item, (tuple, list)) else item
            if isinstance(r, FileRecord) and r not in stale_recs:
                stale_recs.append(r)
            elif isinstance(r, str) and r in path_to_rec and path_to_rec[r] not in stale_recs:
                stale_recs.append(path_to_rec[r])
        for item in stale_now:
            r = item[0] if isinstance(item, (tuple, list)) else item
            if isinstance(r, FileRecord) and r not in stale_recs:
                stale_recs.append(r)
            elif isinstance(r, str) and r in path_to_rec and path_to_rec[r] not in stale_recs:
                stale_recs.append(path_to_rec[r])

        self._normalize_groups_after_change(removed_recs=disarmed, stale_recs=stale_recs)

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
        # T-52: if selector differs from current_result_root, invalidate old scan results first
        norm_dir = os.path.normcase(os.path.abspath(root_dir))
        result_root = getattr(self, "current_result_root", None)
        if result_root and result_root != norm_dir:
            self._invalidate_results()
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

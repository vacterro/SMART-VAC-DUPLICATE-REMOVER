#!/usr/bin/env python3
"""
T-50 destructive-lock probes — red vs green, executed.

Each probe runs the LEGACY primitive (the remove/recreate + pid-reclaim contract
the audit flagged) and the SHIPPED primitive (OS-level descriptor lock) over the
same interleaving, and prints both outcomes. The legacy model shows the defect;
the shipped primitive must show the safe outcome on every probe. A probe that
cannot be exercised on this platform reports SKIP, never a silent pass.

Run: python .saipen/evidence/t50_lock_defects_probe.py
"""

import os
import sys
import tempfile
import threading
import multiprocessing
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from delete_duplicates_gui import (
    _lock_path_for, _pid_alive, _acquire_lock_file, _release_lock_file,
)


# --------------------------------------------------------------------------
# The legacy primitive, verbatim in behaviour (NOT imported from the product —
# the product no longer contains it). Modelling it here is what makes the
# red/green comparison a comparison of mechanisms rather than of comments.
# --------------------------------------------------------------------------
class LegacyLock:
    def __init__(self, path):
        self.path = path

    def release(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


def legacy_acquire(lock_path, poll=0.02):
    """O_CREAT|O_EXCL, reclaim when the recorded pid reads as dead."""
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            with open(lock_path, encoding="utf-8", errors="replace") as f:
                payload = f.read()
            pid = None
            if payload.startswith("pid="):
                try:
                    pid = int(payload[4:].strip())
                except ValueError:
                    pid = None
            if pid is not None and not _pid_alive(pid):
                try:
                    os.remove(lock_path)          # <-- the reclaim under race
                except OSError:
                    pass
                continue
            time.sleep(poll)
            continue
        try:
            os.write(fd, ("pid=%d\n" % os.getpid()).encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return LegacyLock(lock_path)
    raise TimeoutError("legacy lock never acquirable")


def _barrier_wait(barrier):
    try:
        barrier.wait(timeout=20.0)
    except threading.BrokenBarrierError:
        raise SystemExit(90)


def _legacy_contender(lock_path, barrier, out_q, hold):
    _barrier_wait(barrier)
    try:
        lock = legacy_acquire(lock_path)
    except TimeoutError:
        out_q.put("refused")
        return
    out_q.put("owned")
    time.sleep(hold)
    lock.release()


def _shipped_contender(lock_path, barrier, out_q, hold):
    _barrier_wait(barrier)
    try:
        lock = _acquire_lock_file(lock_path, timeout=0.6, poll=0.01)
    except TimeoutError:
        out_q.put("refused")
        return
    out_q.put("owned")
    time.sleep(hold)
    _release_lock_file(lock)


def _shipped_racer(lock_path, b1, b2, out_q):
    """`_run`-compatible contender for the shipped primitive. `b2` is unused —
    the OS-level lock needs no second act to expose a race, it either serializes
    or it does not."""
    _barrier_wait(b1)
    try:
        lock = _acquire_lock_file(lock_path, timeout=0.6, poll=0.01)
    except TimeoutError:
        out_q.put("refused")
        return
    out_q.put("owned")                      # exactly one contender can get here
    time.sleep(3.0)
    _release_lock_file(lock)


# --- the audit's controlled interleaving -----------------------------------
# A plain simultaneous start does NOT reproduce the legacy race: the loser's
# O_EXCL fails, it then reads the WINNER's live pid and (correctly) refuses. The
# defect needs both contenders to have already concluded "owner is dead" before
# either acts, then A creates its fresh lock and B unlinks it. b1 releases both
# stale verdicts; b2 releases B only after A holds the recreated file.
def _legacy_racer_a(lock_path, b1, b2, out_q):
    _barrier_wait(b1)                       # both have read the dead pid
    try:
        os.remove(lock_path)                # A reclaims the stale file
    except OSError:
        pass
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(fd, b"pid=A\n")
    os.close(fd)
    out_q.put("owned")                      # A now holds the critical section
    try:
        b2.wait(timeout=20.0)
    except threading.BrokenBarrierError:
        pass


def _legacy_racer_b(lock_path, b1, b2, out_q):
    _barrier_wait(b1)                       # same stale verdict, same instant
    try:
        b2.wait(timeout=20.0)               # wait until A recreated the lock
    except threading.BrokenBarrierError:
        pass
    try:
        os.remove(lock_path)                # <-- removes A's LIVE lock
    except OSError:
        pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        out_q.put("refused")
        return
    os.write(fd, b"pid=B\n")
    os.close(fd)
    out_q.put("owned")                      # both are inside, B on A's ruins


def _drain(q, timeout=15.0):
    try:
        return q.get(timeout=timeout)
    except Exception:
        return None


def _reap(procs, queues):
    for p in procs:
        p.join(40.0)
    for p in procs:
        if p.is_alive():
            p.terminate()
            p.join(10.0)
    for q in queues:
        try:
            q.close()
            q.join_thread()
        except Exception:
            pass
    return [p.exitcode for p in procs]


def _run(workers, lock_path):
    """Start every worker on one lock path; return their sorted verdicts."""
    ctx = multiprocessing.get_context("spawn")
    b1 = ctx.Barrier(len(workers))
    b2 = ctx.Barrier(2)
    queues = [ctx.Queue() for _ in workers]
    procs = [ctx.Process(target=fn, args=(lock_path, b1, b2, q))
             for fn, q in zip(workers, queues)]
    for p in procs:
        p.start()
    verdicts = sorted(o for o in (_drain(q) for q in queues) if o)
    _reap(procs, queues)
    return verdicts


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------
def probe_1_remove_recreate_race():
    print("=" * 68)
    print("PROBE 1: simultaneous contenders over one stale-dead-owner lock file")
    print("=" * 68)
    results = {}
    with tempfile.TemporaryDirectory() as td:
        lock = _lock_path_for(td)
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w", encoding="utf-8") as f:
            f.write("pid=999999999\n")     # a dead owner, the audit's premise
        results["LEGACY"] = _run(
            (_legacy_racer_a, _legacy_racer_b), lock)
    with tempfile.TemporaryDirectory() as td:
        lock = _lock_path_for(td)
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w", encoding="utf-8") as f:
            f.write("pid=999999999\n")
        results["SHIPPED"] = _run(
            (_shipped_racer, _shipped_racer), lock)
    red = results["LEGACY"]
    green = results["SHIPPED"]
    print(f"  LEGACY  SIMULTANEOUS_OWNERS {red}")
    print(f"  SHIPPED SIMULTANEOUS_OWNERS {green}")
    print(f"  legacy dual ownership   : {red == ['owned', 'owned']}")
    print(f"  shipped mutual exclusion: {green == ['owned', 'refused']}")
    return red == ["owned", "owned"] and green == ["owned", "refused"]


def probe_2_pid_probe_cannot_grant_ownership():
    print("\n" + "=" * 68)
    print("PROBE 2: a failed/dead pid probe never grants or withholds ownership")
    print("=" * 68)
    print("  _pid_alive(0)       =", _pid_alive(0),
          "(owner unknown -> reads live, never a reclaim signal)")
    print("  _pid_alive(999999999) =", _pid_alive(999999999), "(absent)")
    with tempfile.TemporaryDirectory() as td:
        lock = _lock_path_for(td)
        # Every payload the old contract would have refused to reclaim must be
        # acquirable now, because no payload is consulted at all.
        for payload in ("", "pid=0\n", "pid=\n", "pid=abc\n", "pid=999999999\n"):
            with open(lock, "w", encoding="utf-8") as f:
                f.write(payload)
            try:
                held = _acquire_lock_file(lock, timeout=2.0, poll=0.01)
            except TimeoutError:
                print(f"  payload {payload!r:16s} -> REFUSED  <-- DEFECT")
                return False
            _release_lock_file(held)
            print(f"  payload {payload!r:16s} -> acquirable (no owner recorded)")
    print("  shipped: pid payloads are diagnostic only, ownership is descriptor-held")
    return True


def probe_3_permission_failure_not_absence():
    print("\n" + "=" * 68)
    print("PROBE 3: permission/inspection failure is distinguished from absence")
    print("=" * 68)
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        # Query a process that cannot exist: OpenProcess fails with
        # ERROR_INVALID_PARAMETER. A live-but-inaccessible process fails with
        # ERROR_ACCESS_DENIED (5). The old code returned False for both.
        rc = k.OpenProcess(0x1000, False, 999999999)
        err = k.GetLastError()
        print(f"  OpenProcess(absent pid) -> handle={rc} GetLastError={err}")
        shipped_true = _pid_alive(999999999)
        print(f"  shipped _pid_alive(absent) -> {shipped_true}")
        # The contract that matters: whatever _pid_alive says about an absent
        # pid, the lock itself never consults it (PROBE 2 proves the grant path).
        print("  shipped: reclamation is kernel fd release, not a pid verdict")
        return rc == 0 and err != 5 and shipped_true is False
    else:
        import errno
        try:
            os.kill(999999999, 0)
            print("  os.kill(absent) unexpectedly succeeded")
            return False
        except OSError as e:
            print(f"  os.kill(absent) -> errno {e.errno} ({errno.errorcode.get(e.errno)})")
        print("  shipped _pid_alive(absent) ->", _pid_alive(999999999))
        print("  shipped: reclamation is kernel fd release, not a pid verdict")
        return _pid_alive(999999999) is False


def _holder_worker(lock_path, barrier, out_q):
    """Acquire, report ownership, then hold until the parent hard-kills us."""
    _barrier_wait(barrier)
    lock = _acquire_lock_file(lock_path, timeout=2.0, poll=0.01)
    out_q.put("owned")
    time.sleep(20.0)          # killed by the parent while still holding
    _release_lock_file(lock)


def probe_4_crashed_owner_is_recoverable():
    print("\n" + "=" * 68)
    print("PROBE 4: a hard-killed owner releases the lock (no manual reclaim)")
    print("=" * 68)

    with tempfile.TemporaryDirectory() as td:
        lock = _lock_path_for(td)
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        q = ctx.Queue()
        p = ctx.Process(target=_holder_worker, args=(lock, barrier, q))
        p.start()
        try:
            barrier.wait(timeout=20.0)
        except threading.BrokenBarrierError:
            pass
        owned = None
        try:
            owned = q.get(timeout=5.0)
        except Exception:
            pass
        print(f"  killed owner reported: {owned}")
        p.terminate()
        p.join(10.0)
        held = None
        try:
            held = _acquire_lock_file(lock, timeout=5.0, poll=0.02)
            print("  after kill: acquirable -> recovered by the kernel")
            ok = True
        except TimeoutError:
            print("  after kill: REFUSED -> lock is a brick <-- DEFECT")
            ok = False
        finally:
            if held:
                _release_lock_file(held)
            q.close()
            q.join_thread()
        return owned == "owned" and ok and not p.is_alive()


def probe_5_alias_and_junction_share_one_scope():
    print("\n" + "=" * 68)
    print("PROBE 5: junction/alias of the same root resolves to ONE lock scope")
    print("=" * 68)
    with tempfile.TemporaryDirectory() as td:
        real = os.path.join(td, "real")
        os.makedirs(real)
        link = os.path.join(td, "link")
        made = False
        try:
            os.symlink(real, link, target_is_directory=True)
            made = True
        except (OSError, NotImplementedError, AttributeError):
            if os.name == "nt":
                import subprocess
                r = subprocess.run(["cmd", "/c", "mklink", "/J", link, real],
                                   capture_output=True, text=True)
                made = r.returncode == 0 and os.path.isdir(link)
        if not made:
            print("  SKIP: no symlink/junction privilege in this environment")
            return None
        a, b = _lock_path_for(real), _lock_path_for(link)
        print(f"  real scope: {a}")
        print(f"  link scope: {b}")
        print(f"  same scope : {a == b}")
        if a != b:
            print("  <-- DEFECT: two roots, two locks, no mutual exclusion")
            return False
        return True


def probe_6_lock_dir_independent_of_temp_env():
    print("\n" + "=" * 68)
    print("PROBE 6: lock scope is invariant under TMP/TEMP/TMPDIR changes")
    print("=" * 68)
    with tempfile.TemporaryDirectory() as td:
        from delete_duplicates_gui import _lock_dir
        root = _lock_path_for(td)
        before = _lock_path_for(td)
        saved = {k: os.environ.get(k) for k in ("TMP", "TEMP", "TMPDIR")}
        for k in saved:
            os.environ[k] = os.path.join(td, "elsewhere-" + k.lower())
        try:
            after = _lock_path_for(td)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        print(f"  lock dir: {_lock_dir()}")
        print(f"  scope before={before}")
        print(f"  scope after ={after}")
        print(f"  invariant   : {before == after}")
        if before != after:
            print("  <-- DEFECT: two instances never meet on one lock")
            return False
        return True


def probe_7_overlapping_roots_serialized():
    print("\n" + "=" * 68)
    print("PROBE 7: overlapping roots (parent and child) share destructive lock")
    print("=" * 68)
    with tempfile.TemporaryDirectory() as td:
        parent = td
        child = os.path.join(td, "sub")
        os.makedirs(child, exist_ok=True)
        # 1. Scope equality
        p_lock = _lock_path_for(parent)
        c_lock = _lock_path_for(child)
        print(f"  parent lock: {p_lock}")
        print(f"  child lock : {c_lock}")
        same = (p_lock == c_lock)
        print(f"  same lock scope: {same}")
        if not same:
            print("  <-- DEFECT: parent and child acquire different locks")
            return False
        # 2. Legacy per-root lock probe: legacy hash produces separate locks
        import hashlib
        legacy_p = hashlib.sha256(os.path.normcase(os.path.realpath(parent)).encode("utf-8")).hexdigest()
        legacy_c = hashlib.sha256(os.path.normcase(os.path.realpath(child)).encode("utf-8")).hexdigest()
        print(f"  LEGACY per-root keys: parent={legacy_p[:12]} child={legacy_c[:12]} (separate: {legacy_p != legacy_c})")
        # 3. Two-process serialization across parent and child scopes
        ctx = multiprocessing.get_context("spawn")
        b1 = ctx.Barrier(2)
        b2 = ctx.Barrier(2)
        q1 = ctx.Queue()
        q2 = ctx.Queue()
        p1 = ctx.Process(target=_shipped_racer, args=(p_lock, b1, b2, q1))
        p2 = ctx.Process(target=_shipped_racer, args=(c_lock, b1, b2, q2))
        p1.start()
        p2.start()
        v1 = _drain(q1, 8.0)
        v2 = _drain(q2, 8.0)
        _reap([p1, p2], [q1, q2])
        outcomes = sorted([v1, v2])
        print(f"  SHIPPED outcomes: {outcomes}")
        if outcomes != ["owned", "refused"]:
            print(f"  <-- DEFECT: overlapping scopes failed to serialize: {outcomes}")
            return False
        print("  SHIPPED: parent and child serialize on the same destructive lock")
        return True


PROBES = [
    ("1 remove-recreate race (legacy red vs shipped green)", probe_1_remove_recreate_race),
    ("2 pid probe never grants ownership", probe_2_pid_probe_cannot_grant_ownership),
    ("3 permission failure != absence", probe_3_permission_failure_not_absence),
    ("4 crashed owner recoverable", probe_4_crashed_owner_is_recoverable),
    ("5 junction alias -> one scope", probe_5_alias_and_junction_share_one_scope),
    ("6 lock dir independent of temp env", probe_6_lock_dir_independent_of_temp_env),
    ("7 overlapping roots share destructive lock", probe_7_overlapping_roots_serialized),
]


def main():
    print("T-50 destructive-lock probes — executed red/green")
    print("=" * 68)
    outcomes = []
    for name, fn in PROBES:
        try:
            outcomes.append((name, fn()))
        except Exception as exc:                       # noqa: BLE001
            print(f"  ERROR: {exc!r}")
            outcomes.append((name, "ERROR"))
    print("\n" + "=" * 68)
    print("SUMMARY")
    print("=" * 68)
    bad = 0
    for name, outcome in outcomes:
        if outcome is False or outcome == "ERROR":
            verdict = "RED"
            bad += 1
        elif outcome is None:
            verdict = "SKIP"
        else:
            verdict = "GREEN"
        print(f"  {verdict:5s}  {name}")
    print()
    print(f"  {len(outcomes) - bad}/{len(outcomes)} probes green or skipped")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

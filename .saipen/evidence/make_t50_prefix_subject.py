#!/usr/bin/env python3
"""Build the T-50 PRE-FIX subject for the VERIFY-ORACLE-01 red control.

Copies delete_duplicates_gui.py and reverts ONLY the cross-process ownership
primitive to the legacy pid-payload remove/recreate contract the audit flagged:
`_lock_path_for` from abspath+normcase under `tempfile.gettempdir()`, and
`_acquire_lock_file` as O_CREAT|O_EXCL with stale-pid reclaim. `_pid_alive`
loses its GetLastError/EPERM distinction, so an inspection failure reads as a
dead owner. Nothing else in the module changes and the test file is used
byte-identical, so the pair isolates the implementation.
"""

import os
import re
import shutil
import sys
import tempfile

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC = os.path.join(ROOT, "delete_duplicates_gui.py")

LEGACY_BLOCK = '''
# --- Cross-process destructive ownership (T-50) [PRE-FIX LEGACY PRIMITIVE] ---
def _lock_dir():
    return os.path.join(tempfile.gettempdir(), "smart-vac-locks")


def _lock_path_for(root):
    canonical = os.path.normcase(os.path.abspath(root))
    key = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
    return os.path.join(_lock_dir(), key + ".lock")


class _LegacyLock:
    def __init__(self, lock_path):
        self.lock_path = lock_path

    def release(self):
        try:
            os.remove(self.lock_path)
        except OSError:
            pass


def _acquire_lock_file(lock_path, timeout=30.0, poll=0.05):
    """O_CREAT|O_EXCL; reclaim the file when its recorded pid reads as dead."""
    deadline = time.monotonic() + timeout
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    while time.monotonic() < deadline:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pid = None
            try:
                with open(lock_path, encoding="utf-8", errors="replace") as f:
                    payload = f.read()
                if payload.startswith("pid="):
                    pid = int(payload[4:].strip())
            except (OSError, ValueError):
                pid = None
            if pid is not None and not _pid_alive(pid):
                try:
                    os.remove(self_path := lock_path)
                except OSError:
                    pass
                continue
            time.sleep(poll)
            continue
        try:
            os.write(fd, f"pid={os.getpid()}\\n".encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return _LegacyLock(lock_path)
    raise TimeoutError(f"destructive lock held by another instance: {lock_path}")


def _release_lock_file(lock):
    if lock is None:
        return
    try:
        os.remove(lock.lock_path)
    except AttributeError:
        pass
    except OSError:
        pass


@contextmanager
def destructive_lock(root, timeout=30.0, poll=0.05):
    lock_path = _lock_path_for(root)
    held = _acquire_lock_file(lock_path, timeout=timeout, poll=poll)
    try:
        yield True
    finally:
        _release_lock_file(held)


def _pid_alive(pid):
    """Legacy probe: any inspection failure reads as 'owner is gone'."""
    if not pid or pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes as _ct
            process = _ct.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if process:
                _ct.windll.kernel32.CloseHandle(process)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True
'''


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else None
    if not out:
        print("usage: make_t50_prefix_subject.py <target_dir>", file=sys.stderr)
        return 2
    with open(SRC, encoding="utf-8") as f:
        text = f.read()
    start = text.index("# --- Cross-process destructive ownership (T-50)")
    end = text.index("def is_likely_original(filename):")
    legacy = text[:start] + LEGACY_BLOCK.strip("\n") + "\n\n\n" + text[end:]
    legacy = legacy.replace("import time", "import time\nimport tempfile", 1)
    target = os.path.join(out, "delete_duplicates_gui.py")
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(legacy)
    shutil.copyfile(os.path.join(ROOT, "test_delete_duplicates.py"),
                    os.path.join(out, "test_delete_duplicates.py"))
    print(f"pre-fix subject written to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

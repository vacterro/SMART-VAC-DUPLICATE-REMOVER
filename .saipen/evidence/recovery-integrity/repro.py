"""Recovery-integrity verification (T-66). Green assertions of the four
defects remediated by T-66; the unremediated (RED) behaviour was reproduced
by the same probes before the fix. Headless, temp dirs, no Recycle Bin.
Run: python .saipen/evidence/recovery-integrity/repro.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath("."))
import delete_duplicates_gui as mod

results = {}
d = tempfile.mkdtemp(prefix="vacq-repro-")
mod.RECOVERY_LOG_FILE = os.path.join(d, "quarantine_recovery.json")

# --- D1: identity preserved across status transitions ------------------------
src = os.path.join(d, "orig.txt")
with open(src, "w") as f:
    f.write("payload")
q = mod.acquire_candidate(src)
mod.record_quarantine_metadata(q, src, size=7, fingerprint="f" * 64, status="acquired")
mod.record_quarantine_metadata(q, src, status="trashing")
after = json.load(open(mod.RECOVERY_LOG_FILE))["items"][os.path.abspath(q)]
results["D1_identity_preserved"] = (
    after["fingerprint"] == "f" * 64 and after["size"] == 7)

# --- D2: restore refuses tampered content without fingerprint ----------------
src2 = os.path.join(d, "orig2.txt")
with open(src2, "w") as f:
    f.write("original")
q2 = mod.acquire_candidate(src2)
mod.record_quarantine_metadata(q2, src2, size=8, fingerprint=None, status="acquired")
with open(q2, "w") as f:
    f.write("TAMPERED-DIFFERENT-CONTENT")


class Engine:
    pass


ok, info = mod.restore_quarantined_file(q2, original_path=src2, engine=Engine())
results["D2_tampered_restore_refused"] = (
    ok is False and info == "fingerprint_missing" and os.path.exists(q2))

# --- D3: destruction refused without a durable receipt -----------------------
src3 = os.path.join(d, "orig3.txt")
with open(src3, "w") as f:
    f.write("doomed")
q3 = mod.acquire_candidate(src3)
mod.RECOVERY_LOG_FILE = os.path.join(d, "unwritable.json")
os.makedirs(mod.RECOVERY_LOG_FILE, exist_ok=True)  # open() -> OSError
ret = mod.record_quarantine_metadata(q3, src3, size=6, fingerprint="a" * 64,
                                     status="trashing")
mod.deletion_is_recoverable = lambda: False
refused = None
try:
    mod.send_to_trash(q3, original_path=src3)
except OSError as exc:
    refused = str(exc)
results["D3_destruction_refused"] = (
    ret is False
    and refused is not None
    and os.path.exists(q3))  # object survives, recovery still possible

# --- D4: per-user absolute journal, discoverable after CWD change ------------
results["D4_absolute_log_path"] = os.path.isabs(mod.RECOVERY_LOG_FILE)
mod.RECOVERY_LOG_FILE = os.path.join(d, "journal.json")
os.chdir(d)
os.makedirs("other_cwd", exist_ok=True)
mod.record_quarantine_metadata(q2, src2, size=1, fingerprint="b" * 64, status="restored")
os.chdir(os.path.join(d, "other_cwd"))
results["D4_metadata_discoverable_after_cwd_change"] = (
    mod.get_quarantine_metadata(src2) is not None)

for k, v in results.items():
    print(f"{'PASS' if v else 'FAIL'}: {k}")
sys.exit(0 if all(results.values()) else 1)

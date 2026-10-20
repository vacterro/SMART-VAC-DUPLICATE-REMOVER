"""Windows-native Recycle Bin acceptance for T-66 (destructive path is real:
SHFileOperationW recycle, no monkeypatching of send_to_trash).

Runs the full acquire -> receipt -> recycle flow against a temp file and
asserts the durable receipt survives the destructive transition with its
immutable identity. The recycled file stays in the Recycle Bin (recoverable
by the user); nothing is emptied programmatically.
Run: python .saipen/evidence/recovery-integrity/recycle_acceptance.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath("."))
import delete_duplicates_gui as mod

if not mod.deletion_is_recoverable():
    print("SKIP: not Windows; send_to_trash falls back to permanent os.remove")
    sys.exit(0)

d = tempfile.mkdtemp(prefix="vacq-recycle-")
mod.RECOVERY_LOG_FILE = os.path.join(d, "quarantine_recovery.json")

src = os.path.join(d, "recycled-object.txt")
with open(src, "wb") as f:
    f.write(b"windows recycle bin acceptance payload")

qpath = mod.acquire_candidate(src)
fp = mod.hash_file(qpath)
size = os.path.getsize(qpath)
assert mod.record_quarantine_metadata(qpath, src, size=size, fingerprint=fp,
                                      status="acquired")

mod.send_to_trash(qpath, original_path=src)

rec = mod.get_quarantine_metadata(qpath)
checks = {
    "object_left_disk": not os.path.exists(qpath),
    "receipt_status_recycled": rec is not None and rec["status"] == "recycled",
    "receipt_immutable_fingerprint": rec["fingerprint"] == fp,
    "receipt_immutable_size": rec["size"] == size,
    "receipt_original_path": rec["original_path"] == os.path.abspath(src),
    "no_tmp_orphans": not [p for p in os.listdir(d) if p.endswith(".tmp")],
}
for k, v in checks.items():
    print(f"{'PASS' if v else 'FAIL'}: {k}")
print(f"note: the object remains in the Windows Recycle Bin at the quarantine"
      f" name {os.path.basename(qpath)}; delete the bin entry to reclaim space")
sys.exit(0 if all(checks.values()) else 1)

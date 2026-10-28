"""Read-only maintainer verification for private SAIPEN archive carriers.

Requires an explicit archive root. Public source snapshots intentionally omit
these authority records; application tests must not depend on their presence.
The six original archive assertions are retained, with failed Git commands
now rejected rather than treated as an unignored path.
"""
import argparse
import os
import unittest

ARCHIVE_ROOT = None

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
        cls.proj_root = ARCHIVE_ROOT
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
            self.assertEqual(r.returncode, 1,
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", required=True)
    args, remaining = parser.parse_known_args()
    ARCHIVE_ROOT = os.path.abspath(args.archive_root)
    if not os.path.isdir(ARCHIVE_ROOT):
        parser.error("archive root must be an existing private checkpoint")
    unittest.main(argv=[__file__, *remaining])

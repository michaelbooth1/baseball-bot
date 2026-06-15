"""T2 (Hygiene #6): guarded fold-in of fleet roots into a SEPARATE labeled
signal table. Covers the fleet-root discovery (the new pure function) -- the
canonical-vs-fleet separation + config_label relabeling is verified on real
data in the build; here we lock the enumeration + legacy-root exclusion.
"""
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR / "scripts" / "analysis") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import build_unified_signal_table as m  # noqa: E402


class DiscoverFleetRootsTests(unittest.TestCase):

    def test_excludes_legacy_paper_trading_and_labels_arms(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            for name in ("paper_trading", "paper_A_current", "paper_O_nb_stage1",
                         "live_trading", "not_a_paper_dir"):
                (data / name).mkdir(parents=True)
            roots = m._discover_fleet_roots(data_dir=data)
            labels = sorted(lbl for lbl, _ in roots)
            # paper_trading (legacy) excluded; live_trading + non-paper ignored.
            self.assertEqual(labels, ["A_current", "O_nb_stage1"])

    def test_root_paths_point_at_expected_subdirs(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            (data / "paper_B_cal_only").mkdir(parents=True)
            (label, roots), = m._discover_fleet_roots(data_dir=data)
            self.assertEqual(label, "B_cal_only")
            self.assertEqual(roots["sessions"], data / "paper_B_cal_only" / "sessions")
            self.assertEqual(roots["candidates"],
                             data / "paper_B_cal_only" / "candidate_universe")
            self.assertEqual(roots["captures"],
                             data / "paper_B_cal_only" / "book_captures")
            self.assertEqual(roots["ledger"],
                             data / "paper_B_cal_only" / "master_ledger.jsonl")


if __name__ == "__main__":
    unittest.main()

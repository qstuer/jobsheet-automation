"""Synthetic-only checks for the 20-sample PM checklist date probe."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import checklist_date_probe as probe


class ChecklistDateProbeTests(unittest.TestCase):
    def test_independent_pages_agree_with_reviewed_date(self):
        row = probe._grade(["2026-08-19"] * 2, ["2026-08-19"] * 2,
                           "2026-08-19", {"calls": 4})
        self.assertEqual(row["status"], "CONFIRMED_CORRECT")
        self.assertTrue(row["matches_reviewed"])
        self.assertEqual(row["calls"], 4)
        self.assertNotIn("2026-08-19", str(row))

    def test_consistently_wrong_date_is_not_success(self):
        row = probe._grade(["2026-09-19"] * 2, ["2026-09-19"] * 2,
                           "2026-08-19", {})
        self.assertEqual(row["status"], "CONSISTENT_BUT_WRONG")
        self.assertFalse(row["matches_reviewed"])

    def test_crosspage_conflict_or_unreadable_stays_unresolved(self):
        conflict = probe._grade(["2026-08-19"] * 2, ["2026-08-20"] * 2,
                                "2026-08-19", {})
        missing = probe._grade(["2026-08-19", None], ["2026-08-19"] * 2,
                               "2026-08-19", {})
        self.assertEqual(conflict["status"], "CROSSPAGE_CONFLICT")
        self.assertEqual(missing["status"], "UNREADABLE_OR_DISAGREED")
        self.assertFalse(conflict["matches_reviewed"])
        self.assertFalse(missing["matches_reviewed"])

    def test_changed_private_pdf_stops_before_model_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "B01.pdf").write_bytes(b"changed")
            sample = {"sample_id": "B01", "filename": "B01.pdf",
                      "source_sha256": "0" * 64, "job_type": "PM",
                      "review_status": "confirmed"}
            with (patch.dict(os.environ, {"JOBSHEET_BACKTEST_DIR": temporary}, clear=False),
                  patch.object(probe.backtest, "_load_manifest", return_value=[sample]),
                  patch.object(probe, "_read_date") as read):
                with self.assertRaises(probe.backtest.BacktestError):
                    probe.run()
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()

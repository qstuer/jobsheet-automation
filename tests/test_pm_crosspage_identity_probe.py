"""Synthetic checks for the read-only PM checklist identity experiment."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import pm_crosspage_identity_probe as probe


class PMCrosspageIdentityProbeTests(unittest.TestCase):
    def test_fixed_cards_include_labels_and_full_serial_line(self):
        self.assertLess(probe.SERIAL[0], probe.SERIAL[2])
        self.assertGreater(probe.SERIAL[2], 0.565)  # Old B10 crop cut off the right edge.
        self.assertLess(probe.SERIAL[2], probe.ASSET[2])
        self.assertIn("Customer", probe._PROMPTS["header"])
        self.assertIn("System", probe._PROMPTS["header"])
        self.assertIn("sn", probe._PROMPTS["header"])
        self.assertIn("Asset No.", probe._PROMPTS["header"])
        self.assertNotIn("Asana candidate", " ".join(probe._PROMPTS.values()))

    def test_anonymous_grade_requires_correct_serial_not_just_agreement(self):
        header = {"hospital": "Sample Hospital", "product": "EPIQ Elite",
                  "serial": "ZZ123A4567", "date": "20/8/2026", "asset": None}
        reads = {"header": [header, header],
                 "hospital": [{"value": "Sample Hospital"}],
                 "product": [{"value": "EPIQ Elite"}],
                 "serial": [{"value": "ZZ123A4567"}],
                 "date": [{"value": "20/8/2026"}],
                 "asset": [{"value": None}]}
        sample = {"sample_id": "B04", "observed_fields": {
            "hospital_raw": "Sample Hospital", "product_raw": "EPIQ Elite",
            "service_date_raw": "2026-08-20"},
            "expected": {"serial": "ZZ123A4567"}}
        report = probe._grade(reads, sample, {"calls": 7})
        self.assertTrue(report["all_identity_cards_correct"])
        self.assertFalse(report["card_asset_present"])  # Blank asset is not a penalty.
        self.assertNotIn("ZZ123A4567", str(report))
        self.assertNotIn("Sample Hospital", str(report))
        reads["serial"][0]["value"] = "ZZ123A4568"
        report = probe._grade(reads, sample, {})
        self.assertFalse(report["card_serial_correct"])
        self.assertFalse(report["all_identity_cards_correct"])

    def test_modified_private_pdf_stops_before_model_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "B04.pdf").write_bytes(b"not the reviewed PDF")
            sample = {"sample_id": "B04", "filename": "B04.pdf", "job_type": "PM",
                      "review_status": "confirmed", "source_sha256": "0" * 64,
                      "expected": {"kind": "match", "serial": "ZZ123A4567"}}
            with (patch.dict(os.environ, {"JOBSHEET_BACKTEST_DIR": temporary}, clear=False),
                  patch.object(probe.backtest, "_load_manifest", return_value=[sample]),
                  patch.object(probe, "_read") as read):
                with self.assertRaises(probe.backtest.BacktestError):
                    probe.run()
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()

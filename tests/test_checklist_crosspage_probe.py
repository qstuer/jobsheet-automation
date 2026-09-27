"""Synthetic-only checks for the B10 read-only checklist experiment."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import checklist_crosspage_probe as probe


class ChecklistCrosspageProbeTests(unittest.TestCase):
    def test_partial_serial_accepts_only_explicit_uncertain_character(self):
        self.assertTrue(probe._compatible_partial("SZ?25B0149", "SZ725B0149"))
        self.assertFalse(probe._compatible_partial("SZ?25B0149", "SZ725B0249"))
        self.assertFalse(probe._compatible_partial("SZ?25B0149", "SZ725B014"))
        self.assertFalse(probe._compatible_partial("", "SZ725B0149"))

    def test_complete_independent_page_reads_report_anonymous_success(self):
        header = {"hospital": "GH-3F", "product": "EPIQ CVx",
                  "serial": "SZ725B0149", "date": "20/8/2026"}
        footer = {"engineer_date": "20/8/2026", "customer_date": "20/8/2026"}
        product = {"product": "EPIQ CVx"}
        serial = {"serial": "SZ725B0149"}
        observed = {"hospital_raw": "GH-3F", "product_raw": "EPIQ CVx",
                    "serial_raw": "SZ?25B0149", "service_date_raw": "2026-08-20"}
        expected = {"serial": "SZ725B0149"}
        report = probe._anonymous_report([header, header], [footer, footer],
                                         [product, product], [serial, serial],
                                         observed, expected, {"calls": 8})
        self.assertTrue(report["crosspage_evidence_complete"])
        self.assertTrue(report["crosspage_card_evidence_complete"])
        self.assertTrue(report["checklist_serial_matches_reviewed"])
        self.assertEqual(report["calls"], 8)
        self.assertNotIn("SZ725B0149", str(report))
        self.assertNotIn("GH-3F", str(report))

    def test_conflicting_last_page_date_cannot_be_called_complete(self):
        header = {"hospital": "GH-3F", "product": "EPIQ CVx",
                  "serial": "SZ725B0149", "date": "20/8/2026"}
        footer = {"engineer_date": "20/9/2026", "customer_date": "20/8/2026"}
        product = {"product": "EPIQ CVx"}
        serial = {"serial": "SZ725B0149"}
        observed = {"hospital_raw": "GH-3F", "product_raw": "EPIQ CVx",
                    "serial_raw": "SZ?25B0149", "service_date_raw": "2026-08-20"}
        report = probe._anonymous_report([header, header], [footer, footer],
                                         [product, product], [serial, serial],
                                         observed, {"serial": "SZ725B0149"}, {})
        self.assertFalse(report["last_page_engineer_date_two_reads_match_sheet"])
        self.assertFalse(report["crosspage_evidence_complete"])
        self.assertFalse(report["crosspage_card_evidence_complete"])

    def test_agreeing_wrong_serial_is_not_graded_correct(self):
        header = {"hospital": "GH-3F", "product": "EPIQ CVx",
                  "serial": "SZ725B0149", "date": "20/8/2026"}
        footer = {"engineer_date": "20/8/2026", "customer_date": "20/8/2026"}
        observed = {"hospital_raw": "GH-3F", "product_raw": "EPIQ CVx",
                    "serial_raw": "SZ?25B0149", "service_date_raw": "2026-08-20"}
        report = probe._anonymous_report(
            [header, header], [footer, footer], [{"product": "EPIQ CVx"}] * 2,
            [{"serial": "SZ825B0149"}] * 2, observed, {"serial": "SZ725B0149"}, {})
        self.assertTrue(report["serial_card_two_reads_agree"])
        self.assertFalse(report["serial_card_matches_reviewed"])
        self.assertFalse(report["crosspage_card_evidence_complete"])

    def test_changed_private_pdf_stops_before_any_model_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "B10.pdf").write_bytes(b"not the reviewed PDF")
            sample = {
                "sample_id": "B10", "filename": "B10.pdf", "job_type": "PM",
                "review_status": "confirmed", "source_sha256": "0" * 64,
                "expected": {"kind": "match"},
                "observed_fields": {"date_source": "ACTION_DATE"},
            }
            with (patch.dict(os.environ, {"JOBSHEET_BACKTEST_DIR": temporary}, clear=False),
                  patch.object(probe.backtest, "_load_manifest", return_value=[sample]),
                  patch.object(probe, "_read_json") as read):
                with self.assertRaises(probe.backtest.BacktestError):
                    probe.run()
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()

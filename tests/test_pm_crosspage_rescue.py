"""Synthetic safety gates for the opt-in, read-only PM cross-page rescue."""

import os
import unittest
from datetime import date
from unittest.mock import Mock, patch

from src import backtest, pm_crosspage_rescue as rescue


class PMCrosspageRescueTests(unittest.TestCase):
    def setUp(self):
        self.ocr = {
            "product_raw": "EPIQ Elite", "hospital_raw": "Sample Hospital",
            "serial_candidates": ["ZZ123A4567"], "phone_candidates": ["00000000"],
            "service_date_raw": None,
            "_ocr_audit": {"readings": [
                {"context": {"stage": "primary"},
                 "raw": {"service_date_raw": "20/9/2026"}},
            ]},
        }
        self.headers = [
            {"hospital": "Sample Hospital", "product": "EPIQ Elite",
             "serial": "ZZ123A4567", "date": "20/9/2026"},
        ] * 2
        self.last = [{"customer_date": "20/9/2026"}] * 2
        self.today = patch.object(rescue.nvidia_client, "_today", return_value=date(2026, 9, 27))
        self.today.start()
        self.addCleanup(self.today.stop)

    def assess(self, *, serial="ZZ123A4567", date_card="20/9/2026",
               signed=None, headers=None, last=None):
        return rescue._assess(
            self.ocr, headers if headers is not None else self.headers,
            serial, date_card, last if last is not None else self.last, signed,
        )

    def test_matching_independent_evidence_can_attempt_normal_matcher(self):
        candidate, reason = self.assess()
        self.assertEqual(reason, "evidence_accepted")
        self.assertEqual(candidate["service_date_iso"], "2026-09-20")
        self.assertEqual(candidate["service_date_raw"], "20/9/2026")
        self.assertTrue(candidate["date_corrob"])
        self.assertEqual(self.ocr["service_date_raw"], None)

    def test_checklist_cannot_invent_first_page_action_date(self):
        self.ocr["_ocr_audit"]["readings"] = []
        self.assertEqual(self.assess()[1], "no_first_page_action_support")

    def test_two_focused_first_page_reads_on_other_date_veto(self):
        self.ocr["_ocr_audit"]["readings"].extend([
            {"context": {"stage": "date_recheck"},
             "raw": {"service_date_raw": "20/8/2026"}},
            {"context": {"stage": "date_recheck"},
             "raw": {"service_date_raw": "20/8/2026"}},
        ])
        self.assertEqual(self.assess()[1], "first_page_focused_conflict")

    def test_conflicting_customer_signature_vetoes(self):
        self.assertEqual(self.assess(signed="20/8/2026")[1],
                         "first_page_customer_conflict")

    def test_last_page_disagreement_vetoes(self):
        last = [{"customer_date": "20/9/2026"},
                {"customer_date": "20/8/2026"}]
        self.assertEqual(self.assess(last=last)[1], "last_page_date_disagreement")

    def test_serial_and_product_need_cross_page_support(self):
        bad_serial = [{**row, "serial": "ZZ123A4568"} for row in self.headers]
        self.assertEqual(self.assess(serial="ZZ123A4568", headers=bad_serial)[1],
                         "serial_not_corrob")
        bad_product = [{**row, "product": "Affiniti 70"} for row in self.headers]
        self.assertEqual(self.assess(headers=bad_product)[1], "product_not_corrob")

    def test_blank_asset_does_not_block_but_missing_phone_and_asset_does(self):
        self.ocr["phone_candidates"] = []
        self.ocr["asset_candidates"] = []
        self.assertEqual(self.assess()[1], "no_independent_support")
        self.ocr["asset_candidates"] = ["123456"]
        self.assertEqual(self.assess()[1], "evidence_accepted")

    def test_backtest_flag_is_only_entry_point(self):
        doc = Mock(page_count=4)
        ocr = {"ocr_metrics": {"calls": 2}, "_ocr_audit": {}}
        with (patch.object(backtest.nvidia_client, "detect_cm_pm", return_value="PM"),
              patch.object(backtest.nvidia_client, "get_ocr_metrics", return_value={"calls": 2}),
              patch.object(backtest.processor, "_ocr_and_match", return_value=(None, 0, ocr)),
              patch.object(rescue, "try_rescue", return_value=(None, 0, ocr)) as fallback):
            with patch.dict(os.environ, {"JOBSHEET_PM_CROSSPAGE_RESCUE": "1"}):
                backtest._read_sample(doc, "PM")
            fallback.assert_called_once_with(doc, ocr, "PM")
            fallback.reset_mock()
            with patch.dict(os.environ, {"JOBSHEET_PM_CROSSPAGE_RESCUE": "0"}):
                backtest._read_sample(doc, "PM")
            fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()

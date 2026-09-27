"""Synthetic safeguards for the isolated PM checklist-date fallback."""

import unittest
from datetime import date
from unittest.mock import patch

import fitz

from src import nvidia_client, processor


class PMChecklistDateFallbackTests(unittest.TestCase):
    def setUp(self):
        self.today = patch.object(nvidia_client, "_today", return_value=date(2026, 9, 27))
        self.today.start()
        self.addCleanup(self.today.stop)

    def apply(self, actions, focused, customer, checklist):
        result = {"_ocr_audit": {}}
        accepted = processor._apply_pm_checklist_date_corroboration(
            result,
            [{"service_date_raw": value} for value in actions],
            [{"service_date_raw": value} for value in focused],
            customer, checklist,
        )
        return accepted, result

    def test_first_page_customer_and_action_confirm_checklist(self):
        accepted, result = self.apply(
            ["18/8/2026", "18/9/2026"],
            ["18/8/2026", "18/9/2026"],
            "2026-08-18", ["2026-08-18", "2026-08-18"],
        )
        self.assertTrue(accepted)
        self.assertEqual("2026-08-18", result["service_date_iso"])
        self.assertTrue(result["date_corrob"])

    def test_two_focused_action_reads_can_confirm_without_signature(self):
        accepted, _ = self.apply(
            ["18/8/2026"], ["18/8/2026", "18/8/2026"], None,
            ["2026-08-18", "2026-08-18"],
        )
        self.assertTrue(accepted)

    def test_repeated_checklist_error_is_not_accepted(self):
        cases = (
            (["18/8/2026"], ["18/8/2026", "18/8/2026"],
             "2026-08-18", ["2026-09-18", "2026-09-18"]),
            (["18/8/2026"], ["18/8/2026", "18/8/2026"],
             None, ["2026-09-18", "2026-09-18"]),
            (["18/9/2026"], ["18/9/2026", "18/9/2026"],
             "2026-08-18", ["2026-08-18", "2026-08-18"]),
        )
        for args in cases:
            with self.subTest(args=args):
                accepted, result = self.apply(*args)
                self.assertFalse(accepted)
                self.assertNotIn("date_corrob", result)

    def test_signature_or_focused_conflict_vetoes(self):
        for focused, customer in (
            (["18/8/2026", "18/8/2026"], "2026-08-19"),
            (["18/9/2026", "18/9/2026"], "2026-08-18"),
        ):
            accepted, result = self.apply(
                ["18/8/2026"], focused, customer,
                ["2026-08-18", "2026-08-18"],
            )
            self.assertFalse(accepted)
            self.assertEqual("first_page_conflict",
                             result["_ocr_audit"]["pm_checklist_date_check"])

    def test_missing_or_disagreeing_read_stays_pending(self):
        for checklist in (["2026-08-18", None],
                          ["2026-08-18", "2026-08-19"]):
            accepted, result = self.apply(
                ["18/8/2026"], ["18/8/2026", "18/8/2026"],
                "2026-08-18", checklist,
            )
            self.assertFalse(accepted)
            self.assertNotIn("service_date_iso", result)

    def test_reader_is_a_fixed_box_without_asana_answers(self):
        with fitz.open() as document:
            document.new_page()
            document.new_page()
            with patch.object(nvidia_client, "_call_vision",
                              return_value='{"date":"18/8/2026"}') as vision:
                result = nvidia_client.ocr_pm_checklist_date(document, 1)
        self.assertEqual("2026-08-18", result)
        prompt = vision.call_args.args[0]
        self.assertIn("FIRST PAGE of a PM checklist", prompt)
        self.assertNotIn("2026-08-18", prompt)
        self.assertNotIn("SR#", prompt)


if __name__ == "__main__":
    unittest.main()

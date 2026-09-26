"""Synthetic checks for the anonymous date-only fixture probe."""
from datetime import date
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import MagicMock, patch

from src import date_field_probe


class DateFieldProbeTests(TestCase):
    def setUp(self):
        self.sample = {
            "sample_id": "B01",
            "filename": "B01.pdf",
            "review_status": "confirmed",
            "observed_fields": {
                "date_source": "ACTION_DATE",
                "service_date_raw": "2031-04-12",
            },
        }

    def test_reviewed_date_must_be_confirmed_action_date(self):
        self.assertEqual(date_field_probe._reviewed_day(self.sample), date(2031, 4, 12))
        self.sample["review_status"] = "unreviewed"
        with self.assertRaises(ValueError):
            date_field_probe._reviewed_day(self.sample)

    def test_anonymous_result_distinguishes_correct_wrong_and_unreadable(self):
        doc = MagicMock()
        doc.__enter__.return_value = doc
        doc.page_count = 4
        reads = iter([
            date(2031, 4, 12), date(2031, 4, 13),
            date(2031, 4, 12), date(2031, 4, 12),
            None, None,
        ])
        with (patch.object(date_field_probe.fitz, "open", return_value=doc),
              patch.object(date_field_probe, "_read_date", side_effect=lambda *_: next(reads)),
              patch.object(date_field_probe.nvidia_client, "reset_ocr_metrics"),
              patch.object(date_field_probe.nvidia_client, "reset_model_availability"),
              patch.object(date_field_probe.nvidia_client, "get_ocr_metrics", return_value={
                  "calls": 6, "seconds": 2.5, "total_tokens": 120,
                  "estimated_cost_cny_upper": 0.01,
              })):
            result = date_field_probe.probe_sample(self.sample, Path("unused"))
        self.assertEqual(result["fields"]["service_date_raw"],
                         ["CORRECT", "OTHER_VALID_DATE"])
        self.assertEqual(result["fields"]["engineer_signed_date"],
                         ["CORRECT", "CORRECT"])
        self.assertEqual(result["fields"]["customer_signed_date"],
                         ["UNREADABLE", "UNREADABLE"])
        self.assertNotIn("2031", str(result))
        self.assertNotIn("expected", result)

    def test_joint_majority_requires_two_fields_and_two_renderings(self):
        truth = date(2031, 4, 12)
        wrong = date(2031, 5, 12)
        self.assertEqual(date_field_probe._majority_date({
            "a": truth, "b": truth, "c": wrong,
        }), truth)
        self.assertIsNone(date_field_probe._majority_date({
            "a": truth, "b": wrong, "c": None,
        }))
        doc = MagicMock()
        doc.__enter__.return_value = doc
        doc.page_count = 4
        reads = iter([
            {"service_date_raw": truth, "engineer_signed_date": truth,
             "customer_signed_date": wrong},
            {"service_date_raw": wrong, "engineer_signed_date": wrong,
             "customer_signed_date": truth},
        ])
        with (patch.object(date_field_probe.fitz, "open", return_value=doc),
              patch.object(date_field_probe, "_read_joint_dates", side_effect=lambda *_: next(reads)),
              patch.object(date_field_probe.nvidia_client, "reset_ocr_metrics"),
              patch.object(date_field_probe.nvidia_client, "reset_model_availability"),
              patch.object(date_field_probe.nvidia_client, "get_ocr_metrics", return_value={
                  "calls": 2, "seconds": 1.0, "total_tokens": 100,
              })):
            result = date_field_probe.probe_sample_joint(self.sample, Path("unused"))
        self.assertEqual(result["two_render_majority"], "UNRESOLVED")
        self.assertNotIn("2031", str(result))


if __name__ == "__main__":
    main()

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
                "service_date_raw": "2026-08-18",
            },
        }

    def test_reviewed_date_must_be_confirmed_action_date(self):
        self.assertEqual(date_field_probe._reviewed_day(self.sample), date(2026, 8, 18))
        self.sample["review_status"] = "unreviewed"
        with self.assertRaises(ValueError):
            date_field_probe._reviewed_day(self.sample)

    def test_anonymous_result_distinguishes_correct_wrong_and_unreadable(self):
        doc = MagicMock()
        doc.__enter__.return_value = doc
        doc.page_count = 4
        reads = iter([
            date(2026, 8, 18), date(2026, 8, 19),
            date(2026, 8, 18), date(2026, 8, 18),
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
        self.assertNotIn("2026", str(result))
        self.assertNotIn("expected", result)


if __name__ == "__main__":
    main()

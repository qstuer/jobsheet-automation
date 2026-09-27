"""Synthetic checks for the read-only customer-signature date experiment."""

import unittest
from unittest.mock import patch

from src import customer_date_parts_probe, nvidia_client


class CustomerDatePartsProbeTests(unittest.TestCase):
    def test_three_visible_groups_form_a_date_without_candidate_hint(self):
        with patch.object(nvidia_client, "crop_jobsheet_field_card",
                          return_value="synthetic"), \
             patch.object(nvidia_client, "_call_vision",
                          return_value='{"day":"20","month":"8","year":"2026"}') as call:
            result = nvidia_client.ocr_jobsheet_customer_date_parts(None, 0)
        self.assertEqual("2026-08-20", result)
        self.assertNotIn("2026-08-20", call.call_args.args[0])
        self.assertNotIn("Asana", call.call_args.args[0])

    def test_missing_or_invalid_group_cannot_be_guessed(self):
        for raw in (
            '{"day":"20","month":null,"year":"2026"}',
            '{"day":"2?","month":"8","year":"2026"}',
            '{"day":"20","month":"13","year":"2026"}',
        ):
            with self.subTest(raw=raw), \
                 patch.object(nvidia_client, "crop_jobsheet_field_card",
                              return_value="synthetic"), \
                 patch.object(nvidia_client, "_call_vision", return_value=raw):
                self.assertIsNone(
                    nvidia_client.ocr_jobsheet_customer_date_parts(None, 0)
                )

    def test_anonymous_grading_does_not_export_date(self):
        result = customer_date_parts_probe._grade(
            ["2026-08-20", "2026-08-20"], "2026-08-20"
        )
        self.assertEqual("CORRECT", result["two_reads"])
        self.assertNotIn("2026", str(result))
        wrong = customer_date_parts_probe._grade(
            ["2026-09-20", "2026-09-20"], "2026-08-20"
        )
        self.assertEqual("OTHER_VALID_DATE", wrong["two_reads"])


if __name__ == "__main__":
    unittest.main()

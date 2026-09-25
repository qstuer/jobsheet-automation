"""Synthetic date cross-checks; no real jobsheet or Asana values."""
import unittest
from unittest.mock import patch

from src import asana_client, nvidia_client, processor


class SignatureDateCorroborationTests(unittest.TestCase):
    def test_two_signatures_resolve_one_ambiguous_action_date_digit(self):
        result = {"_ocr_audit": {}}
        action = [
            {"service_date_raw": "18/9/2026"},
            {"service_date_raw": "18/8/2026"},
        ]
        self.assertTrue(processor._apply_signature_date_corroboration(
            result, action, ["2026-08-18", "2026-08-18"]
        ))
        self.assertEqual("2026-08-18", result["service_date_iso"])
        self.assertEqual("18/8/2026", result["service_date_raw"])
        self.assertEqual("ACTION_DATE", result["date_source"])
        self.assertTrue(result["date_corrob"])

    def test_signatures_cannot_create_a_date_absent_from_action_panel(self):
        for action in ([{"service_date_raw": "18/9/2026"}], []):
            result = {"_ocr_audit": {}}
            self.assertFalse(processor._apply_signature_date_corroboration(
                result, action, ["2026-08-18", "2026-08-18"]
            ))
            self.assertNotIn("date_corrob", result)

    def test_conflicting_signatures_or_distant_action_date_remain_pending(self):
        cases = [
            (["2026-08-18", "2026-08-19"], ["18/8/2026"]),
            (["2026-08-18", "2026-08-18"],
             ["18/8/2026", "29/9/2026"]),
        ]
        for signed, raw in cases:
            result = {"_ocr_audit": {}}
            action = [{"service_date_raw": item} for item in raw]
            self.assertFalse(processor._apply_signature_date_corroboration(
                result, action, signed
            ))
            self.assertNotIn("date_corrob", result)

    def test_signature_reader_never_receives_asana_candidates(self):
        with patch.object(nvidia_client, "crop_jobsheet_field_card",
                          return_value="synthetic"), \
             patch.object(nvidia_client, "_call_vision",
                          return_value='{"signed_date":"18/8/2026"}') as call:
            result = nvidia_client.ocr_jobsheet_signature_date(
                None, 0, "engineer_signed_date"
            )
        self.assertEqual("2026-08-18", result)
        prompt = call.call_args.args[0]
        self.assertNotIn("Asana", prompt)
        self.assertIn("Do not infer", prompt)

    def test_corrob_date_may_distinguish_visits_but_not_shared_date(self):
        older = {
            "gid": "older", "name": "Example Hospital / EPIQ 5G / AB123B4567",
            "due_on": "2026-07-24", "memberships": [{"project": {"name": "PM"}}],
            "notes": "Phone 11112222",
        }
        expected = {
            "gid": "expected", "name": older["name"],
            "due_on": "2026-08-20", "memberships": older["memberships"],
            "notes": "Phone 11112223",
        }
        ocr = {
            "serial_candidates": ["AB123B4567"],
            "product_raw": "EPIQ 5G", "hospital_raw": "Example Hospital",
            "phone_candidates": ["11112222"],
            "service_date_iso": "2026-08-20", "date_source": "ACTION_DATE",
            "date_corrob": True,
        }
        with patch.object(asana_client, "_device_index", None), \
             patch.object(asana_client, "_gather_pool",
                          return_value=[older, expected]):
            task, _ = asana_client.find_task(ocr, job_type="PM")
        self.assertEqual("expected", task["gid"])


if __name__ == "__main__":
    unittest.main()

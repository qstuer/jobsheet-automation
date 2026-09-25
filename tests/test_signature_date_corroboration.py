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

    def test_engineer_may_sign_earlier_than_customer(self):
        result = {"_ocr_audit": {}}
        action = [{"service_date_raw": "18/8/2026"}]
        self.assertTrue(processor._apply_signature_date_corroboration(
            result, action, ["2026-08-14", "2026-08-18"]
        ))
        self.assertEqual("2026-08-18", result["service_date_iso"])

    def test_rejected_but_parseable_action_date_is_available_for_review(self):
        result = {"_ocr_audit": {}}
        action = [{"service_date_raw": None, "_ocr_audit": {
            "raw": {"service_date_raw": "18/8/2026"},
            "rejections": {"service_date_raw": ["date_outside_window"]},
        }}]
        self.assertTrue(processor._apply_signature_date_corroboration(
            result, action, [None, "2026-08-18"]
        ))
        self.assertEqual("2026-08-18", result["service_date_iso"])

    def test_one_digit_date_error_needs_two_focused_reads_and_customer_signoff(self):
        action = [
            {"service_date_raw": "18/9/2026",
             "_read_context": {"stage": "date_parts", "zoom": 5.0}},
            {"service_date_raw": "18/9/2026",
             "_read_context": {"stage": "date_parts", "zoom": 6.0}},
        ]
        result = {"_ocr_audit": {}}
        self.assertTrue(processor._apply_signature_date_corroboration(
            result, action, ["2026-08-14", "2026-08-18"]
        ))
        self.assertEqual("one_digit_customer_correction",
                         result["_ocr_audit"]["signature_date_check"])
        self.assertEqual("2026-08-18", result["service_date_iso"])
        for reduced in (action[:1], [
            {"service_date_raw": "18/9/2026"},
            {"service_date_raw": "18/9/2026"},
        ]):
            self.assertFalse(processor._apply_signature_date_corroboration(
                {"_ocr_audit": {}}, reduced,
                ["2026-08-14", "2026-08-18"]
            ))
        self.assertFalse(processor._apply_signature_date_corroboration(
            {"_ocr_audit": {}}, action,
            ["2026-08-14", "2026-10-29"]
        ))
        year_error = [
            {"service_date_raw": "18/8/2006",
             "_read_context": {"stage": "date_parts", "zoom": zoom}}
            for zoom in (5.0, 6.0)
        ]
        self.assertTrue(processor._apply_signature_date_corroboration(
            {"_ocr_audit": {}}, year_error,
            [None, "2026-08-18"]
        ))

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

    def test_segmented_action_date_reader_has_no_candidate_answer(self):
        with patch.object(nvidia_client, "crop_jobsheet_field_card",
                          return_value="synthetic"), \
             patch.object(nvidia_client, "_call_vision",
                          return_value='{"day":"18","month":"8","year":"2026"}') as call:
            result = nvidia_client.ocr_jobsheet_action_date_parts(None, 0)
        self.assertEqual("2026-08-18", result)
        prompt = call.call_args.args[0]
        self.assertNotIn("Asana", prompt)
        self.assertNotIn("2026-08-18", prompt)

    def test_segmented_action_date_must_match_customer_date_twice(self):
        result = {"_ocr_audit": {}}
        signed = ["2026-08-14", "2026-08-18"]
        parts = [{"service_date_raw": "2026-08-18"},
                 {"service_date_raw": "2026-08-18"}]
        self.assertTrue(processor._apply_signature_date_corroboration(result, parts, signed))
        self.assertEqual("2026-08-18", result["service_date_iso"])
        wrong = {"_ocr_audit": {}}
        self.assertFalse(processor._apply_signature_date_corroboration(
            wrong, [{"service_date_raw": "2026-10-29"}, *parts], signed
        ))

    def test_customer_month_year_only_confirms_two_matching_action_reads(self):
        result = {"_ocr_audit": {}}
        self.assertTrue(processor._apply_customer_month_year_corroboration(
            result, ["2026-08-18", "2026-08-18"],
            ["2026-08", "2026-08"]
        ))
        self.assertEqual("2026-08-18", result["service_date_iso"])
        self.assertNotIn("date_corrob", result)
        for days, months in (
            (["2026-08-18", "2026-08-19"], ["2026-08", "2026-08"]),
            (["2026-08-18", "2026-08-18"], ["2026-09", "2026-09"]),
            (["2026-08-18", "2026-08-18"], ["2026-08", None]),
        ):
            self.assertFalse(processor._apply_customer_month_year_corroboration(
                {"_ocr_audit": {}}, days, months
            ))

    def test_customer_month_year_reader_has_no_asana_candidate(self):
        with patch.object(nvidia_client, "crop_jobsheet_field_card",
                          return_value="synthetic"), \
             patch.object(nvidia_client, "_call_vision",
                          return_value='{"month":"8","year":"2026"}') as call:
            result = nvidia_client.ocr_jobsheet_customer_month_year(None, 0)
        self.assertEqual("2026-08", result)
        prompt = call.call_args.args[0]
        self.assertNotIn("Asana", prompt)
        self.assertNotIn("2026-08", prompt)

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

    def test_raw_serial_one_character_error_and_unique_formal_date(self):
        old = {
            "gid": "old", "name": "Example Hospital / EPIQ 5G / AB123B4567",
            "due_on": "2026-08-03", "notes": "Phone 11112222",
            "memberships": [{"project": {"name": "PM"}}],
        }
        current = {**old, "gid": "current", "due_on": "2026-08-18"}
        ocr = {
            "serial_candidates": [], "serial_visual_candidates": ["AB123B4568"],
            "product_raw": "EPIQ 5G", "hospital_raw": "Example Hospital",
            "phone_candidates": ["11112222"],
            "service_date_iso": "2026-08-18", "date_source": "ACTION_DATE",
            "date_corrob": True,
        }
        with patch.object(asana_client, "_device_index", {"schema_version": 3}), \
             patch.object(asana_client, "_gather_index_pool",
                          return_value=([old, current], True, True)):
            task, _ = asana_client.find_task(ocr, job_type="PM")
        self.assertEqual("current", task["gid"])

        # A historical task with no formal date must not veto an exact,
        # independently corroborated visit date on the unique top task.
        undated = {**old, "due_on": None}
        with patch.object(asana_client, "_device_index", {"schema_version": 3}), \
             patch.object(asana_client, "_gather_index_pool",
                          return_value=([undated, current], True, True)):
            task, _ = asana_client.find_task(ocr, job_type="PM")
        self.assertEqual("current", task["gid"])

        # The same evidence without independent date corroboration is not a
        # reason to relax the original serial/Asset safety gate.
        del ocr["date_corrob"]
        with patch.object(asana_client, "_device_index", {"schema_version": 3}), \
             patch.object(asana_client, "_gather_index_pool",
                          return_value=([old, current], True, True)):
            task, _ = asana_client.find_task(ocr, job_type="PM")
        self.assertIsNone(task)

    def test_missing_phone_requires_a_clearly_separated_device(self):
        old = {
            "gid": "old", "name": "Example Hospital / EPIQ 5G / AB123B4567",
            "due_on": None, "notes": "", "memberships": [{"project": {"name": "PM"}}],
        }
        current = {**old, "gid": "current", "due_on": "2026-08-18"}
        ocr = {
            "serial_candidates": [], "serial_visual_candidates": ["AB123B4568"],
            "product_raw": "EPIQ 5G", "hospital_raw": "Example Hospital",
            "phone_candidates": [], "service_date_iso": "2026-08-18",
            "date_source": "ACTION_DATE", "date_corrob": True,
        }
        top = {"row": {"serial": "AB123B4567"}, "serial_similarity": .9}
        other = {"row": {"serial": "AB123B9999"}, "serial_similarity": .6}
        with patch.object(asana_client, "_device_index", {"schema_version": 3}), \
             patch.object(asana_client, "_gather_index_pool",
                          return_value=([old, current], True, True)), \
             patch.object(asana_client, "_rank_index_devices", return_value=[top, other]):
            task, _ = asana_client.find_task(ocr, job_type="PM")
        self.assertEqual("current", task["gid"])
        close = {"row": other["row"], "serial_similarity": .85}
        with patch.object(asana_client, "_device_index", {"schema_version": 3}), \
             patch.object(asana_client, "_gather_index_pool",
                          return_value=([old, current], True, True)), \
             patch.object(asana_client, "_rank_index_devices", return_value=[top, close]):
            task, _ = asana_client.find_task(ocr, job_type="PM")
        self.assertIsNone(task)


if __name__ == "__main__":
    unittest.main()

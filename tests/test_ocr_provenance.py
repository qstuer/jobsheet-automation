"""Synthetic regression cases for transcription, validation and consensus."""
import json
import unittest
from datetime import date
from unittest.mock import patch

from src import asana_client, nvidia_client, processor


class OCRProvenanceTests(unittest.TestCase):
    def normalize(self, **fields):
        with patch.object(nvidia_client, "_today", return_value=date(2026, 9, 18)):
            return nvidia_client._normalize_ocr_data(fields)

    def test_product_group_is_not_completed_into_a_model(self):
        for raw, expected in (("EPIQ", "EPIQ"), ("epiq", "EPIQ"),
                              ("Affiniti", "Affiniti"), ("CX", "CX")):
            result = self.normalize(product_raw=raw)
            self.assertEqual(raw, result["product_raw"])
            self.assertEqual(expected, result["product"])
            self.assertEqual(expected, asana_client.product_family(raw))
            consensus = processor._consensus_ocr([result, result])
            self.assertEqual(expected, consensus["product"])

    def test_product_number_and_suffix_are_never_invented(self):
        for raw in ("EPIQ 7", "EPIQ 6G", "Affiniti 7", "CX5", "CX40", "EPIQ 7X"):
            self.assertEqual(raw, asana_client.normalize_product(raw))
            result = self.normalize(product_raw=raw)
            self.assertIsNone(result["product"])
            self.assertEqual(raw, result["_ocr_audit"]["raw"]["product_raw"])
            self.assertIn("unconfirmed_product", result["_ocr_audit"]["rejections"]["product_raw"])

    def test_confirmed_spelling_variants_remain_supported(self):
        for raw, expected in (("EPLQ 5G", "EPIQ 5G"), ("EPIQ7Plus", "EPIQ 7+"),
                              ("EPIQ 7+", "EPIQ 7+"), ("Affiniti70G", "Affiniti 70G")):
            result = self.normalize(product_raw=raw)
            self.assertEqual(expected, result["product"])
            self.assertEqual(raw, result["product_raw"])

    def test_ambiguous_product_correction_does_not_use_list_order(self):
        with patch.object(asana_client, "OCR_PRODUCT_NAMES", ["ABCD 50", "ABCE 50"]):
            self.assertEqual("ABCF 50", asana_client.normalize_product("ABCF 50"))

    def test_same_day_formats_reach_consensus_without_losing_originals(self):
        variants = ("18/8/26", "18/08/2026", "18.8.2026", "18-8-2026", "2026-08-18", " 18 / 8 / 26 ")
        first = self.normalize(service_date_raw=variants[0])
        for raw in variants:
            second = self.normalize(service_date_raw=raw)
            result = processor._consensus_ocr([first, second])
            self.assertEqual("2026-08-18", result["service_date_iso"])
            self.assertEqual(variants[0], result["service_date_raw"])
            self.assertEqual(raw, result["_ocr_audit"]["readings"][1]["raw"]["service_date_raw"])
            self.assertEqual("ACTION_DATE", result["date_source"])

    def test_different_days_and_single_reading_do_not_reach_consensus(self):
        a = self.normalize(service_date_raw="18/8/26")
        b = self.normalize(service_date_raw="19/8/26")
        for readings, reason in (([a, b], "conflicting_readings"), ([a], "insufficient_valid_readings")):
            result = processor._consensus_ocr(readings)
            self.assertIsNone(result["service_date_iso"])
            self.assertIsNone(result["service_date_raw"])
            self.assertEqual(reason, result["_ocr_audit"]["consensus_rejections"]["service_date_raw"])

    def test_invalid_old_future_or_wrong_source_dates_are_audited_not_revived(self):
        for raw, source, reason in (("31/2/26", None, "invalid_date_format"),
                                   ("8/18/26", None, "invalid_date_format"),
                                   ("18/8/2020", None, "date_outside_window"),
                                   ("18/12/26", None, "date_outside_window"),
                                   ("18/8/26", "INVOICE_DATE", "not_action_date")):
            reading = self.normalize(service_date_raw=raw, date_source=source)
            self.assertEqual(raw, reading["_ocr_audit"]["raw"]["service_date_raw"])
            self.assertIn(reason, reading["_ocr_audit"]["rejections"]["service_date_raw"])
            result = processor._consensus_ocr([reading, reading])
            self.assertIsNone(result["service_date_raw"])
            self.assertIsNone(result["service_date_iso"])

    def test_rejected_values_stay_in_private_trace_not_matching_fields(self):
        reading = self.normalize(hospital_raw="PN", product_raw="unlisted gadget",
                                 serial_candidates=["15915F0726"], phone_candidates=["123"],
                                 order_no="123", asset_candidates=["12"], work_order_candidates=["WO12"])
        result = processor._consensus_ocr([reading, reading])
        for field in ("hospital_raw", "product_raw", "order_no"):
            self.assertIsNone(result[field])
        for field in ("serial_candidates", "phone_candidates", "asset_candidates", "work_order_candidates"):
            self.assertEqual([], result[field])
        audit = result["_ocr_audit"]["readings"][0]
        self.assertEqual("PN", audit["raw"]["hospital_raw"])
        self.assertEqual(["123"], audit["raw"]["phone_candidates"])
        self.assertIn("invalid_serial_format", audit["rejections"]["serial_candidates"])
        self.assertIn("invalid_phone_length", audit["rejections"]["phone_candidates"])

    def test_snapshot_is_independent_of_input_and_normalized_mutations(self):
        values = ["+852 6123 4567", "123"]
        reading = self.normalize(phone_candidates=values, department_room_raw="Asset# 123456 6F")
        values.append("76543210")
        reading["phone_candidates"].clear()
        self.assertEqual(["+852 6123 4567", "123"], reading["_ocr_audit"]["raw"]["phone_candidates"])
        self.assertEqual(["61234567"], reading["_ocr_audit"]["normalized"]["phone_candidates"])
        self.assertEqual("6F", reading["_ocr_audit"]["normalized"]["location_raw"])

    def test_candidate_limit_keeps_all_original_values_for_diagnosis(self):
        values = ["61234561", "61234562", "61234563", "61234564"]
        reading = self.normalize(phone_candidates=values)
        self.assertEqual(values[:3], reading["phone_candidates"])
        self.assertEqual(values, reading["_ocr_audit"]["raw"]["phone_candidates"])
        self.assertIn("candidate_limit", reading["_ocr_audit"]["rejections"]["phone_candidates"])

    def test_serial_only_retry_keeps_rejected_original(self):
        response = json.dumps({"serial_candidates": ["15915F0726", "US123F4567"]})
        with patch.object(nvidia_client, "crop_jobsheet_serial", return_value="synthetic"), \
             patch.object(nvidia_client, "_call_vision", return_value=response):
            reading = nvidia_client.ocr_jobsheet_serial_candidates(None, 0, with_audit=True)
            compatibility = nvidia_client.ocr_jobsheet_serial_candidates(None, 0)
        self.assertEqual(["US123F4567"], compatibility)
        self.assertEqual(["15915F0726", "US123F4567"], reading["_ocr_audit"]["raw"]["serial_candidates"])

    def test_matching_trace_has_round_context_and_does_not_leak_to_logs(self):
        reading = self.normalize(product_raw="EPIQ", hospital_raw="QMH", serial_candidates=["US123F4567"],
                                 contact_person_raw="PRIVATECONTACT", phone_candidates=["76543210", "123"])
        with patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=reading), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=reading), \
             patch.object(asana_client, "find_task", return_value=({"gid": "101"}, 2)), \
             self.assertLogs("processor", level="INFO") as logs:
            _, _, result = processor._ocr_and_match(None, "PM")
        audit = result["_ocr_audit"]
        self.assertEqual(["primary", "identity"], [r["context"]["stage"] for r in audit["readings"]])
        public_text = "\n".join(logs.output) + processor._dry_run_ocr_preview(result)
        for private in ("PRIVATECONTACT", "76543210", "US123F4567", "QMH"):
            self.assertNotIn(private, public_text)

    def test_matching_date_uses_canonical_day_not_spaced_display_value(self):
        reading = self.normalize(service_date_raw="18 / 8 / 26")
        consensus = processor._consensus_ocr([reading, reading])
        ref = {"due_on": "2026-08-18", "job_type": "PM"}
        plain = dict(consensus, service_date_raw="18/8/2026", service_date_iso=None)
        self.assertEqual(50, asana_client._score_index_task_ref(ref, consensus, "PM"))
        self.assertEqual(asana_client._score_index_task_ref(ref, plain, "PM"),
                         asana_client._score_index_task_ref(ref, consensus, "PM"))


if __name__ == "__main__":
    unittest.main()

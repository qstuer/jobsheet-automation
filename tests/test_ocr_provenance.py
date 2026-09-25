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
        values = ["+852 9999 0067", "123"]
        reading = self.normalize(phone_candidates=values, department_room_raw="Asset# 123456 6F")
        values.append("99990070")
        reading["phone_candidates"].clear()
        self.assertEqual(["+852 9999 0067", "123"], reading["_ocr_audit"]["raw"]["phone_candidates"])
        self.assertEqual(["99990067"], reading["_ocr_audit"]["normalized"]["phone_candidates"])
        self.assertEqual("6F", reading["_ocr_audit"]["normalized"]["location_raw"])

    def test_candidate_limit_keeps_all_original_values_for_diagnosis(self):
        values = ["99990061", "99990062", "99990063", "99990064"]
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
                                 contact_person_raw="PRIVATECONTACT", phone_candidates=["99990070", "123"])
        with patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=reading), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=reading), \
             patch.object(asana_client, "find_task", return_value=({"gid": "101"}, 2)), \
             self.assertLogs("processor", level="INFO") as logs:
            _, _, result = processor._ocr_and_match(None, "PM")
        audit = result["_ocr_audit"]
        self.assertEqual(["primary", "identity"], [r["context"]["stage"] for r in audit["readings"]])
        public_text = "\n".join(logs.output) + processor._dry_run_ocr_preview(result)
        for private in ("PRIVATECONTACT", "99990070", "US123F4567", "QMH"):
            self.assertNotIn(private, public_text)

    def test_context_single_fields_override_broad_cards_only_after_two_votes(self):
        broad = self.normalize(product_raw="CX50", hospital_raw="QMH",
                               serial_candidates=["US123F4567"])
        focused = [self.normalize(product_raw="EPIQ Elite") for _ in range(2)] + [
            self.normalize(hospital_raw="PYNEH") for _ in range(2)
        ]
        with patch.dict(processor.os.environ, {processor.CONTEXT_FIELD_OCR_ENV: "1"}), \
             patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=broad), \
             patch.object(processor.private_ocr_context, "build_vocabulary", return_value={}) as build, \
             patch.object(nvidia_client, "ocr_jobsheet_context_field", side_effect=focused) as reader, \
             patch.object(asana_client, "find_task", return_value=({"gid": "task"}, 2)) as find:
            task, _, result = processor._ocr_and_match(None, "PM")
        self.assertEqual("task", task["gid"])
        self.assertEqual("EPIQ Elite", result["product_raw"])
        self.assertEqual("PYNEH", result["hospital_raw"])
        self.assertEqual(["product_raw", "product_raw", "hospital_raw", "hospital_raw"],
                         [call.args[2] for call in reader.call_args_list])
        build.assert_called_once()
        self.assertEqual("PYNEH", find.call_args.args[0]["hospital_raw"])
        self.assertEqual(["primary", "identity"] + ["context_field"] * 4,
                         [row["context"]["stage"] for row in result["_ocr_audit"]["readings"]])
        self.assertEqual("QMH", result["_ocr_audit"]["readings"][0]["raw"]["hospital_raw"])

    def test_context_disagreement_never_reaches_asana(self):
        broad = self.normalize(product_raw="CX50", hospital_raw="QMH",
                               serial_candidates=["US123F4567"])
        focused = [self.normalize(product_raw="EPIQ Elite"),
                   self.normalize(product_raw="Affiniti 70"),
                   self.normalize(hospital_raw="PYNEH"),
                   self.normalize(hospital_raw="PYNEH")]
        with patch.dict(processor.os.environ, {processor.CONTEXT_FIELD_OCR_ENV: "1"}), \
             patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=broad), \
             patch.object(processor.private_ocr_context, "build_vocabulary", return_value={}), \
             patch.object(nvidia_client, "ocr_jobsheet_context_field", side_effect=focused), \
             patch.object(asana_client, "find_task") as find:
            task, _, result = processor._ocr_and_match(None, "PM")
        self.assertIsNone(task)
        self.assertIsNone(result["product_raw"])
        self.assertEqual("conflicting_readings",
                         result["_ocr_audit"]["consensus_rejections"]["product_raw"])
        find.assert_not_called()

    def test_context_prompt_is_not_a_candidate_answer_list(self):
        vocabulary = {"product_families": ["TESTFAMILY"],
                      "product_models": ["TESTMODEL"],
                      "hospital_codes": ["ZZ"],
                      "same_hospital_codes": [["YY", "ZZ"]]}
        for field in ("product_raw", "hospital_raw"):
            prompt = nvidia_client._context_field_prompt(field, vocabulary)
            self.assertIn("NOT a", prompt)
            self.assertIn("Never make up", prompt)
        self.assertIn("TESTMODEL", nvidia_client._context_field_prompt("product_raw", vocabulary))
        self.assertIn("ZZ", nvidia_client._context_field_prompt("hospital_raw", vocabulary))
        with self.assertRaises(ValueError):
            nvidia_client._context_field_prompt("serial_candidates", vocabulary)

    def test_matching_date_uses_canonical_day_not_spaced_display_value(self):
        reading = self.normalize(service_date_raw="18 / 8 / 26")
        consensus = processor._consensus_ocr([reading, reading])
        ref = {"due_on": "2026-08-18", "job_type": "PM"}
        plain = dict(consensus, service_date_raw="18/8/2026", service_date_iso=None)
        self.assertEqual(50, asana_client._score_index_task_ref(ref, consensus, "PM"))
        self.assertEqual(asana_client._score_index_task_ref(ref, plain, "PM"),
                         asana_client._score_index_task_ref(ref, consensus, "PM"))

    def test_focused_date_pair_overrides_two_matching_broad_card_errors(self):
        broad = self.normalize(service_date_raw="20/9/2026")
        focused = [self.normalize(service_date_raw=value)
                   for value in ("20/8/26", "20.08.2026")]
        result = processor._consensus_ocr([broad, broad, *focused])
        self.assertTrue(processor._apply_focused_action_date(result, focused))
        self.assertEqual("2026-08-20", result["service_date_iso"])
        self.assertEqual("20/8/26", result["service_date_raw"])
        self.assertEqual("agreed", result["_ocr_audit"]["date_recheck"])
        self.assertEqual("20/9/2026", result["_ocr_audit"]["readings"][0]["raw"]["service_date_raw"])

    def test_focused_date_must_have_two_valid_matching_reads(self):
        broad = self.normalize(service_date_raw="20/9/2026")
        for focused in (
            [self.normalize(service_date_raw="20/8/2026")],
            [self.normalize(service_date_raw="20/8/2026"),
             self.normalize(service_date_raw="21/8/2026")],
            [self.normalize(service_date_raw="20/8/2026"),
             self.normalize(service_date_raw="not legible")],
        ):
            result = processor._consensus_ocr([broad, broad, *focused])
            self.assertFalse(processor._apply_focused_action_date(result, focused))
            self.assertIsNone(result["service_date_iso"])
            self.assertIsNone(result["service_date_raw"])
            self.assertIsNone(result["date_source"])
            self.assertEqual("unresolved", result["_ocr_audit"]["date_recheck"])

    def test_isolated_matching_rechecks_consensus_date_before_accepting_task(self):
        broad = self.normalize(product_raw="EPIQ Elite", hospital_raw="QMH",
                               serial_candidates=["US123F4567"],
                               phone_candidates=["99990070"],
                               contact_person_raw="TEST PERSON",
                               service_date_raw="20/9/2026")
        context_cards = [self.normalize(product_raw="EPIQ Elite") for _ in range(2)] + [
            self.normalize(hospital_raw="QMH") for _ in range(2)
        ]
        focused_dates = [self.normalize(service_date_raw="20/8/26"),
                         self.normalize(service_date_raw="20/08/2026")]

        def find(ocr, job_type):
            return ({"gid": "synthetic-task"}, 2) if ocr.get("service_date_iso") == "2026-08-20" else (None, 0)

        with patch.dict(processor.os.environ, {processor.CONTEXT_FIELD_OCR_ENV: "1"}), \
             patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_support_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_serial_candidates", return_value=["US123F4567"]), \
             patch.object(nvidia_client, "ocr_jobsheet_context_field", side_effect=context_cards), \
             patch.object(nvidia_client, "ocr_jobsheet_focused_field", side_effect=focused_dates) as focused, \
             patch.object(processor.private_ocr_context, "build_vocabulary", return_value={}), \
             patch.object(asana_client, "find_task", side_effect=find):
            task, _, result = processor._ocr_and_match(None, "PM")

        self.assertEqual("synthetic-task", task["gid"])
        self.assertEqual("2026-08-20", result["service_date_iso"])
        self.assertEqual(["service_date_raw", "service_date_raw"],
                         [call.args[2] for call in focused.call_args_list])
        self.assertEqual(["date_recheck", "date_recheck"],
                         [row["context"]["stage"] for row in result["_ocr_audit"]["readings"][-2:]])

    def test_action_identifier_card_only_keeps_visible_mixed_tokens(self):
        response = json.dumps({"action_identifiers": [
            "PRB-A123", "TX9-4567", "12345678", "ordinary", "PRB?999",
            "prba123", "AB1",
        ]})
        with patch.object(nvidia_client, "crop_jobsheet_field_card", return_value="synthetic") as crop, \
             patch.object(nvidia_client, "_call_vision", return_value=response) as call:
            result = nvidia_client.ocr_jobsheet_action_identifiers(None, 0, zoom=5.0)
        self.assertEqual(["PRBA123", "TX94567"], result)
        self.assertEqual(("action_taken",), crop.call_args.args[2])
        self.assertIn("do not include ordinary words", call.call_args.args[0])

    def test_isolated_action_identifiers_resolve_visit_without_leaking_values(self):
        broad = self.normalize(product_raw="EPIQ Elite", hospital_raw="QMH",
                               serial_candidates=["US123F4567"],
                               phone_candidates=["99990070"],
                               contact_person_raw="TEST PERSON",
                               service_date_raw="20/9/2026")
        context_cards = [self.normalize(product_raw="EPIQ Elite") for _ in range(2)] + [
            self.normalize(hospital_raw="QMH") for _ in range(2)
        ]
        focused_dates = [self.normalize(service_date_raw="20/9/2026") for _ in range(2)]
        action_reads = [["PRBA123", "TX94567"], ["TX94567", "PRBA123"]]

        def find(ocr, job_type):
            return ({"gid": "synthetic-task"}, 2) if len(ocr.get("action_identifiers") or []) == 2 else (None, 0)

        with patch.dict(processor.os.environ, {processor.CONTEXT_FIELD_OCR_ENV: "1"}), \
             patch.object(asana_client, "_device_index", {"schema_version": 3}), \
             patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_support_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_serial_candidates", return_value=["US123F4567"]), \
             patch.object(nvidia_client, "ocr_jobsheet_context_field", side_effect=context_cards), \
             patch.object(nvidia_client, "ocr_jobsheet_focused_field", side_effect=focused_dates), \
             patch.object(nvidia_client, "ocr_jobsheet_action_identifiers", side_effect=action_reads) as read, \
             patch.object(processor.private_ocr_context, "build_vocabulary", return_value={}), \
             patch.object(asana_client, "find_task", side_effect=find), \
             self.assertLogs("processor", level="INFO") as logs:
            task, _, result = processor._ocr_and_match(None, "PM")

        self.assertEqual("synthetic-task", task["gid"])
        self.assertEqual(["PRBA123", "TX94567"], result["action_identifiers"])
        self.assertEqual(2, read.call_count)
        for private in ("PRBA123", "TX94567", "US123F4567", "99990070"):
            self.assertNotIn(private, "\n".join(logs.output) + processor._dry_run_ocr_preview(result))

    def test_action_identifier_disagreement_cannot_resolve_visit(self):
        broad = self.normalize(product_raw="EPIQ Elite", hospital_raw="QMH",
                               serial_candidates=["US123F4567"],
                               phone_candidates=["99990070"],
                               contact_person_raw="TEST PERSON",
                               service_date_raw="20/9/2026")
        context_cards = [self.normalize(product_raw="EPIQ Elite") for _ in range(2)] + [
            self.normalize(hospital_raw="QMH") for _ in range(2)
        ]
        with patch.dict(processor.os.environ, {processor.CONTEXT_FIELD_OCR_ENV: "1"}), \
             patch.object(asana_client, "_device_index", {"schema_version": 3}), \
             patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_support_fields", return_value=broad), \
             patch.object(nvidia_client, "ocr_jobsheet_serial_candidates", return_value=["US123F4567"]), \
             patch.object(nvidia_client, "ocr_jobsheet_context_field", side_effect=context_cards), \
             patch.object(nvidia_client, "ocr_jobsheet_focused_field",
                          side_effect=[self.normalize(service_date_raw="20/9/2026") for _ in range(2)]), \
             patch.object(nvidia_client, "ocr_jobsheet_action_identifiers",
                          side_effect=[["PRBA123", "TX94567"], ["PRBA123"]]), \
             patch.object(processor.private_ocr_context, "build_vocabulary", return_value={}), \
             patch.object(asana_client, "find_task", return_value=(None, 0)), \
             patch.object(asana_client, "get_close_index_candidates", return_value=[]):
            task, _, result = processor._ocr_and_match(None, "PM")
        self.assertIsNone(task)
        self.assertNotIn("action_identifiers", result)
        self.assertEqual(["PRBA123"], result["_ocr_audit"]["action_identifier_recheck"]["agreed"])

    def test_action_identifier_fallback_is_off_without_isolated_flag(self):
        reading = self.normalize(product_raw="EPIQ Elite", hospital_raw="QMH",
                                 serial_candidates=["US123F4567"],
                                 phone_candidates=["99990070"],
                                 contact_person_raw="TEST PERSON",
                                 service_date_raw="20/9/2026")
        with patch.dict(processor.os.environ, {processor.CONTEXT_FIELD_OCR_ENV: "0"}), \
             patch.object(asana_client, "_device_index", {"schema_version": 3}), \
             patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=reading), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=reading), \
             patch.object(nvidia_client, "ocr_jobsheet_support_fields", return_value=reading), \
             patch.object(nvidia_client, "ocr_jobsheet_serial_candidates", return_value=["US123F4567"]), \
             patch.object(nvidia_client, "ocr_jobsheet_action_identifiers") as action_reader, \
             patch.object(asana_client, "find_task", return_value=(None, 0)), \
             patch.object(asana_client, "get_close_index_candidates", return_value=[]):
            task, _, result = processor._ocr_and_match(None, "PM")
        self.assertIsNone(task)
        self.assertNotIn("action_identifiers", result)
        action_reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()

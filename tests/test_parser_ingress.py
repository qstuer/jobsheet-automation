"""Generic synthetic ingress tests; no customer cases or external calls."""
import json
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src import asana_client as ac, asana_index, matching_rules as rules
from src import nvidia_client as vision, ocr_reference, processor, product_catalog
from tests.test_matching_rules import reading, task
from tests import test_evidence_release as release_tests


class ParserIngressTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        ac._task_cache.clear()
        self.addCleanup(ac.clear_device_index)

    def install(self, *products, weak=False):
        ac.set_device_index({"devices": [{
            "serial": "" if weak else "ABC12X3456", "weak_identity": weak,
            "product_variants": list(products), "task_refs": [],
        }]})

    def normalize(self, **fields):
        with patch.object(vision, "_today", return_value=date(2026, 9, 30)):
            return vision._normalize_ocr_data(fields)

    def test_indexed_model_outside_fixed_list_survives_ocr(self):
        self.install("Sonovista 30X")
        result = self.normalize(product_raw="Sonovista 30X")
        self.assertEqual("Sonovista 30X", result["product_raw"])
        self.assertEqual("Sonovista 30X", result["product"])
        self.assertEqual("indexed_spelling", result["_ocr_audit"]["product_reference_source"])

    def test_indexed_family_preserves_unlisted_digits_and_suffix(self):
        self.install("Sonovista 30X")
        for raw in ("Sonovista 20", "Sonovista 35W"):
            result = self.normalize(product_raw=raw)
            self.assertEqual(raw, result["product_raw"])
            self.assertEqual(raw, result["product"])
            self.assertEqual("indexed_family", result["_ocr_audit"]["product_reference_source"])
            self.assertEqual(raw, result["_ocr_audit"]["raw"]["product_raw"])

    def test_weak_row_can_supply_product_vocabulary_not_equipment_identity(self):
        self.install("Sonovista 30X", weak=True)
        result = self.normalize(product_raw="Sonovista 30X")
        self.assertEqual("indexed_spelling", result["_ocr_audit"]["product_reference_source"])
        rows, reason = rules.retrieve_rows(ac._device_index, rules.evidence([
            {**reading(), **result}, {**reading(), **result}]))
        self.assertEqual([], rows)
        self.assertEqual("missing_serial_not_unique", reason)

    def test_reference_words_and_identifiers_cannot_supply_product_evidence(self):
        bad = ("WO 123456", "Phone 23456789", "Asset 12345", "ABC12X3456",
               "A1B2CD", "Sonovista 20 30", "CM", "ordinary words")
        self.install(*bad)
        for value in bad:
            with self.subTest(value=value):
                result = self.normalize(product_raw=value)
                self.assertIsNone(result["product_raw"])
                self.assertEqual(value, result["_ocr_audit"]["raw"]["product_raw"])

    def test_unindexed_family_is_not_promoted_by_another_product(self):
        self.install("Sonovista 30X")
        result = self.normalize(product_raw="Othermodel 20")
        self.assertIsNone(result["product_raw"])
        self.assertIn("unconfirmed_product", result["_ocr_audit"]["rejections"]["product_raw"])

    def test_no_index_keeps_old_unknown_product_rejection(self):
        result = self.normalize(product_raw="Sonovista 30X")
        self.assertIsNone(result["product"])

    def test_short_serial_is_literal_visual_evidence_not_standard_serial(self):
        for value in ("A1B2CD", "A1B2CDE", "a1b2cd"):
            result = self.normalize(serial_candidates=[value])
            self.assertEqual([], result["serial_candidates"])
            self.assertEqual([value.upper()], result["serial_visual_candidates"])
            self.assertFalse(vision._valid_serial_token(value))
            self.assertEqual([value], result["_ocr_audit"]["raw"]["serial_candidates"])

    def test_short_serial_does_not_strip_uncertainty_or_labels(self):
        for value in ("A1B?CD", "A1B2C", "A1B2CDEF", "ABCDEF", "123456",
                      "ABCDE1", "A12345", "Serial: A1B2CD", "A1-B2CD"):
            with self.subTest(value=value):
                self.assertIsNone(ocr_reference.compact_serial_token(value))
                self.assertIsNone(vision._visible_serial_token(value))

    def test_preview_counts_visual_serial_without_disclosing_it(self):
        result = self.normalize(serial_candidates=["A1B2CD"])
        text = processor._dry_run_ocr_preview(result)
        self.assertIn("serial", text)
        self.assertNotIn("A1B2CD", text)

    def test_business_labels_are_not_short_machine_serials(self):
        for value in ("CM2026", "PM1234", "WO1234", "SR12345", "ROOM12", "RM12345"):
            self.assertIsNone(ocr_reference.compact_serial_token(value))

    def test_short_title_identifier_requires_own_product_serial_segments(self):
        record = asana_index.task_to_record(task(serial="A1B2CD", product="Sonovista 30X"))
        self.assertEqual("A1B2CD", record["serial"])
        self.assertEqual("SONOVISTA", record["product"])
        self.assertEqual(["Sonovista 30X"], record["product_variants"])

    def test_short_title_identifiers_not_taken_from_wrong_fields(self):
        for title in ("Test Hospital / A1B2CD", "Test Hospital / Engineer / A1B2CD",
                      "Test Hospital / WO 123456 / A1B2CD",
                      "Test Hospital / Affiniti 70 / Tel 23456789",
                      "Test Hospital / Affiniti 70 / Serial: A1B2CD",
                      "Test Hospital / Affiniti 70 A1B2CD"):
            with self.subTest(title=title):
                record = asana_index.task_to_record({"gid": "101", "name": title})
                self.assertFalse(record["serial"])

    def test_multiple_compact_serial_segments_are_not_arbitrarily_selected(self):
        record = asana_index.task_to_record({"gid": "101", "name":
            "Test Hospital / Sonovista 30X / A1B2CD / Othermodel 20 / E3F4GH"})
        self.assertFalse(record["serial"])
        self.assertTrue(record["weak_identity"])

    def test_full_index_records_one_short_device_with_separate_visits(self):
        index = asana_index.build_index([
            {**task("101", serial="A1B2CD"), "_project_job_type": "PM"},
            {**task("102", serial="A1B2CD", phone="23456780", kind="CM"), "_project_job_type": "CM"},
        ], window_start=date(2026, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(1, index["device_count"])
        self.assertEqual(2, len(index["devices"][0]["task_refs"]))
        self.assertEqual({"23456789", "23456780"}, set(index["devices"][0]["phones"]))
        self.assertEqual(2, index["product_parser_version"])

    def test_changed_title_parser_cannot_incrementally_mix_old_rows(self):
        index = asana_index.build_index([task()], window_start=date(2026, 1, 1),
                                        window_end=date(2026, 12, 31))
        index["product_parser_version"] = product_catalog.PARSER_VERSION - 1
        with self.assertRaises(asana_index.AsanaIndexError):
            asana_index.build_index([], existing_index=index)

    def choose_short(self, readings=None, tasks=None, *, upload=date(2026, 10, 5)):
        return rules.select_task(tasks or [task(serial="A1B2CD")],
            rules.evidence(readings or [reading(serial="A1B2CD")] * 2), "PM", upload)

    def test_short_exact_identifier_passes_only_full_joint_confirmation(self):
        normalized = self.normalize(serial_candidates=["A1B2CD"], product_raw="Affiniti 70",
            hospital_raw="Test Hospital", phone_candidates=["23456789"], service_date_raw="28/9/2026")
        selected, reason = self.choose_short([normalized] * 2)
        self.assertEqual("101", selected["gid"])
        self.assertEqual("action_date_live_visit_confirmed", reason)

    def test_short_serial_single_reading_and_fuzzy_spelling_stay_pending(self):
        for readings in ([reading(serial="A1B2CD")],
                         [reading(serial="A1B2CE")] * 2,
                         [reading(serial="A1B2CD? ")] * 2):
            # Production normalization never strips '?' from a short token.
            normalized = [self.normalize(serial_candidates=r["serial_candidates"],
                product_raw=r["product_raw"], hospital_raw=r["hospital_raw"],
                phone_candidates=r["phone_candidates"], service_date_raw=r["service_date_raw"])
                for r in readings]
            self.assertIsNone(self.choose_short(normalized)[0])

    def test_short_identifier_needs_all_three_nonserial_fields(self):
        for missing in ("product", "hospital", "phone"):
            self.assertIsNone(self.choose_short([reading(serial="A1B2CD", **{missing: None})] * 2)[0])

    def test_short_identifier_rejects_wrong_hospital_product_and_phone(self):
        for conflict in ({"hospital": "Other Hospital"}, {"product": "EPIQ"},
                         {"phone": "23456780"}, {"phone": "2345678"}):
            self.assertIsNone(self.choose_short([reading(serial="A1B2CD", **conflict)] * 2)[0])

    def test_short_identifier_keeps_type_date_and_tie_gates(self):
        for candidates in ([task(serial="A1B2CD", kind="CM")],
                           [task(serial="A1B2CD", due="2026-08-01")],
                           [task("101", serial="A1B2CD"), task("102", serial="A1B2CD")]):
            self.assertIsNone(self.choose_short(tasks=candidates)[0])

    def test_short_identifier_missing_asset_is_not_a_penalty(self):
        self.assertIsNotNone(self.choose_short(tasks=[task(serial="A1B2CD", asset="9876543")])[0])

    def test_short_reading_cannot_expand_into_a_longer_serial(self):
        self.assertIsNone(self.choose_short([reading(serial="AB1234")] * 2,
            tasks=[task(serial="AB001234")])[0])

    def test_short_identifier_date_unknown_remains_pending_without_upload_time(self):
        self.assertIsNone(self.choose_short([reading(serial="A1B2CD", day=None)] * 2,
                                           upload=None)[0])

    def test_compact_live_lookup_is_exact_literal_and_bounded(self):
        ev = rules.evidence([reading(serial="A1B2CD")] * 2)
        with patch.object(ac, "_typeahead", return_value=[{"gid": "101"}]) as lookup, \
             patch.object(ac, "_fetch_task", return_value=task(serial="A1B2CD")) as fetch:
            pool, _ = rules.live_pool([], ev, None)
        lookup.assert_called_once_with("A1B2CD")
        fetch.assert_called_once_with("101")
        self.assertEqual(1, len(pool))

    def test_compact_live_lookup_missing_corroboration_does_not_query_workspace(self):
        for readings in ([reading(serial="A1B2CD")],
                         [reading(serial="A1B2CD", product=None)] * 2,
                         [reading(serial="A1B2CD", hospital=None)] * 2,
                         [reading(serial="A1B2CD", phone=None)] * 2,
                         [reading(serial="A1B2CD", phone="2345678")] * 2):
            with patch.object(ac, "_typeahead") as lookup:
                self.assertEqual([], rules.live_pool([], rules.evidence(readings), None)[0])
            lookup.assert_not_called()

    def test_compact_live_lookup_candidate_overflow_fails_closed(self):
        with patch.object(ac, "_typeahead", return_value=[{"gid": str(i)} for i in range(81)]), \
             patch.object(ac, "_fetch_task") as fetch:
            pool, reason = rules.live_pool([], rules.evidence([reading(serial="A1B2CD")] * 2), None)
        self.assertEqual(([], "task_candidate_limit"), (pool, reason))
        fetch.assert_not_called()

    def test_product_variant_normalization_flows_through_live_selection(self):
        self.install("Sonovista 30X")
        normalized = self.normalize(serial_candidates=["ABC12X3456"], product_raw="Sonovista 35W",
            hospital_raw="Test Hospital", phone_candidates=["23456789"], service_date_raw="28/9/2026")
        selected, _ = rules.select_task([task(product="Sonovista 30X")],
            rules.evidence([normalized] * 2), "PM", None)
        self.assertEqual("101", selected["gid"])
        self.assertEqual("Sonovista 35W", normalized["product_raw"])

    def test_prompt_reference_is_incomplete_not_permission_to_invent_model(self):
        prompt = vision._context_field_prompt("product_raw", {
            "product_families": ["SONOVISTA"], "product_models": ["Sonovista 30X"]})
        self.assertIn("context is incomplete", prompt)
        self.assertIn("without replacing its numbers or suffix", prompt)
        self.assertIn("Never make up", prompt)

    def test_public_diagnostic_whitelists_categories_not_private_audit(self):
        audit = {"version": rules.RULESET_VERSION,
            "trace": ["index_miss", "private-person-phone-serial", {"raw": "private"}],
            "read_count": 3, "reference_date_known": True,
            "raw": "private", "last_live_diagnostic": {
                **rules.diagnostic_pool([task()], rules.evidence([reading()] * 2), "PM"),
                "phone": "23456789", "customer": "Test Hospital"}}
        audit["last_live_diagnostic"]["nearest_live_facts"][0]["serial"] = "ABC12X3456"
        public = rules.public_diagnostic(audit)
        self.assertEqual(["index_miss"], public["trace"])
        self.assertEqual(3, public["read_count"])
        self.assertEqual(1, len(public["live"]["nearest_live_facts"]))
        for private in ("private", "23456789", "Test Hospital", "ABC12X3456"):
            self.assertNotIn(private, json.dumps(public))

    def test_public_diagnostic_caps_candidate_rows_and_numeric_values(self):
        audit = {"version": rules.RULESET_VERSION, "read_count": "23456789",
                 "last_live_diagnostic": {"live_task_count": 999999,
                   "nearest_live_facts": [{"serial_distance": "ABC12X3456", "asset_level": 999}] * 20}}
        public = rules.public_diagnostic(audit)
        self.assertEqual(0, public["read_count"])
        # This is the total read budget, not a single device's visit budget.
        self.assertEqual(rules.MAX_SCANNED_TASKS, public["live"]["live_task_count"])
        self.assertEqual(80, rules.MAX_LIVE_TASKS)
        self.assertEqual(10, len(public["live"]["nearest_live_facts"]))
        self.assertEqual(2, public["live"]["nearest_live_facts"][0]["asset_level"])
        self.assertNotIn("ABC12X3456", str(public))

    def test_public_diagnostic_rejects_other_audits(self):
        for audit in (None, "private", {"version": "private"}):
            self.assertEqual({}, rules.public_diagnostic(audit))

    def test_complete_normalized_cm_path_with_short_serial_is_read_only(self):
        self.install("Sonovista 30X")
        normalized = self.normalize(serial_candidates=["A1B2CD"], product_raw="Sonovista 35W",
            hospital_raw="Test Hospital", phone_candidates=["23456789"], service_date_raw="28/9/2026")
        doc = MagicMock()
        doc.__len__.return_value = 1
        with patch.object(vision, "ocr_jobsheet_fields", return_value=normalized), \
             patch.object(vision, "ocr_jobsheet_identity_fields", return_value=normalized), \
             patch.object(vision, "ocr_jobsheet_support_fields", return_value=normalized), \
             patch.object(ac, "_typeahead", return_value=[{"gid": "101"}]), \
             patch.object(ac, "_fetch_task", return_value=task(serial="A1B2CD", product="Sonovista 30X", kind="CM")):
            selected, _, ocr = rules.read_and_match(doc, "CM", date(2026, 10, 5))
        self.assertEqual("101", selected["gid"])
        diagnostic = processor._public_matching_diagnostic(ocr)
        self.assertIn("action_date_live_visit_confirmed", diagnostic["trace"])
        self.assertNotIn("A1B2CD", str(diagnostic))


class ParserDryRunWiringTests(unittest.TestCase):
    """Use the established fake-cloud harness without repeating its test cases."""
    def test_only_readonly_results_include_sanitized_matching_diagnostic(self):
        import tempfile
        harness = release_tests.ReleaseWiringTests()
        with tempfile.TemporaryDirectory() as temp:
            for selected in (task(), None):
                result, _, _, uploads, deletes, moves, _ = harness.process(temp, selected, dry_run=True)
                self.assertEqual(rules.RULESET_VERSION, result["matching_diagnostic"]["version"])
                self.assertEqual((0, 0, 0), (uploads, deletes, moves))
            result, *_ = harness.process(temp, task(), dry_run=False)
            self.assertNotIn("matching_diagnostic", result)

    def test_main_emits_anonymous_diagnostic_in_readonly_log(self):
        import os
        audit = {"version": rules.RULESET_VERSION, "trace": ["live_identity_not_confirmed"],
                 "last_live_diagnostic": rules.diagnostic_pool([task()], rules.evidence([reading()] * 2), "PM")}
        with patch.dict(os.environ, {processor.MATCHING_RULES_ENV: rules.RULESET_VERSION,
                processor.DRY_RUN_ENV: "1", processor.TARGET_FILE_ENV: "scan__job1_PM.pdf",
                processor.CONFIRMED_FILENAME_ENV: "", processor.ASANA_INDEX_FILE_ENV: "",
                processor.REVIEW_ACTION_DATE_ENV: "", processor.REVIEW_TASK_ENV: "", "GITHUB_STEP_SUMMARY": ""}), \
             patch.object(processor.rclone_helper, "list_pdfs", return_value=["scan__job1_PM.pdf"]), \
             patch.object(processor, "_process_split_file", return_value={"status": "pending",
                 "matching_diagnostic": rules.public_diagnostic(audit)}), \
             self.assertLogs("processor", level="INFO") as logs:
            self.assertEqual(0, processor.main())
        public = str(logs.output)
        self.assertIn("live_identity_not_confirmed", public)
        for private in ("ABC12X3456", "Test Hospital", "23456789"):
            self.assertNotIn(private, public)


if __name__ == "__main__":
    unittest.main()

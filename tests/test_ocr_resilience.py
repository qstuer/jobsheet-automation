"""Shared resilience policies; synthetic identities, no network or answer keys."""
import unittest
from contextlib import ExitStack
from datetime import date
from unittest.mock import MagicMock, patch

from src import asana_client as ac, private_ocr_context, product_catalog
from src import asana_index, matching_rules as rules, nvidia_client as vision, ocr_reference, processor
from src import rclone_helper
from tests.test_matching_rules import reading, task


def vocabulary_index(devices=None):
    return {"schema_version": 3, "devices": devices or [], "location_directory": [{
        "match_enabled": True, "alias_sources": ["confirmed"],
        "device_count": 2, "confirmed_aliases": ["ZZ"]}]}


class ProductVocabularyTests(unittest.TestCase):
    def test_verified_legacy_product_does_not_depend_on_device_identity(self):
        index = vocabulary_index([{"serial": "", "weak_identity": True,
                                  "product_variants": ["IU22", "WO 123456", "Unknownmodel 20"]}])
        vocabulary = private_ocr_context.build_vocabulary(index)
        self.assertIn("IU22", vocabulary["product_models"])
        self.assertIn("IU", vocabulary["product_families"])
        self.assertNotIn("WO 123456", str(vocabulary))
        self.assertNotIn("Unknownmodel 20", str(vocabulary))
        self.assertTrue(index["devices"][0]["weak_identity"])

    def test_rare_verified_models_not_lost_to_top_twelve_cutoff(self):
        names = [f"Testmodel {i}" for i in range(15)]
        with patch.object(ac, "OCR_PRODUCT_NAMES", names):
            vocabulary = private_ocr_context.build_vocabulary(vocabulary_index())
        self.assertEqual(set(names), set(vocabulary["product_models"]))

    def test_product_vocabulary_overflow_needs_review_not_truncation(self):
        with patch.object(ac, "OCR_PRODUCT_NAMES", [f"Testmodel {i}" for i in range(33)]):
            with self.assertRaises(ValueError):
                private_ocr_context.build_vocabulary(vocabulary_index())

    def test_verified_written_variants_preserve_digits_and_suffix(self):
        for name in ac.VERIFIED_SERVICE_PRODUCT_NAMES:
            self.assertEqual("confirmed", product_catalog.inspect_product(name)["status"])
            self.assertEqual(name, ac.normalize_product(name))
        for raw in ("IU23", "TC21", "EPIQ 6W", "Affiniti 51W"):
            self.assertEqual(raw, ac.normalize_product(raw))
            self.assertNotEqual("confirmed", product_catalog.inspect_product(raw)["status"])

    def test_short_model_word_not_invented_from_two_wrong_letters(self):
        self.assertEqual("IU22", ac.normalize_product("Iv22"))
        self.assertEqual("TC20", ac.normalize_product("TV20"))
        for raw in ("AB22", "LZ22", "ZZ20"):
            self.assertEqual(raw, ac.normalize_product(raw))


class ProductDriftTests(unittest.TestCase):
    def anchors(self, value):
        return [{"product_raw": None, "_source": source,
                 "_ocr_audit": {"raw": {"product_raw": value}}}
                for source in ("cover", "cover_identity")]

    def test_unrelated_familiar_product_does_not_replace_rejected_raw(self):
        anchors = self.anchors("LZ22")
        self.assertTrue(ocr_reference.product_focus_conflicts(anchors, {"product_raw": "EPIQ 7G"}))
        self.assertEqual("LZ22", anchors[0]["_ocr_audit"]["raw"]["product_raw"])

    def test_plaintext_format_recovery_cannot_bypass_product_drift_guard(self):
        reread = vision._recover_context_literal('The visible value is "EPIQ 7G".', "product_raw")
        self.assertTrue(ocr_reference.product_focus_conflicts(self.anchors("LZ22"), reread))

    def test_small_spelling_change_and_same_family_not_marked_drift(self):
        for original, reread in (("Iv22", "IU22"), ("Affiniti 50", "Affiniti 50W"),
                                 ("EPLQ 5G", "EPIQ 5G"), ("Axfinxxy 50W", "Affiniti 50W"),
                                 ("Affiniti 50", "Affiniti 70")):
            self.assertFalse(ocr_reference.product_focus_conflicts(self.anchors(original), {"product_raw": reread}))

    def test_single_unknown_reading_not_a_permanent_veto(self):
        self.assertFalse(ocr_reference.product_focus_conflicts(self.anchors("LZ22")[:1], {"product_raw": "IU22"}))

    def test_drift_excluded_from_matching_and_display_consensus_not_raw(self):
        excluded = {**reading(product="EPIQ 7G"),
                    "_evidence_exclusions": {"product_raw": "product_reread_drift"},
                    "_ocr_audit": {"raw": {"product_raw": "EPIQ 7G"}}}
        reads = [excluded] * 2 + [reading(product="IU22", page=1)] * 2
        self.assertEqual({"IU"}, rules._stable(rules.evidence(reads), "product_raw"))
        result = processor._consensus_ocr(reads)
        self.assertEqual("IU22", result["product"])
        self.assertEqual("EPIQ 7G", result["_ocr_audit"]["readings"][0]["raw"]["product_raw"])

    def test_real_crosspage_conflict_still_pending(self):
        reads = [reading(product="EPIQ 7G")] * 2 + [reading(product="IU22", page=1)] * 2
        selected, reason = rules.select_task([task(product="IU22")], rules.evidence(reads), "PM", None)
        self.assertIsNone(selected)
        self.assertEqual("crosspage_identity_conflict", reason)


class CompactAmbiguityTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)
        clock = patch.object(vision, "_today", return_value=date(2026, 9, 30))
        clock.start()
        self.addCleanup(clock.stop)

    def reads(self, serial="AOB8CD", **kwargs):
        fields = reading(serial=serial, product="IU22", **kwargs)
        normalized = vision._normalize_ocr_data({
            "serial_candidates": fields["serial_candidates"], "product_raw": fields["product_raw"],
            "hospital_raw": fields["hospital_raw"], "phone_candidates": fields["phone_candidates"],
            "service_date_raw": fields["service_date_raw"], "asset_candidates": fields["asset_candidates"]})
        return [normalized, normalized]

    def live(self, gid="101", serial="A0B8CD", **kwargs):
        return task(gid, serial=serial, product="IU22", **kwargs)

    def choose(self, reads=None, tasks=None):
        return rules.select_task(tasks or [self.live()], rules.evidence(reads or self.reads()), "PM", None)

    def hint_row(self, **kwargs):
        record = asana_index.task_to_record(self.live(**kwargs))
        return {**record, "serial": "", "weak_identity": True,
                "task_refs": [{**record["task_refs"][0], "serial": ""}]}

    def test_ambiguous_literal_retained_without_rewriting_it(self):
        value = self.reads()[0]
        self.assertEqual([], value["serial_candidates"])
        self.assertEqual(["AOB8CD"], value["serial_visual_candidates"])
        self.assertEqual(["AOB8CD"], value["_ocr_audit"]["raw"]["serial_candidates"])
        self.assertIsNone(ocr_reference.compact_serial_token("AOB8CD"))

    def test_one_o_zero_difference_requires_live_joint_confirmation(self):
        selected, reason = self.choose()
        self.assertEqual("101", selected["gid"])
        self.assertEqual("action_date_live_visit_confirmed", reason)
        self.assertEqual("AOB8CD", self.reads()[0]["serial_visual_candidates"][0])

    def test_arbitrary_short_edits_and_length_changes_not_allowed(self):
        for actual in ("A1B8CD", "A0B8CE", "A00B8CD", "US00B8CD", "AOB8CD"):
            with self.subTest(actual=actual):
                self.assertIsNone(self.choose(tasks=[self.live(serial=actual)])[0])

    def test_uncertainty_labels_and_punctuation_are_not_stripped(self):
        for value in ("AO?8CD", "Serial: AOB8CD", "AO-B8CD", "ROOM1O", "ABCDEF", "ABCDE1"):
            self.assertIsNone(ocr_reference.compact_serial_hint(value))

    def test_phone_hospital_type_and_date_conflicts_stay_pending(self):
        for conflict in ({"phone": "23456780"}, {"hospital": "Other Hospital"},
                         {"kind": "CM"}, {"due": "2026-09-13"}):
            self.assertIsNone(self.choose(tasks=[self.live(**conflict)])[0])
        self.assertIsNone(rules.select_task([task(serial="A0B8CD", product="EPIQ")],
                          rules.evidence(self.reads()), "PM", None)[0])

    def test_reliable_date_not_replaced_by_upload_prior(self):
        selected, reason = rules.select_task([self.live()], rules.evidence(self.reads(day=None)),
                                             "PM", date(2026, 10, 5))
        self.assertIsNone(selected)
        self.assertEqual("compact_serial_needs_action_date", reason)

    def test_serial_and_full_phone_must_each_be_repeated(self):
        reads = self.reads()
        self.assertIsNone(self.choose(reads=reads[:1])[0])
        reads[1] = {**reads[1], "phone_candidates": []}
        self.assertIsNone(self.choose(reads=reads)[0])

    def test_no_asset_or_mismatch_never_penalized(self):
        self.assertIsNotNone(self.choose(tasks=[self.live(asset="7654321")])[0])
        self.assertIsNotNone(self.choose(reads=self.reads(asset="1234567"),
                                        tasks=[self.live(asset="7654321")])[0])

    def test_two_reasonable_o_zero_devices_are_pending_even_with_asset_bonus(self):
        reads = self.reads(serial="AOB8CO", asset="1234567")
        selected, reason = self.choose(reads, [self.live("101", serial="A0B8CO", asset="1234567"),
                                               self.live("102", serial="AOB8C0", asset="7654321")])
        self.assertIsNone(selected)
        self.assertEqual("compact_serial_not_unique", reason)

    def test_weak_row_only_retrieves_correlated_task_refs_and_remains_weak(self):
        row = self.hint_row()
        index = {"devices": [row]}
        rows, reason = rules.retrieve_rows(index, rules.evidence(self.reads()))
        self.assertEqual("weak_compact_task_hints", reason)
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["_retrieval_hint_only"])
        self.assertTrue(row["weak_identity"])
        self.assertEqual("", row["serial"])
        self.assertIsNone(self.choose(tasks=[self.live(serial="A1B2CD")])[0])

    def test_weak_row_cannot_mix_different_visit_fields(self):
        row = self.hint_row()
        ref = row["task_refs"][0]
        row["task_refs"] = [{**ref, "phones": []}, {**ref, "gid": "102", "product_variant": "EPIQ", "product": "EPIQ"}]
        self.assertEqual([], rules.retrieve_rows({"devices": [row]}, rules.evidence(self.reads()))[0])

    def test_old_parser_blank_product_needs_same_task_exact_phone_and_asset(self):
        row = self.hint_row(asset="1234567")
        row["task_refs"][0].update({"product_variant": "", "product": ""})
        for asset, expected in (("1234567", 1), (None, 0), ("1234560", 0)):
            ev = rules.evidence(self.reads(asset=asset))
            self.assertEqual(expected, len(rules.retrieve_rows({"devices": [row]}, ev)[0]))
        row["task_refs"][0].update({"product": "EPIQ"})
        self.assertEqual([], rules.retrieve_rows({"devices": [row]}, rules.evidence(self.reads(asset="1234567")))[0])

    def test_old_blank_product_does_not_borrow_another_tasks_asset(self):
        row = self.hint_row(asset="1234567")
        ref = row["task_refs"][0]
        row["task_refs"] = [{**ref, "product_variant": "", "product": "", "assets": []},
                            {**ref, "gid": "102", "phones": []}]
        self.assertEqual([], rules.retrieve_rows({"devices": [row]}, rules.evidence(self.reads(asset="1234567")))[0])

    def test_ambiguous_hint_never_triggers_generated_serial_workspace_query(self):
        with patch.object(ac, "_typeahead") as search:
            self.assertEqual([], rules.live_pool([], rules.evidence(self.reads()), None)[0])
        search.assert_not_called()

    def test_legacy_hints_fetch_live_then_refresh_only_real_canonical_serial(self):
        ev = rules.evidence(self.reads())
        rows, _ = rules.retrieve_rows({"devices": [self.hint_row()]}, ev)
        tasks = {"101": self.live(due="2026-08-28"), "102": self.live("102")}
        with patch.object(ac, "_typeahead", return_value=[{"gid": "102"}]) as search, \
             patch.object(ac, "_fetch_task", side_effect=lambda gid: tasks[gid]) as fetch:
            pool, _ = rules.live_pool(rows, ev, None)
        search.assert_called_once_with("A0B8CD")
        self.assertEqual(2, fetch.call_count)
        self.assertEqual("102", rules.select_task(pool, ev, "PM", None)[0]["gid"])

    def test_weak_hints_candidate_overflow_does_not_drop_rivals(self):
        row = self.hint_row()
        row["task_refs"] = [{**row["task_refs"][0], "gid": str(i)} for i in range(81)]
        ev = rules.evidence(self.reads())
        rows, _ = rules.retrieve_rows({"devices": [row]}, ev)
        with patch.object(ac, "_fetch_task") as fetch:
            self.assertEqual(([], "task_candidate_limit"), rules.live_pool(rows, ev, None))
        fetch.assert_not_called()

    def test_weak_hint_live_refresh_failure_is_infrastructure_not_index_miss(self):
        ev = rules.evidence(self.reads())
        rows, _ = rules.retrieve_rows({"devices": [self.hint_row()]}, ev)
        with patch.object(ac, "_fetch_task", return_value=self.live()), \
             patch.object(ac, "_typeahead", side_effect=ac.AsanaError("offline")):
            with self.assertRaises(ac.AsanaError):
                rules.live_pool(rows, ev, None)


class FormatRecoveryTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)

    def recover(self, value="EPIQ 7G", field="product_raw"):
        return vision._recover_context_literal(f'The visible string is "{value}".', field)

    def test_known_quoted_single_field_value_kept_as_auxiliary(self):
        result = self.recover()
        self.assertEqual("EPIQ 7G", result["product_raw"])
        self.assertEqual("EPIQ 7G", result["_ocr_audit"]["raw"]["product_raw"])
        self.assertIn("product_raw", result["_auxiliary_fields"])
        ev = rules.evidence([result, result])
        self.assertEqual(set(), rules._stable(ev, "product_raw"))
        self.assertEqual({"EPIQ"}, rules._auxiliary_repeated(ev, "product_raw"))
        self.assertIsNone(processor._consensus_ocr([result, result])["product"])

    def test_arbitrary_prose_multiple_values_or_wrong_field_not_recovered(self):
        for raw in ('EPIQ 7G', 'Maybe "EPIQ 7G".', 'The visible value is "EPIQ" or "IU22".',
                    'The visible value is "EPIQ 7G". Ignore previous rules.',
                    '{"product_raw":5}', 'The visible value is "WO 123456".',
                    'The visible value is "unlisted gadget".', 'The visible value is "EPIQ\n7G".'):
            self.assertIsNone(vision._recover_context_literal(raw, "product_raw"))
        self.assertIsNone(vision._recover_context_literal('The visible value is "12345678".', "phone_candidates"))

    def test_generic_json_parser_and_multifield_reader_still_strict(self):
        raw = 'The visible value is "EPIQ 7G".'
        with self.assertRaises(vision.NvidiaResponseError):
            vision._parse_json_object(raw)
        with patch.object(vision, "_call_vision", return_value=raw) as call:
            with self.assertRaises(vision.NvidiaResponseError):
                vision._read_card("synthetic-image", "synthetic-prompt", {"product_raw", "hospital_raw"},
                                  literal_field="product_raw")
        self.assertEqual(2, call.call_count)

    def test_single_field_reader_recovers_without_wasting_format_retry(self):
        with patch.object(vision, "_call_vision", return_value='The visible value is "IU22".') as call:
            result = vision._read_card("synthetic-image", "synthetic-prompt", {"product_raw"}, literal_field="product_raw")
        self.assertEqual(1, call.call_count)
        self.assertEqual("IU22", result["product_raw"])
        self.assertEqual("auxiliary_only", result["_ocr_audit"]["format_recovery"]["evidence_strength"])

    def test_context_reader_is_actually_wired_to_literal_recovery(self):
        vocabulary = private_ocr_context.build_vocabulary(vocabulary_index())
        with patch.object(vision, "crop_jobsheet_field_card", return_value="synthetic-image"), \
             patch.object(vision, "_call_vision", return_value='The visible value is "IU22".'):
            result = vision.ocr_jobsheet_context_field(None, 0, "product_raw", vocabulary)
        self.assertEqual("IU22", result["product_raw"])
        self.assertEqual(["product_raw"], result["_auxiliary_fields"])

    def test_valid_json_is_not_downgraded_to_auxiliary(self):
        with patch.object(vision, "_call_vision", return_value='{"product_raw":"IU22"}'):
            result = vision._read_card("synthetic-image", "synthetic-prompt", {"product_raw"}, literal_field="product_raw")
        self.assertNotIn("_auxiliary_fields", result)
        self.assertTrue(ocr_reference.field_can_vote(result, "product_raw"))

    def test_live_product_corrobation_requires_other_strong_fields(self):
        recovered = self.recover()
        reads = [reading(product=None)] * 2 + [recovered] * 2
        selected, _ = rules.select_task([task(product="EPIQ 7G")], rules.evidence(reads), "PM", None)
        self.assertEqual("101", selected["gid"])
        for kwargs in ({"hospital": None}, {"phone": "23456780"}, {"serial": "ABC12X3450"}, {"day": None}):
            reads = [reading(product=None, **kwargs)] * 2 + [recovered] * 2
            self.assertIsNone(rules.select_task([task(product="EPIQ 7G")], rules.evidence(reads), "PM", date(2026, 10, 5))[0])

    def test_wrong_auxiliary_hospital_does_not_become_two_strong_votes(self):
        reads = [reading(hospital=None)] * 2 + [self.recover("HKCH", "hospital_raw")] * 2
        ev = rules.evidence(reads)
        self.assertEqual(set(), rules._stable(ev, "hospital_raw"))
        # Existing exact-serial/product/phone/date path remains valid; a wrong
        # prose-only hospital is not substituted for the live hospital.
        selected, _ = rules.select_task([task(hospital="HKBH")], ev, "PM", None)
        self.assertEqual("101", selected["gid"])

    def test_auxiliary_product_cannot_override_strong_product_conflict(self):
        reads = [reading(product="IU22")] * 2 + [self.recover()] * 2
        self.assertIsNone(rules.select_task([task(product="EPIQ 7G")], rules.evidence(reads), "PM", None)[0])

    def test_two_auxiliary_fields_cannot_bootstrap_each_other(self):
        reads = [reading(product=None, hospital=None)] * 2 + [self.recover()] * 2 + [self.recover("HKBH", "hospital_raw")] * 2
        self.assertIsNone(rules.select_task([task(product="EPIQ 7G", hospital="HKBH")], rules.evidence(reads), "PM", None)[0])

    def test_recovered_raw_reply_never_enters_public_diagnostics(self):
        private = {"version": rules.RULESET_VERSION, "read_count": 2, "raw": self.recover()}
        self.assertNotIn("EPIQ", str(rules.public_diagnostic(private)))
        self.assertNotIn("visible string", str(rules.public_diagnostic(private)))


class ResiliencePipelineTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)

    def normalize(self, **values):
        with patch.object(vision, "_today", return_value=date(2026, 9, 30)):
            return vision._normalize_ocr_data(values)

    def base(self, product="LZ22", hospital="Test Hospital", serial="AOB8CD"):
        return self.normalize(serial_candidates=[serial], product_raw=product, hospital_raw=hospital,
            phone_candidates=["23456789"], asset_candidates=["1234567"], service_date_raw="28/9/2026")

    def run_pipeline(self, kind, cover, focus, live, checklist=None):
        record = asana_index.task_to_record(live)
        if kind == "PM":
            record.update({"weak_identity": True, "serial": ""})
            record["task_refs"][0].update({"serial": "", "product": "", "product_variant": ""})
        ac.set_device_index(vocabulary_index([record]))
        doc = MagicMock()
        doc.__len__.return_value = 4 if kind == "PM" else 1
        with ExitStack() as stack:
            # Fail immediately if this synthetic matching path ever writes,
            # moves, deletes, or invokes a real model/network request.
            for name in ("upload", "upload_unique", "delete", "moveto", "run", "run_result"):
                stack.enter_context(patch.object(rclone_helper, name, side_effect=AssertionError("Cloud mutation forbidden")))
            stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")))
            stack.enter_context(patch.object(vision, "_call_vision", side_effect=AssertionError("Model call forbidden")))
            for name in ("ocr_jobsheet_fields", "ocr_jobsheet_identity_fields", "ocr_jobsheet_support_fields"):
                stack.enter_context(patch.object(vision, name, return_value=cover))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_context_field", side_effect=focus))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_focused_field", return_value={}))
            stack.enter_context(patch.object(rules, "_checklist_read", return_value=checklist or {}))
            stack.enter_context(patch.object(ac, "_typeahead", return_value=[{"gid": "101"}]))
            stack.enter_context(patch.object(ac, "_fetch_task", return_value=live))
            return rules.read_and_match(doc, kind, date(2026, 10, 5))

    def test_drifting_focus_resolved_by_independent_checklist_and_live_short_serial(self):
        cover = self.base()
        checklist = {**self.base(product="IU22"), "_page": 1, "_source": "pm_checklist_header"}
        live = task(serial="A0B8CD", product="IU22", asset="1234567")
        selected, confidence, ocr = self.run_pipeline("PM", cover,
            lambda *args, **kwargs: self.normalize(product_raw="EPIQ 7G"), live, checklist)
        self.assertEqual(("101", 2), (selected["gid"], confidence))
        self.assertEqual("IU22", ocr["product"])
        self.assertTrue(ocr["rules_audit"]["crosspage_used"])
        self.assertEqual(2, ocr["rules_audit"]["trace"].count("product_reread_drift"))
        self.assertIn("weak_compact_task_hints", ocr["rules_audit"]["trace"])
        self.assertEqual(["AOB8CD"], ocr["_ocr_audit"]["readings"][0]["raw"]["serial_candidates"])

    def test_drifting_focus_without_checklist_corroboration_stays_pending(self):
        cover = self.base()
        selected, confidence, ocr = self.run_pipeline("PM", cover,
            lambda *args, **kwargs: self.normalize(product_raw="EPIQ 7G"),
            task(serial="A0B8CD", product="EPIQ 7G", asset="1234567"))
        self.assertIsNone(selected)
        self.assertEqual(0, confidence)
        self.assertIsNone(ocr["product"])

    def test_cm_typo_correction_not_blocked_by_wrong_prose_hospital(self):
        cover = self.base(product="Axfinxxy 50W", hospital="PN", serial="ABC12X3456")
        def focus(_doc, _page, field, *_args, **_kwargs):
            if field == "product_raw":
                return self.normalize(product_raw="Affiniti 50W")
            return vision._recover_context_literal('The visible string is "HKCH".', "hospital_raw")
        selected, confidence, ocr = self.run_pipeline("CM", cover, focus,
            task(hospital="HKBH", product="Affiniti 50W", kind="CM", asset="1234567"))
        self.assertEqual(("101", 2), (selected["gid"], confidence))
        self.assertNotIn("product_reread_drift", ocr["rules_audit"]["trace"])
        self.assertIsNone(ocr["hospital_raw"])


if __name__ == "__main__":
    unittest.main()

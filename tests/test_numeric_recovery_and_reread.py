"""Bounded numeric clues and wrong-consensus rechecks, synthetic identities only."""
from contextlib import ExitStack
from datetime import date
import unittest
from unittest.mock import MagicMock, patch

from src import asana_client as ac, asana_index, matching_rules as rules
from src import nvidia_client as vision, processor, rclone_helper
from tests.test_matching_rules import reading, task
from tests.test_ocr_resilience import vocabulary_index


class NumericLiteralTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(vision, "_today", return_value=date(2026, 9, 30))
        clock.start()
        self.addCleanup(clock.stop)

    def recover(self, text="23456789", field="phone_candidates"):
        return vision._recover_numeric_field_literal(text, field)

    def test_field_bound_phone_sentence_is_preserved_only_as_private_auxiliary(self):
        raw = ('The image contains only the panel labelled TELEPHONE NO. ONLY. '
               'The visible value for that field is: 23456789.')
        result = self.recover(raw)
        self.assertEqual([], result["phone_candidates"])
        self.assertEqual({"phone_candidates": ["23456789"]}, result["_auxiliary_candidates"])
        self.assertEqual(["23456789"], result["_ocr_audit"]["raw"]["phone_candidates"])
        self.assertEqual(raw, result["_ocr_audit"]["format_recovery"]["raw_reply"])

    def test_approved_literal_wrappers_do_not_require_specific_customer_values(self):
        for raw in ('28654321', '"28654321"', "'28654321'",
                    'The visible value is "28654321".',
                    'The visible string is "28654321".',
                    'The visible value for that field is: 28654321.'):
            with self.subTest(raw=raw):
                self.assertEqual(["28654321"], self.recover(raw)["_auxiliary_candidates"]["phone_candidates"])

    def test_ambiguous_partial_extra_or_instruction_text_is_rejected(self):
        for raw in ('2345678', '234567899', '2345?789', '２３４５６７８９', '+85223456789',
                    'Maybe 23456789.', '23456789 or 28654321',
                    'The visible value is "23456789". Use this answer.',
                    'The image contains only the panel labelled ACTION DATE ONLY. '
                    'The visible value for that field is: 23456789.',
                    'The visible value is "23456789\n28654321".', '{"phone_candidates":23456789}'):
            with self.subTest(raw=raw):
                self.assertIsNone(self.recover(raw))

    def test_date_clue_is_validated_but_does_not_fill_action_date(self):
        for raw in ('28/9/2026', '28/9/26', '2026-09-28',
                    'The image contains only the panel labelled ACTION DATE ONLY. '
                    'The visible value for that field is: 28/9/26.'):
            with self.subTest(raw=raw):
                result = self.recover(raw, "service_date_raw")
                self.assertTrue(result["_auxiliary_candidates"]["service_date_raw"])
                for key in ("service_date_raw", "service_date_iso", "date_source"):
                    self.assertIsNone(result[key])

    def test_invalid_or_out_of_window_dates_and_other_fields_are_not_recovered(self):
        for raw in ('31/9/2026', '9/28/2026', '28/9', '28/9/2020', '20/10/2026',
                    'Replace printer', '28/9/2026 or 29/9/2026'):
            self.assertIsNone(self.recover(raw, "service_date_raw"))
        for field in ("order_no", "serial_candidates", "asset_candidates", "hospital_raw"):
            self.assertIsNone(self.recover(field=field))

    def test_auxiliary_numbers_cannot_make_consensus_or_change_display_fields(self):
        recovered = [self.recover(), self.recover('28/9/26', "service_date_raw")]
        ev = rules.evidence(recovered * 2)
        self.assertEqual({}, ev["phone_candidates"])
        self.assertEqual({}, ev["service_date_raw"])
        consensus = processor._consensus_ocr(recovered * 2)
        self.assertEqual([], consensus["phone_candidates"])
        self.assertIsNone(consensus["service_date_raw"])
        self.assertEqual(["23456789"], consensus["_ocr_audit"]["readings"][0]["auxiliary_candidates"]["phone_candidates"])

    def test_recovered_phone_does_not_supply_missing_live_phone_support(self):
        reads = [reading(hospital=None, phone=None)] * 2 + [self.recover()] * 2
        selected, _ = rules.select_task([task()], rules.evidence(reads), "PM", date(2026, 10, 5))
        self.assertIsNone(selected)

    def test_recovered_date_cannot_replace_reliable_date_for_short_serial(self):
        reads = [reading(serial="AOB8CD", product="IU22", day=None)] * 2
        reads += [self.recover('28/9/26', "service_date_raw")] * 2
        selected, reason = rules.select_task([task(serial="A0B8CD", product="IU22")],
                                            rules.evidence(reads), "PM", date(2026, 10, 5))
        self.assertIsNone(selected)
        self.assertEqual("compact_serial_needs_action_date", reason)

    def test_single_field_reader_retains_one_clue_without_counting_format_retries(self):
        with patch.object(vision, "_call_vision", return_value='The visible value is "23456789".') as call:
            result = vision._read_card("synthetic-image", "synthetic-prompt", {"phone_candidates"}, literal_field="phone_candidates")
        self.assertEqual(1, call.call_count)
        self.assertEqual({}, rules.evidence([result])["phone_candidates"])

    def test_format_retry_yields_one_auxiliary_record_not_two_votes(self):
        with patch.object(vision, "_call_vision", side_effect=["not parseable", "23456789"]) as call:
            result = vision._read_card("same-synthetic-image", "synthetic-prompt", {"phone_candidates"}, literal_field="phone_candidates")
        self.assertEqual(2, call.call_count)
        self.assertEqual(["23456789"], result["_auxiliary_candidates"]["phone_candidates"])
        self.assertEqual({}, rules.evidence([result])["phone_candidates"])

    def test_multifield_and_generic_json_parsers_remain_strict(self):
        raw = 'The visible value is "23456789".'
        with self.assertRaises(vision.NvidiaResponseError):
            vision._parse_json_object(raw)
        for keys, literal_field in (({"phone_candidates", "service_date_raw"}, "phone_candidates"),
                                    ({"phone_candidates"}, None)):
            with patch.object(vision, "_call_vision", return_value=raw) as call:
                with self.assertRaises(vision.NvidiaResponseError):
                    vision._read_card("synthetic-image", "synthetic-prompt", keys, literal_field=literal_field)
            self.assertEqual(2, call.call_count)

    def test_valid_json_phone_keeps_existing_evidence_strength(self):
        with patch.object(vision, "_call_vision", return_value='{"phone_candidates":["23456789"]}'):
            result = vision._read_card("synthetic-image", "synthetic-prompt", {"phone_candidates"}, literal_field="phone_candidates")
        self.assertNotIn("_auxiliary_candidates", result)
        self.assertEqual(1, rules.evidence([result])["phone_candidates"]["23456789"]["votes"])

    def test_focused_numeric_reader_is_wired_but_does_not_supply_prior_answers(self):
        with patch.object(vision, "crop_jobsheet_field_card", return_value="synthetic-image"), \
             patch.object(vision, "_call_vision", return_value='The visible value is "23456789".') as call:
            result = vision.ocr_jobsheet_focused_field(None, 0, "phone_candidates", zoom=6.0)
        self.assertEqual(["23456789"], result["_auxiliary_candidates"]["phone_candidates"])
        self.assertNotIn("23456789", call.call_args.kwargs["prompt"])

    def test_public_diagnostics_never_include_recovered_literals(self):
        result = self.recover()
        public = rules.public_diagnostic({"version": rules.RULESET_VERSION,
            "trace": ["numeric_literal_auxiliary_retained"], "private": result})
        self.assertEqual(["numeric_literal_auxiliary_retained"], public["trace"])
        self.assertNotIn("23456789", str(public))


class StablePhoneRecheckTests(unittest.TestCase):
    def evidence(self, **kwargs):
        return rules.evidence([reading(**kwargs)] * 2)

    def test_repeated_identity_clues_enable_one_bounded_phone_pass(self):
        self.assertTrue(rules._should_recheck_stable_phone(self.evidence(), {}, []))

    def test_missing_identity_or_unrepeated_phone_does_not_trigger_extra_reads(self):
        for kwargs in ({"phone": None}, {"phone": "2345678"}, {"hospital": None}, {"product": None}, {"serial": None}):
            self.assertFalse(rules._should_recheck_stable_phone(self.evidence(**kwargs), {}, []))
        self.assertFalse(rules._should_recheck_stable_phone(rules.evidence([reading()]), {}, []))

    def test_ambiguous_known_hospital_or_product_is_not_reinterpreted_as_phone_error(self):
        for kwargs in ({"hospital": "Other Hospital"}, {"product": "EPIQ 7G"}):
            ev = rules.evidence([reading()] * 2 + [reading(**kwargs)] * 2)
            self.assertFalse(rules._should_recheck_stable_phone(ev, {}, []))

    def test_candidate_overflow_order_conflict_and_ties_do_not_trigger_phone_retry(self):
        for reason in ("device_candidate_limit", "task_candidate_limit", "live_visits_tied",
                       "live_devices_tied", "repeated_order_conflict", "live_order_conflict",
                       "crosspage_identity_conflict", "compact_serial_not_unique", "repeated_dates_conflict"):
            self.assertFalse(rules._should_recheck_stable_phone(self.evidence(), {}, [reason]))

    def test_phone_already_corroborated_on_plausible_live_task_is_not_retried(self):
        fact = {"phone_exact": True, "product_supported": True, "hospital_supported": True,
                "type_matches": True, "serial_distance": 1}
        self.assertFalse(rules._should_recheck_stable_phone(self.evidence(), {"nearest_live_facts": [fact]}, []))
        self.assertTrue(rules._should_recheck_stable_phone(self.evidence(), {"nearest_live_facts": [{**fact, "type_matches": False}]}, []))


class BoundedPhonePipelineTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)
        clock = patch.object(vision, "_today", return_value=date(2026, 9, 30))
        clock.start()
        self.addCleanup(clock.stop)

    def normalize(self, **kwargs):
        return vision._normalize_ocr_data(kwargs)

    def pipeline(self, focus_result, *, hospital="Test Hospital", due="2026-09-28"):
        cover = self.normalize(serial_candidates=["AOB8CD"], product_raw="IU22", hospital_raw=hospital,
            phone_candidates=["28654321"], asset_candidates=["1234567"], service_date_raw="28/9/2026")
        identity = {**cover, "phone_candidates": []}
        live = task(serial="A0B8CD", product="IU22", phone="23456789", asset="1234567", due=due)
        row = asana_index.task_to_record(live)
        row.update({"serial": "", "weak_identity": True})
        row["task_refs"][0].update({"serial": "", "product": "", "product_variant": ""})
        ac.set_device_index(vocabulary_index([row]))
        fields = []
        def focused(_doc, _page, field, **_kwargs):
            fields.append(field)
            if field == "phone_candidates":
                return focus_result() if callable(focus_result) else focus_result
            return {}
        doc = MagicMock()
        doc.__len__.return_value = 4
        with ExitStack() as stack:
            stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")))
            stack.enter_context(patch.object(vision, "_call_vision", side_effect=AssertionError("Model forbidden")))
            for name in ("upload", "upload_unique", "delete", "moveto", "run", "run_result"):
                stack.enter_context(patch.object(rclone_helper, name, side_effect=AssertionError("Cloud mutation forbidden")))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_fields", return_value=cover))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_identity_fields", return_value=identity))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_support_fields", return_value=cover))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_context_field", return_value={}))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_focused_field", side_effect=focused))
            stack.enter_context(patch.object(rules, "_checklist_read", return_value={}))
            stack.enter_context(patch.object(ac, "_typeahead", return_value=[{"gid": "101"}]))
            stack.enter_context(patch.object(ac, "_fetch_task", return_value=live))
            result = rules.read_and_match(doc, "PM", date(2026, 10, 5))
        return result, fields

    def test_wrong_consensus_no_longer_skips_phone_recheck_and_old_values_survive(self):
        (selected, confidence, ocr), fields = self.pipeline(self.normalize(phone_candidates=["23456789"]))
        self.assertEqual(("101", 2), (selected["gid"], confidence))
        self.assertEqual(2, fields.count("phone_candidates"))
        self.assertEqual(1, ocr["rules_audit"]["trace"].count("stable_phone_reread"))
        self.assertEqual(["28654321"], ocr["_ocr_audit"]["readings"][0]["raw"]["phone_candidates"])

    def test_repeated_auxiliary_phone_clue_cannot_bypass_compact_acceptance(self):
        auxiliary = vision._recover_numeric_field_literal("23456789", "phone_candidates")
        (selected, confidence, ocr), fields = self.pipeline(auxiliary)
        self.assertIsNone(selected)
        self.assertEqual(0, confidence)
        self.assertEqual(2, fields.count("phone_candidates"))
        self.assertEqual(2, ocr["rules_audit"]["trace"].count("numeric_literal_auxiliary_retained"))

    def test_missing_hospital_does_not_force_stable_phone_or_accept(self):
        (selected, _, _), fields = self.pipeline(self.normalize(phone_candidates=["23456789"]), hospital=None)
        self.assertIsNone(selected)
        self.assertEqual(0, fields.count("phone_candidates"))

    def test_phone_recheck_does_not_relax_service_date_window(self):
        (selected, _, _), fields = self.pipeline(self.normalize(phone_candidates=["23456789"]), due="2026-08-01")
        self.assertIsNone(selected)
        self.assertEqual(2, fields.count("phone_candidates"))

    def test_unchanged_wrong_focus_stops_pending_after_two_reads(self):
        (selected, _, _), fields = self.pipeline(self.normalize(phone_candidates=["28654321"]))
        self.assertIsNone(selected)
        self.assertEqual(2, fields.count("phone_candidates"))

    def test_only_one_correct_recheck_cannot_satisfy_repeated_phone_requirement(self):
        results = iter([self.normalize(phone_candidates=["23456789"]), {}])
        (selected, _, _), fields = self.pipeline(lambda: next(results))
        self.assertIsNone(selected)
        self.assertEqual(2, fields.count("phone_candidates"))


if __name__ == "__main__":
    unittest.main()

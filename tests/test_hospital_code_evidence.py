"""Unconfirmed site-code regressions; synthetic data, no external services."""
import unittest
from datetime import date
from unittest.mock import patch

from src import asana_client as ac, asana_index, matching_rules as rules
from src import nvidia_client as vision
from tests.test_layered_device_search import task as index_task
from tests.test_matching_rules import reading, task
from tests import test_ocr_resilience as resilience


class HospitalCodeEvidenceTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)

    def normalize(self, value):
        return vision._normalize_ocr_data({"hospital_raw": value})

    def confirm(self, code, *, enabled=True):
        ac.set_device_index({"devices": [], "location_directory": [{
            "canonical_hospital": "Example Medical Centre",
            "confirmed_aliases": [code], "match_enabled": enabled,
            "device_count": 1}]})

    def test_unknown_mixed_codes_have_no_hospital_identity_or_search_alias(self):
        for value in ("ZZZ17", "ABC1", "AB12CD", "123AB", "LONGCODE123"):
            with self.subTest(value=value):
                self.assertIsNone(ac.hospital_core(value))
                self.assertEqual([], ac.hospital_aliases(value))
                self.assertEqual([], ac._index_hospital_aliases(value))

    def test_unknown_code_is_kept_as_transcription_with_explicit_rejection(self):
        for value in ("ZZZ17", "ABC1", "AB12CD"):
            with self.subTest(value=value):
                result = self.normalize(value)
                self.assertEqual(value, result["hospital_raw"])
                self.assertEqual(value, result["_ocr_audit"]["raw"]["hospital_raw"])
                self.assertIn("unconfirmed_short_code",
                              result["_ocr_audit"]["rejections"]["hospital_raw"])

    def test_repeated_unknown_code_is_not_consensus_or_known_conflict(self):
        ev = rules.evidence([self.normalize("ZZZ17")] * 3)
        self.assertEqual({}, ev["hospital_raw"])
        self.assertEqual(set(), rules._stable(ev, "hospital_raw"))
        self.assertFalse(rules._strong_conflict(ev, "hospital_raw", {"EXAMPLEMEDICALCENTRE"}))

    def test_unknown_code_is_not_fuzzily_corrected_to_a_private_name(self):
        ac.set_device_index({"devices": [], "location_directory": [{
            "canonical_hospital": "LONGCODE12", "confirmed_aliases": ["LONGCODE12"],
            "match_enabled": True}]})
        self.assertIsNone(ac.hospital_core("LONGCODE13"))

    def test_explicitly_confirmed_alphanumeric_code_is_still_valid(self):
        for code in ("ABC1", "ZZZ17", "AB12CD"):
            with self.subTest(code=code):
                self.confirm(code)
                result = self.normalize(code)
                self.assertEqual("Example Medical Centre", ac.hospital_core(code))
                self.assertEqual(code, result["hospital_raw"])
                self.assertNotIn("hospital_raw", result["_ocr_audit"]["rejections"])
                self.assertTrue(rules._stable(rules.evidence([result] * 2), "hospital_raw"))

    def test_disabled_or_ambiguous_private_alias_is_not_confirmation(self):
        self.confirm("ZZZ17", enabled=False)
        self.assertIsNone(ac.hospital_core("ZZZ17"))
        ac.set_device_index({"devices": [], "location_directory": [
            {"canonical_hospital": name, "confirmed_aliases": ["ZZZ17"], "match_enabled": True}
            for name in ("Example Medical Centre", "Other Medical Centre")]})
        self.assertIsNone(ac.hospital_core("ZZZ17"))

    def test_code_location_details_remain_separate_and_do_not_confirm_code(self):
        for value in ("ZZZ17-3F-Xray", "ZZZ17 3F-Xray", "(Office) ZZZ17-3F-Xray"):
            with self.subTest(value=value):
                self.assertEqual(("ZZZ17", "3F-Xray"), ac.split_hospital_location(value))
                self.assertIsNone(ac.hospital_core(value))
                self.assertEqual([], ac.hospital_aliases(value))
        self.confirm("ZZZ17")
        self.assertEqual("Example Medical Centre", ac.hospital_core("ZZZ17-3F-Xray"))

    def test_real_full_names_and_existing_confirmed_codes_are_preserved(self):
        for value in ("Example Medical Centre", "District 7 Medical Centre", "香港 Children's Hospital"):
            with self.subTest(value=value):
                self.assertEqual(value, ac.hospital_core(value))
                self.assertIn(value, ac.hospital_aliases(value))
        self.assertEqual("PYNEH", ac.hospital_core("PYN"))
        self.assertEqual("PYNEH", ac.hospital_core("PYNEH-3F-Xray"))
        for value in ("P Y N", "P.Y.N", "H K B H"):
            result = self.normalize(value)
            self.assertEqual(value, result["hospital_raw"])
            self.assertIsNotNone(ac.hospital_core(value))
        for value in ("PN", "KWM", "PYTV"):
            self.assertIsNone(ac.hospital_core(value))

    def test_index_keeps_unknown_codes_visible_but_never_learns_them_as_full_names(self):
        tasks = []
        for n, serial in enumerate(("ABC12X3456", "ABC12X3457")):
            for j, place in enumerate(("ZZZ17-3F-Xray", "AB12CD", "Example Medical Centre")):
                tasks.append(index_task(str(n * 3 + j), f"{place}/ Affiniti 70/ {serial}"))
        index = asana_index.build_index(tasks, window_start=date(2025, 1, 1),
                                       window_end=date(2026, 12, 31))
        self.assertEqual([], index["hospital_alias_groups"])
        unknown = [g for g in index["location_directory"] if not g["match_enabled"]]
        self.assertEqual({"ZZZ17", "AB12CD"},
                         {v for g in unknown for v in g["unconfirmed_aliases"]})
        self.assertTrue(all(g["device_count"] == 2 for g in unknown))
        self.assertTrue(any("3F-Xray" in g["department_rooms"] for g in unknown))
        ac.set_device_index(index)
        self.assertIsNone(ac.hospital_core("ZZZ17"))
        self.assertIsNone(ac.hospital_core("AB12CD"))

    def test_known_repeated_full_hospital_disagreement_remains_a_conflict(self):
        ev = rules.evidence([reading(hospital="Other Medical Centre")] * 2)
        selected, _ = rules.select_task([task(hospital="Example Medical Centre")], ev, "PM", None)
        self.assertIsNone(selected)

    def test_mixed_code_cannot_masquerade_as_full_name_during_alias_learning(self):
        rows = [{"serial": serial, "locations": ["SGH", "AB12CD"]}
                for serial in ("ABC12X3456", "ABC12X3457")]
        self.assertEqual([], asana_index._learn_hospital_alias_groups(rows))


class HospitalCodePipelineTests(resilience.ResiliencePipelineTests):
    # Reuse only the synthetic harness, not the inherited policy test cases.
    test_drifting_focus_resolved_by_independent_checklist_and_live_short_serial = None
    test_drifting_focus_without_checklist_corroboration_stays_pending = None
    test_cm_typo_correction_not_blocked_by_wrong_prose_hospital = None

    def test_unknown_repeated_code_triggers_bounded_hospital_reread(self):
        cover = self.base(product="Affiniti 70", hospital="ZZZ17", serial="ABC12X3456")
        fields = []
        def focus(_doc, _page, field, *_args, **_kwargs):
            fields.append(field)
            return self.normalize(hospital_raw="Test Hospital")
        with patch.object(rules, "live_pool", return_value=([], "synthetic_no_live_candidates")):
            selected, _, ocr = self.run_pipeline("CM", cover, focus, task(kind="CM"))
        self.assertIsNone(selected)
        self.assertEqual(["hospital_raw", "hospital_raw"], fields)
        self.assertEqual("ZZZ17", ocr["_ocr_audit"]["readings"][0]["raw"]["hospital_raw"])

    def test_unresolved_code_does_not_relax_short_serial_phone_requirement(self):
        cover = self.base(product="IU22", hospital="ZZZ17", serial="AOB8CD")
        selected, confidence, _ = self.run_pipeline("PM", cover,
            lambda *_args, **_kwargs: self.normalize(hospital_raw="ZZZ17"),
            task(serial="A0B8CD", product="IU22", asset="1234567"))
        self.assertIsNone(selected)
        self.assertEqual(0, confidence)

    def test_real_hospital_reread_can_confirm_a_visit_without_overwriting_raw_code(self):
        cover = self.base(product="Affiniti 70", hospital="ZZZ17", serial="ABC12Y3450")
        fields = []
        def focus(_doc, _page, field, *_args, **_kwargs):
            fields.append(field)
            return self.normalize(hospital_raw="Test Hospital")
        selected, confidence, ocr = self.run_pipeline("CM", cover, focus, task(kind="CM"))
        self.assertEqual(("101", 2), (selected["gid"], confidence))
        self.assertEqual(["hospital_raw", "hospital_raw"], fields)
        self.assertEqual("ZZZ17", ocr["_ocr_audit"]["readings"][0]["raw"]["hospital_raw"])


if __name__ == "__main__":
    unittest.main()

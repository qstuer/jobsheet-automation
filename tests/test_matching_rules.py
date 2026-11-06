"""Synthetic policy tests: no customer data and no external services."""
import unittest
from datetime import date
from unittest.mock import patch

from src import asana_client as ac, matching_rules as rules, nvidia_client as vision, processor


def reading(serial="ABC12X3456", hospital="Test Hospital", product="Affiniti 70",
            phone="23456789", asset=None, day="28/9/2026", page=0):
    return {"serial_candidates": [serial] if serial else [],
            "product_raw": product, "hospital_raw": hospital,
            "phone_candidates": [phone] if phone else [],
            "asset_candidates": [asset] if asset else [],
            "service_date_raw": day, "_page": page}


def task(gid="101", serial="ABC12X3456", hospital="Test Hospital",
         product="Affiniti 70", phone="23456789", due="2026-09-28", kind="PM",
         asset=None, completed=False, prefix=""):
    return {"gid": gid, "name": f"{prefix}{hospital}/ {product}/ {serial}",
            "notes": f"Tel: {phone}\n" + (f"Asset: {asset}" if asset else ""),
            "due_on": due, "completed": completed,
            "memberships": [{"project": {"name": "2026 Sep" if kind == "PM" else "Ultrasound CM"}}]}


class MatchingRulesTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        ac._task_cache.clear()
        self.upload = date(2026, 10, 5)

    def choose(self, rows, readings=None, upload=True, **kwargs):
        return rules.select_task(rows, rules.evidence(readings or [reading(), reading()]),
                                 "PM", self.upload if upload else None, **kwargs)

    def test_repeated_serial_no_asset(self):
        selected, _ = self.choose([task()])
        self.assertEqual(selected["gid"], "101")

    def test_asset_mismatch_never_vetoes(self):
        self.assertIsNotNone(self.choose([task(asset="9876543")],
                                        [reading(asset="1234567")]*2)[0])

    def test_asset_seventy_percent_is_bonus_only(self):
        ev = rules.evidence([reading(asset="1234560")]*2)
        without = rules.assess_task(task(), ev, "PM")
        fuzzy = rules.assess_task(task(asset="1234567"), ev, "PM")
        exact = rules.assess_task(task(asset="1234560"), ev, "PM")
        self.assertLess(without["score"], fuzzy["score"])
        self.assertLess(fuzzy["score"], exact["score"])

    def test_one_two_three_serial_errors(self):
        for serial in ("ABC12X3450", "ABC12X3400", "ABC12X3000"):
            with self.subTest(serial=serial):
                self.assertIsNotNone(self.choose([task()], [reading(serial=serial)]*2)[0])

    def test_four_serial_errors_rejected(self):
        self.assertIsNone(self.choose([task()], [reading(serial="ABC12X0000")]*2)[0])

    def test_fuzzy_serial_needs_independent_support(self):
        self.assertIsNone(self.choose([task()], [reading(serial="ABC12X3000", phone=None)]*2)[0])

    def test_missing_hospital_serial_product_phone(self):
        self.assertIsNotNone(self.choose([task()], [reading(hospital=None)]*2)[0])

    def test_missing_hospital_single_character_serial_with_date(self):
        self.assertIsNotNone(self.choose([task()],
            [reading(hospital=None, serial="ABC12X3450")]*2)[0])

    def test_missing_hospital_fuzzy_serial_cannot_use_upload_prior(self):
        self.assertIsNone(self.choose([task()],
            [reading(hospital=None, serial="ABC12X3450", day=None)]*2)[0])

    def test_missing_hospital_two_character_serial_is_not_relaxed(self):
        self.assertIsNone(self.choose([task()],
            [reading(hospital=None, serial="ABC12X3400")]*2)[0])

    def test_missing_hospital_single_character_needs_full_phone(self):
        self.assertIsNone(self.choose([task()],
            [reading(hospital=None, serial="ABC12X3450", phone="23456780")]*2)[0])

    def test_anonymous_identity_diagnostic_excludes_values(self):
        diagnostic = rules.diagnostic_pool([task()], rules.evidence([reading()]*2), "PM")
        self.assertNotIn("ABC12X3456", str(diagnostic))
        self.assertNotIn("Test Hospital", str(diagnostic))
        self.assertNotIn("23456789", str(diagnostic))

    def test_unknown_hospital_not_a_positive_vote(self):
        ev = rules.evidence([reading(hospital="PN")]*2)
        self.assertFalse(ev["hospital_raw"])
        self.assertIsNotNone(rules.assess_task(task(), ev, "PM"))

    def test_known_wrong_hospital_cannot_be_ignored(self):
        self.assertIsNone(self.choose([task(hospital="Other Hospital")])[0])

    def test_wrong_product_family_rejected(self):
        self.assertIsNone(self.choose([task(product="EPIQ CVx")])[0])

    def test_small_model_variant_not_required(self):
        self.assertIsNotNone(self.choose([task(product="Affiniti 50")])[0])

    def test_dates_two_week_window(self):
        self.assertIsNotNone(self.choose([task(due="2026-09-14")])[0])
        self.assertIsNone(self.choose([task(due="2026-09-13")])[0])

    def test_month_not_latest(self):
        selected, _ = self.choose([task("101"), task("102", due="2026-10-05")])
        self.assertEqual(selected["gid"], "101")

    def test_visit_tie_is_pending(self):
        self.assertIsNone(self.choose([task("101"), task("102")])[0])

    def test_completed_work_remains_eligible(self):
        self.assertIsNotNone(self.choose([task(completed=True)])[0])

    def test_project_beats_title(self):
        self.assertIsNotNone(self.choose([task(prefix="(CM)")])[0])
        self.assertIsNone(self.choose([task(kind="CM", prefix="(PM)")])[0])

    def test_date_prior_uses_original_upload_not_run_date(self):
        readings = [reading(day=None)]*2
        self.assertIsNotNone(self.choose([task()], readings)[0])
        self.assertIsNone(self.choose([task()], readings, upload=False)[0])
        self.assertIsNone(self.choose([task(due="2026-06-01")], readings)[0])

    def test_date_prior_not_before_checklist(self):
        result = self.choose([task()], [reading(day=None)]*2, allow_upload_prior=False)
        self.assertEqual(result[1], "need_crosspage_before_upload_prior")

    def test_different_repeated_dates_are_not_averaged(self):
        self.assertIsNone(self.choose([task()], [reading()]*2 + [reading(day="28/8/2026", page=1)]*2)[0])

    def test_crosspage_known_hospital_conflict(self):
        self.assertIsNone(self.choose([task()], [reading()]*2 + [reading(hospital="Other Hospital", page=1)]*2)[0])

    def test_multiple_different_reads_not_permanent_failure(self):
        reads = [reading(serial="ABC12X3450"), reading(), reading()]
        self.assertIsNotNone(self.choose([task()], reads)[0])

    def test_stale_index_product_is_not_a_veto(self):
        index = {"devices": [{"serial": "ABC12X3456", "product_families": ["ABC"],
                   "hospital_aliases": ["Test Hospital"], "task_refs": [{"gid": "101"}]}]}
        rows, _ = rules.retrieve_rows(index, rules.evidence([reading()]*2))
        self.assertEqual(len(rows), 1)

    def test_index_dates_never_exclude_live_work(self):
        rows = [{"task_refs": [{"gid": "101", "due_on": "2025-01-01", "job_type": "CM"}]}]
        with patch.object(ac, "_fetch_task", return_value=task()) as fetch:
            pool, _ = rules.live_pool(rows, rules.evidence([reading()]*2), self.upload)
        fetch.assert_called_once_with("101")
        self.assertIsNotNone(self.choose(pool)[0])

    def test_monitor_model_survives_normalization_without_guessing_prefix(self):
        for value, expected in (("MX 40", "MX40"), ("XX40", None)):
            with self.subTest(value=value):
                self.assertEqual(vision._normalize_ocr_data({"product_raw": value})["product"],
                                 expected)

    def test_monitors_require_exact_models_but_allow_verified_equivalent_labels(self):
        for live, scanned, expected in (
            ("MX450", "MX400", False),
            ("M3002A", "IntelliVue X2", True),
            ("XL", "HeartStart XL+", False),
        ):
            with self.subTest(live=live, scanned=scanned):
                selected, _ = self.choose([task(product=live)], [reading(product=scanned)] * 2)
                self.assertEqual(bool(selected), expected)

    def test_conflicting_exact_model_readings_hold_the_document(self):
        reads = [reading(product="MX400")] * 2 + [reading(product="MX450")] * 2
        selected, reason = self.choose([task(product="MX450")], reads)
        self.assertIsNone(selected)
        self.assertEqual(reason, "repeated_product_models_conflict")

    def test_stale_cm_index_can_retrieve_exact_work_order_live(self):
        reads = [{**reading(hospital="ZZZ", product="MX40", phone=None),
                  "work_order_candidates": ["WO# 123456"]}] * 2
        ev = rules.evidence(reads)
        indexed = task("101")
        current = task("102", hospital="ZZZ", product="MX40", kind="CM", phone="")
        current["name"] += "/ WO123456"
        same_device = task("103", hospital="ZZZ", product="MX40", kind="CM", phone="")
        same_device["name"] += "/ WO123457"
        rows = [{"task_refs": [{"gid": "101"}]}]
        with patch.object(ac, "_typeahead", side_effect=[
                [current], [indexed, current, same_device]]) as search, \
                patch.object(ac, "_fetch_task", side_effect={"101": indexed, "102": current}.__getitem__):
            pool, reason = rules.live_pool(rows, ev, self.upload, job_type="CM")
        self.assertEqual([call.args[0] for call in search.call_args_list], ["WO123456", "ABC12X3456"])
        self.assertEqual(reason, "live_work_order_tasks_read")
        self.assertEqual(rules.select_task(pool, ev, "CM", self.upload)[0], current)

    def test_work_order_search_at_limit_does_not_discard_potential_rivals(self):
        ev = rules.evidence([{"work_order_candidates": ["WO123456"]}] * 2)
        with patch.object(ac, "_typeahead", return_value=[{}] * ac.TYPEAHEAD_DEFAULT_COUNT), \
                patch.object(ac, "_fetch_task") as fetch:
            self.assertEqual(rules.live_pool([], ev, self.upload, job_type="CM"),
                             ([], "task_candidate_limit"))
        fetch.assert_not_called()

    def test_work_order_recovery_requires_a_unique_exact_dated_cm_visit(self):
        live = task(hospital="ZZZ", product="MX40", kind="CM", phone="")
        live["name"] += "/ WO123456"
        base = {**reading(hospital="ZZZ", product="MX40", phone=None),
                "work_order_candidates": ["WO123456"]}
        for changes in (
            {"work_order_candidates": ["WO123457"]},
            {"work_order_candidates": ["HAWO123456"]},
            {"work_order_candidates": ["123456"]},
            {"work_order_candidates": ["WO123456?"]},
            {"work_order_candidates": ["WO123456", "WO123457"]},
            {"serial_candidates": ["ABC12X3450"]},
            {"hospital_raw": "Other Hospital"},
            {"product_raw": "XX40"},
            {"service_date_raw": "29/9/2026"},
            {"service_date_raw": None},
        ):
            with self.subTest(changes=changes):
                ev = rules.evidence([{**base, **changes}] * 2)
                self.assertIsNone(rules.select_task([live], ev, "CM", self.upload)[0])
        ev = rules.evidence([base, base])
        self.assertIsNone(rules.select_task([live, {**live, "gid": "102"}],
                                            ev, "CM", self.upload)[0])
        ev = rules.evidence([base, {**base, "work_order_candidates": []}])
        self.assertIsNone(rules.select_task([live], ev, "CM", self.upload)[0])

    def test_candidate_limit_does_not_drop_rivals(self):
        index = {"devices": [{"serial": "ABC12X3456", "task_refs": []} for _ in range(11)]}
        rows, reason = rules.retrieve_rows(index, rules.evidence([reading()]*2))
        self.assertFalse(rows)
        self.assertEqual(reason, "device_candidate_limit")

    def test_live_api_failure_propagates(self):
        with patch.object(ac, "_fetch_task", side_effect=ac.AsanaError("offline")):
            with self.assertRaises(ac.AsanaError):
                rules.live_pool([{"task_refs": [{"gid": "101"}]}], {}, self.upload)

    def test_index_corruption_not_a_miss(self):
        with self.assertRaises(Exception):
            rules.retrieve_rows(None, {})

    def test_exact_order_must_be_from_live_task(self):
        reads = [{**reading(), "order_no": "61234567"}]*2
        live = task()
        self.assertIsNone(self.choose([live], reads)[0])
        live["name"] += "/ 61234567"
        self.assertIsNotNone(self.choose([live], reads)[0])

    def test_explicit_opt_in_is_read_only(self):
        with patch.dict("os.environ", {"JOBSHEET_EVIDENCE_LIVE_RULES": "1", processor.DRY_RUN_ENV: "0"}):
            with self.assertRaises(ValueError):
                processor._ocr_and_match(None, "PM")

    def test_prompts_no_customer_or_known_answer_values(self):
        self.assertNotIn("2026", rules.CHECKLIST_PROMPT)
        self.assertNotIn("ABC12X3456", rules.CHECKLIST_PROMPT)
        self.assertNotIn("Test Hospital", rules.CHECKLIST_PROMPT)

    def test_private_values_not_in_diagnostics(self):
        _, reason = self.choose([task()])
        self.assertNotIn("ABC12X3456", reason)
        self.assertNotIn("23456789", reason)


if __name__ == "__main__":
    unittest.main()

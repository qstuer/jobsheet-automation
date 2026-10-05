"""Synthetic policy tests: no customer data and no external services."""
import unittest
from datetime import date
from unittest.mock import patch

from src import asana_client as ac, matching_rules as rules, processor


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

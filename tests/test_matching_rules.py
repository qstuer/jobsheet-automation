"""Synthetic policy tests: no customer data and no external services."""
import unittest
from datetime import date
from unittest.mock import patch

from src import asana_client as ac, matching_rules as rules, nvidia_client as vision, processor, private_ocr_context


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

    def test_unreadable_date_with_fuzzy_serial_uses_repeated_exact_device_evidence(self):
        reads = [reading(hospital=None, serial="ABC12X3450", asset="1234567", day=None)] * 2
        selected, reason = self.choose([task(asset="1234567")], reads)
        self.assertEqual(selected, task(asset="1234567"))
        self.assertEqual(reason, "upload_prior_live_visit_confirmed")

    def test_unreadable_date_fuzzy_identity_requires_repeated_full_asset_and_phone(self):
        good = reading(hospital=None, serial="ABC12X3450", asset="1234567", day=None)
        for field, weak in (("asset_candidates", []), ("phone_candidates", []),
                            ("asset_candidates", ["1234560"]),
                            ("phone_candidates", ["23456780"])):
            with self.subTest(field=field, weak=weak):
                self.assertIsNone(self.choose([task(asset="1234567")],
                    [good, {**good, field: weak}])[0])

    def test_unreadable_date_fuzzy_identity_requires_one_repeated_serial(self):
        good = reading(hospital=None, serial="ABC12X3450", asset="1234567", day=None)
        for other in (None, "ABC12X3451"):
            with self.subTest(other=other):
                reads = [good, {**good, "serial_candidates": [other] if other else []}]
                self.assertIsNone(self.choose([task(asset="1234567")], reads)[0])

    def test_strong_fuzzy_identity_retains_poorer_serial_readings(self):
        good = reading(hospital=None, serial="ABC12X3450", asset="1234567", day=None)
        poorer = {**good, "serial_candidates": [], "serial_visual_candidates": ["ABC12X3400"]}
        selected, _ = self.choose([task(asset="1234567")], [good] * 2 + [poorer] * 2)
        self.assertEqual(selected, task(asset="1234567"))

    def test_strong_fuzzy_identity_uses_repeated_near_serial_to_corroborate_single_close_read(self):
        close = reading(hospital=None, serial="ABC12X3450", asset="1234567", day=None)
        poorer = {**close, "serial_candidates": [], "serial_visual_candidates": ["ABC12X3400"]}
        selected, _ = self.choose([task(asset="1234567")], [close] + [poorer] * 2)
        self.assertEqual(selected, task(asset="1234567"))
        too_far = {**poorer, "serial_visual_candidates": ["ABC12X3000"]}
        self.assertIsNone(self.choose([task(asset="1234567")], [close] + [too_far] * 2)[0])

    def test_strong_fuzzy_identity_does_not_arbitrate_two_repeated_near_serials(self):
        good = reading(hospital=None, serial="ABC12X3450", asset="1234567", day=None)
        other = {**good, "serial_candidates": ["ABC12X3451"]}
        self.assertIsNone(self.choose([task(asset="1234567")], [good] * 2 + [other] * 2)[0])

    def test_unreadable_date_fuzzy_identity_requires_exact_model(self):
        reads = [reading(hospital=None, serial="ABC12X3450", asset="1234567", day=None)] * 2
        self.assertIsNone(self.choose([task(asset="1234567", product="Affiniti 50")], reads)[0])

    def test_strong_fuzzy_identity_does_not_choose_between_visits_or_devices(self):
        reads = [reading(hospital=None, serial="ABC12X3450", asset="1234567", day=None)] * 2
        for other in (task("102", asset="1234567", due="2026-09-21"),
                      task("102", asset="1234567", serial="ABC12X3451")):
            with self.subTest(other=other["gid"], serial=other["name"]):
                self.assertIsNone(self.choose([task(asset="1234567"), other], reads)[0])

    def test_strong_fuzzy_identity_does_not_override_known_hospital_conflict(self):
        reads = [reading(hospital="Other Hospital", serial="ABC12X3450",
                         asset="1234567", day=None)] * 2
        self.assertIsNone(self.choose([task(asset="1234567")], reads)[0])

    def test_strong_fuzzy_identity_still_requires_reference_window(self):
        reads = [reading(hospital=None, serial="ABC12X3450", asset="1234567", day=None)] * 2
        self.assertIsNone(self.choose([task(asset="1234567", due="2026-08-01")], reads)[0])
        self.assertIsNone(self.choose([task(asset="1234567")], reads, upload=False)[0])

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

    def test_month_fallback_requires_a_reference_day(self):
        readings = [reading(day=None)]*2
        self.assertIsNotNone(self.choose([task()], readings)[0])
        self.assertIsNone(self.choose([task()], readings, upload=False)[0])
        self.assertIsNone(self.choose([task(due="2026-06-01")], readings)[0])

    def test_fallback_accepts_only_the_surrounding_calendar_months(self):
        ev = rules.evidence([reading(day=None)] * 2)
        for due, accepted in (("2026-10-15", False), ("2026-10-16", True),
                              ("2026-11-11", True), ("2026-12-16", True),
                              ("2026-12-17", False), ("2026-09-28", False)):
            with self.subTest(due=due):
                selected, _ = rules.select_task([task(due=due)], ev, "PM", date(2026, 11, 16))
                self.assertEqual(selected is not None, accepted)

    def test_calendar_month_window_handles_month_ends_and_new_year(self):
        for day, first, last in (
            (date(2026, 3, 31), date(2026, 2, 28), date(2026, 4, 30)),
            (date(2024, 3, 31), date(2024, 2, 29), date(2024, 4, 30)),
            (date(2026, 1, 31), date(2025, 12, 31), date(2026, 2, 28)),
        ):
            with self.subTest(day=day):
                self.assertEqual(rules.month_window(day), (first, last))

    def test_reader_uses_today_when_no_replay_day_is_supplied(self):
        current = task(kind="CM", due="2026-11-11")
        ac.set_device_index({"schema_version": 3, "location_directory": [],
            "devices": [{"serial": "ABC12X3456", "task_refs": [{"gid": "101"}]}]})
        with patch.dict("os.environ", {processor.MATCHING_RULES_ENV: rules.RULESET_VERSION}), \
                patch.object(private_ocr_context, "build_vocabulary", return_value={}), \
                patch.object(vision, "_today", return_value=date(2026, 11, 16)), \
                patch.object(vision, "ocr_jobsheet_fields", return_value=reading(day=None)), \
                patch.object(vision, "ocr_jobsheet_identity_fields", return_value=reading(day=None)), \
                patch.object(vision, "ocr_jobsheet_support_fields", return_value=reading(day=None)), \
                patch.object(vision, "ocr_jobsheet_focused_field", return_value={}), \
                patch.object(ac, "_typeahead", return_value=[current]), \
                patch.object(ac, "_fetch_task", return_value=current):
            selected, _, _ = processor._ocr_and_match([object()], "CM")
        self.assertEqual(selected, current)

    def test_date_prior_not_before_checklist(self):
        result = self.choose([task()], [reading(day=None)]*2, allow_upload_prior=False)
        self.assertEqual(result[1], "need_crosspage_before_upload_prior")

    def test_upload_date_does_not_choose_between_same_device_visits(self):
        ev = rules.evidence([reading(day=None)] * 2)
        for kind in ("PM", "CM"):
            with self.subTest(kind=kind):
                pool = [task("101", due="2026-09-21", kind=kind),
                        task("102", due="2026-09-28", kind=kind)]
                self.assertEqual(rules.select_task(pool, ev, kind, self.upload),
                                 (None, "upload_prior_not_unique"))

    def test_clear_action_date_selects_visit_after_upload_prior_is_rejected(self):
        pool = [task("101", due="2026-09-21"), task("102", due="2026-09-28")]
        for paper_date, expected in (("21/9/2026", "101"), ("28/9/2026", "102")):
            with self.subTest(paper_date=paper_date):
                selected, reason = self.choose(pool, [reading(day=paper_date)] * 2)
                self.assertEqual(selected["gid"], expected)
                self.assertEqual(reason, "action_date_live_visit_confirmed")

    def test_single_visit_upload_prior_remains_available(self):
        ev = rules.evidence([reading(day=None)] * 2)
        pool = [task("101", due="2026-06-01"), task("102", due="2026-09-28")]
        selected, reason = rules.select_task(pool, ev, "PM", self.upload)
        self.assertEqual(selected["gid"], "102")
        self.assertEqual(reason, "upload_prior_live_visit_confirmed")

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
        with patch.object(ac, "_typeahead", return_value=[]), \
                patch.object(ac, "_fetch_task", return_value=task()) as fetch:
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

    def test_reference_diagnostics_do_not_disclose_task_or_ocr_identifiers(self):
        import json
        live = task(kind="CM")
        live["name"] += "/ HAWO 12345678"
        ev = rules.evidence([{**reading(), "work_order_candidates": ["WO12345678"]}] * 2)
        diagnostic = rules.diagnostic_pool([live], ev, "CM")
        self.assertTrue(diagnostic["nearest_live_facts"][0]["work_order_number_match"])
        for value in ("12345678", "ABC12X3456", "23456789", live["name"]):
            self.assertNotIn(value, json.dumps(diagnostic))

    def test_eight_digit_hospital_reference_accepts_written_wo_and_asana_hawo(self):
        live = task(kind="CM")
        live["name"] += "/ HAWO 12345678"
        read = {**reading(), "work_order_candidates": ["WO# 12345678"]}
        ev = rules.evidence([read, read])
        self.assertEqual(rules.select_task([live], ev, "CM", self.upload)[0], live)
        with patch.object(ac, "_typeahead", return_value=[{"gid": live["gid"], "name": live["name"]}]), \
                patch.object(ac, "_fetch_task", return_value=live):
            pool, _ = rules.live_pool([], ev, self.upload, job_type="CM")
        self.assertEqual(pool, [live])

    def test_same_eight_digit_reference_across_prefix_spellings_is_one_repeated_reading(self):
        ev = rules.evidence([
            {**reading(), "work_order_candidates": ["WO12345678"]},
            {**reading(), "work_order_candidates": ["HAWO 12345678"]}])
        self.assertEqual(len(rules._stable(ev, "work_order_candidates")), 1)

    def test_reference_alias_does_not_accept_wrong_visit_or_equipment(self):
        live = task(kind="CM")
        live["name"] += "/ HAWO12345678"
        good = {**reading(), "work_order_candidates": ["WO12345678"]}
        for changes in ({"work_order_candidates": ["WO12345679"]},
                        {"serial_candidates": ["ABC12X0000"]},
                        {"hospital_raw": "Other Hospital"},
                        {"product_raw": "MX40"}, {"service_date_raw": "1/8/2026"}):
            with self.subTest(changes=changes):
                ev = rules.evidence([{**good, **changes}] * 2)
                self.assertIsNone(rules.select_task([live], ev, "CM", self.upload)[0])
        ev = rules.evidence([good, good])
        self.assertIsNone(rules.select_task([live, {**live, "gid": "102"}], ev, "CM", self.upload)[0])

    def test_reference_alias_keeps_short_wo_and_company_order_separate(self):
        self.assertNotEqual(rules._key("work_order_candidates", "WO123456"),
                            rules._key("work_order_candidates", "HAWO123456"))
        self.assertEqual(rules._key("work_order_candidates", "62200001"), "")

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

    def test_indexed_device_discovers_new_visit_without_work_order(self):
        for old_kind, new_kind in (("PM", "CM"), ("CM", "CM"), ("PM", "PM")):
            with self.subTest(old_kind=old_kind, new_kind=new_kind):
                ev = rules.evidence([reading()] * 2)
                old = task("101", due="2026-06-01", kind=old_kind)
                current = task("102", kind=new_kind)
                rows = [{"serial": "ABC12X3456", "task_refs": [{"gid": "101"}]}]
                with patch.object(ac, "_typeahead", return_value=[old, current]) as search, \
                        patch.object(ac, "_fetch_task", side_effect={"101": old, "102": current}.__getitem__):
                    pool, _ = rules.live_pool(rows, ev, self.upload, job_type=new_kind)
                self.assertEqual(search.call_args_list[0].args, ("ABC12X3456",))
                self.assertEqual(rules.select_task(pool, ev, new_kind, self.upload)[0], current)

    def test_production_matching_finds_cm_missing_from_index(self):
        old = task("101", kind="PM")
        current = task("102", kind="CM")
        ac.set_device_index({"devices": [{"serial": "ABC12X3456",
            "task_refs": [{"gid": "101"}]}]})
        with patch.dict("os.environ", {processor.MATCHING_RULES_ENV: rules.RULESET_VERSION}), \
                patch.object(vision, "ocr_jobsheet_fields", return_value=reading()), \
                patch.object(vision, "ocr_jobsheet_identity_fields", return_value=reading()), \
                patch.object(vision, "ocr_jobsheet_support_fields", return_value=reading()), \
                patch.object(ac, "_typeahead", return_value=[old, current]), \
                patch.object(ac, "_fetch_task", side_effect={"101": old, "102": current}.__getitem__):
            selected, tier, _ = processor._ocr_and_match([object()], "CM", self.upload)
        self.assertEqual(selected, current)
        self.assertEqual(tier, 2)

    def test_new_same_day_visit_is_retained_as_a_competing_candidate(self):
        ev = rules.evidence([reading()] * 2)
        old = task("101", kind="CM")
        current = task("102", kind="CM")
        rows = [{"serial": "ABC12X3456", "task_refs": [{"gid": "101"}]}]
        with patch.object(ac, "_typeahead", return_value=[old, current]), \
                patch.object(ac, "_fetch_task", side_effect={"101": old, "102": current}.__getitem__):
            pool, _ = rules.live_pool(rows, ev, self.upload, job_type="CM")
        self.assertEqual(rules.select_task(pool, ev, "CM", self.upload),
                         (None, "live_visits_tied"))

    def test_serial_search_at_limit_holds_without_fetching_partial_results(self):
        ev = rules.evidence([reading()] * 2)
        rows = [{"serial": "ABC12X3456", "task_refs": [{"gid": "101"}]}]
        with patch.object(ac, "_typeahead", return_value=[{}] * ac.TYPEAHEAD_DEFAULT_COUNT), \
                patch.object(ac, "_fetch_task") as fetch:
            self.assertEqual(rules.live_pool(rows, ev, self.upload, job_type="CM"),
                             ([], "task_candidate_limit"))
        fetch.assert_not_called()

    def test_serial_search_does_not_truncate_transcription_alternatives(self):
        ev = rules.evidence([{"serial_candidates": [
            "ABC12X3456", "ABC12X3457", "ABC12X3458", "ABC12X3459",
            "ABC12X3460", "ABC12X3461", "ABC12X3462"]}])
        with patch.object(ac, "_typeahead") as search, patch.object(ac, "_fetch_task") as fetch:
            self.assertEqual(rules.live_pool([], ev, self.upload, job_type="CM"),
                             ([], "task_candidate_limit"))
        search.assert_not_called()
        fetch.assert_not_called()

    def test_crosspage_serial_alternatives_do_not_hide_a_late_live_visit(self):
        for count in (4, 6):
            with self.subTest(count=count):
                serials = [f"ABC12X{3456 + i}" for i in range(count)]
                current = task("102", serial=serials[-1])
                ev = rules.evidence([reading(serial=serials[-1])] * 2
                    + [{"serial_candidates": serials[:-1]}])
                with patch.object(ac, "_typeahead", side_effect=lambda term:
                        [current] if term == serials[-1] else []) as search, \
                        patch.object(ac, "_fetch_task", return_value=current):
                    pool, reason = rules.live_pool([], ev, self.upload, job_type="PM")
                self.assertEqual(search.call_count, count)
                self.assertEqual(reason, "live_tasks_read")
                self.assertEqual(rules.select_task(pool, ev, "PM", self.upload)[0], current)

    def test_expanded_serial_search_still_bounds_combined_live_tasks(self):
        ev = rules.evidence([{"serial_candidates": ["ABC12X3456", "ABC12X3457"]}])
        batches = [[{"gid": str(start + i)} for i in range(41)] for start in (100, 200)]
        with patch.object(ac, "_typeahead", side_effect=batches), \
                patch.object(ac, "_fetch_task") as fetch:
            self.assertEqual(rules.live_pool([], ev, self.upload, job_type="PM"),
                             ([], "task_candidate_limit"))
        fetch.assert_not_called()

    def test_serial_lookup_failure_is_an_infrastructure_error(self):
        ev = rules.evidence([reading()] * 2)
        rows = [{"serial": "ABC12X3456", "task_refs": [{"gid": "101"}]}]
        with patch.object(ac, "_typeahead", side_effect=ac.AsanaError("offline")), \
                patch.object(ac, "_fetch_task") as fetch:
            with self.assertRaises(ac.AsanaError):
                rules.live_pool(rows, ev, self.upload, job_type="CM")
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
        with patch.object(ac, "_typeahead", return_value=[]), \
                patch.object(ac, "_fetch_task", side_effect=ac.AsanaError("offline")):
            with self.assertRaises(ac.AsanaError):
                rules.live_pool([{"task_refs": [{"gid": "101"}]}],
                                rules.evidence([reading()] * 2), self.upload, job_type="CM")

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

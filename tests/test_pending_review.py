"""Synthetic, read-only tests for the human date / visit review outlet."""

import unittest
import os
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src import asana_client, asana_index, config, pending_review, processor


def fake_task(gid, *, due="2026-08-20", hospital="Demo General Hospital",
              product="EPIQ Elite", serial="US123B4567", kind="PM",
              phone="99990011", asset=""):
    project = "2026 Aug" if kind == "PM" else "Corrective Maintenance"
    return {
        "gid": gid, "name": f"{hospital} / {product} / {serial}",
        "notes": f"Phone: {phone}\nAsset# {asset}" if asset else f"Phone: {phone}",
        "due_on": due,
        "completed_at": "", "start_on": "", "completed": False,
        "created_at": "2026-08-01T00:00:00Z",
        "modified_at": "2026-08-21T00:00:00Z",
        "_project_job_type": kind,
        "memberships": [{"project": {"name": project}, "section": {"name": "Work"}}],
    }


def fake_ocr(**overrides):
    row = {
        "serial_candidates": ["US123B4567"],
        "product_raw": "EPIQ Elite", "hospital_raw": "Demo General Hospital",
        "phone_candidates": ["99990011"], "asset_candidates": [],
        "service_date_raw": "20/9/2026", "date_source": "ACTION_DATE",
    }
    row.update(overrides)
    return row


class PendingReviewTests(unittest.TestCase):
    def setUp(self):
        self.visits = [fake_task("12345678")]
        self._install()

    def tearDown(self):
        asana_client.clear_device_index()
        asana_client._task_cache.clear()

    def _install(self):
        index = asana_index.build_index(
            self.visits, window_start=date(2025, 1, 1),
            window_end=date(2026, 12, 31),
        )
        asana_client.set_device_index(index)

    def _review(self, ocr=None, day="2026-08-18", chosen="", serial=""):
        with patch.object(asana_client, "_fetch_task", side_effect=lambda gid: next(
                item for item in self.visits if item["gid"] == gid)):
            return pending_review.review_ocr(ocr or fake_ocr(), "PM", day, chosen,
                                             confirmed_serial=serial)

    def test_confirmed_serial_requires_visual_and_exact_same_visit_asset(self):
        self.visits = [fake_task("12345678", asset="88880001")]
        self._install()
        two_typos = fake_ocr(serial_candidates=["US123B4599"],
                             asset_candidates=["88880001"], phone_candidates=[])
        self.assertEqual("confirmed_serial_requires_task", self._review(
            two_typos, serial="US123B4567")["reason"])
        result = self._review(two_typos, chosen="12345678", serial="US123B4567")
        self.assertEqual("READY_READ_ONLY", result["status"])
        self.assertTrue(result["manual_serial_confirmed"])
        self.assertEqual("manual_serial_not_visually_supported", self._review(
            fake_ocr(serial_candidates=["SZ999B9999"],
                     asset_candidates=["88880001"]),
            chosen="12345678", serial="US123B4567")["reason"])
        self.assertEqual("manual_serial_asset_not_confirmed", self._review(
            fake_ocr(serial_candidates=["US123B4599"], asset_candidates=[]),
            chosen="12345678", serial="US123B4567")["reason"])
        self.assertEqual("manual_serial_asset_not_confirmed", self._review(
            fake_ocr(serial_candidates=["US123B4599"],
                     asset_candidates=["88880002"]),
            chosen="12345678", serial="US123B4567")["reason"])

    def test_confirmed_serial_cannot_bypass_date_type_or_live_asset(self):
        self.visits = [fake_task("12345678", asset="88880001")]
        self._install()
        ocr = fake_ocr(serial_candidates=["US123B4599"],
                       asset_candidates=["88880001"])
        self.assertEqual("no_visit_within_14_days", self._review(
            ocr, day="2026-09-20", chosen="12345678",
            serial="US123B4567")["reason"])
        self.assertEqual("selected_visit_not_eligible", self._review(
            ocr, chosen="87654321", serial="US123B4567")["reason"])
        self.visits.append(fake_task("87654321", due="2026-08-21",
                                     asset="88880002"))
        self._install()
        self.assertEqual("manual_serial_asset_not_confirmed", self._review(
            ocr, chosen="87654321", serial="US123B4567")["reason"])
        self.visits[-1] = fake_task("87654321", due="2026-08-21",
                                    asset="88880001", kind="CM")
        self._install()
        self.assertEqual("selected_visit_not_eligible", self._review(
            ocr, chosen="87654321", serial="US123B4567")["reason"])
        self.assertEqual("device_identity_conflict", self._review(
            fake_ocr(serial_candidates=["US123B4599"], asset_candidates=["88880001"],
                     hospital_raw="Wrong Hospital"), chosen="12345678",
            serial="US123B4567")["reason"])
        with patch.object(asana_client, "_fetch_task", return_value=fake_task(
                "12345678", asset="88880002")):
            self.assertEqual("live_support_conflict", pending_review.review_ocr(
                ocr, "PM", "2026-08-18", "12345678",
                confirmed_serial="US123B4567")["reason"])

    def test_confirmed_serial_rejects_partial_input(self):
        for value in ("US123B45?7", "US123B45-7", "US123", "15915F0726"):
            with self.assertRaises(ValueError):
                pending_review.parse_review_serial(value)

    def test_human_date_does_not_replace_other_checks(self):
        result = self._review()
        self.assertEqual("READY_READ_ONLY", result["status"])
        self.assertEqual("12345678", result["task"]["gid"])
        self.assertEqual("PENDING", self._review(fake_ocr(hospital_raw="Wrong Hospital"))["status"])
        self.assertEqual("PENDING", self._review(fake_ocr(product_raw="Affiniti 70"))["status"])
        self.assertEqual("PENDING", self._review(fake_ocr(serial_candidates=["SZ999B9999"]))["status"])

    def test_two_week_boundary_and_live_date(self):
        self.assertEqual("READY_READ_ONLY", self._review(day="2026-08-06")["status"])
        self.assertEqual("no_visit_within_14_days", self._review(day="2026-08-05")["reason"])
        with patch.object(asana_client, "_fetch_task", return_value=fake_task(
                "12345678", due="2026-09-30")):
            result = pending_review.review_ocr(fake_ocr(), "PM", "2026-08-18")
        self.assertEqual("live_date_conflict", result["reason"])

    def test_two_same_type_visits_require_task_and_recheck_live(self):
        self.visits.append(fake_task("87654321", due="2026-08-27"))
        self._install()
        result = self._review()
        self.assertEqual("visit_ambiguous", result["reason"])
        self.assertEqual(2, result["candidate_count"])
        self.assertEqual("READY_READ_ONLY", self._review(chosen="87654321")["status"])
        self.assertEqual("selected_visit_not_eligible", self._review(chosen="99999999")["reason"])

    def test_undated_visit_does_not_silently_disappear(self):
        self.visits.append(fake_task("87654321", due=""))
        self._install()
        self.assertEqual("visit_ambiguous", self._review()["reason"])
        self.assertEqual("READY_READ_ONLY", self._review(chosen="12345678")["status"])

    def test_serial_one_typo_requires_independent_evidence(self):
        self.assertEqual("READY_READ_ONLY", self._review(fake_ocr(
            serial_candidates=["US123B4568"]))["status"])
        result = self._review(fake_ocr(
            serial_candidates=["US123B4568"], phone_candidates=[]))
        self.assertEqual("serial_needs_independent_evidence", result["reason"])

    def test_unique_exact_serial_beats_close_typo_but_two_exact_values_do_not(self):
        self.visits.append(fake_task("87654321", serial="US123B4568",
                                     phone="99990022"))
        self._install()
        self.assertEqual("READY_READ_ONLY", self._review(fake_ocr())["status"])
        self.assertEqual("device_ambiguous", self._review(fake_ocr(
            serial_candidates=["US123B4567", "US123B4568"]))["reason"])

    def test_close_typo_requires_unique_same_visit_support(self):
        self.visits.append(fake_task("87654321", serial="US123B4568",
                                     phone="99990022"))
        self._install()
        disputed = fake_ocr(serial_candidates=["US123B4569"])
        self.assertEqual("READY_READ_ONLY", self._review(disputed)["status"])
        self.visits[-1] = fake_task("87654321", serial="US123B4568",
                                    phone="99990011")
        self._install()
        self.assertEqual("device_ambiguous", self._review(disputed)["reason"])
        self.assertEqual("READY_READ_ONLY", self._review(
            disputed, chosen="87654321")["status"])
        self.assertEqual("device_ambiguous", self._review(fake_ocr(
            serial_candidates=["US123B4569"], phone_candidates=[]),
            chosen="87654321")["reason"])

    def test_other_visit_phone_cannot_rescue_serial_typo(self):
        self.visits.append(fake_task("87654321", due="2026-08-27", phone="99990022"))
        self._install()
        result = self._review(fake_ocr(serial_candidates=["US123B4568"]),
                              chosen="87654321")
        self.assertEqual("selected_visit_not_eligible", result["reason"])

    def test_asana_failure_is_not_treated_as_no_candidate(self):
        with patch.object(asana_client, "_fetch_task", side_effect=asana_client.AsanaError(
                "service unavailable")):
            with self.assertRaises(asana_client.AsanaError):
                pending_review.review_ocr(fake_ocr(), "PM", "2026-08-18")

    def test_wrong_type_order_or_live_identity_stops(self):
        result = self._review(fake_ocr(order_no="61234567"))
        # No Asana order number exists; an OCR-only number cannot name a file.
        self.assertEqual("READY_READ_ONLY", result["status"])
        with patch.object(asana_client, "_fetch_task", return_value=fake_task(
                "12345678", kind="CM")):
            self.assertEqual("live_job_type_conflict", pending_review.review_ocr(
                fake_ocr(), "PM", "2026-08-18")["reason"])
        with patch.object(asana_client, "_fetch_task", return_value=fake_task(
                "12345678", serial="US123B4568")):
            self.assertEqual("live_serial_conflict", pending_review.review_ocr(
                fake_ocr(), "PM", "2026-08-18")["reason"])

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            pending_review.parse_review_date("18/08/2026")
        with self.assertRaises(ValueError):
            pending_review.parse_review_date("2026-08-40")
        with self.assertRaises(ValueError):
            pending_review.parse_task_gid("https://example.com/0/1/12345678")
        self.assertEqual("12345678", pending_review.parse_task_gid(
            "https://app.asana.com/0/100/12345678/f"))

    def test_review_mode_has_no_cloud_write_path(self):
        with TemporaryDirectory() as temp, \
                patch.object(processor.rclone_helper, "download"), \
                patch.object(processor.rclone_helper, "upload_unique") as upload, \
                patch.object(processor.rclone_helper, "delete") as delete, \
                patch.object(processor.rclone_helper, "moveto") as move, \
                patch.object(processor.fitz, "open") as open_pdf, \
                patch.object(processor, "_ocr_for_pending_review", return_value=fake_ocr()), \
                patch.object(pending_review, "review_ocr", return_value={
                    "status": "READY_READ_ONLY", "reason": "all_checks_passed",
                    "task": self.visits[0],
                }):
            open_pdf.return_value.__enter__.return_value.__len__.return_value = 4
            result = processor._process_split_file(
                "demo__job1_PM.pdf", Path(temp),
                source_folder=config.GDRIVE_PENDING, dry_run=True,
                review_action_date="2026-08-18",
            )
        self.assertIn("只讀通過", result["status"])
        upload.assert_not_called()
        delete.assert_not_called()
        move.assert_not_called()

    def test_review_mode_cannot_be_used_for_write_or_filename_override(self):
        with TemporaryDirectory() as temp, \
                patch.object(processor.rclone_helper, "download") as download:
            for options in ({"dry_run": False},
                            {"dry_run": True, "confirmed_filename": "unsafe"},
                            {"dry_run": True, "source_folder": config.GDRIVE_SPLIT}):
                with self.assertRaises(ValueError):
                    processor._process_split_file(
                        "demo__job1_PM.pdf", Path(temp),
                        review_action_date="2026-08-18", **options,
                    )
        download.assert_not_called()

    def test_manual_review_rejects_write_before_cloud_listing(self):
        settings = {
            processor.REVIEW_ACTION_DATE_ENV: "2026-08-18",
            processor.TARGET_FILE_ENV: "demo__job1_PM.pdf",
            processor.SOURCE_QUEUE_ENV: "pending",
            processor.DRY_RUN_ENV: "false",
            processor.ASANA_INDEX_FILE_ENV: "unused.json",
        }
        with patch.dict(os.environ, settings, clear=True), \
                patch.object(processor.asana_index, "load_index", return_value={
                    "devices": [], "device_count": 0,
                }), \
                patch.object(processor.rclone_helper, "list_pdfs") as list_pdfs:
            self.assertEqual(1, processor.main())
        list_pdfs.assert_not_called()

    def test_review_ocr_does_not_chase_dates_or_call_asana(self):
        with patch.object(processor.nvidia_client, "reset_ocr_metrics"), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_fields",
                             return_value=fake_ocr()), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=fake_ocr()), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_support_fields",
                             return_value=fake_ocr()), \
                patch.object(processor.nvidia_client, "get_ocr_metrics",
                             return_value={"calls": 3, "seconds": 0.0, "total_tokens": 0}), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_focused_field") as focus, \
                patch.object(asana_client, "find_task") as find:
            result = processor._ocr_for_pending_review(object())
        self.assertEqual("EPIQ Elite", result["product_raw"])
        focus.assert_not_called()
        find.assert_not_called()

    def test_review_rechecks_one_seen_phone_without_answer_context(self):
        missing_phone = fake_ocr(phone_candidates=[])
        with patch.object(processor.nvidia_client, "reset_ocr_metrics"), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_fields",
                             return_value=fake_ocr()), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=missing_phone), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_support_fields",
                             return_value=missing_phone), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_focused_field",
                             return_value={"phone_candidates": ["99990011"]}) as focus, \
                patch.object(processor.nvidia_client, "get_ocr_metrics",
                             return_value={"calls": 4, "seconds": 0.0, "total_tokens": 0}):
            result = processor._ocr_for_pending_review(object())
        self.assertEqual(["99990011"], result["phone_candidates"])
        self.assertEqual("phone_candidates", focus.call_args.args[2])
        self.assertNotIn("2026-08-18", str(focus.call_args))

    def test_review_rechecks_visible_asset_panel_twice_without_looking_up_answer(self):
        broad = fake_ocr(department_room_raw="Asset# 98765432",
                         asset_candidates=[])
        other = fake_ocr(department_room_raw=None, asset_candidates=[])
        focused = {"department_room_raw": "Asset# 98765432",
                   "asset_candidates": ["98765432"]}
        with patch.object(processor.nvidia_client, "reset_ocr_metrics"), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_fields",
                             return_value=broad), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=other), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_support_fields",
                             return_value=other), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_focused_field",
                             return_value=focused) as focus, \
                patch.object(processor.nvidia_client, "get_ocr_metrics",
                             return_value={"calls": 5, "seconds": 0.0,
                                           "total_tokens": 0}):
            result = processor._ocr_for_pending_review(object())
        self.assertEqual(["98765432"], result["asset_candidates"])
        self.assertEqual(2, focus.call_count)
        self.assertTrue(all(call.args[2] == "department_room_raw"
                            for call in focus.call_args_list))

    def test_review_allows_one_last_asset_read_after_disagreement(self):
        broad = fake_ocr(department_room_raw="Asset# 98765432",
                         asset_candidates=[])
        other = fake_ocr(department_room_raw=None, asset_candidates=[])
        readings = [
            {"asset_candidates": ["98765431"]},
            {"asset_candidates": ["98765432"]},
            {"asset_candidates": ["98765432"]},
        ]
        with patch.object(processor.nvidia_client, "reset_ocr_metrics"), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_fields",
                             return_value=broad), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=other), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_support_fields",
                             return_value=other), \
                patch.object(processor.nvidia_client, "ocr_jobsheet_focused_field",
                             side_effect=readings) as focus, \
                patch.object(processor.nvidia_client, "get_ocr_metrics",
                             return_value={"calls": 6, "seconds": 0.0,
                                           "total_tokens": 0}):
            result = processor._ocr_for_pending_review(object())
        self.assertEqual(["98765432"], result["asset_candidates"])
        self.assertEqual([5.0, 6.0, 5.5],
                         [call.kwargs["zoom"] for call in focus.call_args_list])


if __name__ == "__main__":
    unittest.main()

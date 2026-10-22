"""Synthetic date provenance, bounded reads, and unchanged live acceptance gates."""
from contextlib import ExitStack
from datetime import date
import os
import unittest
from unittest.mock import MagicMock, patch

import fitz

from src import asana_client as ac, asana_index, config, date_corroboration as dates
from src import matching_rules as rules, nvidia_client as vision, rclone_helper
from tests.test_matching_rules import reading, task
from tests.test_ocr_resilience import vocabulary_index


def panels(day="2026-09-28", *, page=0):
    fields = dates.COVER_FIELDS if page == 0 else dates.CHECKLIST_FIELDS
    return [{"page": page, "field": field, "source": dates.SOURCES[page, field],
             "zoom": zoom, "date": day} for field in fields for zoom in dates.ZOOMS]


class DateProofTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(vision, "_today", return_value=date(2026, 9, 30))
        clock.start()
        self.addCleanup(clock.stop)

    def choose(self, support, *, reads=None, tasks=None, kind="PM"):
        return rules.select_task(tasks or [task()],
            rules.evidence(reads or [reading(day=None)] * 2), kind, date(2026, 10, 5),
            allow_upload_prior=False, date_support=support)

    def test_two_distinct_panels_repeated_consistently_confirm_date(self):
        proof = dates.summarize(panels())
        self.assertEqual(("confirmed", date(2026, 9, 28), 2),
                         (proof["status"], proof["date"], proof["stable_panel_count"]))
        selected, reason = self.choose(panels())
        self.assertEqual(("101", "signature_date_live_visit_confirmed"), (selected["gid"], reason))

    def test_same_panel_twice_is_not_two_sources(self):
        self.assertEqual("insufficient", dates.summarize(panels()[:2])["status"])
        self.assertIsNone(self.choose(panels()[:2])[0])

    def test_one_read_per_panel_is_not_repeated_evidence(self):
        self.assertEqual("insufficient", dates.summarize(panels()[::2])["status"])
        self.assertIsNone(self.choose(panels()[::2])[0])

    def test_one_correct_and_one_unreadable_rendering_is_not_consensus(self):
        proof = panels()
        proof[1]["date"] = None
        self.assertEqual("insufficient", dates.summarize(proof)["status"])

    def test_disagreeing_stable_panels_hold_instead_of_voting_or_upload_prior(self):
        proof = panels()
        for row in proof[2:]:
            row["date"] = "2026-09-29"
        self.assertEqual("conflict", dates.summarize(proof)["status"])
        self.assertEqual((None, "signature_date_conflict"), self.choose(proof))
        selected, reason = rules.select_task([task()], rules.evidence([reading(day=None)] * 2),
            "PM", date(2026, 10, 5), allow_upload_prior=True, date_support=proof)
        self.assertEqual((None, "signature_date_conflict"), (selected, reason))

    def test_stable_action_date_cannot_be_overridden(self):
        selected, reason = self.choose(panels("2026-09-29"), reads=[reading()] * 2)
        self.assertEqual((None, "signature_date_conflict"), (selected, reason))

    def test_existing_crosspage_date_conflict_remains_a_hold(self):
        reads = [reading()] * 2 + [reading(day="29/9/2026", page=1)] * 2
        self.assertEqual((None, "repeated_dates_conflict"), self.choose(panels(), reads=reads))

    def test_two_week_boundary_not_widened(self):
        self.assertIsNotNone(self.choose(panels(), tasks=[task(due="2026-09-14")])[0])
        self.assertIsNone(self.choose(panels(), tasks=[task(due="2026-09-13")])[0])

    def test_hospital_phone_serial_and_pm_cm_checks_remain(self):
        for live, reads in ((task(hospital="Other Hospital"), [reading(day=None)] * 2),
                            (task(kind="CM"), [reading(day=None)] * 2),
                            (task(), [reading(serial="ABC12X0000", day=None)] * 2),
                            (task(), [reading(serial="ABC12X3400", phone="28654321", day=None)] * 2)):
            self.assertIsNone(self.choose(panels(), tasks=[live], reads=reads)[0])

    def test_live_visit_tie_still_holds_and_completion_is_not_a_filter(self):
        self.assertEqual((None, "live_visits_tied"), self.choose(panels(), tasks=[task(), task(gid="102")]))
        self.assertIsNotNone(self.choose(panels(), tasks=[task(completed=True)])[0])

    def test_short_serial_can_use_corroborated_date_but_not_weak_phone(self):
        live = task(serial="A0B8CD", product="IU22")
        reads = [reading(serial="AOB8CD", product="IU22", day=None)] * 2
        self.assertIsNotNone(self.choose(panels(), tasks=[live], reads=reads)[0])
        reads[1] = {**reads[1], "phone_candidates": []}
        self.assertIsNone(self.choose(panels(), tasks=[live], reads=reads)[0])

    def test_duplicate_rendering_unknown_source_and_invalid_date_are_rejected(self):
        for proof in (panels() + panels()[:1], [{**row, "page": 2} for row in panels()],
                      [{**row, "source": "task_date"} for row in panels()],
                      [{**row, "date": "2026-09-31"} for row in panels()],
                      [{**row, "date": "20260928"} for row in panels()],
                      [{**row, "zoom": 7.0} for row in panels()], [None], 123):
            self.assertEqual("invalid", dates.summarize(proof)["status"])
            self.assertEqual((None, "signature_date_protocol_rejected"), self.choose(proof))

    def test_final_checklist_sources_are_pm_only(self):
        self.assertIsNotNone(self.choose(panels(page=3))[0])
        self.assertEqual((None, "signature_date_protocol_rejected"),
            self.choose(panels(page=3), tasks=[task(kind="CM")], kind="CM"))

    def test_ambiguous_order_cannot_be_resolved_by_signature_date(self):
        live = task()
        live["name"] += "/ 60000011 / 60000022"
        self.assertEqual((None, "ambiguous_order_numbers"), self.choose(panels(), tasks=[live]))

    def test_public_audit_does_not_emit_dates_or_sources(self):
        audit = rules.public_diagnostic({"version": rules.RULESET_VERSION,
            "trace": ["signature_dates_read", "signature_date_live_visit_confirmed"],
            "date_support": dates.summarize(panels()), "private": panels()})
        self.assertEqual({"read_count": 4, "stable_panel_count": 2, "page_count": 1,
                          "confirmed": True}, audit["date_support"])
        self.assertNotIn("2026-09-28", str(audit))
        self.assertNotIn("engineer_signed_date", str(audit))


class DateReaderTests(unittest.TestCase):
    def test_tall_date_strokes_above_underline_remain_inside_crop(self):
        with fitz.open() as doc:
            for _ in range(4):
                doc.new_page(width=600, height=800)
            doc[3].draw_rect(fitz.Rect(100, 576, 110, 584), color=(0, 0, 0), fill=(0, 0, 0))
            with patch.object(vision, "_preprocess_field_image", side_effect=lambda img, **_: img.convert("RGB")):
                img = vision._render_field_crop(doc, 3, dates.CHECKLIST_FIELDS[0], zoom=1)
            self.assertGreater(sum(1 for r, g, b in img.getdata() if max(r, g, b) < 30), 30)

    def test_checklist_crop_excludes_calibration_row_and_other_signature(self):
        with fitz.open() as doc:
            for _ in range(4):
                doc.new_page(width=600, height=800)
            page = doc[3]
            page.draw_rect(fitz.Rect(0, 500, 600, 550), color=(1, 0, 0), fill=(1, 0, 0))
            for field, color in zip(dates.CHECKLIST_FIELDS, ((0, 1, 0), (0, 0, 1))):
                l, t, r, b = config.OCR_FIELD_BOXES[field]
                page.draw_rect(fitz.Rect(l*600, t*800, r*600, b*800), color=color, fill=color)
            for field in dates.CHECKLIST_FIELDS:
                with patch.object(vision, "_preprocess_field_image", side_effect=lambda img, **_: img.convert("RGB")):
                    img = vision._render_field_crop(doc, 3, field, zoom=1)
                red = sum(1 for r, g, b in img.getdata() if r > 200 and g < 40 and b < 40)
                self.assertEqual(0, red)
                self.assertGreater(img.size[0], 100)

    def test_checklist_reader_refuses_wrong_page_or_incomplete_packet_before_model(self):
        for count, page in ((1, 0), (3, 2), (4, 1)):
            doc = MagicMock()
            doc.__len__.return_value = count
            with patch.object(vision, "_call_vision") as call, self.assertRaises(ValueError):
                vision.ocr_jobsheet_signature_date(doc, page, dates.CHECKLIST_FIELDS[0])
            call.assert_not_called()

    def test_reader_uses_only_crop_and_no_task_answer_and_format_retry_is_one_value(self):
        with patch.object(vision, "_today", return_value=date(2026, 9, 30)), \
             patch.object(vision, "crop_jobsheet_field_card", return_value="synthetic-image") as crop, \
             patch.object(vision, "_call_vision", side_effect=['bad-format', '{"signed_date":"28/9/2026"}']) as call:
            result = vision.ocr_jobsheet_signature_date(None, 0, dates.COVER_FIELDS[0])
        self.assertEqual("2026-09-28", result)
        self.assertEqual(2, call.call_count)
        crop.assert_called_once()
        self.assertNotIn("2026-09-28", str(call.call_args))


class DatePipelineTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)
        self.clock = patch.object(vision, "_today", return_value=date(2026, 9, 30))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def pipeline(self, *, kind="PM", signed=None, day=None, enabled=True):
        live = task(serial="A0B8CD", product="IU22", kind=kind)
        ac.set_device_index(vocabulary_index([asana_index.task_to_record(live)]))
        raw = reading(serial="AOB8CD", product="IU22", day=day)
        doc = MagicMock()
        doc.__len__.return_value = 4 if kind == "PM" else 1
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {dates.ENABLED_ENV: "1" if enabled else "0",
                "JOBSHEET_DRY_RUN": "1", "JOBSHEET_DEVICE_SEARCH_AGENT": "0"}))
            stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")))
            stack.enter_context(patch.object(vision, "_call_vision", side_effect=AssertionError("Model forbidden")))
            for name in ("upload", "upload_unique", "delete", "moveto", "run", "run_result"):
                stack.enter_context(patch.object(rclone_helper, name, side_effect=AssertionError("Cloud write forbidden")))
            for name in ("ocr_jobsheet_fields", "ocr_jobsheet_identity_fields", "ocr_jobsheet_support_fields"):
                stack.enter_context(patch.object(vision, name, return_value=raw))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_context_field", return_value={}))
            stack.enter_context(patch.object(vision, "ocr_jobsheet_focused_field", return_value={}))
            stack.enter_context(patch.object(rules, "_checklist_read", return_value={}))
            sign = stack.enter_context(patch.object(vision, "ocr_jobsheet_signature_date",
                side_effect=signed or (lambda *_args, **_kwargs: "2026-09-28")))
            stack.enter_context(patch.object(ac, "_typeahead", return_value=[{"gid": "101"}]))
            stack.enter_context(patch.object(ac, "_fetch_task", return_value=live))
            result = rules.read_and_match(doc, kind, date(2026, 10, 5))
        return result, sign

    def test_disabled_by_default_does_not_call_signature_reader(self):
        (selected, _, _), sign = self.pipeline(enabled=False)
        self.assertIsNone(selected)
        sign.assert_not_called()

    def test_non_dry_run_rejected_before_any_ocr(self):
        with patch.dict(os.environ, {dates.ENABLED_ENV: "1", "JOBSHEET_DRY_RUN": "0"}), \
             patch.object(vision, "ocr_jobsheet_fields") as read:
            with self.assertRaises(ValueError):
                rules.read_and_match(None, "CM")
        read.assert_not_called()

    def test_two_cover_panels_select_without_overwriting_missing_action_date(self):
        (selected, _, ocr), sign = self.pipeline()
        self.assertEqual("101", selected["gid"])
        self.assertEqual(4, sign.call_count)
        self.assertIsNone(ocr["service_date_raw"])
        self.assertEqual(4, len(ocr["_signature_date_audit"]))
        self.assertTrue(all(row["page"] == 0 for row in ocr["_signature_date_audit"]))

    def test_pm_falls_back_to_two_fixed_last_page_panels_bounded_to_eight_reads(self):
        def reader(_doc, page, _field, **_kwargs):
            return "2026-09-28" if page == 3 else None
        (selected, _, ocr), sign = self.pipeline(signed=reader)
        self.assertEqual("101", selected["gid"])
        self.assertEqual(8, sign.call_count)
        self.assertEqual([0]*4 + [3]*4, [row["page"] for row in ocr["_signature_date_audit"]])

    def test_cm_never_reads_a_nonexistent_checklist(self):
        (selected, _, ocr), sign = self.pipeline(kind="CM", signed=lambda *_args, **_kwargs: None)
        self.assertIsNone(selected)
        self.assertEqual(4, sign.call_count)
        self.assertTrue(all(row["page"] == 0 for row in ocr["_signature_date_audit"]))

    def test_stable_action_date_does_not_trigger_unnecessary_date_calls(self):
        (selected, _, _), sign = self.pipeline(day="28/9/2026")
        self.assertEqual("101", selected["gid"])
        sign.assert_not_called()

    def test_conflict_stops_without_seeking_majority_from_extra_page(self):
        def reader(_doc, _page, field, **_kwargs):
            return "2026-09-28" if field == dates.COVER_FIELDS[0] else "2026-09-29"
        (selected, _, ocr), sign = self.pipeline(signed=reader)
        self.assertIsNone(selected)
        self.assertEqual(4, sign.call_count)
        self.assertIn("signature_date_conflict", ocr["rules_audit"]["trace"])


if __name__ == "__main__":
    unittest.main()

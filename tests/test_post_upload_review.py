"""Business and preservation checks using synthetic Asana data only."""
from copy import deepcopy
from datetime import date
import html
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import fitz
import requests

from src import batch_state, config, nvidia_client, post_upload_review as policy, processor, rclone_helper


def task(kind="CM", notes="Fault: probe cable damaged\nContact: Test User\nAsset: 12345", **extra):
    return {"gid": "101", "name": "Test Hospital/ Affiniti 70/ ABC12X3456",
        "notes": notes, "html_notes": "<body>" + html.escape(notes) +
        '<img data-asana-gid="9001" data-asana-type="attachment" /></body>',
        "due_at": "2026-11-12T08:30:00Z", "modified_at": "2026-11-12T00:00:00Z",
        "memberships": [{"project": {"name": "Ultrasound - CM" if kind == "CM" else "2026 Nov"}}], **extra}


class Client:
    def __init__(self, value, *, timeout_after_write=False, drop_image=False):
        self.value = deepcopy(value)
        self.reads = 0
        self.writes = 0
        self.timeout_after_write = timeout_after_write
        self.drop_image = drop_image

    def read(self, gid):
        self.reads += 1
        return deepcopy(self.value)

    def write_html(self, gid, value):
        self.writes += 1
        if self.drop_image:
            value = re.sub(r"<img\b[^>]*>", "", value)
        self.value["html_notes"] = value
        self.value["notes"] = html.unescape(re.sub(r"<[^>]+>", "", value))

        self.value["modified_at"] = "2026-11-12T01:00:00Z"
        if self.timeout_after_write:
            raise policy.ReviewError("simulated uncertain response")


class OrderReviewTests(unittest.TestCase):
    def apply(self, value=None, **kwargs):
        value = value or task()
        client = kwargs.pop("client", None) or Client(value)
        result = policy.review(value, kwargs.pop("kind", "CM"), mode=kwargs.pop("mode", "apply"), client=client, **kwargs)
        return result, client

    def test_hospital_reference_is_not_a_company_order(self):
        self.assertIsNone(policy.company_order(task(name="Test/ HAWO 62223344/ WO 65556666")))
        self.assertEqual(policy.company_order(task(name="Test/ HAWO 12345678/ 62200001")), "62200001")
        self.assertEqual(policy.company_order(task(custom_fields=[{
            "name": "Order Number", "number_value": 62200001.0}])), "62200001")

    def test_existing_company_order_never_edits_description(self):
        result, client = self.apply(task(name="Test/ 62200001"))
        self.assertFalse(result["needs_attention"])
        self.assertEqual(client.writes, 0)

    def test_pm_without_order_only_requests_notification(self):
        result, client = self.apply(task(kind="PM"), kind="PM")
        self.assertEqual(result["status"], "pm_missing_order")
        self.assertTrue(result["needs_attention"])
        self.assertEqual(client.writes, 0)

    def test_append_preserves_description_image_and_is_idempotent(self):
        value = task()
        original = deepcopy(value)
        result, client = self.apply(value)
        self.assertEqual(result["status"], "description_updated")
        self.assertEqual(result["appended"], "Probe issue\n12/11 16:30")
        self.assertIn(original["notes"], client.value["notes"])
        self.assertEqual(policy.image_ids(original["html_notes"]), policy.image_ids(client.value["html_notes"]))
        second, _ = self.apply(value, client=client)
        self.assertEqual(second["status"], "description_complete")
        self.assertEqual(client.writes, 1)

    def test_existing_problem_date_time_is_not_rewritten_to_new_schedule_time(self):
        value = task(notes="Fault: probe cable damaged\n\nProbe issue\n12/11 1700")
        result, client = self.apply(value)
        self.assertEqual(result["status"], "description_complete")
        self.assertEqual(client.writes, 0)

    def test_preview_produces_approved_style_without_writing(self):
        value = task(notes='Fault: CW & COLOR 制建無反應', due_at="2026-11-16T07:00:00Z")
        result, client = self.apply(value, mode="preview")
        self.assertEqual(result["draft"], "Button issue\n16/11 15:00")
        self.assertEqual(client.writes, 0)

    def test_schedule_is_hong_kong_time_including_midnight(self):
        value = task(due_at="2026-11-11T16:30:00Z")
        result, _ = self.apply(value)
        self.assertEqual(result["appended"], "Probe issue\n12/11 00:30")

    def test_missing_or_timezone_free_time_cannot_be_invented(self):
        for value in (None, "2026-11-12", "2026-11-12T16:30:00", "invalid"):
            with self.subTest(value=value):
                result, client = self.apply(task(due_at=value))
                self.assertEqual(result["status"], "missing_schedule")
                self.assertEqual(client.writes, 0)

    def test_other_visit_date_or_asset_url_is_not_current_service_record(self):
        for note in ("Fault: probe damaged\n11/11 16:30", "Fault: probe damaged\n12/11/2026",
                     "Fault: probe damaged\nhttps://example.com/12/11/1630"):
            with self.subTest(note=note):
                result, client = self.apply(task(notes=note))
                self.assertEqual(result["status"], "description_updated")
                self.assertEqual(client.writes, 1)

    def test_unconfirmed_or_changed_task_cannot_receive_a_note(self):
        for value, kind, expected in ((None, "CM", "task_unconfirmed"),
                (task(kind="PM"), "CM", "task_type_changed")):
            client = Client(value or task())
            result = policy.review(value, kind, mode="apply", client=client)
            self.assertEqual(result["status"], expected)
            self.assertEqual(client.writes, 0)

    def test_rebase_keeps_concurrent_user_edit(self):
        value = task()
        client = Client(value)
        read = client.read
        def concurrent(gid):
            if client.reads == 1:
                client.value["notes"] += "\nColleague detail"
                client.value["html_notes"] = client.value["html_notes"].replace("</body>", "\nColleague detail</body>")
                client.value["modified_at"] = "changed"
            return read(gid)
        client.read = concurrent
        result, _ = self.apply(value, client=client)
        self.assertEqual(result["status"], "description_updated")
        self.assertIn("Colleague detail", client.value["notes"])
        self.assertEqual(client.writes, 1)

    def test_uncertain_write_is_read_back_without_duplicate_put(self):
        value = task()
        client = Client(value, timeout_after_write=True)
        result, _ = self.apply(value, client=client)
        self.assertEqual(result["status"], "description_updated")
        self.assertEqual(client.writes, 1)

    def test_backup_is_written_before_any_asana_update(self):
        value = task()
        client = Client(value)
        def backup(current, block):
            self.assertEqual(client.writes, 0)
            self.assertEqual(current["notes"], value["notes"])
            self.assertEqual(block, "Probe issue\n12/11 16:30")
        result, _ = self.apply(value, client=client, before_write=backup)
        self.assertEqual(result["status"], "description_updated")

    def test_failed_durable_backup_prevents_an_asana_write(self):
        value = task()
        client = Client(value)
        with self.assertRaises(RuntimeError):
            self.apply(value, client=client, before_write=lambda *_: (_ for _ in ()).throw(RuntimeError("disk failed")))
        self.assertEqual(client.writes, 0)

    def test_same_pdf_retry_does_not_append_again_if_schedule_changes(self):
        value = task(notes="Fault: probe cable damaged\n\nProbe issue\n11/11 16:30")
        result, client = self.apply(value, prior_block="Probe issue\n11/11 16:30")
        self.assertEqual(result["status"], "description_complete")
        self.assertEqual(client.writes, 0)

    def test_lost_image_is_reported_as_unverified_not_success(self):
        value = task()
        client = Client(value, drop_image=True)
        result, _ = self.apply(value, client=client)
        self.assertEqual(result["status"], "update_unverified")
        self.assertTrue(result["needs_attention"])

    def test_unknown_fault_does_not_invent_a_category(self):
        result, client = self.apply(task(notes="Unclear technical request"))
        self.assertEqual(result["status"], "issue_needs_review")
        self.assertEqual(client.writes, 0)

    def test_verified_paper_fault_can_supply_missing_summary(self):
        result, _ = self.apply(task(notes="Contact: Test User"), fault_reader=lambda: "Cooling issue")
        self.assertEqual(result["appended"], "Cooling issue\n12/11 16:30")

    def test_missing_rich_text_does_not_flatten_existing_description(self):
        result, client = self.apply(task(html_notes=None))
        self.assertEqual(result["status"], "rich_text_unavailable")
        self.assertEqual(client.writes, 0)

    def test_service_failure_is_separate_from_pdf_delivery(self):
        client = MagicMock()
        client.read.side_effect = policy.ReviewError("private detail must not be logged")
        result, _ = self.apply(client=client)
        self.assertEqual(result["status"], "service_unavailable")
        self.assertNotIn("private detail", str(result))

    def test_uploaded_job_can_be_final_and_still_need_an_order_notification(self):
        manifest = {"jobs": [{"file": "scan__job1_CM.pdf", "type": "CM"}]}
        batch_state.record_result(manifest, "scan__job1_CM.pdf", "uploaded",
            order_review={"needs_attention": True})
        self.assertTrue(manifest["final"])
        self.assertTrue(manifest["action_required"])

    def test_report_is_not_consumed_between_delivery_and_description_check(self):
        filename = "scan__job1_CM.pdf"
        manifest = {"jobs": [{"file": filename, "type": "CM"}]}
        batch_state.record_result(manifest, filename, "uploaded", order_review_pending=True)
        self.assertFalse(manifest["final"])
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(rclone_helper, "list_files", return_value=["scan.json"]), \
                patch.object(batch_state, "load", return_value=manifest), \
                patch.object(batch_state, "save") as save:
            self.assertEqual(batch_state.finalize_ready_manifests(Path(temp)), 0)
        save.assert_not_called()
        batch_state.record_result(manifest, filename, "uploaded", order_review_pending=False,
            order_review={"status": "description_updated", "needs_attention": True})
        self.assertTrue(manifest["final"])
        self.assertTrue(manifest["action_required"])

    def test_old_batch_is_preview_only_even_if_current_mode_applies(self):
        with patch.dict("os.environ", {policy.MODE_ENV: "apply"}), \
                patch.object(policy, "review", return_value={"status": "preview"}) as review:
            processor._review_uploaded_job(task(), "CM", Path("unused"), {})
        self.assertEqual(review.call_args.kwargs["mode"], "preview")

    def test_unknown_mode_default_off_has_no_asana_requests(self):
        with patch.dict("os.environ", {policy.MODE_ENV: "off"}), patch.object(policy, "AsanaNotesClient") as client:
            self.assertIsNone(processor._review_uploaded_job(task(), "CM", Path("unused"), {}))
        client.assert_not_called()


class FaultSummaryTests(unittest.TestCase):
    def read(self, first, second):
        with fitz.open() as doc:
            doc.new_page()
            with patch.object(nvidia_client, "_call_vision", side_effect=[json.dumps(first), json.dumps(second)]):
                return nvidia_client.ocr_jobsheet_fault_issue(doc)

    def test_two_matching_readings_are_required_for_new_issue_label(self):
        reading = {"fault_text": "Fan not running", "short_issue": "Cooling issue"}
        self.assertEqual(self.read(reading, reading), "Cooling issue")
        self.assertIsNone(self.read(reading, {**reading, "short_issue": "Power issue"}))
        self.assertIsNone(self.read(reading, {**reading, "fault_text": "Device not booting"}))

    def test_fault_summary_rejects_empty_or_identifier_like_label(self):
        for value in (None, "SR12345678 issue", "<b>Cooling issue</b>", "Repaired successfully"):
            data = {"fault_text": "Fan not running", "short_issue": value}
            with self.subTest(value=value):
                self.assertIsNone(self.read(data, data))


class NotesApiTests(unittest.TestCase):
    def test_write_body_changes_only_html_notes(self):
        response = MagicMock(status_code=200)
        with patch.object(config, "ASANA_TOKEN", "synthetic-token"), \
                patch.object(requests, "put", return_value=response) as put:
            policy.AsanaNotesClient().write_html("101", "<body>test</body>")
        self.assertEqual(put.call_args.kwargs["json"], {"data": {"html_notes": "<body>test</body>"}})
        self.assertEqual(put.call_count, 1)

    def test_timed_out_write_has_no_blind_network_retry(self):
        with patch.object(config, "ASANA_TOKEN", "synthetic-token"), \
                patch.object(requests, "put", side_effect=requests.Timeout()) as put:
            with self.assertRaises(policy.ReviewError):
                policy.AsanaNotesClient().write_html("101", "<body>test</body>")
        self.assertEqual(put.call_count, 1)

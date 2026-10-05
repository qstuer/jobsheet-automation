"""Production wiring only: synthetic fixtures, no model or cloud writes."""
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src import batch_state, config, matching_rules, nvidia_client, processor, rclone_helper, splitter
from tests.test_matching_rules import reading, task


class ReleaseWiringTests(unittest.TestCase):
    filename = "scan__job1_PM.pdf"

    def manifest(self, **extra):
        return {"source_file": "scan.pdf", "source_uploaded_at": "2026-10-04T17:00:00Z",
                "created_at": "2026-11-01T00:00:00Z",
                "jobs": [{"file": self.filename, "attempts": 0}], **extra}

    def test_birth_time_saved_separately_from_processing_time(self):
        result = batch_state.new_manifest("scan.pdf", 0, [], "2026-10-04T17:00:00Z")
        self.assertEqual(result["source_uploaded_at"], "2026-10-04T17:00:00Z")
        self.assertNotEqual(result["created_at"], result["source_uploaded_at"])

    def test_original_upload_day_uses_hong_kong_timezone(self):
        self.assertEqual(batch_state.source_upload_day(self.manifest(), self.filename), date(2026, 10, 5))

    def test_missing_upload_time_does_not_use_processing_time(self):
        self.assertIsNone(batch_state.source_upload_day(self.manifest(source_uploaded_at=None), self.filename))

    def test_wrong_source_manifest_cannot_supply_date(self):
        self.assertIsNone(batch_state.source_upload_day(self.manifest(source_file="other.pdf"), self.filename))

    def test_unknown_timezone_or_invalid_date_is_not_inferred(self):
        for value in ("2026-10-05T00:00:00", "invalid"):
            self.assertIsNone(batch_state.source_upload_day(self.manifest(source_uploaded_at=value), self.filename))

    def test_remote_birth_time_does_not_use_mtime(self):
        result = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "ModTime": "2026-11-01T00:00:00Z",
            "Metadata": {"btime": "2026-10-04T17:00:00Z"}}))
        with patch.object(rclone_helper, "run_result", return_value=result) as run:
            self.assertEqual(rclone_helper.remote_created_at("googledrive:scan.pdf"), "2026-10-04T17:00:00+00:00")
        self.assertEqual(run.call_args.args[0], "lsjson")
        self.assertIn("--metadata", run.call_args.args)

    def test_missing_birth_time_returns_unknown(self):
        result = SimpleNamespace(returncode=0, stderr="", stdout='{"ModTime":"2026-11-01T00:00:00Z"}')
        with patch.object(rclone_helper, "run_result", return_value=result):
            self.assertIsNone(rclone_helper.remote_created_at("googledrive:scan.pdf"))

    def test_metadata_failure_not_treated_as_unknown(self):
        result = SimpleNamespace(returncode=1, stderr="unavailable", stdout="")
        with patch.object(rclone_helper, "run_result", return_value=result):
            with self.assertRaises(rclone_helper.RcloneError):
                rclone_helper.remote_created_at("googledrive:scan.pdf")

    def test_production_engine_receives_per_document_upload_day(self):
        with patch.dict(os.environ, {processor.MATCHING_RULES_ENV: matching_rules.RULESET_VERSION,
                                    "JOBSHEET_EVIDENCE_LIVE_RULES": "", processor.DRY_RUN_ENV: "0"}), \
                patch.object(matching_rules, "read_and_match", return_value=(task(), 2, {})) as run:
            processor._ocr_and_match("doc", "PM", reference_day=date(2026, 10, 5))
        run.assert_called_once_with("doc", "PM", date(2026, 10, 5))

    def test_unknown_production_engine_fails_closed(self):
        with patch.dict(os.environ, {processor.MATCHING_RULES_ENV: "unknown",
                                    "JOBSHEET_EVIDENCE_LIVE_RULES": ""}):
            with self.assertRaises(ValueError):
                processor._ocr_and_match(None, "PM")

    def test_readonly_experimental_flag_cannot_enable_upload(self):
        with patch.dict(os.environ, {"JOBSHEET_EVIDENCE_LIVE_RULES": "1",
                                    processor.MATCHING_RULES_ENV: matching_rules.RULESET_VERSION,
                                    processor.DRY_RUN_ENV: "0"}):
            with self.assertRaises(ValueError):
                processor._ocr_and_match(None, "PM")

    def test_cm_project_and_single_page_path_remain_supported(self):
        chosen, _ = matching_rules.select_task([task(kind="CM")],
            matching_rules.evidence([reading(), reading()]), "CM", date(2026, 10, 5))
        self.assertIsNotNone(chosen)
        chosen, _ = matching_rules.select_task([task(kind="PM")],
            matching_rules.evidence([reading(), reading()]), "CM", date(2026, 10, 5))
        self.assertIsNone(chosen)

    def test_total_vision_outage_raises_for_durable_retry(self):
        doc = MagicMock()
        doc.__len__.return_value = 4
        with patch.object(nvidia_client, "ocr_jobsheet_fields", side_effect=nvidia_client.NvidiaResponseError("offline")), \
                patch.object(nvidia_client, "ocr_jobsheet_identity_fields", side_effect=nvidia_client.NvidiaResponseError("offline")), \
                patch.object(nvidia_client, "ocr_jobsheet_support_fields", side_effect=nvidia_client.NvidiaResponseError("offline")):
            with self.assertRaises(nvidia_client.NvidiaResponseError):
                matching_rules.read_and_match(doc, "PM", date(2026, 10, 5))

    def process(self, temp, selected, *, dry_run=False, outage=False):
        events = []
        ocr = {"rules_audit": {"version": matching_rules.RULESET_VERSION}}
        with patch.dict(os.environ, {processor.MATCHING_RULES_ENV: matching_rules.RULESET_VERSION,
                                    "JOBSHEET_EVIDENCE_LIVE_RULES": ""}), \
                patch.object(rclone_helper, "download", side_effect=lambda _r, p: p.write_bytes(b"synthetic")), \
                patch.object(processor.fitz, "open"), \
                patch.object(processor, "_manifest_for_job", return_value=self.manifest()), \
                patch.object(processor, "_ocr_and_match",
                    side_effect=nvidia_client.NvidiaResponseError("offline") if outage else None,
                    return_value=(selected, 2 if selected else 0, ocr)) as match, \
                patch.object(processor, "_finalize_match", side_effect=lambda *_: events.append("upload") or {"state":"uploaded"}) as upload, \
                patch.object(processor, "_save_result", side_effect=lambda *a, **k: events.append(a[3])) as save, \
                patch.object(rclone_helper, "delete", side_effect=lambda *_: events.append("delete")) as delete, \
                patch.object(processor, "_move_to_pending_unique") as move, \
                patch.object(processor.asana_client, "neutral_name_for_ambiguous_visit", return_value="legacy name") as neutral, \
                patch.object(processor.selection_audit, "receipt", return_value={"task":"proof","filename":"proof"}):
            result = processor._process_split_file(self.filename, Path(temp), dry_run=dry_run)
            return result, events, match.call_args, upload.call_count, delete.call_count, move.call_count, neutral.call_count

    def test_match_uploads_only_after_matching_then_records_before_source_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            result, events, call, *_ = self.process(temp, task())
        self.assertEqual(call.kwargs["reference_day"], date(2026, 10, 5))
        self.assertEqual(events, ["upload", "uploaded", "delete"])
        self.assertEqual(result["state"], "uploaded")

    def test_full_processor_dryrun_does_not_write_or_move(self):
        with tempfile.TemporaryDirectory() as temp:
            result, events, _, uploads, deletes, moves, _ = self.process(temp, task(), dry_run=True)
        self.assertFalse(events)
        self.assertEqual((uploads, deletes, moves), (0, 0, 0))
        self.assertIn("預覽", result["status"])

    def test_rejected_evidence_cannot_use_legacy_neutral_naming(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, _, uploads, deletes, moves, neutral = self.process(temp, None, dry_run=True)
        self.assertEqual((uploads, deletes, moves, neutral), (0, 0, 0, 0))

    def test_outage_retains_source_and_records_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            result, events, _, uploads, deletes, moves, neutral = self.process(temp, None, outage=True)
        self.assertEqual(result["state"], "retryable")
        self.assertEqual(events, ["retryable"])
        self.assertEqual((uploads, deletes, moves, neutral), (0, 0, 0, 0))

    def test_formal_public_report_redacts_real_filename(self):
        with patch.dict(os.environ, {processor.MATCHING_RULES_ENV: matching_rules.RULESET_VERSION}):
            value = processor._public_planned_filename({"planned":"private-device.pdf"}, False)
        self.assertNotIn("private-device", value)

    def test_formal_error_log_does_not_print_customer_exception_text(self):
        with patch.dict(os.environ, {processor.MATCHING_RULES_ENV: matching_rules.RULESET_VERSION,
                                    processor.TARGET_FILE_ENV:"", processor.DRY_RUN_ENV:"0",
                                    processor.ASANA_INDEX_FILE_ENV:"",
                                    processor.REVIEW_ACTION_DATE_ENV:"", processor.REVIEW_TASK_ENV:""}), \
                patch.object(rclone_helper, "list_pdfs", return_value=[self.filename]), \
                patch.object(processor, "_process_split_file", side_effect=RuntimeError("private-customer-text")), \
                patch.object(batch_state, "finalize_ready_manifests", return_value=0), \
                self.assertLogs("processor", level="INFO") as logs:
            self.assertEqual(processor.main(), 1)
        self.assertNotIn("private-customer-text", str(logs.output))


if __name__ == "__main__":
    unittest.main()

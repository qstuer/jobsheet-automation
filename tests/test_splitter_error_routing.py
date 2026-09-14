"""Stage A 錯誤分流的回歸測試。"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# 這組測試只驗證例外分流，不會開啟 PDF；讓沒有安裝 runtime requirements
# 的輕量開發環境也能執行。正式 workflow 仍會從 requirements.txt 安裝 PyMuPDF。
if "fitz" not in sys.modules and importlib.util.find_spec("fitz") is None:
    sys.modules["fitz"] = MagicMock()

from src import config, healthcheck, processor, rclone_helper, splitter
from src import pdf_utils
from src.pdf_utils import SplitError


class SplitterErrorRoutingTests(unittest.TestCase):
    def test_document_split_error_moves_source_to_manual_review(self):
        filename = "scan.pdf"
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(splitter.rclone_helper, "download"), \
                patch.object(splitter.pdf_utils, "split_jobs",
                             side_effect=SplitError("頁數驗算失敗")), \
                patch.object(splitter.rclone_helper, "moveto") as moveto:
            result = splitter._split_one(filename, Path(tmpdir))

        moveto.assert_called_once_with(
            f"{config.GDRIVE_INPUT}/{filename}",
            f"{config.GDRIVE_SPLIT_FAILED}/{filename}",
        )
        self.assertEqual(result["status"], "切割失敗(已轉人工)")

    def test_infrastructure_error_keeps_source_in_input_folder(self):
        filename = "scan.pdf"
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(splitter.rclone_helper, "download"), \
                patch.object(splitter.pdf_utils, "split_jobs",
                             side_effect=RuntimeError("NVIDIA API unavailable")), \
                patch.object(splitter.rclone_helper, "moveto") as moveto:
            with self.assertRaisesRegex(RuntimeError, "NVIDIA API unavailable"):
                splitter._split_one(filename, Path(tmpdir))

        moveto.assert_not_called()

    def test_stage_a_returns_failure_for_unexpected_file_error(self):
        with patch.object(splitter.rclone_helper, "list_pdfs", return_value=["scan.pdf"]), \
                patch.object(splitter, "_split_one", side_effect=RuntimeError("API down")):
            self.assertEqual(splitter.main(), 1)

    def test_incomplete_pm_is_quarantined_but_complete_job_continues(self):
        filename = "20260914120000_001.pdf"
        jobs = [
            {"type": "PM", "start": 0, "end": 6, "input_pages": 6,
             "keep_pages": [0, 2, 3, 4], "expected_content_pages": 4,
             "complete": True, "incomplete_reason": None},
            {"type": "PM", "start": 6, "end": 10, "input_pages": 4,
             "keep_pages": [6, 8, 9], "expected_content_pages": 4,
             "complete": False, "incomplete_reason": "PM 只有 3/4 張有內容頁"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(splitter.rclone_helper, "download"), \
                patch.object(splitter.pdf_utils, "split_jobs", return_value=jobs), \
                patch.object(splitter.pdf_utils, "extract_pages"), \
                patch.object(splitter.rclone_helper, "upload") as upload, \
                patch.object(splitter.batch_state, "save"), \
                patch.object(splitter.rclone_helper, "moveto") as moveto, \
                patch.object(splitter.rclone_helper, "delete") as delete:
            result = splitter._split_one(filename, Path(tmpdir))

        destinations = [call.args[1] for call in upload.call_args_list]
        self.assertTrue(any(config.GDRIVE_SPLIT in value for value in destinations))
        self.assertTrue(any(config.GDRIVE_INCOMPLETE in value for value in destinations))
        moveto.assert_called_once_with(
            f"{config.GDRIVE_INPUT}/{filename}",
            f"{config.GDRIVE_INCOMPLETE_RAW}/{filename}",
        )
        delete.assert_not_called()
        self.assertEqual("切割完成（有缺頁）", result["status"])

    def test_stage_b_returns_failure_for_processing_error(self):
        with patch.dict(os.environ, {processor.TARGET_FILE_ENV: ""}), \
                patch.object(processor.rclone_helper, "list_pdfs", return_value=["job_CM.pdf"]), \
                patch.object(processor, "_process_split_file",
                             side_effect=RuntimeError("API down")), \
                patch.object(processor.batch_state, "finalize_ready_manifests", return_value=0):
            self.assertEqual(processor.main(), 1)

    def test_stage_b_single_file_mode_does_not_touch_other_queues(self):
        files = ["one_PM.pdf", "two_PM.pdf"]
        with patch.dict(os.environ, {processor.TARGET_FILE_ENV: "two_PM.pdf"}), \
                patch.object(processor.rclone_helper, "list_pdfs", return_value=files), \
                patch.object(processor, "_process_split_file",
                             return_value={"status": "完成"}) as process, \
                patch.object(processor.batch_state, "finalize_ready_manifests", return_value=0):
            self.assertEqual(processor.main(), 0)

        process.assert_called_once()
        self.assertEqual(process.call_args.args[0], "two_PM.pdf")

    def test_stage_b_missing_single_file_fails_without_processing(self):
        with patch.dict(os.environ, {processor.TARGET_FILE_ENV: "missing_PM.pdf"}), \
                patch.object(processor.rclone_helper, "list_pdfs",
                             return_value=["one_PM.pdf"]), \
                patch.object(processor, "_process_split_file") as process:
            self.assertEqual(processor.main(), 1)

        process.assert_not_called()


class VariableLengthSplitTests(unittest.TestCase):
    def test_pm_duplicate_checklist_is_incomplete(self):
        fake_doc = MagicMock()
        with patch.object(
                pdf_utils.pdf_identity, "page_dhash", side_effect=[1, 1, 999]), \
                patch.object(
                    pdf_utils.pdf_identity, "hash_distance_ratio",
                    side_effect=lambda left, right: 0.0 if left == right else 1.0):
            complete, reason = pdf_utils._completeness(
                fake_doc, "PM", [0, 1, 2, 3]
            )
        self.assertFalse(complete)
        self.assertIn("重複頁", reason)

    def test_fifty_two_page_batch_finds_eleven_real_job_boundaries(self):
        starts = {0: "PM", 6: "PM", 12: "CM", 14: "PM", 20: "PM",
                  24: "PM", 30: "PM", 36: "PM", 38: "PM", 44: "PM",
                  46: "PM"}
        meaningful = {
            2, 3, 4, 8, 9, 10, 16, 17, 18, 21, 22, 26, 27, 28,
            32, 33, 34, 40, 41, 42, 48, 49, 50,
        }
        fake_doc = MagicMock()
        fake_doc.__len__.return_value = 52

        with patch.object(pdf_utils.fitz, "open", return_value=fake_doc), \
                patch.object(pdf_utils, "_page_layout_signature", return_value=(True,)), \
                patch.object(pdf_utils, "page_looks_like_jobsheet",
                             side_effect=lambda _doc, page, _ref: page in starts), \
                patch.object(pdf_utils.nvidia_client, "detect_cm_pm",
                             side_effect=lambda _doc, page: starts.get(page, "UNKNOWN")), \
                patch.object(pdf_utils, "page_has_meaningful_content",
                             side_effect=lambda _doc, page: page in meaningful), \
                patch.object(pdf_utils.pdf_identity, "hash_distance_ratio", return_value=1.0), \
                patch.object(pdf_utils.pdf_identity, "page_dhash", return_value=1):
            jobs = pdf_utils.split_jobs(Path("scan.pdf"))

        self.assertEqual(
            [6, 6, 2, 6, 4, 6, 6, 2, 6, 2, 6],
            [job["input_pages"] for job in jobs],
        )
        self.assertEqual(
            [4, 4, 1, 4, 3, 4, 4, 1, 4, 1, 4],
            [len(job["keep_pages"]) for job in jobs],
        )
        self.assertEqual(8, sum(job["complete"] for job in jobs))
        self.assertEqual([5, 8, 10], [
            index for index, job in enumerate(jobs, 1) if not job["complete"]
        ])

    def test_checklist_candidate_is_rejected_before_calling_vision(self):
        fake_doc = MagicMock()
        fake_doc.__len__.return_value = 8
        with patch.object(pdf_utils.fitz, "open", return_value=fake_doc), \
                patch.object(pdf_utils, "_page_layout_signature", return_value=(True,)), \
                patch.object(pdf_utils, "page_looks_like_jobsheet",
                             side_effect=lambda _doc, page, _ref: page == 4), \
                patch.object(pdf_utils.nvidia_client, "detect_cm_pm",
                             side_effect=lambda _doc, page: "PM") as vision, \
                patch.object(pdf_utils, "page_has_meaningful_content", return_value=False), \
                patch.object(pdf_utils.pdf_identity, "page_dhash", return_value=1):
            jobs = pdf_utils.split_jobs(Path("scan.pdf"))

        self.assertEqual([0, 4], [job["start"] for job in jobs])
        self.assertEqual([4, 4], [job["input_pages"] for job in jobs])
        self.assertEqual([0, 4], [call.args[1] for call in vision.call_args_list])


class ProcessorPendingTests(unittest.TestCase):
    def test_confirmed_single_file_bypasses_ocr_and_uses_safe_name(self):
        filename = "scan__job3_PM.pdf"
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(processor.rclone_helper, "download"), \
                patch.object(processor, "_ocr_and_match") as ocr, \
                patch.object(processor.rclone_helper, "upload_unique", return_value={
                    "disposition": "uploaded",
                    "filename": "Tung Wah Hospital - Affiniti 70 - SZN22F1275.pdf",
                }) as upload, \
                patch.object(processor, "_manifest_for_job", return_value={"jobs": []}), \
                patch.object(processor, "_save_result"), \
                patch.object(processor.rclone_helper, "delete") as delete:
            result = processor._process_split_file(
                filename,
                Path(tmpdir),
                source_folder=config.GDRIVE_PENDING,
                confirmed_filename="Tung Wah Hospital/ Affiniti 70/ SZN22F1275",
            )

        ocr.assert_not_called()
        self.assertEqual(
            "Tung Wah Hospital - Affiniti 70 - SZN22F1275.pdf",
            upload.call_args.args[2],
        )
        delete.assert_called_once_with(f"{config.GDRIVE_PENDING}/{filename}")
        self.assertEqual("完成（人工確認）", result["status"])

    def test_uncertain_match_moves_only_to_pending_and_never_onedrive(self):
        filename = "scan__job1_PM.pdf"
        fake_open = MagicMock()
        fake_open.return_value.__enter__.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(processor.rclone_helper, "download"), \
                patch.object(processor.fitz, "open", fake_open), \
                patch.object(processor, "_ocr_and_match",
                             return_value=(None, 0, {})), \
                patch.object(processor.rclone_helper, "remote_stat", return_value=None), \
                patch.object(processor.rclone_helper, "moveto") as moveto, \
                patch.object(processor.rclone_helper, "upload_unique") as upload, \
                patch.object(processor, "_manifest_for_job", return_value={"jobs": []}), \
                patch.object(processor, "_save_result"):
            result = processor._process_split_file(filename, Path(tmpdir))

        moveto.assert_called_once_with(
            f"{config.GDRIVE_SPLIT}/{filename}",
            f"{config.GDRIVE_PENDING}/{filename}",
        )
        upload.assert_not_called()
        self.assertEqual("等待人工核對", result["status"])

    def test_first_model_outage_is_kept_for_automatic_retry(self):
        filename = "scan__job1_PM.pdf"
        fake_open = MagicMock()
        fake_open.return_value.__enter__.return_value = MagicMock()
        manifest = {
            "source_file": "scan.pdf",
            "jobs": [{"file": filename, "attempts": 0}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(processor.rclone_helper, "download"), \
                patch.object(processor.fitz, "open", fake_open), \
                patch.object(processor, "_ocr_and_match",
                             side_effect=processor.nvidia_client.NvidiaResponseError("503")), \
                patch.object(processor, "_manifest_for_job", return_value=manifest), \
                patch.object(processor, "_save_result") as save, \
                patch.object(processor, "_move_to_pending_unique") as move:
            result = processor._process_split_file(filename, Path(tmpdir))
        self.assertEqual("retryable", result["state"])
        self.assertEqual(1, result["attempts"])
        move.assert_not_called()
        self.assertEqual("retryable", save.call_args.args[3])

    def test_third_model_outage_moves_to_pending_once(self):
        filename = "scan__job1_PM.pdf"
        fake_open = MagicMock()
        fake_open.return_value.__enter__.return_value = MagicMock()
        manifest = {
            "source_file": "scan.pdf",
            "jobs": [{"file": filename, "attempts": 2}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(processor.rclone_helper, "download"), \
                patch.object(processor.fitz, "open", fake_open), \
                patch.object(processor, "_ocr_and_match",
                             side_effect=processor.nvidia_client.NvidiaResponseError("503")), \
                patch.object(processor, "_manifest_for_job", return_value=manifest), \
                patch.object(processor, "_save_result") as save, \
                patch.object(processor, "_move_to_pending_unique",
                             return_value=filename) as move:
            result = processor._process_split_file(filename, Path(tmpdir))
        self.assertEqual("pending", result["state"])
        self.assertEqual(3, result["attempts"])
        move.assert_called_once()
        self.assertEqual("pending", save.call_args.args[3])

    def test_dry_run_match_never_uploads_moves_or_deletes(self):
        filename = "scan__job1_PM.pdf"
        fake_open = MagicMock()
        fake_open.return_value.__enter__.return_value = MagicMock()
        task = {"gid": "task", "name": "Hospital / 61932685"}
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(processor.rclone_helper, "download"), \
                patch.object(processor.fitz, "open", fake_open), \
                patch.object(processor, "_ocr_and_match", return_value=(task, 1, {})), \
                patch.object(processor.rclone_helper, "upload_unique") as upload, \
                patch.object(processor.rclone_helper, "moveto") as move, \
                patch.object(processor.rclone_helper, "delete") as delete, \
                patch.object(processor.batch_state, "save") as save:
            result = processor._process_split_file(
                filename, Path(tmpdir), dry_run=True
            )
        self.assertEqual("預覽：可以可靠配對", result["status"])
        self.assertEqual("SR#61932685.pdf", result["planned"])
        upload.assert_not_called()
        move.assert_not_called()
        delete.assert_not_called()
        save.assert_not_called()

    def test_success_is_recorded_before_google_source_is_deleted(self):
        filename = "scan__job1_PM.pdf"
        fake_open = MagicMock()
        fake_open.return_value.__enter__.return_value = MagicMock()
        task = {"gid": "task", "name": "Hospital / 61932685"}
        events = []
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(processor.rclone_helper, "download"), \
                patch.object(processor.fitz, "open", fake_open), \
                patch.object(processor, "_ocr_and_match", return_value=(task, 1, {})), \
                patch.object(processor, "_finalize_match", return_value={
                    "status": "完成", "state": "uploaded",
                    "onedrive": "SR#61932685.pdf", "asana_task_gid": "task",
                }), \
                patch.object(processor, "_manifest_for_job", return_value={"jobs": []}), \
                patch.object(processor, "_save_result",
                             side_effect=lambda *_args, **_kwargs: events.append("state")), \
                patch.object(processor.rclone_helper, "delete",
                             side_effect=lambda *_args, **_kwargs: events.append("delete")):
            processor._process_split_file(filename, Path(tmpdir))

        self.assertEqual(["state", "delete"], events)


class RcloneListTests(unittest.TestCase):
    def test_root_only_listing_uses_max_depth_without_mixed_filters(self):
        with patch.object(rclone_helper, "run", return_value="one.pdf\ntwo.pdf\n") as run:
            files = rclone_helper.list_pdfs("remote:path", exclude_subdirs=True)

        self.assertEqual(files, ["one.pdf", "two.pdf"])
        run.assert_called_once_with(
            "lsf", "remote:path", "--include", "*.pdf", "--files-only",
            "--max-depth", "1",
        )

    def test_healthcheck_uses_missing_folder_safe_listing(self):
        with patch.object(rclone_helper, "list_files", return_value=[]) as listing:
            self.assertEqual(0, healthcheck.main())

        self.assertEqual(len(healthcheck.QUEUES), listing.call_count)
        listing.assert_any_call(config.GDRIVE_INCOMPLETE, "*.pdf")


class RcloneSafetyTests(unittest.TestCase):
    def test_only_exit_code_three_means_remote_file_is_missing(self):
        missing = MagicMock(returncode=3, stdout="", stderr="directory not found")
        with patch.object(rclone_helper, "run_result", return_value=missing):
            self.assertIsNone(rclone_helper.remote_stat("remote:missing.pdf"))

        broken = MagicMock(returncode=1, stdout="", stderr="token expired")
        with patch.object(rclone_helper, "run_result", return_value=broken):
            with self.assertRaisesRegex(rclone_helper.RcloneError, "token expired"):
                rclone_helper.remote_stat("remote:file.pdf")

    def test_retry_reuses_identical_already_uploaded_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            local = Path(tmpdir) / "job.pdf"
            local.write_bytes(b"same pdf")
            stat = {"Name": "SR#12345678.pdf", "Size": local.stat().st_size}
            with patch.object(rclone_helper, "remote_stat", return_value=stat), \
                    patch.object(rclone_helper, "remote_matches", return_value=True), \
                    patch.object(rclone_helper, "run") as run:
                result = rclone_helper.upload_unique(
                    local, "onedrive:JOBSHEETS", "SR#12345678.pdf")

        self.assertEqual(result, "SR#12345678.pdf")
        run.assert_not_called()

    def test_visual_match_reuses_reencoded_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            local = Path(tmpdir) / "job.pdf"
            local.write_bytes(b"new encoding")
            stat = {"Name": "SR#12345678.pdf", "Size": 999}
            with patch.object(rclone_helper, "remote_stat", return_value=stat), \
                    patch.object(rclone_helper, "remote_matches", return_value=False), \
                    patch.object(rclone_helper, "remote_visually_matches", return_value=True), \
                    patch.object(rclone_helper, "run") as run:
                result = rclone_helper.upload_unique(
                    local, "onedrive:JOBSHEETS", "SR#12345678.pdf")
        self.assertEqual(result, "SR#12345678.pdf")
        run.assert_not_called()

    def test_changed_scan_gets_stable_rescan_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            local = Path(tmpdir) / "job.pdf"
            local.write_bytes(b"changed")
            with patch.object(
                    rclone_helper, "remote_stat",
                    side_effect=[{"Name": "SR#12345678.pdf", "Size": 1}, None]), \
                    patch.object(rclone_helper, "remote_matches", return_value=False), \
                    patch.object(rclone_helper, "remote_visually_matches", return_value=False), \
                    patch.object(rclone_helper, "run") as run:
                result = rclone_helper.upload_unique(
                    local, "onedrive:JOBSHEETS", "SR#12345678.pdf",
                    source_name="20260914153022_001__job1_PM.pdf",
                )
        self.assertEqual(result, "SR#12345678_重掃_20260914-1530.pdf")
        run.assert_called_once()

    def test_delete_uses_single_file_command(self):
        with patch.object(rclone_helper, "run") as run:
            rclone_helper.delete("remote:path/file.pdf")
        run.assert_called_once_with("deletefile", "remote:path/file.pdf")


if __name__ == "__main__":
    unittest.main()

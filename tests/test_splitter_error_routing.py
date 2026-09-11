"""Stage A 錯誤分流的回歸測試。"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# 這組測試只驗證例外分流，不會開啟 PDF；讓沒有安裝 runtime requirements
# 的輕量開發環境也能執行。正式 workflow 仍會從 requirements.txt 安裝 PyMuPDF。
if "fitz" not in sys.modules and importlib.util.find_spec("fitz") is None:
    sys.modules["fitz"] = MagicMock()

from src import config, processor, rclone_helper, splitter
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

    def test_stage_b_returns_failure_for_processing_error(self):
        with patch.object(processor.rclone_helper, "list_pdfs", return_value=["job_CM.pdf"]), \
                patch.object(processor, "_process_split_file",
                             side_effect=RuntimeError("API down")), \
                patch.object(processor, "_retry_pending", return_value=[]):
            self.assertEqual(processor.main(), 1)


class VariableLengthSplitTests(unittest.TestCase):
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
                patch.object(pdf_utils.nvidia_client, "detect_cm_pm",
                             side_effect=lambda _doc, page: starts.get(page, "UNKNOWN")), \
                patch.object(pdf_utils, "page_has_meaningful_content",
                             side_effect=lambda _doc, page: page in meaningful):
            jobs = pdf_utils.split_jobs(Path("scan.pdf"))

        self.assertEqual(
            [6, 6, 2, 6, 4, 6, 6, 2, 6, 2, 6],
            [job["input_pages"] for job in jobs],
        )
        self.assertEqual(
            [4, 4, 1, 4, 3, 4, 4, 1, 4, 1, 4],
            [len(job["keep_pages"]) for job in jobs],
        )


class ProcessorPendingTests(unittest.TestCase):
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
                patch.object(processor.rclone_helper, "upload_unique") as upload:
            result = processor._process_split_file(filename, Path(tmpdir))

        moveto.assert_called_once_with(
            f"{config.GDRIVE_SPLIT}/{filename}",
            f"{config.GDRIVE_PENDING}/{filename}",
        )
        upload.assert_not_called()
        self.assertEqual("等待人工核對", result["status"])


class RcloneListTests(unittest.TestCase):
    def test_root_only_listing_uses_max_depth_without_mixed_filters(self):
        with patch.object(rclone_helper, "run", return_value="one.pdf\ntwo.pdf\n") as run:
            files = rclone_helper.list_pdfs("remote:path", exclude_subdirs=True)

        self.assertEqual(files, ["one.pdf", "two.pdf"])
        run.assert_called_once_with(
            "lsf", "remote:path", "--include", "*.pdf", "--files-only",
            "--max-depth", "1",
        )


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

    def test_delete_uses_single_file_command(self):
        with patch.object(rclone_helper, "run") as run:
            rclone_helper.delete("remote:path/file.pdf")
        run.assert_called_once_with("deletefile", "remote:path/file.pdf")


if __name__ == "__main__":
    unittest.main()

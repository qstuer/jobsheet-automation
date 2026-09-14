"""耐久批次狀態的純本機規則測試。"""
import importlib.util
import sys
import unittest
from unittest.mock import MagicMock

# 系統 Python 只用來跑不需實際開啟 PDF 的輕量測試；正式虛擬環境會載入 PyMuPDF。
if "fitz" not in sys.modules and importlib.util.find_spec("fitz") is None:
    sys.modules["fitz"] = MagicMock()

from src import batch_state


class BatchStateTests(unittest.TestCase):
    def test_manifest_is_final_only_when_every_job_is_terminal(self):
        jobs = [
            {"type": "PM", "input_pages": 6, "keep_pages": [0, 2, 3, 4],
             "expected_content_pages": 4, "complete": True,
             "incomplete_reason": None, "output_name": "scan__job1_PM.pdf"},
            {"type": "PM", "input_pages": 4, "keep_pages": [6, 8, 9],
             "expected_content_pages": 4, "complete": False,
             "incomplete_reason": "PM 只有 3/4 張有內容頁",
             "output_name": "scan__job2_PM.pdf"},
        ]
        manifest = batch_state.new_manifest("scan.pdf", 10, jobs)
        self.assertFalse(manifest["final"])
        self.assertTrue(manifest["action_required"])

        batch_state.record_result(
            manifest, "scan__job1_PM.pdf", "uploaded", onedrive="SR#12345678.pdf"
        )
        self.assertTrue(manifest["final"])
        self.assertTrue(manifest["action_required"])

    def test_retryable_job_prevents_final_report(self):
        manifest = batch_state.ensure_legacy_manifest("scan__job1_PM.pdf")
        batch_state.record_result(
            manifest, "scan__job1_PM.pdf", "retryable", attempts=1
        )
        self.assertFalse(manifest["final"])
        self.assertFalse(manifest["action_required"])

    def test_versioned_file_requires_one_batch_notice(self):
        manifest = batch_state.ensure_legacy_manifest("scan__job1_PM.pdf")
        batch_state.record_result(
            manifest, "scan__job1_PM.pdf", "versioned",
            onedrive="SR#12345678_重掃_20260914-1530.pdf",
        )
        self.assertTrue(manifest["final"])
        self.assertTrue(manifest["action_required"])


if __name__ == "__main__":
    unittest.main()

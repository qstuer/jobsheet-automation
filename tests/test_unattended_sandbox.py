"""Safety checks for the transport adapter, without real services or papers."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import fitz

from scripts.unattended_sandbox import LocalCloud, NetworkGuard, execute
from src import asana_index, pdf_utils, processor, rclone_helper


class SandboxSafetyTests(unittest.TestCase):
    def test_synthetic_batch_runs_both_real_entrypoints_with_virtual_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "synthetic.pdf"
            with fitz.open() as doc:
                for _ in range(16):
                    doc.new_page()
                doc.save(source)
            jobs = []
            tasks = []
            for index, (kind, start, end, kept) in enumerate((
                ("PM", 0, 6, [0, 2, 3, 4]), ("CM", 6, 8, [6]),
                ("PM", 8, 14, [8, 10, 11, 12]), ("CM", 14, 16, [14]))):
                jobs.append({"type": kind, "start": start, "end": end,
                    "input_pages": end - start, "keep_pages": kept,
                    "complete": True, "expected_content_pages": len(kept),
                    "incomplete_reason": None})
                tasks.append(({"gid": str(100 + index),
                    "name": f"Test Hospital/ Affiniti 70/ ABC12X{3456 + index}"}, 2, {}))
            index = {"schema_version": 3, "devices": [], "location_directory": [],
                "generated_at": "2026-11-06T07:15:42.749143Z"}
            with patch.dict(os.environ, {processor.MATCHING_RULES_ENV: "evidence-live-v2",
                    "OCR_PROVIDER": "nvidia", processor.DRY_RUN_ENV: "false"}), \
                    patch.object(asana_index, "load_index", return_value=index), \
                    patch.object(pdf_utils, "split_jobs", return_value=jobs), \
                    patch.object(processor, "_ocr_and_match", side_effect=tasks), \
                    patch("builtins.print"):
                self.assertEqual(execute(source, Path("unused"), Path("unused"),
                    {"source_created_at": "2026-11-16T01:00:00Z"}), 0)

    def test_upload_deduplication_uses_only_sandbox_files(self):
        with tempfile.TemporaryDirectory() as folder:
            cloud = LocalCloud(folder)
            source = Path(folder) / "test.pdf"
            source.write_bytes(b"synthetic fixture")
            with patch.object(rclone_helper, "run_result", side_effect=cloud.command):
                self.assertEqual(rclone_helper.upload_unique(source, "onedrive:output", "job.pdf"), "job.pdf")
                self.assertEqual(rclone_helper.upload_unique(source, "onedrive:output", "job.pdf"), "job.pdf")
            self.assertEqual(cloud.path("onedrive:output/job.pdf").read_bytes(), source.read_bytes())
            self.assertEqual(len(list(cloud.path("onedrive:output").iterdir())), 1)

    def test_path_escape_and_unknown_commands_are_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            cloud = LocalCloud(folder)
            for remote in ("onedrive:../outside.pdf", "googledrive:/absolute.pdf",
                           str(Path(folder).parent / "outside.pdf")):
                with self.subTest(remote=remote), self.assertRaises(AssertionError):
                    cloud.path(remote)
            with self.assertRaises(AssertionError):
                cloud.command("sync", "googledrive:input", "onedrive:output")

    def test_business_cloud_writes_and_unexpected_network_are_blocked(self):
        guard = NetworkGuard()
        guard.validate("GET", "https://app.asana.com/api/1.0/tasks/123")
        guard.validate("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
        for method, url in (("POST", "https://app.asana.com/api/1.0/tasks"),
                            ("DELETE", "https://app.asana.com/api/1.0/tasks/123"),
                            ("GET", "https://graph.microsoft.com/v1.0/me/drive/root"),
                            ("POST", "https://www.googleapis.com/drive/v3/files")):
            with self.subTest(method=method, url=url), self.assertRaises(AssertionError):
                guard.validate(method, url)


if __name__ == "__main__":
    unittest.main()

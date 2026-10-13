"""Read-only cloud entry gates; never call rclone or any real API."""
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import ocr_trace_cloud as cloud


class CloudTraceGates(unittest.TestCase):
    def test_wrong_branch_rejected_without_download_or_model(self):
        with patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main", "JOBSHEET_DRY_RUN": "1"}), \
                patch.object(cloud, "download_id") as download, patch.object(cloud, "capture_main") as capture, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(3, cloud.main())
        download.assert_not_called()
        capture.assert_not_called()
        self.assertNotIn("traceback", output.getvalue().lower())

    def test_download_destination_cannot_be_a_remote_or_outside_private_root(self):
        with patch.object(cloud.subprocess, "run") as run:
            for destination in ("onedrive:company/file.pdf", "/outside/source.pdf"):
                with self.assertRaises(ValueError):
                    cloud.download_id("synthetic_file_id", Path(destination))
        run.assert_not_called()

    def test_download_command_is_drive_to_local_only_and_errors_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "source.pdf"
            def download(command, **kwargs):
                destination.write_bytes(b"synthetic")
                return Mock(returncode=0)
            with patch.object(cloud, "PRIVATE_ROOT", root), patch.object(cloud.subprocess, "run", side_effect=download) as run:
                cloud.download_id("synthetic_file_id", destination)
            self.assertEqual(["rclone", "backend", "copyid", "googledrive:", "synthetic_file_id", str(destination.resolve())], run.call_args.args[0])

    def test_existing_local_source_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "source.pdf"
            destination.write_bytes(b"original")
            with patch.object(cloud, "PRIVATE_ROOT", root), patch.object(cloud.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    cloud.download_id("synthetic_file_id", destination)
            run.assert_not_called()
            self.assertEqual(b"original", destination.read_bytes())

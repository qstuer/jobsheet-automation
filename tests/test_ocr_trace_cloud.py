"""Read-only cloud entry gates; never call rclone or any real API."""
import hashlib
import base64
import importlib.util
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

    def test_disposable_mode_reads_pdf_only_not_official_drive_index(self):
        if not importlib.util.find_spec("cryptography"):
            self.skipTest("Diagnostic-only crypto dependency not installed")
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        source = b"synthetic-private-pdf"
        environment = {"GITHUB_REF": "refs/heads/codex/parser-ingress-fix-20261013",
            "JOBSHEET_DRY_RUN": "1", "OCR_PROVIDER": "nvidia", "OCR_TRACE_INDEX_MODE": "local_rebuild",
            "OCR_TRACE_RECIPIENT_KEY": base64.b64encode(public).decode(),
            "OCR_TRACE_SOURCE_SHA256": hashlib.sha256(source).hexdigest(),
            "OCR_TRACE_SOURCE_ID": "synthetic_source_id", "OCR_TRACE_JOB_TYPE": "CM",
            "OCR_TRACE_REFERENCE_DATE": "2026-09-28"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def download(_file_id, destination):
                destination.write_bytes(source)
            with patch.dict(os.environ, environment), patch.object(cloud, "PRIVATE_ROOT", root), \
                 patch.object(cloud, "download_id", side_effect=download) as downloads, \
                 patch("src.readonly_test_index.build", return_value=(root / "test-index.json", root / "manifest.json")) as build, \
                 patch.object(cloud, "capture_main", return_value=0) as capture:
                self.assertEqual(0, cloud.main())
            downloads.assert_called_once()
            self.assertEqual("synthetic_source_id", downloads.call_args.args[0])
            build.assert_called_once()
            self.assertIn(str(root / "test-index.json"), capture.call_args.args[0])

    def test_invalid_mode_stops_before_any_download_or_model(self):
        with patch.dict(os.environ, {"GITHUB_REF": "refs/heads/codex/parser-ingress-fix-20261013",
            "JOBSHEET_DRY_RUN": "1", "OCR_PROVIDER": "nvidia", "OCR_TRACE_INDEX_MODE": "upload",
            "OCR_TRACE_SOURCE_SHA256": "a" * 64, "OCR_TRACE_RECIPIENT_KEY": "c3ludGhldGlj"}), \
             patch("src.private_trace_crypto.load_recipient"), patch.object(cloud, "download_id") as download, \
             patch.object(cloud, "capture_main") as capture, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(3, cloud.main())
        download.assert_not_called()
        capture.assert_not_called()

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

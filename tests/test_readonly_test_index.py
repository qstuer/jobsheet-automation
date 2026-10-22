"""Disposable index guards and encrypted readback, synthetic data only."""
import base64
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import readonly_test_index as rebuild, private_trace_crypto as crypto


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "Diagnostic-only crypto dependency not installed")
class DisposableIndexTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public = self.key.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        self.environment = patch.dict(os.environ, {"GITHUB_REF": rebuild.TEST_REF, "JOBSHEET_DRY_RUN": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_wrong_branch_or_write_mode_stops_before_asana(self):
        for values in ({"GITHUB_REF": "refs/heads/main"}, {"JOBSHEET_DRY_RUN": "0"}):
            with patch.dict(os.environ, values), patch.object(rebuild.asana_index, "build_from_asana") as build:
                with self.assertRaises(ValueError):
                    rebuild.build("unused", self.public)
            build.assert_not_called()

    def test_bad_public_key_stops_before_asana(self):
        with patch.object(rebuild.asana_index, "build_from_asana") as build:
            with self.assertRaises(ValueError):
                rebuild.build("unused", b"not-a-key")
        build.assert_not_called()

    def test_outside_or_existing_output_is_rejected_before_asana(self):
        with tempfile.TemporaryDirectory() as temporary:
            for destination in (Path(temporary), Path("onedrive:company/file")):
                with patch.object(rebuild.asana_index, "build_from_asana") as build:
                    with self.assertRaises((ValueError, FileExistsError)):
                        rebuild.build(destination, self.public)
                build.assert_not_called()

    def test_local_outputs_are_encrypted_with_recipient_only_no_cloud_transfer(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "index"
            def write_outputs(index, folder):
                folder.mkdir()
                for name in ("asana-device-index.json", "asana-device-index-manifest.json"):
                    (folder / name).write_bytes(b"synthetic-private-index")
            with patch.object(rebuild, "private_output", return_value=destination), \
                 patch.object(rebuild.asana_index, "build_from_asana", return_value={}) as build, \
                 patch.object(rebuild.asana_index, "write_outputs", side_effect=write_outputs), \
                 patch("src.rclone_helper.run_result", side_effect=AssertionError("No cloud transfer")):
                paths = rebuild.build(destination, self.public)
            build.assert_called_once_with()
            for path in paths:
                encrypted = path.with_suffix(path.suffix + ".enc").read_bytes()
                self.assertNotIn(b"synthetic-private-index", encrypted)
                self.assertEqual(path.read_bytes(), crypto.unseal(encrypted, self.key, path.name))

    def test_main_redacts_external_error_body_and_does_not_change_logging(self):
        import logging
        previous = logging.root.manager.disable
        with patch.dict(os.environ, {"OCR_TRACE_RECIPIENT_KEY": base64.b64encode(self.public).decode()}), \
             patch.object(rebuild, "build", side_effect=RuntimeError("PRIVATE_SENTINEL")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(3, rebuild.main())
        self.assertEqual(previous, logging.root.manager.disable)
        self.assertNotIn("PRIVATE_SENTINEL", output.getvalue())


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import backtest


class BacktestTests(unittest.TestCase):
    def _manifest(self, root: Path, *, duplicate=False):
        samples = []
        for number in range(1, 21):
            name = f"B{number:02d}.pdf"
            (root / name).write_bytes(b"same" if duplicate else f"pdf-{number}".encode())
            samples.append({
                "sample_id": f"B{number:02d}",
                "filename": name,
                "job_type": "PM",
                "expected": {"kind": "pending"},
            })
        path = root / "manifest.json"
        path.write_text(json.dumps({"schema_version": 1, "samples": samples}))
        return path

    def test_manifest_requires_exactly_twenty_anonymous_samples(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._manifest(Path(tmpdir))
            samples = backtest._load_manifest(path)
            self.assertEqual(20, len(samples))
            self.assertEqual("B01", samples[0]["sample_id"])

    def test_binary_duplicates_are_rejected_before_ocr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            samples = backtest._load_manifest(self._manifest(root, duplicate=True))
            with self.assertRaises(backtest.BacktestError):
                backtest._validate_files(samples, root)

    def test_expected_values_are_checked_without_public_output(self):
        self.assertTrue(backtest._matches_expected(None, {"kind": "pending"}))
        self.assertTrue(backtest._matches_expected(
            {"name": "Hospital / Model / SERIAL / 61932689"},
            {"kind": "order", "value": "61932689"},
        ))
        with patch("src.backtest.asana_client._task_serials", return_value=["USN16F0565"]):
            self.assertTrue(backtest._matches_expected(
                {"name": "private"},
                {"kind": "serial", "value": "USN16F0565"},
            ))

    def test_summary_contains_only_sample_ids_and_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "summary.md"
            rows = [{
                "sample_id": "B01", "status": "PASS", "expected_pending": False,
                "calls": 2, "seconds": 1.5, "tokens": 100, "cost": 0.01,
            }]
            backtest._append_summary(rows, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("B01", text)
            self.assertIn("OneDrive 写入：**0**", text)
            self.assertNotIn("USN16F0565", text)
            self.assertNotIn("61932689", text)


if __name__ == "__main__":
    unittest.main()

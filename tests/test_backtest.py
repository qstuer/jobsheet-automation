import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz

from src import backtest


class BacktestTests(unittest.TestCase):
    def test_main_rejects_cloud_file_operations(self):
        def attempt_write():
            backtest.rclone_helper.run_result("copyto", "local.pdf", "onedrive:forbidden")
        with patch("sys.argv", ["backtest"]), patch.object(backtest, "run", side_effect=attempt_write):
            self.assertEqual(1, backtest.main())

    def test_checkpoint_preserves_completed_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "results.json"
            with patch.dict(os.environ, {"JOBSHEET_BACKTEST_REPORT": str(target)}):
                backtest._checkpoint([{"sample_id": "B01", "status": "FAIL"}])
                self.assertEqual("B01", json.loads(target.read_text())[0]["sample_id"])
                backtest._checkpoint([{"sample_id": "B01", "status": "FAIL"},
                                      {"sample_id": "B02", "status": "PASS"}])
                self.assertEqual(2, len(json.loads(target.read_text())))

    def test_main_does_not_log_private_exception_text(self):
        with patch("sys.argv", ["backtest"]), \
             patch.object(backtest, "run", side_effect=RuntimeError("PRIVATE_CUSTOMER_VALUE")), \
             self.assertLogs("backtest", level="ERROR") as logs:
            self.assertEqual(1, backtest.main())
        self.assertNotIn("PRIVATE_CUSTOMER_VALUE", " ".join(logs.output))
        self.assertIn("RuntimeError", " ".join(logs.output))

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

    def test_reviewed_answer_is_bound_to_pdf_not_just_its_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            samples = backtest._load_manifest(self._manifest(root))
            samples[0]["source_sha256"] = hashlib.sha256((root / "B01.pdf").read_bytes()).hexdigest()
            backtest._validate_files(samples, root)
            (root / "B01.pdf").write_bytes(b"another scan with the same anonymous name")
            with self.assertRaisesRegex(backtest.BacktestError, "B01"):
                backtest._validate_files(samples, root)

    def test_invalid_source_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = self._manifest(root)
            payload = json.loads(path.read_text())
            payload["samples"][0]["source_sha256"] = "not-a-hash"
            path.write_text(json.dumps(payload))
            with self.assertRaises(backtest.BacktestError):
                backtest._load_manifest(path)

    def _task(self, **changes):
        task = {"gid": "101", "name": "TEST / EPIQ Elite / US123B4567 / 60000001",
                "memberships": [{"project": {"name": "PM"}}]}
        task.update(changes)
        return task

    def _sample(self, **changes):
        sample = {"sample_id": "B01", "filename": "B01.pdf", "job_type": "PM",
                  "_verified_answer": True, "review_status": "confirmed",
                  "review_note": "Synthetic independently reviewed answer",
                  "reference_date": "2026-08-20", "reference_date_source": "original_upload",
                  "expected": {"kind": "match", "serial": "US123B4567",
                               "task_gid": "101", "filename": "SR#60000001.pdf"}}
        sample.update(changes)
        return sample

    def test_legacy_values_never_prove_exact_task(self):
        self.assertTrue(backtest._matches_expected(None, {"kind": "pending"}))
        self.assertFalse(backtest._matches_expected(
            self._task(), {"kind": "order", "value": "60000001"},
        ))
        self.assertFalse(backtest._matches_expected(
            self._task(), {"kind": "serial", "value": "US123B4567"},
        ))

    def test_same_device_wrong_month_fails_even_with_same_filename(self):
        result = backtest._evaluate_result(self._task(gid="102"), self._sample(), 4)
        self.assertEqual("PASS", result["device_check"])
        self.assertEqual("PASS", result["filename_check"])
        self.assertEqual("FAIL", result["task_check"])
        self.assertEqual("FAIL", result["status"])

    def test_correct_task_wrong_filename_fails(self):
        sample = self._sample()
        sample["expected"]["filename"] = "SR#60000002.pdf"
        result = backtest._evaluate_result(self._task(), sample, 4)
        self.assertEqual("PASS", result["task_check"])
        self.assertEqual("FAIL", result["status"])

    def test_correct_task_wrong_device_fails(self):
        sample = self._sample()
        sample["expected"]["serial"] = "US123B4568"
        self.assertEqual("FAIL", backtest._evaluate_result(self._task(), sample, 4)["status"])

    def test_correct_match_reports_untested_stages_honestly(self):
        result = backtest._evaluate_result(self._task(), self._sample(), 4)
        self.assertEqual("PASS", result["status"])
        for key in ("circle_check", "field_accuracy", "full_pipeline_check"):
            self.assertEqual("NOT_TESTED", result[key])
        self.assertEqual("COUNT_ONLY", result["page_check"])

    def test_missing_pages_cannot_count_as_success(self):
        result = backtest._evaluate_result(self._task(), self._sample(), 3)
        self.assertEqual("PASS", result["task_check"])
        self.assertEqual("DIAGNOSTIC_ONLY", result["status"])

    def test_wrong_or_unknown_asana_type_fails(self):
        for memberships in ([], [{"project": {"name": "CM"}}]):
            result = backtest._evaluate_result(self._task(memberships=memberships), self._sample(), 4)
            self.assertEqual("FAIL", result["status"])

    def test_no_order_uses_exact_sanitized_title(self):
        task = self._task(name="TEST / EPIQ Elite / US123B4567")
        sample = self._sample()
        sample["expected"]["filename"] = "TEST - EPIQ Elite - US123B4567.pdf"
        self.assertTrue(backtest._matches_expected(task, sample["expected"]))

    def test_legacy_pending_is_unverified_not_a_free_pass(self):
        sample = self._sample(_verified_answer=False, expected={"kind": "pending"})
        self.assertEqual("UNVERIFIED", backtest._evaluate_result(None, sample, 4)["status"])

    def test_confirmed_pending_rejects_any_match(self):
        sample = self._sample(expected={"kind": "pending"})
        self.assertEqual("PASS", backtest._evaluate_result(None, sample, 4)["status"])
        self.assertEqual("FAIL", backtest._evaluate_result(self._task(), sample, 4)["status"])

    def test_bad_circle_cannot_pass_even_when_pending_is_expected(self):
        sample = self._sample(expected={"kind": "pending"})
        for detected in ("CM", "UNKNOWN", "FCO", "INS"):
            result = backtest._evaluate_result(None, sample, 4, detected)
            self.assertEqual("FAIL", result["circle_check"])
            self.assertEqual("FAIL", result["status"])
        self.assertEqual("PASS", backtest._evaluate_result(None, sample, 4, "PM")["circle_check"])

    def test_unreadable_or_wrong_circle_stops_matching_without_answer_injection(self):
        doc = object()
        for detected in ("CM", "UNKNOWN", "FCO", "INS"):
            with patch.object(backtest.nvidia_client, "detect_cm_pm", return_value=detected) as read, \
                 patch.object(backtest.processor, "_ocr_and_match") as match:
                task, actual, _ = backtest._read_sample(doc, "PM")
            read.assert_called_once_with(doc, 0)
            match.assert_not_called()
            self.assertIsNone(task)
            self.assertEqual(detected, actual)

    def test_matching_uses_detected_type_and_counts_circle_usage(self):
        doc = object()
        for detected in ("PM", "CM"):
            with patch.object(backtest.nvidia_client, "detect_cm_pm", return_value=detected) as read, \
                 patch.object(backtest.nvidia_client, "get_ocr_metrics", return_value={
                     "calls": 2, "seconds": 1.5, "total_tokens": 10,
                     "estimated_cost_cny_upper": 0.01}), \
                 patch.object(backtest.processor, "_ocr_and_match", return_value=(
                     self._task(), 1, {"ocr_metrics": {
                         "calls": 3, "seconds": 2.5, "total_tokens": 20,
                         "estimated_cost_cny_upper": 0.02}})) as match:
                _, actual, metrics = backtest._read_sample(doc, detected)
            read.assert_called_once_with(doc, 0)
            match.assert_called_once_with(doc, detected)
            self.assertEqual(detected, actual)
            self.assertEqual(5, metrics["calls"])
            self.assertEqual(4, metrics["seconds"])
            self.assertEqual(30, metrics["total_tokens"])
            self.assertAlmostEqual(0.03, metrics["estimated_cost_cny_upper"])

    def test_v2_requires_independent_complete_answers_and_original_date_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._manifest(Path(tmpdir))
            payload = json.loads(path.read_text())
            payload["schema_version"] = 2
            payload["samples"] = [self._sample(sample_id=f"B{i:02d}", filename=f"B{i:02d}.pdf")
                                  for i in range(1, 21)]
            path.write_text(json.dumps(payload))
            self.assertTrue(backtest._load_manifest(path)[0]["_verified_answer"])
            for key in ("task_gid", "filename", "serial"):
                bad = json.loads(json.dumps(payload))
                del bad["samples"][0]["expected"][key]
                path.write_text(json.dumps(bad))
                with self.assertRaises(backtest.BacktestError):
                    backtest._load_manifest(path)
            for key, value in (("reference_date", "not-a-date"),
                               ("reference_date_source", "rerun_today"),
                               ("review_note", "")):
                bad = json.loads(json.dumps(payload))
                bad["samples"][0][key] = value
                path.write_text(json.dumps(bad))
                with self.assertRaises(backtest.BacktestError):
                    backtest._load_manifest(path)

    def test_v2_unknown_dates_stay_unknown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._manifest(Path(tmpdir))
            payload = json.loads(path.read_text())
            payload["schema_version"] = 2
            for sample in payload["samples"]:
                sample.update(review_status="unreviewed", reference_date=None)
            path.write_text(json.dumps(payload))
            sample = backtest._load_manifest(path)[0]
            self.assertIsNone(sample["reference_date"])
            self.assertFalse(sample["_verified_answer"])

    def test_invalid_ids_and_path_separators_rejected_on_all_platforms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._manifest(Path(tmpdir))
            original = json.loads(path.read_text())
            for key, value in (("sample_id", "B99"), ("filename", "..\\private.pdf"),
                               ("filename", "../private.pdf")):
                payload = json.loads(json.dumps(original))
                payload["samples"][0][key] = value
                path.write_text(json.dumps(payload))
                with self.assertRaises(backtest.BacktestError):
                    backtest._load_manifest(path)

    def test_offline_audit_detects_same_work_and_does_not_contact_services(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            samples = [self._sample(), self._sample(sample_id="B02", filename="B02.pdf")]
            for i, sample in enumerate(samples, 1):
                with fitz.open() as doc:
                    for _ in range(i):
                        doc.new_page()
                    doc.save(root / sample["filename"])
            with patch("src.backtest.processor._ocr_and_match") as match:
                rows = backtest.audit_fixtures(samples, root)
            match.assert_not_called()
            self.assertEqual("B01", rows[1]["same_work_as"])
            self.assertEqual(2, rows[1]["pages"])
            self.assertNotIn("task_gid", json.dumps(rows))
            self.assertNotIn("US123B4567", json.dumps(rows))

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

    def test_run_never_calls_upload_move_delete_or_full_processor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._manifest(root)
            fake_doc = MagicMock()
            fake_doc.__enter__.return_value.page_count = 4
            with patch.dict(os.environ, {backtest.BACKTEST_DIR_ENV: str(root),
                                         backtest.BACKTEST_MANIFEST_ENV: str(manifest),
                                         "GITHUB_STEP_SUMMARY": ""}), \
                 patch("src.backtest.asana_index.load_index", return_value={}), \
                 patch("src.backtest.asana_client.set_device_index"), \
                 patch("src.backtest.fitz.open", return_value=fake_doc), \
                 patch("src.backtest.nvidia_client.detect_cm_pm", return_value="PM"), \
                 patch("src.backtest.processor._ocr_and_match", return_value=(None, None, {})), \
                 patch("src.processor._process_split_file") as process, \
                 patch("src.processor._finalize_match") as upload, \
                 patch("src.processor.rclone_helper") as cloud:
                rows = backtest.run()
            self.assertEqual(20, len(rows))
            self.assertTrue(all(row["status"] == "UNVERIFIED" for row in rows))
            process.assert_not_called()
            upload.assert_not_called()
            self.assertEqual([], cloud.mock_calls)


if __name__ == "__main__":
    unittest.main()

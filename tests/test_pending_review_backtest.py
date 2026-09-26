"""The private trial never supplies answers to vision or writes cloud data."""

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src import backtest, pending_review_backtest


class PendingReviewBacktestTests(unittest.TestCase):
    def _fixture(self, folder: Path):
        samples = []
        for sample_id in pending_review_backtest.SAMPLE_IDS:
            source = folder / f"{sample_id}.pdf"
            source.write_bytes(f"synthetic-{sample_id}".encode())
            samples.append({
                "sample_id": sample_id, "filename": source.name,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "job_type": "PM", "review_status": "confirmed",
                "observed_fields": {
                    "service_date_raw": "2026-08-18", "date_source": "ACTION_DATE",
                },
                "expected": {"kind": "match", "task_gid": "12345678"},
            })
        return samples

    def test_date_only_then_one_explicit_visit_and_anonymous_report(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            samples = self._fixture(folder)
            report = folder / "anonymous.json"
            env = {
                "JOBSHEET_BACKTEST_DIR": temp,
                "JOBSHEET_BACKTEST_MANIFEST": str(folder / "manifest.json"),
                "JOBSHEET_REVIEW_REPORT": str(report),
                "ASANA_INDEX_LOCAL_FILE": str(folder / "index.json"),
                "ASANA_INDEX_MANIFEST_LOCAL_FILE": str(folder / "index-manifest.json"),
            }
            reviewed = []

            def review(ocr, kind, day, selected_task=""):
                reviewed.append((ocr["sample"], kind, day, selected_task))
                if ocr["sample"] == "B01" and not selected_task:
                    return {"status": "PENDING", "reason": "visit_ambiguous"}
                return {"status": "READY_READ_ONLY", "reason": "all_checks_passed",
                        "task": {"gid": "12345678"}}

            with patch.dict(os.environ, env, clear=True), \
                    patch.object(backtest, "_load_manifest", return_value=samples), \
                    patch.object(pending_review_backtest.asana_index, "load_index",
                                 return_value={"devices": []}), \
                    patch.object(pending_review_backtest.fitz, "open") as open_pdf, \
                    patch.object(pending_review_backtest.nvidia_client, "detect_cm_pm",
                                 return_value="PM"), \
                    patch.object(pending_review_backtest.nvidia_client, "get_ocr_metrics",
                                 return_value={"calls": 1, "seconds": 0, "total_tokens": 0}), \
                    patch.object(pending_review_backtest.nvidia_client, "reset_ocr_metrics"), \
                    patch.object(pending_review_backtest.nvidia_client,
                                 "reset_model_availability"), \
                    patch.object(pending_review_backtest.processor,
                                 "_ocr_for_pending_review", side_effect=[
                                     {"sample": sample_id, "ocr_metrics": {"calls": 2},
                                      "_ocr_audit": {"readings": [{
                                          "context": {"stage": "primary"},
                                          "normalized": {"phone_candidates": ["55559999"]},
                                          "raw": {"hospital_raw": "SENSITIVE_HOSPITAL"},
                                      }]}}
                                     for sample_id in pending_review_backtest.SAMPLE_IDS
                                 ]), \
                    patch.object(pending_review_backtest.pending_review,
                                 "review_ocr", side_effect=review), \
                    patch.object(backtest, "_matches_expected", return_value=True):
                open_pdf.return_value.__enter__.return_value.page_count = 4
                rows = pending_review_backtest.run()
            self.assertEqual(["PASS_ASSISTED", "PASS_DATE_ONLY", "PASS_DATE_ONLY",
                              "PASS_DATE_ONLY"], [row["status"] for row in rows])
            self.assertEqual(("B01", "PM", "2026-08-18", ""), reviewed[0])
            self.assertEqual(("B01", "PM", "2026-08-18", "12345678"), reviewed[1])
            self.assertTrue(all(item[3] == "" for item in reviewed[2:]))
            public_rows = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(set(public_rows[0]), {
                "sample_id", "status", "reason", "task_choice_supplied",
                "calls", "seconds", "tokens", "cost_cny_upper",
                "identity_diagnostic",
            })
            self.assertNotIn("serial", public_rows[0]["identity_diagnostic"])
            self.assertEqual("primary", public_rows[0]["identity_diagnostic"]
                             ["read_passes"][0]["stage"])
            self.assertNotIn("SENSITIVE_HOSPITAL", report.read_text(encoding="utf-8"))
            self.assertNotIn("55559999", report.read_text(encoding="utf-8"))

    def test_source_change_stops_before_any_model_call(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            samples = self._fixture(folder)
            (folder / "B04.pdf").write_bytes(b"altered")
            env = {"JOBSHEET_BACKTEST_DIR": temp}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(backtest, "_load_manifest", return_value=samples), \
                    patch.object(pending_review_backtest.asana_index, "load_index",
                                 return_value={"devices": []}), \
                    patch.object(pending_review_backtest.processor,
                                 "_ocr_for_pending_review") as ocr:
                with self.assertRaises(backtest.BacktestError):
                    pending_review_backtest.run()
            ocr.assert_not_called()


if __name__ == "__main__":
    unittest.main()

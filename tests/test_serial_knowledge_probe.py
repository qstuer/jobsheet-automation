"""The held-out serial comparison cannot publish customer identifiers."""

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src import backtest, serial_knowledge_probe


class SerialKnowledgeProbeTests(unittest.TestCase):
    def _fixtures(self, folder):
        samples = []
        for sample_id in serial_knowledge_probe.SAMPLE_IDS:
            pdf = folder / f"{sample_id}.pdf"
            pdf.write_bytes(f"private-{sample_id}".encode())
            samples.append({
                "sample_id": sample_id, "filename": pdf.name,
                "source_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                "job_type": "PM", "review_status": "confirmed",
                "expected": {"kind": "match", "serial": "US123B4567",
                             "task_gid": "12345678"},
            })
        index = {"devices": [{
            "serial": "US123B4567", "product_families": ["EPIQ"],
            "hospital_aliases": ["DEMOHOSPITAL"],
            "task_refs": [{"gid": "12345678", "assets": ["98765432"]}],
        }]}
        reading = {
            "transcription": {
                "serial_candidates": ["US123B4567"],
                "product_raw": "EPIQ Elite", "hospital_raw": "Demo Hospital",
                "phone_candidates": ["55667788"],
            },
            "assisted": None, "relation": "consistent",
        }
        arm = {"status": "read", "reading": reading,
               "metrics": {"calls": 1, "total_tokens": 123,
                           "estimated_cost_cny_upper": 0.002}}
        comparison = {"guided_first": False,
                      "arms": {"baseline": arm, "guided": arm}}
        return samples, index, comparison

    def test_held_out_answer_never_reaches_model_or_report(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            samples, index, comparison = self._fixtures(folder)
            report = folder / "anonymous.json"
            env = {"JOBSHEET_BACKTEST_DIR": temp,
                   "JOBSHEET_SERIAL_REPORT": str(report)}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(backtest, "_load_manifest", return_value=samples), \
                    patch.object(serial_knowledge_probe.asana_index, "load_index",
                                 return_value=index), \
                    patch.object(serial_knowledge_probe.vision_knowledge,
                                 "build_knowledge", return_value={"knowledge_version": 1}) as build, \
                    patch.object(serial_knowledge_probe.vision_knowledge,
                                 "prompt_reference", return_value={"known_products": []}), \
                    patch.object(serial_knowledge_probe.vision_experiment,
                                 "compare", return_value=comparison) as compare, \
                    patch.object(serial_knowledge_probe.fitz, "open") as open_pdf, \
                    patch.object(serial_knowledge_probe, "_probe_asset",
                                 return_value={"independent_exact_pair": False}):
                open_pdf.return_value.__enter__.return_value.page_count = 4
                rows = serial_knowledge_probe.run()
            self.assertEqual(2, len(rows))
            self.assertEqual(("US123B4567",), build.call_args.kwargs["excluded_serials"])
            self.assertNotIn("US123B4567", str(compare.call_args.args[1]))
            contents = report.read_text(encoding="utf-8")
            for sensitive in ("US123B4567", "55667788", "98765432", "12345678",
                              "Demo Hospital"):
                self.assertNotIn(sensitive, contents)
            self.assertTrue(json.loads(contents)[0]["baseline"]
                            ["transcription"]["serial_exact"])

    def test_source_hash_mismatch_stops_before_any_model_call(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            samples, index, _ = self._fixtures(folder)
            (folder / "B10.pdf").write_bytes(b"different")
            with patch.dict(os.environ, {"JOBSHEET_BACKTEST_DIR": temp}, clear=True), \
                    patch.object(backtest, "_load_manifest", return_value=samples), \
                    patch.object(serial_knowledge_probe.asana_index, "load_index",
                                 return_value=index), \
                    patch.object(serial_knowledge_probe.vision_knowledge,
                                 "build_knowledge", return_value={"knowledge_version": 1}), \
                    patch.object(serial_knowledge_probe.vision_knowledge,
                                 "prompt_reference", return_value={"known_products": []}), \
                    patch.object(serial_knowledge_probe.vision_experiment,
                                 "compare") as compare:
                with self.assertRaises(backtest.BacktestError):
                    serial_knowledge_probe.run()
            compare.assert_not_called()


if __name__ == "__main__":
    unittest.main()

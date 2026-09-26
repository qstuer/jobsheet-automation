"""Synthetic checks for the anonymous date-only fixture probe."""
import hashlib
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import MagicMock, patch

from src import date_field_probe


class DateFieldProbeTests(TestCase):
    def setUp(self):
        self.sample = {
            "sample_id": "B01",
            "filename": "B01.pdf",
            "review_status": "confirmed",
            "observed_fields": {
                "date_source": "ACTION_DATE",
                "service_date_raw": "2031-04-12",
            },
        }

    def test_reviewed_date_must_be_confirmed_action_date(self):
        self.assertEqual(date_field_probe._reviewed_day(self.sample), date(2031, 4, 12))
        self.sample["review_status"] = "unreviewed"
        with self.assertRaises(ValueError):
            date_field_probe._reviewed_day(self.sample)

    def test_anonymous_result_distinguishes_correct_wrong_and_unreadable(self):
        doc = MagicMock()
        doc.__enter__.return_value = doc
        doc.page_count = 4
        reads = iter([
            date(2031, 4, 12), date(2031, 4, 13),
            date(2031, 4, 12), date(2031, 4, 12),
            None, None,
        ])
        with (patch.object(date_field_probe.fitz, "open", return_value=doc),
              patch.object(date_field_probe, "_read_date", side_effect=lambda *_: next(reads)),
              patch.object(date_field_probe.nvidia_client, "reset_ocr_metrics"),
              patch.object(date_field_probe.nvidia_client, "reset_model_availability"),
              patch.object(date_field_probe.nvidia_client, "get_ocr_metrics", return_value={
                  "calls": 6, "seconds": 2.5, "total_tokens": 120,
                  "estimated_cost_cny_upper": 0.01,
              })):
            result = date_field_probe.probe_sample(self.sample, Path("unused"))
        self.assertEqual(result["fields"]["service_date_raw"],
                         ["CORRECT", "OTHER_VALID_DATE"])
        self.assertEqual(result["fields"]["engineer_signed_date"],
                         ["CORRECT", "CORRECT"])
        self.assertEqual(result["fields"]["customer_signed_date"],
                         ["UNREADABLE", "UNREADABLE"])
        self.assertNotIn("2031", str(result))
        self.assertNotIn("expected", result)

    def test_joint_majority_requires_two_fields_and_two_renderings(self):
        truth = date(2031, 4, 12)
        wrong = date(2031, 5, 12)
        self.assertEqual(date_field_probe._majority_date({
            "a": truth, "b": truth, "c": wrong,
        }), truth)
        self.assertIsNone(date_field_probe._majority_date({
            "a": truth, "b": wrong, "c": None,
        }))
        doc = MagicMock()
        doc.__enter__.return_value = doc
        doc.page_count = 4
        reads = iter([
            {"service_date_raw": truth, "engineer_signed_date": truth,
             "customer_signed_date": wrong},
            {"service_date_raw": wrong, "engineer_signed_date": wrong,
             "customer_signed_date": truth},
        ])
        with (patch.object(date_field_probe.fitz, "open", return_value=doc),
              patch.object(date_field_probe, "_read_joint_dates", side_effect=lambda *_: next(reads)),
              patch.object(date_field_probe.nvidia_client, "reset_ocr_metrics"),
              patch.object(date_field_probe.nvidia_client, "reset_model_availability"),
              patch.object(date_field_probe.nvidia_client, "get_ocr_metrics", return_value={
                  "calls": 2, "seconds": 1.0, "total_tokens": 100,
              })):
            result = date_field_probe.probe_sample_joint(self.sample, Path("unused"))
        self.assertEqual(result["two_render_majority"], "UNRESOLVED")
        self.assertEqual(result["joint_components"]["service_date_raw"][0],
                         {"day": True, "month": True, "year": True})
        self.assertNotIn("2031", str(result))

    def test_only_selected_files_need_fingerprints(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / "B01.pdf"
            path.write_bytes(b"synthetic fixture")
            sample = dict(self.sample, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            date_field_probe._validate_selected_files([sample], directory)
            path.write_bytes(b"changed fixture")
            with self.assertRaises(ValueError):
                date_field_probe._validate_selected_files([sample], directory)

    def test_cross_provider_requires_both_models_to_agree_without_fallback(self):
        truth = date(2031, 4, 12)
        wrong = date(2031, 5, 12)
        doc = MagicMock()
        doc.__enter__.return_value = doc
        doc.page_count = 4
        seen = []

        def read_date(_doc, _field, _zoom):
            seen.append((date_field_probe.config.OCR_PROVIDER,
                         date_field_probe.config.NVIDIA_MODEL,
                         date_field_probe.config.NVIDIA_FALLBACK_MODEL))
            return truth if date_field_probe.config.OCR_PROVIDER == "deepseek" else wrong

        with (patch.object(date_field_probe.fitz, "open", return_value=doc),
              patch.object(date_field_probe, "_read_date", side_effect=read_date),
              patch.object(date_field_probe.nvidia_client, "reset_ocr_metrics"),
              patch.object(date_field_probe.nvidia_client, "reset_model_availability"),
              patch.object(date_field_probe.nvidia_client, "get_ocr_metrics", return_value={
                  "calls": 2, "seconds": 1.0, "total_tokens": 100,
              })):
            result = date_field_probe.probe_sample_cross_provider(
                self.sample, Path("unused")
            )
        self.assertEqual(result["cross_provider_agreement"], "UNRESOLVED")
        self.assertEqual(result["reader_statuses"]["deepseek"],
                         ["CORRECT", "CORRECT"])
        self.assertEqual(result["reader_statuses"]["nvidia_nemotron"],
                         ["OTHER_VALID_DATE", "OTHER_VALID_DATE"])
        self.assertEqual(result["reader_statuses"]["nvidia_llama"],
                         ["OTHER_VALID_DATE", "OTHER_VALID_DATE"])
        self.assertEqual([provider for provider, _, _ in seen],
                         ["deepseek", "deepseek", "nvidia", "nvidia",
                          "nvidia", "nvidia"])
        self.assertTrue(all(fallback == "" for _, _, fallback in seen))
        self.assertNotEqual(seen[2][1], seen[4][1])
        self.assertNotIn("2031", str(result))

    def test_cross_provider_agreement_is_still_scored_against_private_truth(self):
        wrong = date(2031, 5, 12)
        doc = MagicMock()
        doc.__enter__.return_value = doc
        doc.page_count = 4
        with (patch.object(date_field_probe.fitz, "open", return_value=doc),
              patch.object(date_field_probe, "_read_date", return_value=wrong),
              patch.object(date_field_probe.nvidia_client, "reset_ocr_metrics"),
              patch.object(date_field_probe.nvidia_client, "reset_model_availability"),
              patch.object(date_field_probe.nvidia_client, "get_ocr_metrics", return_value={
                  "calls": 2, "seconds": 1.0, "total_tokens": 100,
              })):
            result = date_field_probe.probe_sample_cross_provider(
                self.sample, Path("unused")
            )
        self.assertEqual(result["cross_provider_agreement"], "OTHER_VALID_DATE")
        self.assertNotIn("2031", str(result))


if __name__ == "__main__":
    main()

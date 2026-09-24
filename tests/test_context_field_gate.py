"""Opt-in OCR gate tests with deliberately fictitious field values."""
import unittest
from unittest.mock import patch

from src import asana_client, nvidia_client, processor


class ContextFieldGateTests(unittest.TestCase):
    def setUp(self):
        self.broad = {
            "serial_candidates": ["TEST_SERIAL"],
            "product_raw": "Affiniti 30",
            "hospital_raw": "Example Medical Centre",
        }
        self.vocabulary = {
            "product_families": ["TESTFAMILY"],
            "product_models": ["TESTMODEL"],
            "hospital_codes": ["ZZ"],
            "same_hospital_codes": [],
        }

    def test_two_focused_votes_replace_broad_values(self):
        focused = [
            {"product_raw": "EPIQ 5G"}, {"product_raw": "EPIQ 5G"},
            {"hospital_raw": "Sample Medical Centre"},
            {"hospital_raw": "Sample Medical Centre"},
        ]
        with patch.dict(processor.os.environ, {processor.CONTEXT_FIELD_OCR_ENV: "1"}), \
             patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=self.broad), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=self.broad), \
             patch.object(processor.private_ocr_context, "build_vocabulary",
                          return_value=self.vocabulary), \
             patch.object(nvidia_client, "ocr_jobsheet_context_field", side_effect=focused) as reader, \
             patch.object(asana_client, "find_task", return_value=({"gid": "TEST_TASK"}, 2)) as find:
            task, _, result = processor._ocr_and_match(None, "PM")
        self.assertEqual("TEST_TASK", task["gid"])
        self.assertEqual("EPIQ 5G", result["product_raw"])
        self.assertEqual("Sample Medical Centre", result["hospital_raw"])
        self.assertEqual(4, reader.call_count)
        self.assertEqual("Sample Medical Centre", find.call_args.args[0]["hospital_raw"])

    def test_disagreement_stops_before_matching(self):
        focused = [
            {"product_raw": "EPIQ 5G"}, {"product_raw": "Affiniti 30"},
            {"hospital_raw": "Sample Medical Centre"},
            {"hospital_raw": "Sample Medical Centre"},
        ]
        with patch.dict(processor.os.environ, {processor.CONTEXT_FIELD_OCR_ENV: "1"}), \
             patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=self.broad), \
             patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=self.broad), \
             patch.object(processor.private_ocr_context, "build_vocabulary",
                          return_value=self.vocabulary), \
             patch.object(nvidia_client, "ocr_jobsheet_context_field", side_effect=focused), \
             patch.object(asana_client, "find_task") as find:
            task, _, result = processor._ocr_and_match(None, "PM")
        self.assertIsNone(task)
        self.assertIsNone(result["product_raw"])
        find.assert_not_called()

    def test_prompt_uses_only_supplied_vocabulary(self):
        prompt = nvidia_client._context_field_prompt("hospital_raw", self.vocabulary)
        self.assertIn("ZZ", prompt)
        self.assertNotIn("Example Medical Centre", prompt)
        self.assertIn("NOT a list to choose", prompt)


if __name__ == "__main__":
    unittest.main()

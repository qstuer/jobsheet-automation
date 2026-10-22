"""Synthetic capture checks; no keys, customer files or external requests."""
import base64
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from src import matching_rules, nvidia_client as vision, private_ocr_trace as trace


class PrivateOCRTraceTests(unittest.TestCase):
    def setUp(self):
        # These tests use synthetic local fixtures, including on a CI runner.
        # The explicit cloud-plaintext refusal test overrides this to true.
        self.environment = patch.dict("os.environ", {"GITHUB_ACTIONS": "false"})
        self.environment.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "private"
        self.output = self.root / "run1"
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(buffer, format="JPEG")
        self.image = buffer.getvalue()
        self.b64 = base64.b64encode(self.image).decode("ascii")
        vision.reset_model_availability()

    def tearDown(self):
        self.temporary.cleanup()
        self.environment.stop()

    def capture(self):
        return trace.Capture(self.output, root=self.root)

    def events(self):
        return [json.loads(line) for line in (self.output / "events.private.jsonl").read_text(encoding="utf-8").splitlines()]

    def client(self, content, finish="stop"):
        create = Mock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15)))
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def call(self):
        return vision._call_vision_once("Transcribe only", self.b64,
                                      "meta/llama-3.2-11b-vision-instruct", 300, True)

    def test_exact_response_and_actual_image_not_trimmed_or_regenerated(self):
        raw = '  \n{"product_raw":"Affiniti"}\n  '
        client = self.client(raw)
        with patch.object(vision, "get_client", return_value=client):
            original = vision.get_client
            with self.capture():
                self.assertEqual(raw.strip(), self.call())
            self.assertIs(original, vision.get_client)
        response = next(row for row in self.events() if row["event"] == "response")
        self.assertEqual(raw, response["content"])
        self.assertEqual(15, response["usage"]["total_tokens"])
        request = self.events()[0]
        self.assertEqual(self.image, (self.output / request["images"][0]["file"]).read_bytes())
        client.chat.completions.create.assert_called_once()

    def test_raw_rejected_value_and_normalized_reason_are_separate(self):
        with self.capture(), patch.object(vision, "_today", return_value=date(2026, 10, 13)):
            result = vision._normalize_ocr_data({"serial_candidates": ["BOY8KL"],
                "phone_candidates": ["123"], "hospital_raw": "PN"})
        saved = next(row["value"] for row in self.events() if row["event"] == "_normalize_ocr_data")
        self.assertEqual(["BOY8KL"], saved["_ocr_audit"]["raw"]["serial_candidates"])
        self.assertEqual([], result["serial_candidates"])
        self.assertEqual([], saved["phone_candidates"])
        self.assertIn("invalid_phone_length", saved["_ocr_audit"]["rejections"]["phone_candidates"])

    def test_malformed_json_saved_even_when_parser_raises(self):
        raw = '{"serial_candidates":['
        with patch.object(vision, "get_client", return_value=self.client(raw, "length")):
            with self.capture():
                reply = self.call()
                with self.assertRaises(vision.NvidiaResponseError):
                    vision._parse_json_object(reply)
        self.assertEqual(raw, next(row["content"] for row in self.events() if row["event"] == "response"))
        self.assertTrue(any(row["event"] == "_parse_json_object_error" for row in self.events()))

    def test_error_status_saved_without_secret_or_exception_body(self):
        error = RuntimeError("secret-key-sentinel and private response body")
        error.status_code = 503
        client = self.client(None)
        client.chat.completions.create.side_effect = error
        with patch.object(vision, "get_client", return_value=client), self.capture():
            with self.assertRaises(RuntimeError):
                self.call()
        events = self.events()
        self.assertEqual(503, next(row["status_code"] for row in events if row["event"] == "request_error"))
        self.assertNotIn("secret-key-sentinel", json.dumps(events))

    def test_no_overwrite(self):
        self.output.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            self.capture()

    def test_rejects_outside_private_root_and_root_itself(self):
        for path in (self.root, Path(self.temporary.name) / "outside"):
            with self.assertRaises(ValueError):
                trace.Capture(path, root=self.root)

    def test_cloud_plaintext_capture_is_rejected_before_files_created(self):
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}):
            with self.assertRaises(ValueError):
                self.capture()
        self.assertFalse(self.output.exists())

    def test_reader_context_and_result_are_not_changed(self):
        result = {"serial_candidates": [], "hospital_raw": None}
        def reader(document, page_idx, field, zoom=5.0):
            return result
        with self.capture() as capture:
            self.assertIs(result, capture.wrap_reader(reader)(None, 1, "hospital_raw", zoom=6.0))
        saved = self.events()[0]
        self.assertEqual({"reader": "reader", "page_idx": 1, "field": "hospital_raw", "zoom": 6.0}, saved["context"])
        self.assertEqual(result, saved["value"])

    def test_signature_date_reader_has_its_own_source_context(self):
        with patch.object(vision, "_today", return_value=date(2026, 9, 30)), \
             patch.object(vision, "crop_jobsheet_field_card", return_value=self.b64), \
             patch.object(vision, "_call_vision", return_value='{"signed_date":"28/9/2026"}'):
            with self.capture():
                result = vision.ocr_jobsheet_signature_date(None, 0, "engineer_signed_date", zoom=6.0)
        saved = next(row for row in self.events() if row["event"] == "reading")
        self.assertEqual("2026-09-28", result)
        self.assertEqual({"reader": "ocr_jobsheet_signature_date", "page_idx": 0,
                          "field": "engineer_signed_date", "zoom": 6.0}, saved["context"])
        self.assertEqual(result, saved["value"])

    def test_patches_restored_after_failure_and_existing_evidence_preserved(self):
        original = vision._normalize_ocr_data
        with self.assertRaises(RuntimeError):
            with self.capture() as capture:
                capture.emit("before_failure")
                raise RuntimeError("synthetic")
        self.assertIs(original, vision._normalize_ocr_data)
        self.assertEqual("before_failure", self.events()[0]["event"])

    def test_recording_does_not_change_consensus_votes_or_emit_stdout(self):
        readings = [{"serial_candidates": ["ABC12X3456"], "product_raw": "Affiniti 70"}] * 2
        expected = matching_rules.evidence(readings)
        with patch("sys.stdout", new_callable=io.StringIO) as output, self.capture():
            self.assertEqual(expected, matching_rules.evidence(readings))
            self.assertEqual("", output.getvalue())
        saved = self.events()[0]
        self.assertEqual(2, saved["value"]["serial_candidates"]["ABC12X3456"]["votes"])

    def test_streaming_is_rejected_without_api_call(self):
        client = self.client("unused")
        with self.capture() as capture:
            with self.assertRaises(ValueError):
                capture.client_proxy(client).chat.completions.create(stream=True)
        client.chat.completions.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""外部服務故障不得被誤認成單據內容問題。"""
import importlib.util
import sys
import unittest
from unittest.mock import MagicMock, patch

if "fitz" not in sys.modules and importlib.util.find_spec("fitz") is None:
    sys.modules["fitz"] = MagicMock()

import requests

from src import asana_client, nvidia_client


class AsanaFailureTests(unittest.TestCase):
    def setUp(self):
        asana_client._typeahead_cache.clear()

    def test_auth_failure_raises_instead_of_returning_no_match(self):
        response = MagicMock(status_code=401, headers={})
        response.raise_for_status.side_effect = requests.HTTPError("unauthorized")
        with patch.object(asana_client.config, "ASANA_TOKEN", "test-token"), \
                patch.object(asana_client.requests, "get", return_value=response):
            with self.assertRaises(asana_client.AsanaError):
                asana_client._typeahead("HKCH")
        self.assertNotIn("HKCH", asana_client._typeahead_cache)

    def test_rate_limit_waits_then_retries(self):
        limited = MagicMock(status_code=429, headers={"Retry-After": "7"})
        ok = MagicMock(status_code=200, headers={})
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"data": [{"gid": "1", "name": "task"}]}
        with patch.object(asana_client.config, "ASANA_TOKEN", "test-token"), \
                patch.object(asana_client.requests, "get", side_effect=[limited, ok]), \
                patch.object(asana_client.time, "sleep") as sleep:
            tasks = asana_client._typeahead("QEH")
        self.assertEqual(tasks[0]["gid"], "1")
        sleep.assert_called_once_with(7.0)

    def test_real_empty_result_is_cached(self):
        ok = MagicMock(status_code=200, headers={})
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"data": []}
        with patch.object(asana_client.config, "ASANA_TOKEN", "test-token"), \
                patch.object(asana_client.requests, "get", return_value=ok):
            self.assertEqual(asana_client._typeahead("not-there"), [])
        self.assertIn("not-there", asana_client._typeahead_cache)


class NvidiaResponseTests(unittest.TestCase):
    def test_configuration_error_is_not_retried(self):
        with patch.object(
                nvidia_client, "get_client",
                side_effect=RuntimeError("NVIDIA_API_KEY 未設定")) as get_client:
            with self.assertRaisesRegex(RuntimeError, "未設定"):
                nvidia_client._call_vision("prompt", "image")
        get_client.assert_called_once()

    def test_multiple_job_type_words_are_rejected(self):
        with patch.object(
                nvidia_client, "_call_vision",
                return_value="The choices are CM, PM, FCO and INS"):
            self.assertEqual(nvidia_client._read_job_nature("image"), "UNKNOWN")

    def test_single_job_type_word_is_accepted(self):
        with patch.object(nvidia_client, "_call_vision", return_value="PM"):
            self.assertEqual(nvidia_client._read_job_nature("image"), "PM")

    def test_invalid_ocr_json_raises_instead_of_becoming_blank_fields(self):
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value="not json") as call:
            with self.assertRaises(nvidia_client.NvidiaResponseError):
                nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)
        self.assertEqual(call.call_count, 2)

    def test_wrapped_ocr_json_is_accepted(self):
        wrapped = (
            "Here is the requested result:\n```json\n"
            '{"order_no":"", "serial_no":"US123", '
            '"product":"Affiniti 70", "customer":"PYNEH"}\n```'
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=wrapped):
            result = nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

        self.assertIsNone(result["order_no"])
        self.assertEqual(result["serial_no"], "US123")

    def test_unrelated_json_before_ocr_json_is_skipped(self):
        wrapped = (
            'Example: {"status":"ok"}\nActual: '
            '{"order_no":null,"serial_no":"US123","product":"CX50",'
            '"customer":"HKCH"} trailing words'
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=wrapped):
            result = nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

        self.assertEqual(result["serial_no"], "US123")

    def test_non_text_ocr_field_is_rejected(self):
        invalid = (
            '{"order_no":12345678,"serial_no":"US123",'
            '"product":"CX50","customer":"HKCH"}'
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=invalid) as call:
            with self.assertRaises(nvidia_client.NvidiaResponseError):
                nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)
        self.assertEqual(call.call_count, 2)

    def test_json_list_is_rejected_even_if_it_contains_an_object(self):
        invalid = (
            '[{"order_no":null,"serial_no":"US123",'
            '"product":"CX50","customer":"HKCH"}]'
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=invalid):
            with self.assertRaises(nvidia_client.NvidiaResponseError):
                nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

    def test_unexpected_ocr_fields_are_not_returned(self):
        response = (
            '{"order_no":null,"serial_no":"US123","product":"CX50",'
            '"customer":"HKCH","notes":"must not reach logs"}'
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=response):
            result = nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

        self.assertEqual(set(result), {"order_no", "serial_no", "product", "customer"})


if __name__ == "__main__":
    unittest.main()

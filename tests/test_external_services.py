"""外部服務故障不得被誤認成單據內容問題。"""
import importlib.util
import sys
import unittest
from unittest.mock import MagicMock, patch

if "fitz" not in sys.modules and importlib.util.find_spec("fitz") is None:
    sys.modules["fitz"] = MagicMock()

import requests

from src import asana_client, config, nvidia_client, processor


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
    @staticmethod
    def _payload(**overrides):
        data = {
            "order_no": None,
            "serial_candidates": ["US123"],
            "product_raw": "CX50",
            "customer_raw": "HKCH",
            "location_raw": None,
            "phone_candidates": [],
            "asset_candidates": [],
            "service_date_raw": "10/09/2026",
            "date_source": "ACTION_DATE",
            "unreadable_fields": [],
        }
        data.update(overrides)
        import json
        return json.dumps(data)

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
            + self._payload(order_no="", product_raw="Affiniti 70", customer_raw="PYNEH")
            + "\n```"
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=wrapped):
            result = nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

        self.assertIsNone(result["order_no"])
        self.assertEqual(result["serial_no"], "US123")

    def test_unrelated_json_before_ocr_json_is_skipped(self):
        wrapped = (
            'Example: {"status":"ok"}\nActual: '
            + self._payload()
            + " trailing words"
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=wrapped):
            result = nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

        self.assertEqual(result["serial_no"], "US123")

    def test_non_text_ocr_field_is_rejected(self):
        invalid = self._payload(order_no=12345678)
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=invalid) as call:
            with self.assertRaises(nvidia_client.NvidiaResponseError):
                nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)
        self.assertEqual(call.call_count, 2)

    def test_json_list_is_rejected_even_if_it_contains_an_object(self):
        invalid = f"[{self._payload()}]"
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=invalid):
            with self.assertRaises(nvidia_client.NvidiaResponseError):
                nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

    def test_unexpected_ocr_fields_are_not_returned(self):
        import json
        response_data = json.loads(self._payload())
        response_data["notes"] = "must not reach logs"
        response = json.dumps(response_data)
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=response):
            result = nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

        self.assertEqual(set(result), {
            "order_no", "serial_candidates", "product_raw", "customer_raw",
            "location_raw", "phone_candidates", "asset_candidates",
            "service_date_raw", "date_source", "unreadable_fields",
            "serial_no", "product", "customer",
        })

    def test_ocr_prompt_contains_no_realistic_example_values(self):
        response = self._payload(
            serial_candidates=[], product_raw=None, customer_raw=None,
            service_date_raw=None, date_source=None,
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=response) as call:
            nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

        prompt = call.call_args.kwargs["prompt"]
        for old_example in ("US622B1115", "USO16D0865", "PYNEH", "EPIQ Elite"):
            self.assertNotIn(old_example, prompt)


class ProcessorConsensusTests(unittest.TestCase):
    def setUp(self):
        self.ocr_a = {
            "order_no": None,
            "serial_no": "SERIAL-A",
            "product": "MODEL-A",
            "customer": "CUSTOMER-A",
        }

    def test_same_task_must_match_twice(self):
        task = {"gid": "task-1", "name": "Task 1"}
        with patch.object(config, "OCR_RETRY_ZOOMS", [2.0, 2.5, 3.0]), \
                patch.object(config, "OCR_MATCH_CONFIRMATIONS", 2), \
                patch.object(nvidia_client, "ocr_jobsheet_fields",
                             side_effect=[self.ocr_a, self.ocr_a]) as ocr, \
                patch.object(asana_client, "find_task", return_value=(task, 2)):
            matched, tier, _ = processor._ocr_and_match(MagicMock(), "PM")

        self.assertEqual(matched, task)
        self.assertEqual(tier, 2)
        self.assertEqual(ocr.call_count, 2)

    def test_single_match_is_not_accepted(self):
        task = {"gid": "task-1", "name": "Task 1"}
        with patch.object(config, "OCR_RETRY_ZOOMS", [2.0, 2.5, 3.0]), \
                patch.object(config, "OCR_MATCH_CONFIRMATIONS", 2), \
                patch.object(nvidia_client, "ocr_jobsheet_fields",
                             side_effect=[self.ocr_a, self.ocr_a, self.ocr_a]), \
                patch.object(asana_client, "find_task",
                             side_effect=[(task, 2), (None, 0), (None, 0)]):
            matched, tier, _ = processor._ocr_and_match(MagicMock(), "PM")

        self.assertIsNone(matched)
        self.assertEqual(tier, 0)

    def test_two_different_tasks_are_not_accepted(self):
        tasks = [
            ({"gid": "task-1", "name": "Task 1"}, 2),
            ({"gid": "task-2", "name": "Task 2"}, 2),
            (None, 0),
        ]
        with patch.object(config, "OCR_RETRY_ZOOMS", [2.0, 2.5, 3.0]), \
                patch.object(config, "OCR_MATCH_CONFIRMATIONS", 2), \
                patch.object(nvidia_client, "ocr_jobsheet_fields",
                             side_effect=[self.ocr_a, self.ocr_a, self.ocr_a]), \
                patch.object(asana_client, "find_task", side_effect=tasks):
            matched, tier, _ = processor._ocr_and_match(MagicMock(), "PM")

        self.assertIsNone(matched)
        self.assertEqual(tier, 0)


class AsanaMatchSafetyTests(unittest.TestCase):
    def test_unique_candidate_without_serial_is_not_auto_matched(self):
        ocr = {
            "order_no": None,
            "serial_no": None,
            "product": "EPIQ Elite",
            "customer": "Hospital",
        }
        with patch.object(asana_client, "_gather_pool", return_value=[{
            "gid": "only-task",
            "name": "Hospital, EPIQ Elite / US12345678",
        }]):
            task, tier = asana_client.find_task(ocr, job_type="PM")

        self.assertIsNone(task)
        self.assertEqual(tier, 0)

    def test_completed_recent_task_beats_future_incomplete_task(self):
        ocr = {
            "order_no": None,
            "serial_candidates": ["SZN22B1280"],
            "serial_no": "SZN22B1280",
            "product": "EPIQ Elite",
            "customer": "KWH-6F",
            "phone_candidates": [],
            "asset_candidates": [],
            "service_date_raw": "10/09/2026",
            "date_source": "ACTION_DATE",
            "location_raw": "6F",
        }
        tasks = [
            {"gid": "current", "name": "KWH, EPIQ Elite, SZN22B1280",
             "completed": True, "due_on": "2026-09-10"},
            {"gid": "future", "name": "KWH, EPIQ Elite, SZN22B1280",
             "completed": False, "due_on": "2027-03-10"},
        ]
        with patch.object(asana_client, "_gather_pool", return_value=tasks):
            task, tier = asana_client.find_task(ocr, job_type="PM")

        self.assertEqual("current", task["gid"])
        self.assertEqual(2, tier)

    def test_one_character_serial_error_needs_two_supporting_signals(self):
        base = {
            "order_no": None,
            "serial_candidates": ["SZN22B128O"],
            "serial_no": "SZN22B128O",
            "product": "EPIQ Elite",
            "customer": "KWH-6F",
            "phone_candidates": [],
            "asset_candidates": [],
            "service_date_raw": None,
            "date_source": None,
            "location_raw": None,
        }
        task_row = {"gid": "task", "name": "KWH, EPIQ Elite, SZN22B1280"}
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, _ = asana_client.find_task(base, job_type="PM")
        self.assertIsNone(task)

        supported = dict(base, service_date_raw="10/09/2026", date_source="ACTION_DATE")
        task_row["due_on"] = "2026-09-10"
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, tier = asana_client.find_task(supported, job_type="PM")
        self.assertEqual("task", task["gid"])
        self.assertEqual(2, tier)


if __name__ == "__main__":
    unittest.main()

"""外部服務故障不得被誤認成單據內容問題。"""
import importlib.util
import json
import sys
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if "fitz" not in sys.modules and importlib.util.find_spec("fitz") is None:
    sys.modules["fitz"] = MagicMock()

import requests

from src import asana_client, config, nvidia_client, processor


class AsanaFailureTests(unittest.TestCase):
    def setUp(self):
        asana_client._typeahead_cache.clear()
        asana_client._task_cache.clear()

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
            "serial_candidates": ["US123F4567"],
            "product_raw": "CX50",
            "hospital_raw": "HKCH",
            "contact_person_raw": None,
            "department_room_raw": None,
            "phone_candidates": [],
            "asset_candidates": [],
            "work_order_candidates": [],
            "service_date_raw": "10/09/2026",
            "date_source": "ACTION_DATE",
            "unreadable_fields": [],
        }
        if "customer_raw" in overrides:
            overrides["hospital_raw"] = overrides.pop("customer_raw")
        if "location_raw" in overrides:
            overrides["department_room_raw"] = overrides.pop("location_raw")
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

    def test_client_has_bounded_timeout_and_no_hidden_retries(self):
        nvidia_client._client = None
        try:
            with patch.object(nvidia_client.config, "NVIDIA_API_KEY", "test-key"), \
                    patch.object(nvidia_client, "OpenAI") as openai:
                nvidia_client.get_client()

            openai.assert_called_once_with(
                base_url=config.NVIDIA_BASE_URL,
                api_key="test-key",
                timeout=config.NVIDIA_REQUEST_TIMEOUT_SECONDS,
                max_retries=0,
            )
        finally:
            nvidia_client._client = None

    def test_deepseek_uses_paid_endpoint_and_separate_key(self):
        nvidia_client._client = None
        nvidia_client._client_identity = None
        try:
            with patch.object(config, "OCR_PROVIDER", "deepseek"), \
                    patch.object(config, "DEEPSEEK_API_KEY", "deepseek-test-key"), \
                    patch.object(nvidia_client, "OpenAI") as openai:
                nvidia_client.get_client()

            openai.assert_called_once_with(
                base_url=config.DEEPSEEK_BASE_URL,
                api_key="deepseek-test-key",
                timeout=config.DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
                max_retries=0,
            )
        finally:
            nvidia_client._client = None
            nvidia_client._client_identity = None

    def test_deepseek_key_trims_copy_paste_newline(self):
        nvidia_client._client = None
        nvidia_client._client_identity = None
        try:
            with patch.object(config, "OCR_PROVIDER", "deepseek"), \
                    patch.object(config, "DEEPSEEK_API_KEY", "  deepseek-test-key\r\n"), \
                    patch.object(nvidia_client, "OpenAI") as openai:
                nvidia_client.get_client()

            openai.assert_called_once_with(
                base_url=config.DEEPSEEK_BASE_URL,
                api_key="deepseek-test-key",
                timeout=config.DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
                max_retries=0,
            )
        finally:
            nvidia_client._client = None
            nvidia_client._client_identity = None

    def test_deepseek_key_rejects_internal_whitespace(self):
        nvidia_client._client = None
        nvidia_client._client_identity = None
        try:
            with patch.object(config, "OCR_PROVIDER", "deepseek"), \
                    patch.object(config, "DEEPSEEK_API_KEY", "deepseek bad-key"):
                with self.assertRaisesRegex(RuntimeError, "內含空白或換行"):
                    nvidia_client.get_client()
        finally:
            nvidia_client._client = None
            nvidia_client._client_identity = None

    def test_deepseek_vision_disables_thinking_and_enforces_json(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"ok":true}')
        )])
        client = MagicMock()
        client.chat.completions.create.return_value = response

        with patch.object(config, "OCR_PROVIDER", "deepseek"), \
                patch.object(config, "DEEPSEEK_MODEL", "deepseek-flash"), \
                patch.object(nvidia_client, "get_client", return_value=client):
            self.assertEqual(
                '{"ok":true}',
                nvidia_client._call_vision(
                    "return JSON", "image", max_tokens=128, expects_json=True
                ),
            )

        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual("deepseek-flash", request["model"])
        self.assertEqual(
            {"thinking": {"type": "disabled"}}, request["extra_body"]
        )
        self.assertEqual({"type": "json_object"}, request["response_format"])
        self.assertEqual(config.DEEPSEEK_JSON_MAX_TOKENS, request["max_tokens"])
        self.assertEqual(config.DEEPSEEK_REQUEST_TIMEOUT_SECONDS, request["timeout"])

    def test_kimi_uses_reasoning_space_for_json(self):
        first = SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content='{"ok":')
        )])
        second = SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content='true}')
        )])
        client = MagicMock()
        client.chat.completions.create.return_value = [first, second]

        with patch.object(nvidia_client, "get_client", return_value=client), \
                patch.object(config, "NVIDIA_MODEL", "moonshotai/kimi-k3"):
            self.assertEqual(
                '{"ok":true}',
                nvidia_client._call_vision(
                    "prompt", "image", max_tokens=300, expects_json=True
                ),
            )

        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "moonshotai/kimi-k3")
        self.assertEqual(request["temperature"], 1)
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertEqual(request["seed"], 0)
        self.assertTrue(request["stream"])
        self.assertEqual(request["max_tokens"], config.KIMI_JSON_MAX_TOKENS)
        self.assertNotIn("response_format", request)
        self.assertEqual(request["timeout"], config.NVIDIA_REQUEST_TIMEOUT_SECONDS)

    def test_nemotron_uses_official_instruct_settings(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"ok":true}')
        )])
        client = MagicMock()
        client.chat.completions.create.return_value = response

        model = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
        with patch.object(nvidia_client, "get_client", return_value=client), \
                patch.object(config, "NVIDIA_MODEL", model):
            self.assertEqual(
                '{"ok":true}',
                nvidia_client._call_vision(
                    "prompt", "image", max_tokens=128, expects_json=True
                ),
            )

        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], model)
        self.assertEqual(request["temperature"], 0.2)
        self.assertEqual(request["seed"], 0)
        self.assertEqual(
            request["max_tokens"], config.NEMOTRON_INSTRUCT_MAX_TOKENS
        )
        self.assertEqual(request["extra_body"], {
            "top_k": 1,
            "chat_template_kwargs": {"enable_thinking": False},
        })
        self.assertNotIn("stream", request)

    def test_fallback_json_has_enough_space_to_finish(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"ok":true}')
        )])
        client = MagicMock()
        client.chat.completions.create.return_value = response

        with patch.object(nvidia_client, "get_client", return_value=client):
            result = nvidia_client._call_vision_once(
                "prompt", "image", "meta/fallback", 300, expects_json=True
            )

        self.assertEqual('{"ok":true}', result)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["max_tokens"], config.NVIDIA_JSON_MAX_TOKENS)

    def test_primary_model_retries_before_using_fallback(self):
        class APITimeoutError(Exception):
            pass

        nvidia_client._unavailable_models.clear()
        try:
            with patch.object(config, "NVIDIA_MODEL", "nvidia/primary"), \
                    patch.object(config, "NVIDIA_FALLBACK_MODEL", "meta/fallback"), \
                    patch.object(
                        nvidia_client,
                        "_call_vision_once",
                        side_effect=[
                            APITimeoutError("timeout"),
                            APITimeoutError("timeout"),
                            "PM",
                        ],
                    ) as call:
                self.assertEqual(nvidia_client._call_vision("p", "i"), "PM")

            self.assertEqual(
                [item.args[2] for item in call.call_args_list],
                ["nvidia/primary", "nvidia/primary", "meta/fallback"],
            )
        finally:
            nvidia_client._unavailable_models.clear()

    def test_kimi_timeout_uses_fallback_once_per_process(self):
        class APITimeoutError(Exception):
            pass

        nvidia_client._unavailable_models.clear()
        try:
            with patch.object(config, "NVIDIA_MODEL", "moonshotai/kimi-k3"), \
                    patch.object(config, "NVIDIA_FALLBACK_MODEL", "meta/fallback"), \
                    patch.object(
                        nvidia_client,
                        "_call_vision_once",
                        side_effect=[APITimeoutError("timeout"), "PM", "CM"],
                    ) as call:
                self.assertEqual(nvidia_client._call_vision("p", "i"), "PM")
                self.assertEqual(nvidia_client._call_vision("p", "i"), "CM")

            models = [item.args[2] for item in call.call_args_list]
            self.assertEqual(models, [
                "moonshotai/kimi-k3", "meta/fallback", "meta/fallback"
            ])
        finally:
            nvidia_client._unavailable_models.clear()

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
        self.assertEqual(result["serial_no"], "US123F4567")

    def test_unrelated_json_before_ocr_json_is_skipped(self):
        wrapped = (
            'Example: {"status":"ok"}\nActual: '
            + self._payload()
            + " trailing words"
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=wrapped):
            result = nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

        self.assertEqual(result["serial_no"], "US123F4567")

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
            "order_no", "serial_candidates", "serial_visual_candidates",
            "product_raw", "hospital_raw", "contact_person_raw",
            "department_room_raw", "customer_raw", "location_raw",
            "phone_candidates", "asset_candidates",
            "work_order_candidates", "service_date_raw", "date_source",
            "unreadable_fields",
            "serial_no", "product", "customer",
            "_ocr_audit", "service_date_iso",
        })
        self.assertNotIn("notes", result["_ocr_audit"]["raw"])

    def test_ocr_prompt_contains_no_realistic_example_values(self):
        response = self._payload(
            serial_candidates=[], product_raw=None, customer_raw=None,
            service_date_raw=None, date_source=None,
        )
        with patch.object(nvidia_client, "crop_jobsheet_top", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=response) as call:
            nvidia_client.ocr_jobsheet_fields(MagicMock(), 0)

        prompt = call.call_args.kwargs["prompt"]
        self.assertTrue(call.call_args.kwargs["expects_json"])
        for old_example in ("USZ00B0006", "USO00D0010", "PYNEH", "EPIQ Elite"):
            self.assertNotIn(old_example, prompt)

    def test_focused_serial_reader_filters_non_serial_text(self):
        response = '{"serial_candidates":["US123F4567","SERIAL NO.","--"]}'
        with patch.object(nvidia_client, "crop_jobsheet_serial", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=response) as call:
            result = nvidia_client.ocr_jobsheet_serial_candidates(MagicMock(), 0)

        self.assertEqual(["US123F4567"], result)
        self.assertTrue(call.call_args.kwargs["expects_json"])

    def test_field_card_uses_fixed_panels_instead_of_half_page(self):
        from PIL import Image
        fields = ("order_no", "serial_candidates", "hospital_raw")
        with patch.object(
                nvidia_client, "_render_field_crop",
                return_value=Image.new("RGB", (320, 90), "white"),
        ) as render:
            encoded = nvidia_client.crop_jobsheet_field_card(
                MagicMock(), 0, fields, zoom=3.0
            )

        self.assertTrue(encoded)
        self.assertEqual(
            list(fields), [call.args[2] for call in render.call_args_list]
        )

    def test_focused_field_card_uses_value_only_crop(self):
        from PIL import Image
        with patch.object(
                nvidia_client, "_render_field_crop",
                return_value=Image.new("RGB", (320, 90), "white"),
        ) as render:
            nvidia_client.crop_jobsheet_field_card(
                MagicMock(), 0, ("serial_candidates",), focused=True
            )

        self.assertTrue(render.call_args.kwargs["focused"])

    def test_business_gate_rejects_impossible_hospital_and_serials(self):
        candidate = {
            "order_no": None,
            "serial_candidates": ["15915F0726", "S2N22F1275"],
            "product_raw": "Affiniti 70",
            "hospital_raw": "PN",
            "department_room_raw": "Asset# 88880001",
            "phone_candidates": [],
            "asset_candidates": [],
            "work_order_candidates": [],
            "service_date_raw": None,
            "date_source": None,
            "unreadable_fields": [],
        }

        result = nvidia_client._normalize_ocr_data(candidate)

        self.assertEqual([], result["serial_candidates"])
        self.assertEqual(
            ["15915F0726", "S2N22F1275"],
            result["serial_visual_candidates"],
        )
        self.assertIsNone(result["hospital_raw"])
        self.assertEqual(["88880001"], result["asset_candidates"])
        self.assertIsNone(result["location_raw"])
        self.assertIn("serial_candidates", result["unreadable_fields"])
        self.assertIn("hospital_raw", result["unreadable_fields"])

    def test_asset_parser_preserves_room_text_after_asset_number(self):
        candidate = json.loads(self._payload(
            department_room_raw="Asset# 88880001 6F",
        ))

        result = nvidia_client._normalize_ocr_data(candidate)

        self.assertEqual(["88880001"], result["asset_candidates"])
        self.assertEqual("6F", result["location_raw"])

    def test_observed_serial_families_pass_without_autocorrection(self):
        for value in (
            "USY00F0004", "USX00F0001", "SZX00F0005", "SG00000011",
        ):
            with self.subTest(value=value):
                self.assertTrue(nvidia_client._valid_serial_token(value))

    def test_old_action_date_is_removed_from_matching_evidence(self):
        candidate = json.loads(self._payload(service_date_raw="01/01/2020"))
        with patch.object(nvidia_client, "_today", return_value=date(2026, 9, 15)):
            result = nvidia_client._normalize_ocr_data(candidate)

        self.assertIsNone(result["service_date_raw"])
        self.assertIsNone(result["date_source"])
        self.assertIn("service_date_raw", result["unreadable_fields"])


class ProcessorConsensusTests(unittest.TestCase):
    def setUp(self):
        self.ocr_a = {
            "order_no": None,
            "serial_candidates": ["US123F4567"],
            "serial_no": "US123F4567",
            "product_raw": "CX50",
            "product": "CX50",
            "hospital_raw": "QMH",
            "customer": "QMH",
            "unreadable_fields": [],
        }

    def test_same_task_must_match_twice(self):
        task = {"gid": "task-1", "name": "Task 1"}
        with patch.object(nvidia_client, "ocr_jobsheet_fields",
                          return_value=self.ocr_a) as ocr, \
                patch.object(nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=self.ocr_a), \
                patch.object(asana_client, "find_task", return_value=(task, 2)):
            matched, tier, _ = processor._ocr_and_match(MagicMock(), "PM")

        self.assertEqual(matched, task)
        self.assertEqual(tier, 2)
        self.assertEqual(ocr.call_count, 1)

    def test_close_index_tie_gets_one_constrained_candidate_pass(self):
        reading = dict(
            self.ocr_a,
            contact_person_raw="Alice", department_room_raw="3F",
            phone_candidates=["99990011"], asset_candidates=["88880001"],
            service_date_raw="08/09/2026", date_source="ACTION_DATE",
        )
        candidates = [
            {"candidate_id": "C1", "device_key": "SERIAL-1"},
            {"candidate_id": "C2", "device_key": "SERIAL-2"},
        ]
        task = {"gid": "task-2", "name": "Task 2"}
        with patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=reading), \
                patch.object(nvidia_client, "ocr_jobsheet_identity_fields", return_value=reading), \
                patch.object(nvidia_client, "ocr_jobsheet_serial_candidates",
                             return_value=["US123F4567"]), \
                patch.object(nvidia_client, "ocr_jobsheet_support_fields", return_value=reading), \
                patch.object(asana_client, "get_close_index_candidates",
                             return_value=candidates), \
                patch.object(nvidia_client, "choose_device_candidate", return_value="C2") as choose, \
                patch.object(asana_client, "find_task",
                             side_effect=[(None, 0), (None, 0), (None, 0),
                                          (None, 0), (task, 2)]) as find:
            matched, tier, _ = processor._ocr_and_match(MagicMock(), "PM")

        self.assertEqual("task-2", matched["gid"])
        self.assertEqual(2, tier)
        choose.assert_called_once()
        self.assertEqual("SERIAL-2", find.call_args.kwargs["selected_device_key"])

    def test_repeated_service_date_is_treated_as_action_date_when_source_is_omitted(self):
        readings = [
            {"service_date_raw": "18/8/2026", "date_source": None},
            {"service_date_raw": "18/8/2026", "date_source": None},
        ]

        consensus = processor._consensus_ocr(readings)

        self.assertEqual("18/8/2026", consensus["service_date_raw"])
        self.assertEqual("ACTION_DATE", consensus["date_source"])

    def test_contact_person_has_its_own_consensus_field(self):
        consensus = processor._consensus_ocr([
            {"contact_person_raw": "Ms. Chan", "phone_candidates": ["99990011"]},
            {"contact_person_raw": "Ms Chan", "phone_candidates": ["99990011"]},
        ])

        self.assertEqual("Ms. Chan", consensus["contact_person_raw"])
        self.assertEqual(["99990011"], consensus["phone_candidates"])

    def test_one_character_serial_disagreement_is_kept_as_two_candidates(self):
        readings = [
            {"serial_candidates": ["USY00F0004"]},
            {"serial_candidates": ["USY00F000G"]},
        ]

        consensus = processor._consensus_ocr(readings)

        self.assertEqual(
            ["USY00F0004", "USY00F000G"],
            consensus["serial_candidates"],
        )

    def test_majority_serial_stays_ambiguous_after_a_different_valid_read(self):
        readings = [
            {"serial_candidates": ["USY00F0004"]},
            {"serial_candidates": ["USY00F000G"]},
            {"serial_candidates": ["USY00F0004"]},
        ]

        consensus = processor._consensus_ocr(readings)

        self.assertEqual(["USY00F0004"], consensus["serial_candidates"])
        self.assertTrue(consensus["serial_ambiguous"])

    def test_non_device_words_do_not_gain_fuzzy_serial_consensus(self):
        readings = [
            {"serial_candidates": ["SERIAL-A"]},
            {"serial_candidates": ["SERIAL-B"]},
        ]

        consensus = processor._consensus_ocr(readings)

        self.assertEqual([], consensus["serial_candidates"])

    def test_disputed_serial_is_not_sent_to_asana_as_evidence(self):
        other = dict(self.ocr_a, serial_no="SG987F6543")
        other["serial_candidates"] = ["SG987F6543"]
        first = dict(self.ocr_a, serial_candidates=["US123F4567"])
        with patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=first), \
                patch.object(nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=other), \
                patch.object(nvidia_client, "ocr_jobsheet_focused_field",
                             side_effect=[other, other]), \
                patch.object(nvidia_client, "ocr_jobsheet_support_fields",
                             return_value={}), \
                patch.object(asana_client, "find_task", return_value=(None, 0)) as find:
            matched, tier, _ = processor._ocr_and_match(MagicMock(), "PM")

        self.assertIsNone(matched)
        self.assertEqual(tier, 0)
        self.assertNotIn("US123F4567", find.call_args.args[0].get("serial_candidates", []))

    def test_asana_is_not_called_before_two_ocr_readings(self):
        task = {"gid": "task-1", "name": "Task 1"}
        with patch.object(nvidia_client, "ocr_jobsheet_fields",
                          return_value=self.ocr_a), \
                patch.object(nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=self.ocr_a), \
                patch.object(asana_client, "find_task", return_value=(task, 2)) as find:
            matched, tier, _ = processor._ocr_and_match(MagicMock(), "PM")

        self.assertEqual("task-1", matched["gid"])
        find.assert_called_once()

    def test_failed_full_crop_uses_focused_serial_consensus(self):
        general = {
            "serial_candidates": ["WRONG12345"],
            "product_raw": "CX50",
            "hospital_raw": "QMH",
            "phone_candidates": ["99990001"],
            "asset_candidates": ["88880001"],
            "service_date_raw": "18/8/2026",
            "date_source": "ACTION_DATE",
        }
        task = {"gid": "task-1", "name": "Task 1"}
        with patch.object(nvidia_client, "ocr_jobsheet_fields", return_value=general), \
                patch.object(nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=general), \
                patch.object(nvidia_client, "ocr_jobsheet_serial_candidates",
                             side_effect=[["US123F4567"], ["US123F4567"]]) as focused, \
                patch.object(nvidia_client, "ocr_jobsheet_support_fields",
                             return_value=general), \
                patch.object(asana_client, "find_task",
                             side_effect=[(None, 0), (None, 0), (task, 2)]) as find:
            matched, tier, consensus = processor._ocr_and_match(MagicMock(), "PM")

        self.assertEqual("task-1", matched["gid"])
        self.assertEqual(2, tier)
        self.assertIn("US123F4567", consensus["serial_candidates"])
        self.assertEqual(2, focused.call_count)
        self.assertEqual(3, find.call_count)

    def test_disputed_action_date_gets_one_field_recheck(self):
        primary = dict(self.ocr_a, service_date_raw="18/8/2026",
                       date_source="ACTION_DATE")
        identity = dict(self.ocr_a)
        support = {"service_date_raw": "19/8/2026",
                   "date_source": "ACTION_DATE", "unreadable_fields": []}
        focused = {"service_date_raw": "18/8/2026",
                   "date_source": "ACTION_DATE", "unreadable_fields": []}
        task = {"gid": "task-1", "name": "Task 1"}
        with patch.object(nvidia_client, "ocr_jobsheet_fields",
                          return_value=primary), \
                patch.object(nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=identity), \
                patch.object(nvidia_client, "ocr_jobsheet_support_fields",
                             return_value=support), \
                patch.object(nvidia_client, "ocr_jobsheet_serial_candidates",
                             return_value=["US123F4567"]), \
                patch.object(nvidia_client, "ocr_jobsheet_focused_field",
                             return_value=focused) as reread, \
                patch.object(asana_client, "find_task",
                             side_effect=[(None, 0), (None, 0),
                                          (None, 0), (task, 2)]):
            matched, tier, consensus = processor._ocr_and_match(MagicMock(), "PM")

        self.assertEqual("task-1", matched["gid"])
        self.assertEqual(2, tier)
        self.assertEqual("18/8/2026", consensus["service_date_raw"])
        reread.assert_called_once_with(
            unittest.mock.ANY, 0, "service_date_raw",
            zoom=config.OCR_FOCUSED_RETRY_ZOOMS[0],
        )

    def test_disputed_phone_gets_one_field_recheck(self):
        primary = dict(self.ocr_a, phone_candidates=["99990031"])
        support = {"phone_candidates": ["99990032"],
                   "unreadable_fields": []}
        focused = {"phone_candidates": ["99990031"],
                   "unreadable_fields": []}
        task = {"gid": "task-1", "name": "Task 1"}
        with patch.object(nvidia_client, "ocr_jobsheet_fields",
                          return_value=primary), \
                patch.object(nvidia_client, "ocr_jobsheet_identity_fields",
                             return_value=self.ocr_a), \
                patch.object(nvidia_client, "ocr_jobsheet_support_fields",
                             return_value=support), \
                patch.object(nvidia_client, "ocr_jobsheet_serial_candidates",
                             return_value=["US123F4567"]), \
                patch.object(nvidia_client, "ocr_jobsheet_focused_field",
                             return_value=focused) as reread, \
                patch.object(asana_client, "find_task",
                             side_effect=[(None, 0), (None, 0),
                                          (None, 0), (task, 2)]):
            matched, tier, consensus = processor._ocr_and_match(MagicMock(), "PM")

        self.assertEqual("task-1", matched["gid"])
        self.assertEqual(2, tier)
        self.assertEqual(["99990031"], consensus["phone_candidates"])
        reread.assert_called_once_with(
            unittest.mock.ANY, 0, "phone_candidates",
            zoom=config.OCR_FOCUSED_RETRY_ZOOMS[0],
        )


class AsanaMatchSafetyTests(unittest.TestCase):
    def test_order_number_is_not_reused_as_phone_or_asset_evidence(self):
        task_row = {
            "gid": "task", "name": "PYN / Affiniti 70 / USY00F0004 / 60000011",
            "notes": "Telephone 99990001; Asset 88880001",
        }
        scored = asana_client._candidate_score(
            task_row,
            {
                "phone_candidates": ["60000011"],
                "asset_candidates": ["99990001"],
            },
            serials=[], hosp=None, product=None,
        )

        self.assertNotIn("phone_exact", scored["support"])
        self.assertNotIn("asset_exact", scored["support"])

    def test_one_character_visual_serial_needs_unique_candidate_and_two_fields(self):
        ocr = {
            "order_no": None,
            "serial_candidates": [],
            "serial_visual_candidates": ["S2X00F0005"],
            "product": "Affiniti 70",
            "phone_candidates": ["99990041"],
        }
        task_row = {
            "gid": "task",
            "name": "Sample Regional Hospital/ Affiniti 70/ SZX00F0005",
            "notes": "Telephone 99990041",
        }
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, tier = asana_client.find_task(ocr, job_type="PM")

        self.assertEqual("task", task["gid"])
        self.assertEqual(2, tier)

    def test_two_character_visual_serial_is_not_accepted(self):
        ocr = {
            "order_no": None,
            "serial_candidates": [],
            "serial_visual_candidates": ["15915F0726"],
            "product": "Affiniti 70",
            "phone_candidates": ["99990001"],
        }
        task_row = {
            "gid": "task",
            "name": "PYN/ Affiniti 70/ USY00F0004/ 60000011",
            "notes": "Telephone 99990001",
        }
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, tier = asana_client.find_task(ocr, job_type="PM")

        self.assertIsNone(task)
        self.assertEqual(0, tier)

    def test_tung_wah_search_keeps_spaces_for_typeahead(self):
        canonical = asana_client.hospital_core("Sample Regional Hospital")

        self.assertEqual("Sample Regional Hospital", canonical)
        self.assertEqual(
            ["Sample Regional Hospital"],
            asana_client.hospital_search_terms(canonical),
        )

    def test_recent_task_can_correct_obviously_misread_service_year(self):
        ocr = {
            "order_no": None,
            "serial_candidates": ["USX00F0001"],
            "serial_no": "USX00F0001",
            "phone_candidates": ["99990031"],
            "service_date_raw": "18/8/2020",
            "date_source": "ACTION_DATE",
        }
        current = {
            "gid": "current",
            "name": "PYNEH Affiniti 70 USX00F0001 60000012",
            "notes": "99990031",
            "due_on": "2026-08-20",
            "memberships": [{"project": {"name": "2026 PM"}}],
        }
        historical = {
            "gid": "historical",
            "name": "PYNEH Affiniti 70 USX00F0001 60000001",
            "notes": "99990031",
            "due_on": "2025-08-20",
            "memberships": [{"project": {"name": "2025 PM"}}],
        }
        with patch.object(asana_client, "_today", return_value=date(2026, 9, 14)), \
                patch.object(asana_client, "_gather_pool",
                             return_value=[historical, current]):
            task, tier = asana_client.find_task(ocr, job_type="PM")

        self.assertEqual("current", task["gid"])
        self.assertEqual(2, tier)

    def test_work_order_number_is_used_to_find_candidates(self):
        with patch.object(asana_client, "_typeahead", return_value=[]) as search:
            asana_client._gather_pool(
                None, [], None, None, work_orders=["HAWO 9876543"]
            )

        search.assert_called_once_with("HAWO 9876543")

    def test_phone_and_long_asset_are_used_to_find_candidates(self):
        with patch.object(asana_client, "_typeahead", return_value=[]) as search:
            asana_client._gather_pool(
                None, [], None, None,
                phones=["9999 0001"], assets=["88880001"],
            )

        self.assertEqual(
            ["99990001", "88880001"],
            [call.args[0] for call in search.call_args_list],
        )

    def test_exact_work_order_is_strong_support(self):
        ocr = {
            "order_no": None,
            "serial_candidates": ["USZ00A0000"],
            "serial_no": "USZ00A0000",
            "product": "MODEL Z",
            "customer": "TESTH",
            "phone_candidates": [],
            "asset_candidates": [],
            "work_order_candidates": ["9876543"],
            "service_date_raw": None,
            "date_source": None,
            "location_raw": None,
        }
        task_row = {
            "gid": "task",
            "name": "TESTH/ MODEL Z/ USZ00A0000/ HAWO 9876543",
        }
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, tier = asana_client.find_task(ocr, job_type="PM")

        self.assertEqual("task", task["gid"])
        self.assertEqual(2, tier)

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

    def test_unique_candidate_can_match_without_serial_on_phone_asset_and_date(self):
        ocr = {
            "order_no": None,
            "serial_candidates": [],
            "serial_no": None,
            "product": "Affiniti 70",
            "customer": "PYN",
            "phone_candidates": ["99990001"],
            "asset_candidates": ["88880001"],
            "service_date_raw": "18/8/2026",
            "date_source": "ACTION_DATE",
        }
        task_row = {
            "gid": "task",
            "name": "PYN/ Affiniti 70/ USY00F0004/ 60000011",
            "notes": "Phone 99990001; Asset 88880001",
            "due_on": "2026-08-18",
            "memberships": [{"project": {"name": "2026 PM"}}],
        }
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, tier = asana_client.find_task(ocr, job_type="PM")

        self.assertEqual("task", task["gid"])
        self.assertEqual(2, tier)

    def test_serialless_match_needs_all_three_strong_fields(self):
        ocr = {
            "order_no": None,
            "serial_candidates": [],
            "serial_no": None,
            "phone_candidates": ["99990001"],
            "asset_candidates": ["88880001"],
            "service_date_raw": None,
            "date_source": None,
        }
        task_row = {
            "gid": "task",
            "name": "PYN/ Affiniti 70/ USY00F0004/ 60000011",
            "notes": "Phone 99990001; Asset 88880001",
            "memberships": [{"project": {"name": "2026 PM"}}],
        }
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, tier = asana_client.find_task(ocr, job_type="PM")

        self.assertIsNone(task)
        self.assertEqual(0, tier)

    def test_completed_recent_task_beats_future_incomplete_task(self):
        ocr = {
            "order_no": None,
            "serial_candidates": ["SZX00B0000"],
            "serial_no": "SZX00B0000",
            "product": "EPIQ Elite",
            "customer": "KWH-6F",
            "phone_candidates": [],
            "asset_candidates": [],
            "service_date_raw": "10/09/2026",
            "date_source": "ACTION_DATE",
            "location_raw": "6F",
        }
        tasks = [
            {"gid": "current", "name": "KWH, EPIQ Elite, SZX00B0000",
             "completed": True, "due_on": "2026-09-10"},
            {"gid": "future", "name": "KWH, EPIQ Elite, SZX00B0000",
             "completed": False, "due_on": "2027-03-10"},
        ]
        with patch.object(asana_client, "_gather_pool", return_value=tasks):
            task, tier = asana_client.find_task(ocr, job_type="PM")

        self.assertEqual("current", task["gid"])
        self.assertEqual(2, tier)

    def test_one_character_serial_error_needs_two_supporting_signals(self):
        base = {
            "order_no": None,
            "serial_candidates": ["SZX00B000O"],
            "serial_no": "SZX00B000O",
            "product": "EPIQ Elite",
            "customer": None,
            "phone_candidates": [],
            "asset_candidates": [],
            "service_date_raw": None,
            "date_source": None,
            "location_raw": None,
        }
        task_row = {"gid": "task", "name": "KWH, EPIQ Elite, SZX00B0000"}
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, _ = asana_client.find_task(base, job_type="PM")
        self.assertIsNone(task)

        supported = dict(base, service_date_raw="10/09/2026", date_source="ACTION_DATE")
        task_row["due_on"] = "2026-09-10"
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, tier = asana_client.find_task(supported, job_type="PM")
        self.assertEqual("task", task["gid"])
        self.assertEqual(2, tier)

    def test_pm_sheet_rejects_explicit_cm_project(self):
        ocr = {
            "order_no": "60000014",
            "serial_candidates": ["USW00F0008"],
            "serial_no": "USW00F0008",
            "product": "Affiniti 70",
            "customer": "HKCH",
        }
        cm_task = {
            "gid": "repair",
            "name": "HKCH Affiniti 70 USW00F0008 60000014",
            "memberships": [{"project": {"name": "Corrective Maintenance"}}],
        }
        with patch.object(asana_client, "_gather_pool", return_value=[cm_task]):
            task, tier = asana_client.find_task(ocr, job_type="PM")
        self.assertIsNone(task)
        self.assertEqual(0, tier)

    def test_known_pm_project_is_matching_evidence(self):
        ocr = {
            "order_no": None,
            "serial_candidates": ["USW00F0008"],
            "serial_no": "USW00F0008",
            "product": None,
            "customer": None,
        }
        pm_task = {
            "gid": "pm",
            "name": "HKCH Affiniti 70 USW00F0008",
            "memberships": [{"project": {"name": "2026 PM"}}],
        }
        with patch.object(asana_client, "_gather_pool", return_value=[pm_task]):
            task, tier = asana_client.find_task(ocr, job_type="PM")
        self.assertEqual("pm", task["gid"])
        self.assertEqual(2, tier)

    def test_unknown_short_hospital_code_is_not_used(self):
        self.assertIsNone(asana_client.hospital_core("KWM"))
        self.assertIsNone(asana_client.hospital_core("PYTV-6F"))
        self.assertEqual("QMH", asana_client.hospital_core("Quick Medical Hospital"))

    def test_pyn_and_pyneh_are_the_same_hospital(self):
        self.assertEqual("PYNEH", asana_client.hospital_core("PYN"))
        self.assertEqual("PYNEH", asana_client.hospital_core("PYNEH"))

    def test_pyneh_candidate_search_uses_both_confirmed_short_names(self):
        self.assertEqual(
            ["PYNEH", "PYN"],
            asana_client.hospital_search_terms("PYNEH"),
        )

    def test_full_hospital_name_allows_unique_two_character_ocr_error(self):
        asana_client.set_device_index({
            "devices": [],
            "location_directory": [{
                "canonical_hospital": "Alpha Medical Hospital",
                "confirmed_aliases": ["Alpha Medical Hospital"],
                "learned_aliases": [],
                "match_enabled": True,
            }],
        })
        try:
            self.assertEqual(
                "Alpha Medical Hospital",
                asana_client.hospital_core("Alphe Medical Hospital"),
            )
            self.assertEqual(
                "Completely Unknown Clinic",
                asana_client.hospital_core("Completely Unknown Clinic"),
            )
        finally:
            asana_client.clear_device_index()

    def test_ambiguous_serial_needs_two_supporting_signals_even_if_one_is_exact(self):
        ocr = {
            "order_no": None,
            "serial_candidates": ["SZX00B0000", "SZX00B000O"],
            "serial_no": "SZX00B0000",
            "serial_ambiguous": True,
            "product": "EPIQ Elite",
            "hospital_raw": None,
        }
        task_row = {"gid": "task", "name": "KWH, EPIQ Elite, SZX00B0000"}
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, _ = asana_client.find_task(ocr, job_type="PM")
        self.assertIsNone(task)

        supported = dict(
            ocr,
            service_date_raw="10/09/2026",
            date_source="ACTION_DATE",
        )
        task_row["due_on"] = "2026-09-10"
        with patch.object(asana_client, "_gather_pool", return_value=[task_row]):
            task, tier = asana_client.find_task(supported, job_type="PM")
        self.assertEqual("task", task["gid"])
        self.assertEqual(2, tier)

    def test_safe_title_removes_trailing_separators(self):
        title = asana_client.get_safe_title({
            "name": "PYNEH, EPIQ Elite / USZ00B0006/ "
        })
        self.assertEqual("PYNEH, EPIQ Elite - USZ00B0006", title)


if __name__ == "__main__":
    unittest.main()

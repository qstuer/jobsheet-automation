"""Synthetic bounded recovery and identity reread regressions; no live calls."""
from contextlib import ExitStack
from datetime import date
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import fitz
from src import asana_client as ac, asana_index, backtest, config
from src import device_search_agent, matching_rules as rules, nvidia_client as vision
from tests.test_matching_rules import reading, task
from tests import test_backtest as backtest_tests
from tests.test_ocr_resilience import vocabulary_index


class FakeTime:
    now = 0.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class Transient(Exception):
    def __init__(self, status=503, retry_after=None):
        self.status_code = status
        self.response = SimpleNamespace(headers={} if retry_after is None else {'retry-after': retry_after})


class APITimeoutError(Exception):
    pass


class TransportRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict('os.environ', {config.OCR_TRANSPORT_RECOVERY_ENV: '1', 'JOBSHEET_DRY_RUN': '1'}))
        self.stack.enter_context(patch.object(vision, '_provider_settings', return_value=('nvidia', '', '', 'primary', 45., 'fallback')))
        self.clock = FakeTime()
        self.stack.enter_context(patch.object(vision.time, 'monotonic', side_effect=self.clock.monotonic))
        self.stack.enter_context(patch.object(vision.time, 'sleep', side_effect=self.clock.sleep))
        vision.reset_model_availability()
        self.addCleanup(vision.reset_model_availability)

    def test_transient_main_and_fallback_failure_can_recover_on_same_reader(self):
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(), Transient(), APITimeoutError(), APITimeoutError(), 'PM']) as call:
            self.assertEqual('PM', vision._call_vision('same prompt', 'same image'))
        self.assertEqual(['primary', 'primary', 'fallback', 'fallback', 'primary'], [c.args[2] for c in call.call_args_list])
        self.assertTrue(all(c.args[:2] == ('same prompt', 'same image') for c in call.call_args_list))
        self.assertNotIn('primary', vision._model_retry_at)
        self.assertGreater(self.clock.now, 2)

    def test_full_outage_is_bounded_and_does_not_disable_models_forever(self):
        with patch.object(vision, '_call_vision_once', side_effect=Transient()) as call:
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i')
        self.assertEqual(config.OCR_TRANSPORT_MAX_REQUESTS, call.call_count)
        self.assertLess(self.clock.now, config.OCR_TRANSPORT_DEADLINE_SECONDS)
        with patch.object(vision, '_call_vision_once', return_value='PM') as call:
            self.assertEqual('PM', vision._call_vision('p', 'i'))
        self.assertEqual(1, call.call_count)

    def test_failure_budget_shared_by_readers_but_reset_between_documents(self):
        with patch.object(vision, '_call_vision_once', side_effect=Transient()) as call:
            for _ in range(4):
                with self.assertRaises(vision.NvidiaServiceUnavailable):
                    vision._call_vision('p', 'i')
        self.assertEqual(config.OCR_TRANSPORT_MAX_FAILURES, call.call_count)
        vision.reset_model_availability()
        with patch.object(vision, '_call_vision_once', return_value='PM'):
            self.assertEqual('PM', vision._call_vision('p', 'i'))

    def test_retry_after_respected_not_truncated_to_two_seconds(self):
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(503, '8'), 'PM']):
            self.assertEqual('PM', vision._call_vision('p', 'i'))
        self.assertEqual(8, self.clock.now)

    def test_long_retry_after_stops_without_fallback_or_busy_wait(self):
        with patch.object(vision, '_call_vision_once', side_effect=Transient(429, '120')) as call:
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i')
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i')
        self.assertEqual(1, call.call_count)
        self.assertEqual(0, self.clock.now)

    def test_server_delay_also_precedes_switch_to_existing_fallback(self):
        started = []
        def respond(*args, **kwargs):
            started.append(self.clock.now)
            if args[2] == 'primary':
                raise Transient(503, '8')
            return 'PM'
        with patch.object(vision, '_call_vision_once', side_effect=respond):
            self.assertEqual('PM', vision._call_vision('p', 'i'))
        self.assertEqual([0, 8, 16], started)

    def test_deadline_prevents_a_second_request_after_time_budget(self):
        def stalled(*args, **kwargs):
            self.clock.now += config.OCR_TRANSPORT_DEADLINE_SECONDS
            raise APITimeoutError()
        with patch.object(vision, '_call_vision_once', side_effect=stalled) as call:
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i')
        self.assertEqual(1, call.call_count)

    def test_auth_and_invalid_requests_are_not_retried(self):
        for code in (400, 401, 403, 404):
            with self.subTest(code=code), patch.object(vision, '_call_vision_once', side_effect=Transient(code)) as call:
                with self.assertRaises(Transient):
                    vision._call_vision('p', 'i')
            self.assertEqual(1, call.call_count)

    def test_read_only_gate_precedes_any_api_call(self):
        with patch.dict('os.environ', {'JOBSHEET_DRY_RUN': '0'}), patch.object(vision, '_call_vision_once') as call:
            with self.assertRaises(ValueError):
                vision._call_vision('p', 'i')
        call.assert_not_called()

    def test_disabled_flag_preserves_legacy_dispatch(self):
        with patch.dict('os.environ', {config.OCR_TRANSPORT_RECOVERY_ENV: '0'}), patch.object(vision, '_call_vision_legacy', return_value='PM') as legacy:
            self.assertEqual('PM', vision._call_vision('p', 'i', allow_fallback=False))
        legacy.assert_called_once_with('p', 'i', 300, False, False)

    def test_date_and_malformed_retry_after_header_are_bounded(self):
        self.assertEqual(0, vision._retry_after_seconds(Transient(503, 'Wed, 01 Jan 2020 00:00:00 GMT')))
        for text in ('garbage', 'nan', 'inf'):
            self.assertIsNone(vision._retry_after_seconds(Transient(503, text)))

    def test_remaining_deadline_never_extends_configured_request_timeout(self):
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='PM'), finish_reason='stop')], usage=None)
        with patch.object(vision, 'get_client', return_value=client):
            for remaining in (12, 180):
                self.assertEqual('PM', vision._call_vision_once('p', 'i', 'primary', 64, False, timeout_seconds=remaining))
                self.assertEqual(min(45, remaining), client.chat.completions.create.call_args.kwargs['timeout'])

    def prime_successful_fallback(self):
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(), Transient(), 'PM']):
            self.assertEqual('PM', vision._call_vision('p', 'i'))
        self.assertEqual({'fallback'}, vision._transport_healthy_models)
        self.clock.now += 20  # Primary cooldown is expired; preference must not depend on it.
        vision._transport_failures = config.OCR_TRANSPORT_MAX_FAILURES - 1

    def test_last_failure_reserve_prefers_proven_fallback_for_later_readers(self):
        self.prime_successful_fallback()
        with patch.object(vision, '_call_vision_once', return_value='PM') as call:
            for _ in range(3):
                self.assertEqual('PM', vision._call_vision('same prompt', 'same image'))
        self.assertEqual(['fallback'] * 3, [c.args[2] for c in call.call_args_list])
        self.assertTrue(all(c.args[:2] == ('same prompt', 'same image') for c in call.call_args_list))
        self.assertEqual(config.OCR_TRANSPORT_MAX_FAILURES - 1, vision._transport_failures)

    def test_proven_fallback_failure_uses_last_token_then_stops_all_requests(self):
        self.prime_successful_fallback()
        with patch.object(vision, '_call_vision_once', side_effect=Transient()) as call:
            for _ in range(3):
                with self.assertRaises(vision.NvidiaServiceUnavailable):
                    vision._call_vision('p', 'i')
        self.assertEqual(1, call.call_count)
        self.assertEqual('fallback', call.call_args.args[2])
        self.assertEqual(config.OCR_TRANSPORT_MAX_FAILURES, vision._transport_failures)

    def test_unproven_model_is_not_granted_extra_requests_at_the_cap(self):
        vision._transport_failures = config.OCR_TRANSPORT_MAX_FAILURES - 1
        with patch.object(vision, '_call_vision_once', side_effect=Transient()) as call:
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i')
        self.assertEqual(1, call.call_count)
        self.assertEqual('primary', call.call_args.args[2])

    def test_conservation_never_enables_a_disallowed_fallback(self):
        self.prime_successful_fallback()
        with patch.object(vision, '_call_vision_once', side_effect=Transient()) as call:
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i', allow_fallback=False)
        self.assertEqual(['primary'], [c.args[2] for c in call.call_args_list])

    def test_server_hold_cannot_be_bypassed_by_a_previously_healthy_model(self):
        self.prime_successful_fallback()
        for model in ('primary','fallback'):
            vision._model_retry_at[model] = self.clock.now + 120
        with patch.object(vision, '_call_vision_once') as call:
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i')
        call.assert_not_called()

    def test_proven_model_state_never_leaks_between_documents(self):
        self.prime_successful_fallback()
        vision.reset_model_availability()
        self.assertEqual(set(), vision._transport_healthy_models)
        with patch.object(vision, '_call_vision_once', return_value='PM') as call:
            self.assertEqual('PM', vision._call_vision('p', 'i'))
        self.assertEqual('primary', call.call_args.args[2])

    def test_recent_primary_failure_uses_healthy_fallback_before_final_budget(self):
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(), Transient(), 'PM']):
            self.assertEqual('PM', vision._call_vision('p', 'i'))
        self.clock.now += config.OCR_TRANSPORT_COOLDOWN_SECONDS + 1
        with patch.object(vision, '_call_vision_once', return_value='PM') as call:
            self.assertEqual('PM', vision._call_vision('next', 'same'))
        self.assertEqual('fallback', call.call_args.args[2])
        self.assertEqual(2, vision._transport_failures)

    def test_health_lease_expires_without_renewal_from_fallback_success(self):
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(), Transient(), 'PM']):
            vision._call_vision('p', 'i')
        failed_at = vision._transport_last_failure_at['primary']
        with patch.object(vision, '_call_vision_once', return_value='PM') as call:
            self.clock.now = failed_at + config.OCR_TRANSPORT_HEALTH_PREFERENCE_SECONDS - 1
            vision._call_vision('p', 'i')
            self.assertEqual('fallback', call.call_args.args[2])
            self.clock.now += 2
            vision._call_vision('p', 'i')
            self.assertEqual('primary', call.call_args.args[2])
        self.assertNotIn('primary', vision._transport_last_failure_at)

    def test_healthy_preference_respects_disallowed_fallback_and_server_hold(self):
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(), Transient(), 'PM']):
            vision._call_vision('p', 'i')
        self.clock.now += 20
        with patch.object(vision, '_call_vision_once', return_value='PM') as call:
            vision._call_vision('p', 'i', allow_fallback=False)
        self.assertEqual('primary', call.call_args.args[2])
        vision._transport_last_failure_at['primary'] = self.clock.now
        vision._transport_healthy_models.discard('primary')
        for model in ('primary', 'fallback'):
            vision._model_retry_at[model] = self.clock.now + 120
        with patch.object(vision, '_call_vision_once') as call:
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i')
        call.assert_not_called()

    def test_failed_preferred_model_does_not_gain_extra_attempts(self):
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(), Transient(), 'PM']):
            vision._call_vision('p', 'i')
        self.clock.now += 20
        with patch.object(vision, '_call_vision_once', side_effect=Transient()) as call:
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i')
        self.assertEqual(config.OCR_TRANSPORT_MAX_REQUESTS, call.call_count)
        self.assertEqual(2 + config.OCR_TRANSPORT_MAX_REQUESTS, vision._transport_failures)

    def test_last_failure_times_are_reset_between_documents(self):
        vision._transport_last_failure_at['primary'] = self.clock.now
        vision.reset_model_availability()
        self.assertEqual({}, vision._transport_last_failure_at)

    def prime_expired_preference(self):
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(), Transient(), 'PM']):
            vision._call_vision('p', 'i')
        self.clock.now += config.OCR_TRANSPORT_HEALTH_PREFERENCE_SECONDS + 1

    def test_failed_primary_probe_can_failover_without_second_primary_failure(self):
        self.prime_expired_preference()
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(), 'PM']) as call:
            self.assertEqual('PM', vision._call_vision('same', 'image'))
        self.assertEqual(['primary', 'fallback'], [c.args[2] for c in call.call_args_list])
        self.assertEqual(3, vision._transport_failures)
        self.assertTrue(all(c.args[:2] == ('same', 'image') for c in call.call_args_list))

    def test_fast_failover_still_waits_for_server_delay(self):
        self.prime_expired_preference()
        started = self.clock.now
        with patch.object(vision, '_call_vision_once', side_effect=[Transient(503, '8'), 'PM']) as call:
            self.assertEqual('PM', vision._call_vision('p', 'i'))
        self.assertEqual(2, call.call_count)
        self.assertEqual(8, self.clock.now - started)

    def test_fast_failover_does_not_bypass_disallowed_or_cooling_fallback(self):
        for allow in (False, True):
            vision.reset_model_availability()
            self.prime_expired_preference()
            if allow:
                vision._model_retry_at['fallback'] = self.clock.now + 120
            with patch.object(vision, '_call_vision_once', side_effect=[Transient(), 'PM']) as call:
                self.assertEqual('PM', vision._call_vision('p', 'i', allow_fallback=allow))
            self.assertEqual(['primary', 'primary'], [c.args[2] for c in call.call_args_list])

    def test_fast_failover_never_bypasses_long_retry_after(self):
        self.prime_expired_preference()
        with patch.object(vision, '_call_vision_once', side_effect=Transient(429, '120')) as call:
            with self.assertRaises(vision.NvidiaServiceUnavailable):
                vision._call_vision('p', 'i')
        self.assertEqual(1, call.call_count)


class FieldRecoveryTests(unittest.TestCase):
    def setUp(self):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)

    def test_stable_wrong_serial_only_requests_bounded_readers_not_an_answer(self):
        ev = rules.evidence([reading(serial='ABC12X3400', phone=None)] * 2)
        fact = {'product_supported': True, 'hospital_supported': True, 'type_matches': True, 'serial_distance': 2}
        self.assertTrue(rules._should_recheck_stable_serial(ev, {'nearest_live_facts': [fact]}, []))
        for key in ('product_supported', 'hospital_supported', 'type_matches'):
            self.assertFalse(rules._should_recheck_stable_serial(ev, {'nearest_live_facts': [{**fact, key: False}]}, []))
        self.assertFalse(rules._should_recheck_stable_serial(ev, {'nearest_live_facts': [{**fact, 'serial_distance': 0}]}, []))
        for reason in rules.SEARCH_HARD_STOPS:
            self.assertFalse(rules._should_recheck_stable_serial(ev, {'nearest_live_facts': [fact]}, [reason]))

    def pipeline(self, serial_reads=(), *, search_error=None):
        live = task(kind='CM')
        raw = reading(serial='ABC12X3400', phone=None)
        ac.set_device_index(vocabulary_index([asana_index.task_to_record(live)]))
        focus_serial = iter(serial_reads)
        def focus(_doc, _page, field, **kwargs):
            return next(focus_serial, {}) if field == 'serial_candidates' else {}
        doc = MagicMock()
        doc.__len__.return_value = 1
        with ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ', {'JOBSHEET_DRY_RUN': '1', 'JOBSHEET_DATE_CORROBORATION': '0',
                'JOBSHEET_DEVICE_SEARCH_AGENT': '1' if search_error else '0'}))
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('No network')))
            for name in ('ocr_jobsheet_fields', 'ocr_jobsheet_identity_fields', 'ocr_jobsheet_support_fields'):
                stack.enter_context(patch.object(vision, name, return_value=raw))
            focused = stack.enter_context(patch.object(vision, 'ocr_jobsheet_focused_field', side_effect=focus))
            stack.enter_context(patch.object(ac, '_typeahead', return_value=[{'gid': '101'}]))
            stack.enter_context(patch.object(ac, '_fetch_task', return_value=live))
            if search_error:
                stack.enter_context(patch.object(device_search_agent, 'run_search', side_effect=search_error))
            result = rules.read_and_match(doc, 'CM', date(2026, 10, 5))
        return result, focused

    def test_two_correct_serial_reads_can_recover_without_rewriting_old_reads(self):
        correct = {'serial_candidates': ['ABC12X3456']}
        (selected, _, ocr), focused = self.pipeline([correct, correct])
        self.assertEqual('101', selected['gid'])
        self.assertEqual(2, sum(c.args[2] == 'serial_candidates' for c in focused.call_args_list))
        self.assertIn('stable_serial_reread', ocr['rules_audit']['trace'])
        self.assertIn('ABC12X3400', json.dumps(ocr['_ocr_audit']))

    def test_one_correct_serial_read_or_unchanged_errors_do_not_auto_pass(self):
        for reads in ([{'serial_candidates': ['ABC12X3456']}, {}], [{}, {}]):
            (selected, _, _), _ = self.pipeline(reads)
            self.assertIsNone(selected)

    def test_agent_service_failure_retains_successful_ocr_and_failure_class(self):
        (selected, _, ocr), _ = self.pipeline(search_error=vision.NvidiaServiceUnavailable('offline'))
        self.assertIsNone(selected)
        self.assertTrue(ocr['_vision_service_incomplete'])
        self.assertGreater(len(ocr['_ocr_audit']['readings']), 2)
        self.assertIn('device_search_unavailable', ocr['rules_audit']['trace'])
        with patch.object(backtest, '_read_sample', return_value=(None, 'PM', {}, ocr)):
            sample = backtest_tests.BacktestTests()._sample()
            doc = MagicMock(page_count=4)
            result = backtest._match_complete_sample(doc, sample)
        self.assertEqual('SERVICE_ERROR', result[-1]['status'])
        self.assertIs(ocr, result[-2])
        self.assertEqual('PASS', result[-1]['circle_check'])

    def test_agent_format_rejection_does_not_claim_transport_outage(self):
        (selected, _, ocr), _ = self.pipeline(search_error=vision.NvidiaResponseError('format'))
        self.assertIsNone(selected)
        self.assertFalse(ocr['_vision_service_incomplete'])

    def test_phone_focus_keeps_left_written_digits_but_not_contact_row(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.draw_rect(fitz.Rect(360, 165, 370, 188), color=(0, 0, 0), fill=(0, 0, 0))
            page.draw_rect(fitz.Rect(360, 120, 370, 150), color=(1, 0, 0), fill=(1, 0, 0))
            with patch.object(vision, '_preprocess_field_image', side_effect=lambda img, **_: img.convert('RGB')):
                image = vision._render_field_crop(doc, 0, 'phone_candidates', zoom=1, focused=True)
        pixels = list(image.getdata())
        self.assertGreater(sum(max(p) < 30 for p in pixels), 200)
        self.assertEqual(0, sum(r > 200 and g < 40 and b < 40 for r, g, b in pixels))


if __name__ == '__main__':
    unittest.main()

"""Synthetic resource-bound, crop and single-value serialization regressions."""
from datetime import date
import unittest
from unittest.mock import patch

import fitz

from src import asana_client as ac, matching_rules as rules, nvidia_client as vision
from tests.test_matching_rules import reading, task


class PerDeviceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.ev = rules.evidence([reading()] * 2)
        self.serials = ('ABC12X3456', 'ABC12X3457')
        self.rows = [{'serial': serial, 'task_refs': []} for serial in self.serials]

    def pool(self, count=60, *, same_actual=False):
        compact, full = {}, {}
        for device, serial in enumerate(self.serials):
            compact[serial] = []
            for visit in range(count):
                gid = str(1000 + device * 100 + visit)
                compact[serial].append({'gid': gid})
                full[gid] = task(gid, serial=self.serials[0] if same_actual else serial,
                                 due='2026-09-28' if visit == count - 1 else '2026-06-01')
        with patch.object(ac, '_typeahead', side_effect=lambda serial: compact[serial]), \
             patch.object(ac, '_fetch_task', side_effect=lambda gid: full[gid]) as fetch:
            pool, reason = rules.live_pool(self.rows, self.ev, None)
        return pool, reason, fetch.call_count

    def test_two_devices_each_under_eighty_do_not_fail_combined_history_limit(self):
        pool, reason, fetched = self.pool()
        self.assertEqual(('live_tasks_read', 120, 120), (reason, len(pool), fetched))
        self.assertEqual(set(self.serials), {rules._live_facts(item)['serial'] for item in pool})
        selected, _ = rules.select_task(pool, self.ev, 'PM', None)
        self.assertEqual('1059', selected['gid'])

    def test_rival_is_not_removed_to_create_a_unique_winner(self):
        pool, _, _ = self.pool()
        reads = [{**reading(), 'serial_candidates': list(self.serials)}] * 2
        self.assertEqual((None, 'live_devices_tied'),
                         rules.select_task(pool, rules.evidence(reads), 'PM', None))

    def test_eighty_one_visits_of_one_device_still_stops_before_fetch(self):
        pool, reason, fetched = self.pool(81)
        self.assertEqual(([], 'task_candidate_limit', 0), (pool, reason, fetched))

    def test_stale_rows_merging_into_overfull_live_device_still_hold(self):
        pool, _, _ = self.pool(same_actual=True)
        self.assertEqual((None, 'task_candidate_limit'),
                         rules.select_task(pool, self.ev, 'PM', None))

    def test_weak_or_unknown_serials_do_not_get_a_larger_hint_budget(self):
        for row in ({'weak_identity': True, 'serial': self.serials[0]}, {}):
            row['task_refs'] = [{'gid': str(i)} for i in range(81)]
            with patch.object(ac, '_typeahead') as search, patch.object(ac, '_fetch_task') as fetch:
                self.assertEqual(([], 'task_candidate_limit'), rules.live_pool([row], self.ev, None))
            search.assert_not_called()
            fetch.assert_not_called()

    def test_competitor_api_failure_is_not_an_empty_rival_pool(self):
        with patch.object(ac, '_typeahead', side_effect=[[], ac.AsanaError('offline')]), \
             patch.object(ac, '_fetch_task') as fetch:
            with self.assertRaises(ac.AsanaError):
                rules.live_pool(self.rows, self.ev, None)
        fetch.assert_not_called()

    def test_more_than_ten_serials_still_stops_before_network(self):
        rows = [{'serial': f'ABC12X34{i:02d}', 'task_refs': []} for i in range(11)]
        with patch.object(ac, '_typeahead') as search, patch.object(ac, '_fetch_task') as fetch:
            self.assertEqual(([], 'task_candidate_limit'), rules.live_pool(rows, self.ev, None))
        search.assert_not_called()
        fetch.assert_not_called()

    def test_ten_devices_with_eighty_visits_each_have_a_bounded_total(self):
        rows = [{'serial': f'ABC12X34{i:02d}', 'task_refs': [
            {'gid': str(i * 100 + j)} for j in range(80)]} for i in range(10)]
        with patch.object(ac, '_typeahead', return_value=[]), \
             patch.object(ac, '_fetch_task', side_effect=lambda gid: {'gid': gid}) as fetch:
            pool, reason = rules.live_pool(rows, self.ev, None)
        self.assertEqual(('live_tasks_read', rules.MAX_SCANNED_TASKS), (reason, len(pool)))
        self.assertEqual(800, fetch.call_count)


class SignatureSerializationTests(unittest.TestCase):
    def test_json_fence_and_unambiguous_single_date_serializations(self):
        for raw in ('{"signed_date":"28/9/2026"}', '```json\n{"signed_date":"2026-09-28"}\n```',
                    '28/9/2026', '"28/9/2026"',
                    'The handwritten date beside the signature in the panel is 28/9/2026.',
                    'The handwritten date beside the signature in the one labelled panel is 28/9/2026.'):
            self.assertEqual('2026-09-28', vision._parse_signature_date_reply(raw), raw)

    def test_null_is_unreadable_not_a_synthetic_date(self):
        for raw in ('null', '{"signed_date":null}'):
            self.assertIsNone(vision._parse_signature_date_reply(raw))

    def test_multiple_dates_uncertainty_partial_or_foreign_fields_are_rejected(self):
        for raw in ('28/9', '31/9/2026', '28/9/2026 or 29/9/2026', '28/9/2026?',
                    'The date might be 28/9/2026.', '{"signed_date":"28/9/2026","phone":"12345678"}',
                    '{"signed_date":"28/9/2026"} {"signed_date":"29/9/2026"}',
                    'Maybe {"signed_date":"28/9/2026"}',
                    'The date is 29/9/2026. {"signed_date":"28/9/2026"}',
                    '[{"signed_date":"28/9/2026"}]', '{"signed_date":20260928}',
                    '{"signed_date":{"value":"28/9/2026"}}'):
            with self.subTest(raw=raw), self.assertRaises(vision.NvidiaResponseError):
                vision._parse_signature_date_reply(raw)

    def test_general_json_and_multifield_ocr_parser_not_relaxed(self):
        with self.assertRaises(vision.NvidiaResponseError):
            vision._parse_json_object('28/9/2026', {'signed_date'})

    def test_one_literal_reply_is_one_reader_result_not_two_votes(self):
        with patch.object(vision, 'crop_jobsheet_field_card', return_value='same-panel'), \
             patch.object(vision, '_call_vision', return_value='28/9/2026') as call:
            self.assertEqual('2026-09-28', vision.ocr_jobsheet_signature_date(None, 0, 'engineer_signed_date'))
        self.assertEqual(1, call.call_count)

    def test_format_retry_never_supplies_previous_answer_or_asana_date(self):
        with patch.object(vision, 'crop_jobsheet_field_card', return_value='same-panel'), \
             patch.object(vision, '_call_vision', side_effect=['bad reply', '28/9/2026']) as call:
            self.assertEqual('2026-09-28', vision.ocr_jobsheet_signature_date(None, 0, 'engineer_signed_date'))
        self.assertEqual(2, call.call_count)
        for args, _ in call.call_args_list:
            self.assertNotIn('28/9/2026', args[0])
            self.assertNotIn('bad reply', args[0])


class CoverDateCropTests(unittest.TestCase):
    def test_upper_strokes_retained_without_stamp_or_footer(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            # Tall handwriting above the old .947 top: this must remain whole.
            page.draw_rect(fitz.Rect(480, 748, 490, 774), color=(0, 0, 0), fill=(0, 0, 0))
            page.draw_rect(fitz.Rect(350, 730, 440, 780), color=(1, 0, 0), fill=(1, 0, 0))
            page.draw_rect(fitz.Rect(450, 792, 590, 800), color=(1, 0, 0), fill=(1, 0, 0))
            with patch.object(vision, '_preprocess_field_image', side_effect=lambda img, **_: img.convert('RGB')):
                image = vision._render_field_crop(doc, 0, 'customer_signed_date', zoom=1)
        pixels = list(image.getdata())
        self.assertGreater(sum(max(pixel) < 30 for pixel in pixels), 230)
        self.assertEqual(0, sum(r > 200 and g < 40 and b < 40 for r, g, b in pixels))


if __name__ == '__main__':
    unittest.main()

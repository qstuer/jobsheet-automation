"""Synthetic PM date focus, provenance and unchanged live-acceptance gates."""
import base64
from contextlib import ExitStack
from datetime import date, timedelta
import io
import os
import unittest
from unittest.mock import MagicMock, patch

import fitz
from PIL import Image

from src import asana_client as ac, asana_index, checklist_date_focus as focus, config
from src import date_corroboration, matching_rules as rules, nvidia_client as vision, rclone_helper
from tests.test_matching_rules import reading, task
from tests.test_ocr_resilience import vocabulary_index
from tests.test_date_corroboration import panels


def support(day='2026-09-28'):
    return [{'page': focus.PAGE, 'source': focus.SOURCE, 'field': focus.FIELD,
             'zoom': zoom, 'date': day} for zoom in focus.ZOOMS]


class ChecklistDateProofTests(unittest.TestCase):
    def choose(self, records, *, reads=None, tasks=None, kind='PM', signatures=()):
        return rules.select_task(tasks or [task()], rules.evidence(reads or [reading(day=None)]*2),
            kind, None, allow_upload_prior=False, checklist_date_support=records, date_support=signatures)

    def test_two_consistent_header_reads_use_existing_pm_date_standard(self):
        proof = focus.summarize(support())
        self.assertEqual(('confirmed', date(2026, 9, 28), 2, 1),
                         (proof['status'], proof['date'], proof['read_count'], proof['page_count']))
        selected, reason = self.choose(support())
        self.assertEqual(('101', 'checklist_date_live_visit_confirmed'), (selected['gid'], reason))

    def test_one_read_or_unreadable_second_render_is_not_confirmed(self):
        for rows in (support()[:1], [support()[0], {**support()[1], 'date': None}], support(None)):
            self.assertEqual('insufficient', focus.summarize(rows)['status'])
            self.assertIsNone(self.choose(rows)[0])

    def test_disagreement_is_hold_not_majority_or_upload_prior(self):
        rows = [support()[0], {**support()[1], 'date': '2026-09-29'}]
        self.assertEqual((None, 'checklist_date_conflict'), self.choose(rows, signatures=panels()))
        selected, reason = rules.select_task([task()], rules.evidence([reading(day=None)]*2),
            'PM', date(2026, 10, 1), checklist_date_support=rows)
        self.assertEqual((None, 'checklist_date_conflict'), (selected, reason))

    def test_reliable_action_date_and_signature_conflicts_are_not_overridden(self):
        self.assertEqual((None, 'checklist_date_conflict'),
                         self.choose(support('2026-09-29'), reads=[reading()]*2))
        self.assertEqual((None, 'signature_date_conflict'),
                         self.choose(support(), signatures=panels('2026-09-29')))
        reads = [reading()]*2 + [reading(day='29/9/2026', page=1)]*2
        self.assertEqual((None, 'repeated_dates_conflict'), self.choose(support(), reads=reads))

    def test_wrong_year_is_not_repaired_and_fourteen_day_window_is_unchanged(self):
        self.assertEqual((None, 'live_visit_date_conflict'), self.choose(support('2006-09-28')))
        self.assertIsNotNone(self.choose(support(), tasks=[task(due='2026-09-14')])[0])
        self.assertIsNone(self.choose(support(), tasks=[task(due='2026-09-13')])[0])

    def test_identity_project_ties_and_ambiguous_orders_still_stop(self):
        for live, reads in ((task(hospital='Other Hospital'), [reading(day=None)]*2),
                            (task(kind='CM'), [reading(day=None)]*2),
                            (task(), [reading(serial='ABC12X0000', day=None)]*2),
                            (task(), [reading(serial='ABC12X3400', phone='28654321', day=None)]*2)):
            self.assertIsNone(self.choose(support(), tasks=[live], reads=reads)[0])
        self.assertEqual((None, 'live_visits_tied'), self.choose(support(), tasks=[task(), task(gid='102')]))
        ambiguous = task()
        ambiguous['name'] += '/ 60000011 / 60000022'
        self.assertEqual((None, 'ambiguous_order_numbers'), self.choose(support(), tasks=[ambiguous]))

    def test_provenance_duplicates_extra_reads_and_noncanonical_dates_rejected(self):
        for rows in (support() + support()[:1], [{**r, 'page': 3} for r in support()],
                     [{**r, 'page': True} for r in support()], [{**r, 'source': 'cover'} for r in support()],
                     [{**r, 'field': 'action_date'} for r in support()], [support()[0]]*2,
                     [{**r, 'date': '20260928'} for r in support()],
                     [{**r, 'date': '2026-09-31'} for r in support()],
                     [{**r, 'zoom': 7} for r in support()], [None], 123):
            self.assertEqual('invalid', focus.summarize(rows)['status'])
            self.assertEqual((None, 'checklist_date_protocol_rejected'), self.choose(rows))

    def test_checklist_source_is_never_accepted_for_cm(self):
        self.assertEqual((None, 'checklist_date_protocol_rejected'),
                         self.choose(support(), tasks=[task(kind='CM')], kind='CM'))

    def test_public_diagnostic_contains_counts_only(self):
        audit = rules.public_diagnostic({'version': rules.RULESET_VERSION, 'checklist_date_focus': focus.summarize(support()),
            'trace': ['checklist_date_live_visit_confirmed'], 'private': support()})
        self.assertEqual({'read_count': 2, 'page_count': 1, 'confirmed': True}, audit['checklist_date_focus'])
        self.assertNotIn('2026-09-28', str(audit))
        self.assertNotIn('checklist_header_date', str(audit))


class ChecklistDateReaderTests(unittest.TestCase):
    def setUp(self):
        flag = patch.dict(os.environ, {focus.ENABLED_ENV: '1', 'JOBSHEET_DRY_RUN': '1'})
        flag.start()
        self.addCleanup(flag.stop)

    def test_real_crop_preserves_date_edges_and_excludes_neighbouring_fields(self):
        with fitz.open() as doc:
            for _ in range(4):
                doc.new_page(width=600, height=850)
            doc[1].draw_rect(fitz.Rect(0, 0, 390, 850), color=None, fill=(1, 0, 0))
            doc[1].draw_rect(fitz.Rect(400, 155, 600, 185), color=None, fill=(1, 0, 0))
            for x, y in ((.667, .138), (.915, .165)):
                doc[1].draw_rect(fitz.Rect(x*600, y*850, x*600+5, y*850+4), color=None, fill=(0, 0, 1))
            with patch.object(vision, '_preprocess_field_image', side_effect=lambda image, **_: image.convert('RGB')):
                for zoom in focus.ZOOMS:
                    image = Image.open(io.BytesIO(base64.b64decode(focus.render(doc, 1, zoom))))
                    colors = list(image.getdata())
                    self.assertEqual(0, sum(r > 180 and g < 60 and b < 60 for r, g, b in colors))
                    self.assertGreater(sum(b > 180 and r < 60 and g < 60 for r, g, b in colors), 100)
                    self.assertLessEqual(image.width, 1132)

    def test_wrong_page_incomplete_document_or_invalid_zoom_rejected(self):
        for count, page, zoom in ((3, 1, 5), (4, 3, 5), (4, True, 5), (4, 1, 0),
                                  (4, 1, float('nan')), (4, 1, True)):
            doc = MagicMock()
            doc.__len__.return_value = count
            with self.assertRaises(ValueError):
                focus.render(doc, page, zoom)

    def test_format_retry_is_one_result_and_does_not_supply_answers(self):
        with patch.object(focus, 'render', return_value='only-date') as crop, \
             patch.object(vision, '_call_vision', side_effect=['bad', '{"checklist_date":"28/9/2026"}']) as call:
            value = vision.ocr_pm_checklist_focused_date(None)
        self.assertEqual('2026-09-28', value)
        self.assertEqual(2, call.call_count)
        crop.assert_called_once()
        self.assertEqual('insufficient', focus.summarize([{**support()[0], 'date': value}])['status'])
        for args in call.call_args_list:
            self.assertEqual('only-date', args.args[1])
            self.assertNotIn('2026', args.args[0])

    def test_invalid_dates_multiple_answers_uncertainty_and_extra_keys_fail_closed(self):
        for raw in ('{"checklist_date":"20/18/2026"}', '{"checklist_date":"28/9/206"}',
                    '{"checklist_date":"28/9/2026","extra":"x"}',
                    'maybe {"checklist_date":"28/9/2026"}',
                    '{"checklist_date":"28/9/2026"} or {"checklist_date":"29/9/2026"}'):
            with patch.object(focus, 'render', return_value='only-date'), \
                 patch.object(vision, '_call_vision', return_value=raw), self.assertRaises(vision.NvidiaResponseError):
                vision.ocr_pm_checklist_focused_date(None)

    def test_non_read_only_refused_before_crop_or_model(self):
        with patch.dict(os.environ, {'JOBSHEET_DRY_RUN': '0'}), \
             patch.object(focus, 'render') as crop, patch.object(vision, '_call_vision') as call:
            with self.assertRaises(ValueError):
                vision.ocr_pm_checklist_focused_date(None)
        crop.assert_not_called()
        call.assert_not_called()

    def test_out_of_window_year_is_rejected_without_repair_or_another_format_attempt(self):
        raw = '{"checklist_date":"28/9/2016"}'
        with patch.object(vision, '_today', return_value=date(2026, 9, 30)), \
             patch.object(focus, 'render', return_value='only-date'), \
             patch.object(vision, '_call_vision', return_value=raw) as call, \
             self.assertLogs(vision.log, level='INFO') as logs:
            self.assertIsNone(vision.ocr_pm_checklist_focused_date(None))
        self.assertEqual(1, call.call_count)
        self.assertIn('date_outside_window', str(logs.output))
        self.assertNotIn('2016', str(logs.output))

    def test_existing_header_age_and_future_boundaries_are_unchanged(self):
        today = date(2026, 9, 30)
        for age, accepted in ((config.OCR_SERVICE_DATE_MAX_AGE_DAYS, True),
                              (config.OCR_SERVICE_DATE_MAX_AGE_DAYS+1, False),
                              (-config.OCR_SERVICE_DATE_FUTURE_TOLERANCE_DAYS, True),
                              (-config.OCR_SERVICE_DATE_FUTURE_TOLERANCE_DAYS-1, False)):
            day = today-timedelta(days=age)
            raw = '{"checklist_date":"'+day.strftime('%d/%m/%Y')+'"}'
            with self.subTest(age=age), patch.object(vision, '_today', return_value=today), \
                 patch.object(focus, 'render', return_value='only-date'), \
                 patch.object(vision, '_call_vision', return_value=raw):
                self.assertEqual(day.isoformat() if accepted else None, vision.ocr_pm_checklist_focused_date(None))


class ChecklistDatePipelineTests(unittest.TestCase):
    def pipeline(self, *, kind='PM', dates=('2026-09-28', '2026-09-28'), day=None,
                 enabled=True, live=None):
        ac.clear_device_index()
        self.addCleanup(ac.clear_device_index)
        current = live or task(kind=kind)
        ac.set_device_index(vocabulary_index([asana_index.task_to_record(current)]))
        raw = reading(day=day)
        doc = MagicMock()
        doc.__len__.return_value = 4 if kind == 'PM' else 1
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {focus.ENABLED_ENV: '1' if enabled else '0',
                'JOBSHEET_DRY_RUN': '1', 'JOBSHEET_DEVICE_SEARCH_AGENT': '0', date_corroboration.ENABLED_ENV: '1'}))
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')))
            for name in ('upload', 'upload_unique', 'delete', 'moveto', 'run', 'run_result'):
                stack.enter_context(patch.object(rclone_helper, name, side_effect=AssertionError('Cloud writes forbidden')))
            for name in ('ocr_jobsheet_fields', 'ocr_jobsheet_identity_fields', 'ocr_jobsheet_support_fields'):
                stack.enter_context(patch.object(vision, name, return_value=raw))
            for name in ('ocr_jobsheet_context_field', 'ocr_jobsheet_focused_field'):
                stack.enter_context(patch.object(vision, name, return_value={}))
            stack.enter_context(patch.object(rules, '_checklist_read', return_value={'_page': 1}))
            stack.enter_context(patch.object(vision, '_call_vision', side_effect=AssertionError('Unscheduled model call')))
            focused = stack.enter_context(patch.object(vision, 'ocr_pm_checklist_focused_date', side_effect=dates))
            signed = stack.enter_context(patch.object(vision, 'ocr_jobsheet_signature_date', return_value=None))
            stack.enter_context(patch.object(ac, '_fetch_task', return_value=current))
            stack.enter_context(patch.object(ac, '_typeahead', return_value=[]))
            result = rules.read_and_match(doc, kind)
        return result, focused, signed

    def test_two_focused_reads_select_without_rewriting_action_date_or_identity(self):
        (selected, _, ocr), focused, signed = self.pipeline()
        self.assertEqual('101', selected['gid'])
        self.assertEqual(2, focused.call_count)
        signed.assert_not_called()
        self.assertIsNone(ocr['service_date_raw'])
        self.assertEqual(2, len(ocr['_checklist_date_audit']))
        self.assertEqual([], ocr['_signature_date_audit'])

    def test_conflict_does_not_seek_more_date_votes(self):
        (selected, _, ocr), focused, signed = self.pipeline(dates=('2026-09-28', '2026-09-29'))
        self.assertIsNone(selected)
        self.assertEqual(2, focused.call_count)
        signed.assert_not_called()
        self.assertIn('checklist_date_conflict', ocr['rules_audit']['trace'])

    def test_reliably_read_wrong_year_does_not_seek_signatures_to_replace_it(self):
        (selected, _, ocr), focused, signed = self.pipeline(dates=('2006-09-28', '2006-09-28'))
        self.assertIsNone(selected)
        self.assertEqual(2, focused.call_count)
        signed.assert_not_called()
        self.assertIn('live_visit_date_conflict', ocr['rules_audit']['trace'])

    def test_partial_read_keeps_original_bounded_signature_path(self):
        (selected, _, ocr), focused, signed = self.pipeline(dates=('2026-09-28', None))
        self.assertIsNone(selected)
        self.assertEqual(2, focused.call_count)
        self.assertEqual(8, signed.call_count)
        self.assertIsNone(ocr['service_date_raw'])

    def test_service_failure_is_preserved_not_mislabeled_as_paper_failure(self):
        (selected, _, ocr), focused, _ = self.pipeline(dates=(vision.NvidiaServiceUnavailable('offline'), None))
        self.assertIsNone(selected)
        self.assertEqual(2, focused.call_count)
        self.assertTrue(ocr['_vision_service_incomplete'])
        self.assertIn('checklist_date_focus_unavailable', ocr['rules_audit']['trace'])

    def test_no_extra_read_for_cm_stable_date_or_disabled_flag(self):
        for options in ({'kind': 'CM'}, {'day': '28/9/2026'}, {'enabled': False}):
            _, focused, _ = self.pipeline(**options)
            focused.assert_not_called()

    def test_identity_must_pass_before_extra_date_read(self):
        (selected, _, _), focused, _ = self.pipeline(live=task(hospital='Other Hospital'))
        self.assertIsNone(selected)
        focused.assert_not_called()

    def test_read_only_gate_runs_before_any_cover_read(self):
        with patch.dict(os.environ, {focus.ENABLED_ENV: '1', 'JOBSHEET_DRY_RUN': '0'}), \
             patch.object(vision, 'ocr_jobsheet_fields') as cover:
            with self.assertRaises(ValueError):
                rules.read_and_match(None, 'PM')
        cover.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Synthetic title roles; no customer examples or external calls."""
from datetime import date
import unittest
from unittest.mock import patch

from src import asana_client as ac, matching_rules as rules, processor, rclone_helper
from tests.test_matching_rules import reading, task


class OrderNumberResolutionTests(unittest.TestCase):
    def test_single_order_and_repeated_same_order(self):
        for title in ('Test Hospital / Affiniti 70 / ABC12X3456 / 60000011',
                      'Test Hospital / ABC12X3456 / SR#60000011 / 60000011'):
            self.assertEqual('60000011', ac.extract_order_no_from_name({'name': title}))

    def test_leading_reference_never_names_this_job(self):
        live = task(prefix='(transfer part from 60000011)')
        live['name'] += '/ 60000022'
        self.assertEqual(('SR#60000022.pdf', '60000022'), processor._planned_filename(live))

    def test_nested_and_repeated_balanced_annotations(self):
        title = '(reference (60000011))[CM]Test Hospital/ ABC12X3456/ 60000022'
        self.assertEqual('60000022', ac.extract_order_no_from_name({'name': title}))

    def test_multiple_body_orders_annotation_only_and_unclosed_comment_are_held(self):
        for title in ('Test Hospital/ ABC12X3456/ 60000011/ 60000022',
                      '(reference 60000011)Test Hospital/ ABC12X3456',
                      '(reference 60000011 Test Hospital/ ABC12X3456/ 60000022'):
            with self.subTest(title=title), self.assertRaises(ac.AmbiguousOrderNumberError) as error:
                processor._planned_filename({'name': title})
            self.assertNotIn('60000011', str(error.exception))

    def test_explicit_non_order_fields_are_not_orders(self):
        for label in ('Tel:', 'Phone:', 'Mobile:', 'Asset:', 'HAWO', 'WO:'):
            self.assertIsNone(ac.extract_order_no_from_name({'name': f'Test Hospital/ {label} 60000011'}))
        self.assertEqual('60000022', ac.extract_order_no_from_name(
            {'name': 'Test Hospital/ Tel: 60000011/ 60000022'}))

    def test_true_missing_order_still_uses_complete_safe_title(self):
        title = 'Test Hospital/ Affiniti 70/ ABC12X3456'
        self.assertEqual(('Test Hospital - Affiniti 70 - ABC12X3456.pdf', ''),
                         processor._planned_filename({'name': title}))

    def test_embedded_serial_and_longer_digits_are_not_orders(self):
        self.assertIsNone(ac.extract_order_no_from_name({'name': 'ABC60000011 / 1600000112'}))

    def test_ambiguous_matching_visit_cannot_disappear_and_leave_a_wrong_visit(self):
        ambiguous = task()
        ambiguous['name'] += '/ 60000011/ 60000022'
        old = task(gid='102', due='2026-09-20')
        old['name'] += '/ 60000033'
        selected, reason = rules.select_task([ambiguous, old], rules.evidence([reading()] * 2),
                                             'PM', date(2026, 10, 5))
        self.assertIsNone(selected)
        self.assertEqual('ambiguous_order_numbers', reason)

    def test_explicit_ocr_order_does_not_resolve_two_body_orders(self):
        live = task()
        live['name'] += '/ 60000011/ 60000022'
        reads = [{**reading(), 'order_no': '60000022'}] * 2
        selected, reason = rules.select_task([live], rules.evidence(reads), 'PM', date(2026, 10, 5))
        self.assertIsNone(selected)
        self.assertEqual('ambiguous_order_numbers', reason)

    def test_finalizer_refuses_ambiguous_title_before_any_upload(self):
        live = {'name': 'Test Hospital/ 60000011/ 60000022'}
        with patch.object(rclone_helper, 'upload_unique') as upload:
            with self.assertRaises(ac.AmbiguousOrderNumberError):
                processor._finalize_match(None, live, 2, 'synthetic.pdf')
        upload.assert_not_called()

    def test_public_reason_contains_no_number(self):
        public = rules.public_diagnostic({'version': rules.RULESET_VERSION,
            'trace': ['ambiguous_order_numbers'], 'private_title': '60000011 / 60000022'})
        self.assertEqual(['ambiguous_order_numbers'], public['trace'])
        self.assertNotIn('60000011', str(public))


if __name__ == '__main__':
    unittest.main()

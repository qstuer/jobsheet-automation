"""A bounded candidate may schedule phone reading, never certify identity."""
from contextlib import ExitStack
from datetime import date
import unittest
from unittest.mock import MagicMock, patch

from src import asana_client as ac, asana_index, matching_rules as rules, nvidia_client as vision
from tests.test_matching_rules import reading, task
from tests.test_ocr_resilience import vocabulary_index


class UnstableSerialPhoneTests(unittest.TestCase):
    def readings(self):
        return [reading(serial=s, phone='28654321') for s in ('ABC12X3450', 'ABC12X3406', 'ABC12X3056')]

    def facts(self, **extra):
        return {'nearest_live_facts':[{'product_supported':True, 'hospital_supported':True,
            'type_matches':True, 'serial_distance':1, 'phone_exact':False, **extra}]}

    def test_near_literal_serials_need_not_agree_before_phone_can_be_read(self):
        ev = rules.evidence(self.readings())
        self.assertFalse(rules._stable(ev, 'serial_candidates'))
        self.assertTrue(rules._should_recheck_stable_phone(ev, self.facts(), []))
        self.assertFalse(rules._should_recheck_stable_phone(ev, {}, []))

    def test_wrong_type_distant_serial_or_conflicting_identity_cannot_request_read(self):
        ev = rules.evidence(self.readings())
        for delta in ({'type_matches':False}, {'serial_distance':4}, {'product_supported':False},
                      {'hospital_supported':False}, {'product_conflict':True}, {'hospital_conflict':True}):
            self.assertFalse(rules._should_recheck_stable_phone(ev, self.facts(**delta), []))
        missing = [reading(serial=None, phone='28654321')] * 2
        self.assertFalse(rules._should_recheck_stable_phone(rules.evidence(missing), self.facts(), []))

    def test_known_ties_or_already_supported_phone_do_not_trigger_new_pass(self):
        ev = rules.evidence(self.readings())
        for reason in rules.SEARCH_HARD_STOPS:
            self.assertFalse(rules._should_recheck_stable_phone(ev, self.facts(), [reason]))
        self.assertFalse(rules._should_recheck_stable_phone(ev, self.facts(phone_exact=True), []))

    def test_scheduling_never_changes_matching_evidence_or_acceptance(self):
        reads = self.readings()
        ev = rules.evidence(reads)
        snapshot = repr(ev)
        self.assertTrue(rules._should_recheck_stable_phone(ev, self.facts(), []))
        self.assertEqual(snapshot, repr(ev))
        self.assertIsNone(rules.select_task([task()], ev, 'PM', None)[0])

    def pipeline(self, focus_values=None, due='2026-09-28'):
        self.addCleanup(ac.clear_device_index)
        live = task(due=due)
        ac.set_device_index(vocabulary_index([asana_index.task_to_record(live)]))
        initial = self.readings()
        doc = MagicMock()
        doc.__len__.return_value = 4
        fields = []
        snapshots = []
        original_evidence = rules.evidence
        def observed_evidence(readings):
            ev = original_evidence(readings)
            snapshots.append(ev)
            return ev
        values = iter(focus_values if focus_values is not None else [{'phone_candidates':['23456789']}] * 2)
        def focus(_doc, _page, field, **kwargs):
            fields.append(field)
            return next(values) if field == 'phone_candidates' else {}
        with ExitStack() as stack:
            stack.enter_context(patch.object(rules, 'evidence', side_effect=observed_evidence))
            stack.enter_context(patch.dict('os.environ', {'JOBSHEET_DRY_RUN':'1',
                'JOBSHEET_DEVICE_SEARCH_AGENT':'0', 'JOBSHEET_DATE_CORROBORATION':'0',
                'JOBSHEET_SERIAL_SHAPE_REFERENCE':'0'}))
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('No network')))
            for name, value in zip(('ocr_jobsheet_fields','ocr_jobsheet_identity_fields','ocr_jobsheet_support_fields'), initial):
                stack.enter_context(patch.object(vision, name, return_value=value))
            stack.enter_context(patch.object(vision, 'ocr_jobsheet_focused_field', side_effect=focus))
            stack.enter_context(patch.object(rules, '_checklist_read', return_value={}))
            stack.enter_context(patch.object(ac, '_typeahead', return_value=[{'gid':'101'}]))
            stack.enter_context(patch.object(ac, '_fetch_task', return_value=live))
            selected, _, result = rules.read_and_match(doc, 'PM', date(2026,10,5))
        self.last_evidence = snapshots[-1]
        return selected, result, fields

    def test_pipeline_adds_only_existing_two_phone_reads_and_keeps_old_values(self):
        selected, result, fields = self.pipeline()
        self.assertEqual(2, fields.count('phone_candidates'))
        self.assertEqual(1, result['rules_audit']['trace'].count('stable_phone_reread'))
        self.assertIn('28654321', str(result['_ocr_audit']))
        self.assertEqual('101', selected['gid'])  # Original gates accept actual phone/date support, not scheduling alone.

    def test_one_correct_phone_read_remains_one_vote_under_original_matching_policy(self):
        selected, _, fields = self.pipeline([{'phone_candidates':['23456789']}, {}])
        self.assertEqual(2, fields.count('phone_candidates'))
        self.assertEqual(1, self.last_evidence['phone_candidates']['23456789']['votes'])
        self.assertNotIn('23456789', rules._stable(self.last_evidence, 'phone_candidates'))
        self.assertEqual('101', selected['gid'])  # Existing long-serial rules, not new acceptance logic.

    def test_wrong_phone_or_formal_date_conflict_stays_pending(self):
        for focus, due in (([{'phone_candidates':['28654321']}] * 2, '2026-09-28'),
                           ([{'phone_candidates':['23456789']}] * 2, '2026-08-01')):
            selected, _, fields = self.pipeline(focus, due)
            self.assertEqual(2, fields.count('phone_candidates'))
            self.assertIsNone(selected)


if __name__ == '__main__':
    unittest.main()

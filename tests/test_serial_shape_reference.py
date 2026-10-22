"""Advisory masks must not leak or invent serials or create extra votes."""
import json
import unittest
from unittest.mock import patch

from src import config, matching_rules as rules, nvidia_client as vision, serial_shape_reference as reference
from tests import test_transport_field_recovery as pipelines


class SerialShapeTests(unittest.TestCase):
    def device(self, serial, product='EPIQ CVx', **extra):
        return {'serial': serial, 'product_variants': [product], **extra}

    def index(self, count=3):
        return {'devices': [self.device(f'AB123C{i:04d}') for i in range(count)]}

    def test_three_distinct_devices_not_repeated_tasks_or_duplicate_rows(self):
        self.assertEqual([], reference.project(self.index(2), {'EPIQ'}))
        idx = self.index(3)
        idx['devices'] += [idx['devices'][0]] * 20
        self.assertEqual([{'pattern': 'LLDDDLDDDD', 'devices': 3}], reference.project(idx, {'EPIQ'}))

    def test_weak_unknown_product_and_other_family_do_not_certify_pattern(self):
        idx = {'devices': [self.device(f'AB123C{i:04d}', weak_identity=True) for i in range(3)]}
        self.assertEqual([], reference.project(idx, {'EPIQ'}))
        for families in ({'AFFINITI'}, {'EPIQ','AFFINITI'}, set()):
            self.assertEqual([], reference.project(self.index(), families))
        self.assertEqual([], reference.project({'devices':[self.device(f'AB123C{i:04d}', product='Unverified77') for i in range(3)]}, {'UNVERIFIED'}))

    def test_projection_contains_classes_and_counts_only(self):
        idx = self.index()
        for row in idx['devices']:
            row.update(phones=['23456789'], contacts=['Private'], task_refs=[{'gid':'123456789'}])
        data = reference.project(idx, {'EPIQ'})
        self.assertEqual({'pattern','devices'}, set(data[0]))
        for secret in ('AB123C0000','23456789','Private','123456789','EPIQ CVx'):
            self.assertNotIn(secret, json.dumps(data))

    def read(self, raw):
        with patch.dict('os.environ', {reference.ENABLED_ENV:'1', 'JOBSHEET_DRY_RUN':'1'}), \
             patch.object(vision, 'crop_jobsheet_field_card', return_value='same image') as crop, \
             patch.object(vision, '_call_vision', return_value=json.dumps(raw)) as call:
            result = vision.ocr_jobsheet_serial_with_shapes(None, 0, reference.project(self.index(), {'EPIQ'}))
        self.assertEqual(('product_raw','serial_candidates'), crop.call_args.args[2])
        return result, call

    def test_assisted_serial_never_enters_votes_or_replaces_literal(self):
        result, call = self.read({'serial_candidates':['5B123C0000'], 'assisted_candidates':['AB123C0000']})
        self.assertEqual([], result['serial_candidates'])
        self.assertEqual(['5B123C0000'], result['_ocr_audit']['raw']['serial_candidates'])
        ev = rules.evidence([result])
        self.assertNotIn('AB123C0000', ev['serial_candidates'])
        self.assertNotIn('AB123C0000', call.call_args.args[0])
        self.assertEqual(1, call.call_count)

    def test_clear_uncommon_pattern_is_not_rejected_by_reference(self):
        result, _ = self.read({'serial_candidates':['ABC12F3456'], 'assisted_candidates':[]})
        self.assertEqual(['ABC12F3456'], result['serial_candidates'])

    def test_read_only_and_reference_validation_happen_before_calls(self):
        with patch.object(vision, '_call_vision') as call:
            for mode in ('0', ''):
                with patch.dict('os.environ', {reference.ENABLED_ENV:'1', 'JOBSHEET_DRY_RUN':mode}):
                    with self.assertRaises(ValueError):
                        vision.ocr_jobsheet_serial_with_shapes(None, 0, [{'pattern':'LLDDDLDDDD','devices':3}])
            with patch.dict('os.environ', {reference.ENABLED_ENV:'1', 'JOBSHEET_DRY_RUN':'1'}):
                for shapes in ([], [{'pattern':'AB123C0000','devices':3}], [{'pattern':'LLDDDLDDDD','devices':2}]):
                    with self.assertRaises(ValueError):
                        vision.ocr_jobsheet_serial_with_shapes(None, 0, shapes)
        call.assert_not_called()

    def test_six_x_hint_replaces_one_existing_read_not_an_extra_vote(self):
        correct = {'serial_candidates':['ABC12X3456']}
        self.addCleanup(pipelines.ac.clear_device_index)
        with patch.dict('os.environ', {reference.ENABLED_ENV:'1'}), \
             patch.object(reference, 'project', return_value=[{'pattern':'LLLDDLDDDD','devices':3}]), \
             patch.object(vision, 'ocr_jobsheet_serial_with_shapes', return_value=correct) as hinted:
            (selected, _, ocr), focused = pipelines.FieldRecoveryTests().pipeline([correct])
        self.assertEqual('101', selected['gid'])
        self.assertEqual(1, hinted.call_count)
        self.assertEqual(1, sum(c.args[2]=='serial_candidates' for c in focused.call_args_list))
        self.assertIn('serial_shape_reference_read', ocr['rules_audit']['trace'])


if __name__ == '__main__':
    unittest.main()

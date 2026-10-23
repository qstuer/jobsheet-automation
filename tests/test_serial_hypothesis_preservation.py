"""More retained evidence must not disable an already-supported serial path."""
from copy import deepcopy
import unittest

from src import matching_rules as rules
from tests.test_matching_rules import reading, task


class SerialHypothesisTests(unittest.TestCase):
    def observations(self, fuzzy='ABC12X3450', asset='12345678', phone='29876543'):
        return [reading(serial=fuzzy, asset=asset, phone=phone),
                reading(serial=fuzzy, asset=asset, phone=phone),
                reading(serial='ABC12X3456', asset=asset, phone=phone)]

    def test_lone_exact_read_does_not_veto_existing_fuzzy_asset_path(self):
        actual = task(asset='12345678')
        observations = self.observations()
        original = deepcopy(observations)
        without = rules.assess_task(actual, rules.evidence(observations[:2]), 'PM')
        with_exact = rules.assess_task(actual, rules.evidence(observations), 'PM')
        self.assertIsNotNone(without)
        self.assertIsNotNone(with_exact)
        self.assertEqual(without['score'], with_exact['score'])
        self.assertEqual(1, with_exact['serial_distance'])
        self.assertEqual(original, observations)
        self.assertEqual(1, rules.evidence(observations)['serial_candidates']['ABC12X3456']['votes'])

    def test_two_and_three_character_alternatives_keep_their_lower_score_tier(self):
        for fuzzy, distance in [('ABC12X3400', 2), ('ABC12X3000', 3)]:
            observations = self.observations(fuzzy=fuzzy)
            a = rules.assess_task(task(asset='12345678'), rules.evidence(observations[:2]), 'PM')
            b = rules.assess_task(task(asset='12345678'), rules.evidence(observations), 'PM')
            self.assertEqual(a['score'], b['score'])
            self.assertEqual(distance, b['serial_distance'])

    def test_no_supported_alternative_cannot_promote_single_exact_read(self):
        for fuzzy in (None, 'ABC12X0000'):
            self.assertIsNone(rules.assess_task(task(asset='12345678'),
                rules.evidence(self.observations(fuzzy=fuzzy)), 'PM'))

    def test_missing_or_fuzzy_asset_does_not_substitute_for_exact_asset_support(self):
        for asset in (None, '12345670', '87654321'):
            self.assertIsNone(rules.assess_task(task(asset=asset),
                rules.evidence(self.observations()), 'PM'))

    def test_hospital_product_and_project_conflicts_still_stop(self):
        for actual in (task(asset='12345678', hospital='Different Hospital'),
                       task(asset='12345678', product='CX50'),
                       task(asset='12345678', kind='CM')):
            self.assertIsNone(rules.assess_task(actual, rules.evidence(self.observations()), 'PM'))

    def test_wrong_visit_date_and_tied_visits_still_stop(self):
        ev = rules.evidence(self.observations())
        for tasks in ([task(asset='12345678', due='2026-07-01')],
                      [task(gid='101', asset='12345678'), task(gid='102', asset='12345678')]):
            chosen, _ = rules.select_task(tasks, ev, 'PM', None)
            self.assertIsNone(chosen)


if __name__ == '__main__':
    unittest.main()

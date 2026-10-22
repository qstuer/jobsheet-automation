"""Task-status comments are not hospital locations or private index orders."""
import json
import unittest

from src import asana_index as index
from tests.test_matching_rules import task


class StatusLocationTests(unittest.TestCase):
    def test_order_in_leading_status_comment_is_not_serialized_as_location(self):
        raw = task(hospital="(transfer parts from 61234567)Test Hospital-3F-Room2")
        before = raw["name"]
        result = index.task_to_record(raw)
        self.assertNotIn("61234567", json.dumps(result))
        self.assertEqual(["Test Hospital-3F-Room2"], result["locations"])
        self.assertEqual("Test Hospital-3F-Room2", result["task_refs"][0]["location"])
        self.assertEqual(before, raw["name"])
        self.assertEqual("ABC12X3456", result["serial"])

    def test_multiple_status_comments_removed_without_removing_room_details(self):
        result = index.task_to_record(task(hospital="(Office)(Aug)Test Hospital-3F-Room2"))
        self.assertEqual(["Test Hospital-3F-Room2"], result["locations"])
        self.assertEqual("Test Hospital", result["hospitals"][0])

    def test_parentheses_in_actual_location_are_preserved(self):
        result = index.task_to_record(task(hospital="Test Hospital (East Wing)"))
        self.assertEqual(["Test Hospital (East Wing)"], result["locations"])


if __name__ == "__main__":
    unittest.main()

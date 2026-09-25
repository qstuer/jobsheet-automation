"""Synthetic repeated-visit safety checks; no customer records are embedded."""
import unittest
from datetime import date
from unittest.mock import patch

from src import asana_client


def _task(gid: str, due_on: str) -> dict:
    return {
        "gid": gid,
        "name": "Example Hospital / Affiniti 70 / AB123B4567",
        "due_on": due_on,
        "memberships": [{"project": {"name": "2026 PM"}}],
    }


def _ocr(day: str) -> dict:
    return {
        "serial_candidates": ["AB123B4567"],
        "product_raw": "Affiniti 70",
        "hospital_raw": "Example Hospital",
        "service_date_iso": day,
        "date_source": "ACTION_DATE",
    }


class RepeatedVisitDateGuardTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [_task("older", "2026-08-20"),
                      _task("newer", "2026-09-29")]

    def _find(self, day):
        with patch.object(asana_client, "_device_index", None), \
                patch.object(asana_client, "_gather_pool", return_value=self.tasks):
            return asana_client.find_task(_ocr(day), job_type="PM")

    def test_midpoint_date_cannot_choose_wrong_month_by_score_bucket(self):
        # The newer visit is 11 days away and the older is 29 days away.
        # Neither has a phone, asset or work-order distinguishing it.
        task, tier = self._find("2026-09-18")
        self.assertIsNone(task)
        self.assertEqual(0, tier)

    def test_exact_formal_date_can_select_visit(self):
        task, tier = self._find("2026-08-20")
        self.assertEqual("older", task["gid"])
        self.assertEqual(2, tier)

    def test_single_hydrated_visit_does_not_hide_repeated_history(self):
        # The index can remove an older visit before live Asana hydration.
        # A handwritten month error must not turn the remaining visit into a
        # falsely unique task when its formal date is still 10 days away.
        with patch.object(asana_client, "_device_index", {"schema_version": 3}), \
                patch.object(asana_client, "_gather_index_pool",
                             return_value=([self.tasks[1]], True, True)):
            task, tier = asana_client.find_task(_ocr("2026-09-19"), job_type="PM")
        self.assertIsNone(task)
        self.assertEqual(0, tier)

    def test_old_date_in_notes_does_not_override_formal_date(self):
        dates = asana_client._task_dates({
            "due_on": "2026-08-20",
            "completed_at": "2026-08-21T10:00:00Z",
            "notes": "Previous visit: 2026-09-18",
        })
        self.assertEqual([date(2026, 8, 20), date(2026, 8, 21)], dates)

    def test_notes_only_used_when_formal_dates_missing(self):
        dates = asana_client._task_dates({"notes": "Visit: 2026-08-20"})
        self.assertEqual([date(2026, 8, 20)], dates)


if __name__ == "__main__":
    unittest.main()

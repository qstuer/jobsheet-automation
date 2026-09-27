"""Synthetic checks for the isolated recent-month visual prompt experiment."""

import unittest
from datetime import date

from src import recent_month_date_probe as probe


class RecentMonthDateProbeTests(unittest.TestCase):
    def test_rolling_window_keeps_july_control(self):
        start, months = probe._window(date(2026, 9, 27))
        self.assertEqual(start, date(2026, 7, 19))
        self.assertEqual(months, {"07", "08", "09"})

    def test_prompt_has_window_but_not_reviewed_answer(self):
        start, months = probe._window(date(2026, 9, 27))
        neutral = probe._prompt(recent=False, start=start, as_of=date(2026, 9, 27), months=months)
        recent = probe._prompt(recent=True, start=start, as_of=date(2026, 9, 27), months=months)
        self.assertNotIn("2026-09-27", neutral)
        self.assertIn("2026-09-27", recent)
        self.assertIn("only a weak prior", recent)
        self.assertIn("8 and 9", recent)

    def test_month_parser_rejects_ambiguous_or_invalid_reading(self):
        self.assertEqual(probe._month("8"), "08")
        self.assertIsNone(probe._month("8?"))
        self.assertIsNone(probe._month("13"))
        self.assertIsNone(probe._month(8))

    def test_correct_prior_guess_without_visual_panel_is_not_supported(self):
        reading = {"action_month": None, "customer_month": None,
                   "checklist_month": None, "selected_month": "08"}
        grade = probe._grade(reading, "08", {"07", "08", "09"})
        self.assertTrue(grade["correct"])
        self.assertFalse(grade["supported_correct"])

    def test_september_control_must_not_be_forced_to_august(self):
        reading = {"action_month": "08", "customer_month": "08",
                   "checklist_month": "08", "selected_month": "08"}
        grade = probe._grade(reading, "09", {"07", "08", "09"})
        self.assertFalse(grade["correct"])
        self.assertEqual(grade["panel_support_count"], 3)


if __name__ == "__main__":
    unittest.main()

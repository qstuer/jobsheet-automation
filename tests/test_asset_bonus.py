"""Synthetic-only checks: Asset is optional evidence, not a penalty."""
import unittest
from unittest.mock import patch

from src import asana_client as client


def sheet(assets=None):
    return {
        "serial_candidates": ["ZZ123F4567"], "product_raw": "Affiniti 70",
        "hospital_raw": "QMH", "asset_candidates": assets or [],
    }


def device(assets=None):
    return {
        "serial": "ZZ123F4567", "weak_identity": False,
        "product_families": ["AFFINITI"], "hospital_aliases": ["QMH"],
        "assets": assets or [],
    }


def task(gid="test-task", assets="1234567890"):
    return {
        "gid": gid, "name": "QMH / Affiniti 70 / ZZ123F4567",
        "notes": f"Asset# {assets}" if assets else "",
        "memberships": [{"project": {"name": "PM"}}],
    }


def score(assets=None, candidate=None):
    return client._candidate_score(
        candidate or task(), sheet(assets), ["ZZ123F4567"], "QMH", "Affiniti 70", "PM",
    )


class AssetBonusTests(unittest.TestCase):
    def setUp(self):
        client.clear_device_index()

    def tearDown(self):
        client.clear_device_index()

    def test_seventy_percent_three_changes_is_fuzzy_not_exact(self):
        self.assertEqual(70, client._asset_similarity_percent("1234567890", "1234560000"))
        self.assertEqual(1, client._asset_match_level(["1234567890"], ["1234560000"]))

    def test_sixty_percent_has_no_bonus(self):
        self.assertEqual(60, client._asset_similarity_percent("1234567890", "1234500000"))
        self.assertEqual(0, client._asset_match_level(["1234567890"], ["1234500000"]))

    def test_threshold_rounds_half_up_without_float_error(self):
        # 16/23=69.565...% rounds to 70; 9/13=69.23...% stays below.
        self.assertEqual(1, client._asset_match_level(["1" * 23], ["1" * 16 + "2" * 7]))
        self.assertEqual(0, client._asset_match_level(["1" * 13], ["1" * 9 + "2" * 4]))
        self.assertEqual(63, client._asset_similarity_percent("1" * 8, "1" * 5 + "2" * 3))

    def test_blank_short_or_missing_values_do_not_match(self):
        for left, right in [(None, None), ([], ["12345"]), (["123"], ["123"]),
                            ([""], [""]), ([None], [None])]:
            with self.subTest(left=left, right=right):
                self.assertEqual(0, client._asset_match_level(left, right))

    def test_separators_are_ignored_and_duplicates_do_not_stack(self):
        self.assertEqual(2, client._asset_match_level(["1234-5678"], ["1234 5678"]))
        self.assertEqual(score(["1234567890"])["score"],
                         score(["1234567890", "1234567890"])["score"])

    def test_rounded_hundred_percent_is_not_exact(self):
        self.assertEqual(1, client._asset_match_level(["1" * 201], ["1" * 200 + "2"]))

    def test_live_score_has_no_missing_or_mismatch_penalty(self):
        baseline = score()["score"]
        self.assertEqual(baseline, score(["9999999999"])["score"])
        self.assertEqual(baseline, score(["1234567890"], task(assets=""))["score"])
        self.assertEqual(baseline + 15, score(["1234567000"])["score"])
        self.assertEqual(baseline + 35, score(["1234567890"])["score"])

    def test_index_task_uses_same_threshold_once_for_all_history_values(self):
        ref = {"assets": ["1234567890", "1234567890"], "job_type": "PM"}
        baseline = client._score_index_task_ref(ref, sheet(), "PM")
        self.assertEqual(baseline, client._score_index_task_ref(ref, sheet(["9999999999"]), "PM"))
        self.assertEqual(baseline + 10,
                         client._score_index_task_ref(ref, sheet(["1234567000"]), "PM"))
        self.assertEqual(baseline + 20,
                         client._score_index_task_ref(ref, sheet(["1234567890"]), "PM"))

    def test_device_gate_is_unchanged_by_absent_or_wrong_asset(self):
        row = device(["1234567890"])
        scores = [client._score_index_device(row, sheet(values), "PM")
                  for values in ([], ["9999999999"], ["1234567000"], ["1234567890"])]
        self.assertTrue(all(item["eligible"] for item in scores))
        self.assertEqual(scores[0]["auxiliary"], scores[1]["auxiliary"])
        self.assertIn("asset_fuzzy", scores[2]["support"])
        self.assertNotIn("asset_exact", scores[2]["support"])
        self.assertIn("asset_exact", scores[3]["support"])
        self.assertLess(scores[2]["auxiliary"], scores[3]["auxiliary"])

    def test_confirmed_serial_can_match_with_no_or_wrong_asset(self):
        for values in ([], ["9999999999"]):
            with self.subTest(values=values), patch.object(client, "_gather_pool", return_value=[task()]):
                matched, tier = client.find_task(sheet(values), "PM")
                self.assertEqual("test-task", matched["gid"])
                self.assertEqual(2, tier)

    def test_bonus_does_not_select_between_identical_historical_tasks(self):
        with patch.object(client, "_gather_pool", return_value=[task("one"), task("two")]):
            self.assertEqual((None, 0), client.find_task(sheet(["1234567000"]), "PM"))

    def test_fuzzy_asset_cannot_replace_exact_identity_rescue(self):
        # Existing no-Serial recovery is not loosened by the new bonus threshold.
        values = sheet(["1234567000"])
        values["serial_candidates"] = []
        values["phone_candidates"] = ["61234567"]
        row = dict(device(["1234567890"]), phones=["61234567"])
        result = client._score_index_device(row, values, "PM")
        self.assertEqual([], client._filter_ranked_device_scores([result], False))

    def test_public_match_log_does_not_include_asset_values(self):
        with patch.object(client, "_gather_pool", return_value=[task()]), \
                self.assertLogs(client.log, level="INFO") as captured:
            client.find_task(sheet(["1234567000"]), "PM")
        self.assertNotIn("1234567000", "\n".join(captured.output))
        self.assertNotIn("1234567890", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()

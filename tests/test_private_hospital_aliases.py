"""Hospital aliases must come from private index data, not repo literals."""

import unittest
from datetime import date

from src import asana_client, asana_index


class PrivateHospitalAliasTests(unittest.TestCase):
    def tearDown(self):
        asana_client.clear_device_index()

    def test_private_directory_corrects_small_full_name_ocr_error(self):
        asana_client.set_device_index({
            "devices": [],
            "location_directory": [{
                "canonical_hospital": "Example General Hospital",
                "confirmed_aliases": ["Example General Hospital", "EGH"],
                "learned_aliases": [],
                "match_enabled": True,
            }],
        })
        self.assertEqual("Example General Hospital", asana_client.hospital_core(
            "Exomple Genoral Hospital"
        ))
        self.assertIsNone(asana_client.hospital_core("ZZZ"))

    def test_unconfirmed_directory_alias_cannot_become_a_match(self):
        asana_client.set_device_index({
            "devices": [],
            "location_directory": [{
                "canonical_hospital": "Example General Hospital",
                "confirmed_aliases": ["Example General Hospital"],
                "learned_aliases": ["EGH"],
                "match_enabled": False,
            }],
        })
        self.assertIsNone(asana_client.hospital_core("EGH"))

    def test_conflicting_private_alias_does_not_choose_first_or_last_group(self):
        asana_client.set_device_index({
            "devices": [],
            "location_directory": [
                {"canonical_hospital": "Alpha General Hospital",
                 "confirmed_aliases": ["Shared General Hospital"],
                 "learned_aliases": [], "match_enabled": True},
                {"canonical_hospital": "Beta General Hospital",
                 "confirmed_aliases": ["Shared General Hospital"],
                 "learned_aliases": [], "match_enabled": True},
            ],
        })
        self.assertEqual("Shared General Hospital", asana_client.hospital_core(
            "Shared General Hospital"
        ))
        self.assertEqual(["Shared General Hospital", "SGH"],
                         asana_client._index_hospital_aliases(
                             "Shared General Hospital"))

    def test_code_only_index_does_not_invent_official_name(self):
        task = {
            "gid": "synthetic-1", "name": "QMH / CX50 / US000F0000",
            "notes": "", "modified_at": "2026-09-01T00:00:00Z",
            "created_at": "2026-09-01T00:00:00Z",
            "due_on": "2026-09-01", "_project_job_type": "PM",
        }
        index = asana_index.build_index(
            [task], window_start=date(2025, 1, 1),
            window_end=date(2026, 12, 31),
        )
        self.assertEqual("QMH", index["location_directory"][0]["canonical_hospital"])

    def test_full_name_from_private_task_replaces_code_as_display_name(self):
        base = {
            "notes": "", "modified_at": "2026-09-01T00:00:00Z",
            "created_at": "2026-09-01T00:00:00Z",
            "due_on": "2026-09-01", "_project_job_type": "PM",
        }
        tasks = [
            {**base, "gid": "synthetic-1", "name": "QMH / CX50 / US000F0000"},
            {**base, "gid": "synthetic-2", "name":
             "Quick Medical Hospital / CX50 / US000F0001"},
        ]
        index = asana_index.build_index(
            tasks, window_start=date(2025, 1, 1),
            window_end=date(2026, 12, 31),
        )
        self.assertEqual(1, index["location_count"])
        self.assertEqual("Quick Medical Hospital",
                         index["location_directory"][0]["canonical_hospital"])


if __name__ == "__main__":
    unittest.main()

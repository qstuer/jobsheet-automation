"""Regression tests for the serial-row index and layered fuzzy search."""
import unittest
from datetime import date
from unittest.mock import patch

from src import asana_client, asana_index, config, nvidia_client


def task(gid, name, *, notes="", due_on="2026-09-08", project_type="PM"):
    return {
        "gid": gid, "name": name, "notes": notes, "due_on": due_on,
        "created_at": "2026-09-01T00:00:00Z",
        "modified_at": "2026-09-10T00:00:00Z", "completed_at": "",
        "start_on": "", "completed": False, "permalink_url": "",
        "_project_job_type": project_type,
    }


def row(serial, *, phone="61234567", asset="19130438", hospital="PYNEH"):
    ref = {
        "gid": f"task-{serial}", "work_dates": ["2026-09-08"], "job_type": "PM",
        "location": hospital, "hospital": hospital, "hospital_aliases": [hospital],
        "department_rooms": ["3F"], "product": "AFFINITI",
        "product_family": "AFFINITI", "product_variant": "Affiniti 70",
        "serial": serial, "phones": [phone], "contacts": ["Alice"],
        "assets": [asset],
    }
    return {
        "device_key": serial, "weak_identity": False, "serial": serial,
        "product": "AFFINITI", "product_families": ["AFFINITI"],
        "product_variants": ["Affiniti 70", "Affiniti 70G"],
        "hospitals": [hospital], "hospital_aliases": [hospital],
        "locations": [hospital, f"{hospital}-3F"], "department_rooms": ["3F"],
        "phones": [phone], "contacts": ["Alice"], "assets": [asset],
        "work_dates": ["2026-09-08"], "job_types": ["PM"], "task_refs": [ref],
    }


def ocr(serial="USN16F0565", **values):
    data = {
        "serial_candidates": [serial] if serial else [], "serial_no": serial or None,
        "product_raw": "Affiniti 70G", "hospital_raw": "PYN",
        "phone_candidates": ["61234567"], "contact_person_raw": "Alice",
        "asset_candidates": ["19130438"], "department_room_raw": "3F",
        "service_date_raw": "08/09/2026", "date_source": "ACTION_DATE",
    }
    data.update(values)
    return data


class SerialRowIndexTests(unittest.TestCase):
    def test_one_serial_is_one_row_even_when_product_family_changes(self):
        index = asana_index.build_index([
            task("1", "PYNEH / Affiniti 70 / USN16F0565", notes="Phone 61234567 Contact: Alice"),
            task("2", "PYN / EPIQ Elite / USN16F0565", notes="Phone 69876543 Contact: Bob"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(3, index["schema_version"])
        self.assertEqual(1, index["device_count"])
        device = index["devices"][0]
        self.assertEqual("USN16F0565", device["device_key"])
        self.assertEqual({"AFFINITI", "EPIQ"}, set(device["product_families"]))
        self.assertEqual({"Alice", "Bob"}, set(device["contacts"]))
        self.assertEqual(2, len(device["task_refs"]))

    def test_unknown_product_family_is_learned_from_title_segment(self):
        record = asana_index.task_to_record(
            task("1", "QMH / Lumify Pro / USN16F0565"), "PM"
        )
        self.assertEqual("LUMIFY", record["product"])
        self.assertEqual(["Lumify Pro"], record["product_variants"])

    def test_alias_learning_requires_two_serials_not_one_relocated_device(self):
        one = asana_index.build_index([
            task("1", "Alpha Hospital / CX50 / USN16F0565"),
            task("2", "Beta Hospital / CX50 / USN16F0565"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual([], one["hospital_alias_groups"])

        learned = asana_index.build_index([
            task("3", "SGH / CX50 / USN16F0566"),
            task("4", "Starlight General Hospital / CX50 / USN16F0566"),
            task("5", "SGH / CX50 / USN16F0567"),
            task("6", "Starlight General Hospital / CX50 / USN16F0567"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertTrue(learned["hospital_alias_groups"])

    def test_unified_location_table_separates_hospital_from_floor_and_room(self):
        index = asana_index.build_index([
            task("1", "PYNEH-3F-Xray / CX50 / USN16F0565"),
            task("2", "PYN / Ultrasound 6F / CX50 / USN16F0566"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(1, index["location_count"])
        location = index["location_directory"][0]
        self.assertEqual(
            "Pamela Youde Nethersole Eastern Hospital",
            location["canonical_hospital"],
        )
        self.assertEqual(2, location["device_count"])
        self.assertIn("3F-Xray", location["department_rooms"])
        self.assertIn("Ultrasound 6F", location["department_rooms"])
        self.assertIn("PYN", location["confirmed_aliases"])

    def test_unknown_short_location_is_visible_but_never_match_enabled(self):
        index = asana_index.build_index([
            task("1", "KWM / CX50 / USN16F0565"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        location = index["location_directory"][0]
        self.assertFalse(location["match_enabled"])
        self.assertEqual(["KWM"], location["unconfirmed_aliases"])
        self.assertEqual("", location["canonical_hospital"])

    def test_status_prefix_and_short_code_room_are_not_hospital_names(self):
        self.assertEqual(
            ("QMH", "A7"),
            asana_client.split_hospital_location("(Cancel) QMH A7"),
        )
        self.assertEqual(
            ("TKO", "MB-G-A"),
            asana_client.split_hospital_location("TKO MB-G-A"),
        )
        self.assertEqual(
            ("Alpha Medical Diagnostic Centre", ""),
            asana_client.split_hospital_location("Alpha Medical Diagnostic Centre"),
        )

    def test_location_table_groups_status_variants_under_confirmed_hospital(self):
        index = asana_index.build_index([
            task("1", "(Cancel) QMH A7 / CX50 / USN16F0565"),
            task("2", "(Office)QMH K3 / CX50 / USN16F0566"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(1, index["location_count"])
        location = index["location_directory"][0]
        self.assertEqual("Queen Mary Hospital", location["canonical_hospital"])
        self.assertIn("A7", location["department_rooms"])
        self.assertIn("K3", location["department_rooms"])

    def test_unclosed_status_text_is_visible_but_not_matchable(self):
        index = asana_index.build_index([
            task("1", "(**Before 14 / CX50 / USN16F0565"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        location = index["location_directory"][0]
        self.assertFalse(location["match_enabled"])
        self.assertEqual("", location["canonical_hospital"])
        self.assertEqual(["(**Before 14"], location["unconfirmed_aliases"])

    def test_relocated_serial_keeps_two_distinct_full_hospitals(self):
        index = asana_index.build_index([
            task("1", "Alpha Hospital / CX50 / USN16F0565"),
            task("2", "Beta Hospital / CX50 / USN16F0565"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        names = {
            row["canonical_hospital"] for row in index["location_directory"]
        }
        self.assertEqual({"Alpha Hospital", "Beta Hospital"}, names)
        self.assertEqual([], index["hospital_alias_groups"])


class LayeredSearchTests(unittest.TestCase):
    def setUp(self):
        asana_client.clear_device_index()

    def tearDown(self):
        asana_client.clear_device_index()

    def test_product_group_and_similarity_rules(self):
        self.assertEqual("AFFINITI", asana_client.product_group("Affiniti 50"))
        self.assertEqual("AFFINITI", asana_client.product_group("Affiniti 70G"))
        self.assertEqual("EPIQ", asana_client.product_group("EPIQ CVx"))
        self.assertGreaterEqual(asana_client._family_similarity("EPLQ", "EPIQ"), .33)
        self.assertEqual(0, asana_client._family_similarity("CX", "CT"))

    def test_unknown_short_hospital_does_not_enter_fuzzy_search(self):
        self.assertEqual([], asana_client.hospital_aliases("PN"))
        self.assertIn("PYNEH", asana_client.hospital_aliases("PYN"))
        self.assertIn("PYNEH", asana_client.hospital_aliases("PYNEH-3F"))

    def test_embedded_location_directory_expands_a_learned_short_name(self):
        index = asana_index.build_index([
            task("1", "SGH / CX50 / USN16F0566"),
            task("2", "Starlight General Hospital / CX50 / USN16F0566"),
            task("3", "SGH / CX50 / USN16F0567"),
            task("4", "Starlight General Hospital / CX50 / USN16F0567"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        asana_client.set_device_index(index)
        expanded = asana_client._index_hospital_aliases("SGH")
        self.assertIn("Starlight General Hospital", expanded)

    def test_location_directory_is_expanded_once_per_sheet_not_once_per_device(self):
        asana_client.set_device_index({
            "schema_version": 3,
            "devices": [row("USN16F0565"), row("USN16F0566")],
            "location_directory": [],
        })
        original = asana_client._index_hospital_aliases
        with patch.object(
            asana_client, "_index_hospital_aliases", wraps=original
        ) as expand:
            asana_client._rank_index_devices(ocr(), "PM")
        self.assertEqual(1, expand.call_count)

    def test_serial_fifty_percent_gate(self):
        device = row("USN16F0565")
        passing = asana_client._score_index_device(device, ocr("USN16FGGGX"))
        failing = asana_client._score_index_device(device, ocr("AAA99BBBBB"))
        self.assertGreaterEqual(passing["serial_similarity"], .50)
        self.assertTrue(passing["eligible"])
        self.assertFalse(failing["eligible"])

    def test_close_candidates_are_capped_at_ten_and_contain_no_task_order(self):
        rows = [row(f"USN16F05{i:02d}") for i in range(12)]
        asana_client.set_device_index({"schema_version": 3, "devices": rows})
        candidates = asana_client.get_close_index_candidates(ocr("USN16F05XX"), "PM")
        self.assertEqual(10, len(candidates))
        self.assertEqual("C1", candidates[0]["candidate_id"])
        self.assertNotIn("task_refs", candidates[0])
        self.assertNotIn("order_no", candidates[0])

    def test_candidate_exactly_ten_percentage_points_behind_is_included(self):
        asana_client.set_device_index({
            "schema_version": 3,
            "devices": [row("ABCDEFGHXX"), row("ABCDEFGXXX")],
        })
        candidates = asana_client.get_close_index_candidates(
            ocr("ABCDEFGHIJ"), "PM"
        )
        self.assertEqual(2, len(candidates))

    def test_serial_missing_requires_unique_product_hospital_phone_and_asset(self):
        asana_client.set_device_index({"schema_version": 3, "devices": [row("USN16F0565")]})
        ranked = asana_client._rank_index_devices(ocr(None), "PM")
        self.assertEqual(1, len(ranked))
        two = [row("USN16F0565"), row("USN16F0566")]
        asana_client.set_device_index({"schema_version": 3, "devices": two})
        self.assertEqual([], asana_client._rank_index_devices(ocr(None), "PM"))
        with patch.object(asana_client, "_fetch_task") as fetch:
            pool, index_had_candidates = asana_client._gather_index_pool(ocr(None), "PM")
        self.assertEqual([], pool)
        self.assertTrue(index_had_candidates)
        fetch.assert_not_called()

    def test_action_date_over_one_month_rejects_historical_task(self):
        ref = row("USN16F0565")["task_refs"][0]
        self.assertGreaterEqual(asana_client._score_index_task_ref(ref, ocr(), "PM"), 0)
        ref = dict(ref, work_dates=["2026-07-01"])
        self.assertEqual(-1000, asana_client._score_index_task_ref(ref, ocr(), "PM"))

    def test_candidate_resolver_accepts_only_listed_c_number(self):
        candidates = [{"candidate_id": "C1", "device_key": "secret", "serial": "ABC12345"}]
        with patch.object(nvidia_client, "crop_jobsheet_field_card", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=(
                    '{"candidate_id":"C1","matched_fields":["serial"],'
                    '"conflicting_fields":[],"uncertain":false}'
                )) as call:
            self.assertEqual("C1", nvidia_client.choose_device_candidate(object(), 0, candidates))
        self.assertNotIn("device_key", call.call_args.args[0] if call.call_args.args else call.call_args.kwargs["prompt"])

    def test_candidate_resolver_uncertain_means_pending(self):
        candidates = [{"candidate_id": "C1", "device_key": "secret", "serial": "ABC12345"}]
        with patch.object(nvidia_client, "crop_jobsheet_field_card", return_value="image"), \
                patch.object(nvidia_client, "_call_vision", return_value=(
                    '{"candidate_id":null,"matched_fields":[],"conflicting_fields":[],"uncertain":true}'
                )):
            self.assertIsNone(nvidia_client.choose_device_candidate(object(), 0, candidates))


if __name__ == "__main__":
    unittest.main()

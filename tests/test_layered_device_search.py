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


def row(serial, *, phone="99990011", asset="88880001", hospital="PYNEH"):
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


def dated_row(serial, *, hospital="PYNEH", phone="99990002",
              due_on="2026-08-20", completed_at="2026-08-19T05:28:07Z"):
    device = row(serial, phone=phone, hospital=hospital)
    device.update({
        "product": "EPIQ", "product_families": ["EPIQ"],
        "product_variants": ["EPIQ Elite"],
    })
    ref = device["task_refs"][0]
    ref.update({
        "product": "EPIQ", "product_family": "EPIQ",
        "product_variant": "EPIQ Elite", "due_on": due_on,
        "completed_at": completed_at, "work_dates": [due_on],
    })
    device["work_dates"] = [due_on]
    return device


def ocr(serial="USX00F0001", **values):
    data = {
        "serial_candidates": [serial] if serial else [], "serial_no": serial or None,
        "product_raw": "Affiniti 70G", "hospital_raw": "PYN",
        "phone_candidates": ["99990011"], "contact_person_raw": "Alice",
        "asset_candidates": ["88880001"], "department_room_raw": "3F",
        "service_date_raw": "08/09/2026", "date_source": "ACTION_DATE",
    }
    data.update(values)
    return data


class SerialRowIndexTests(unittest.TestCase):
    def test_one_serial_is_one_row_even_when_product_family_changes(self):
        index = asana_index.build_index([
            task("1", "PYNEH / Affiniti 70 / USX00F0001", notes="Phone 99990011 Contact: Alice"),
            task("2", "PYN / EPIQ Elite / USX00F0001", notes="Phone 99990014 Contact: Bob"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(3, index["schema_version"])
        self.assertEqual(1, index["device_count"])
        device = index["devices"][0]
        self.assertEqual("USX00F0001", device["device_key"])
        self.assertEqual({"AFFINITI", "EPIQ"}, set(device["product_families"]))
        self.assertEqual({"Alice", "Bob"}, set(device["contacts"]))
        self.assertEqual(2, len(device["task_refs"]))

    def test_unknown_product_family_is_learned_from_title_segment(self):
        record = asana_index.task_to_record(
            task("1", "QMH / Lumify Pro / USX00F0001"), "PM"
        )
        self.assertEqual("LUMIFY", record["product"])
        self.assertEqual(["Lumify Pro"], record["product_variants"])

    def test_alias_learning_requires_two_serials_not_one_relocated_device(self):
        one = asana_index.build_index([
            task("1", "Alpha Hospital / CX50 / USX00F0001"),
            task("2", "Beta Hospital / CX50 / USX00F0001"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual([], one["hospital_alias_groups"])

        learned = asana_index.build_index([
            task("3", "SGH / CX50 / USX00F0002"),
            task("4", "Starlight General Hospital / CX50 / USX00F0002"),
            task("5", "SGH / CX50 / USX00F0003"),
            task("6", "Starlight General Hospital / CX50 / USX00F0003"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertTrue(learned["hospital_alias_groups"])

    def test_unified_location_table_separates_hospital_from_floor_and_room(self):
        index = asana_index.build_index([
            task("1", "PYNEH-3F-Xray / CX50 / USX00F0001"),
            task("2", "PYN / Ultrasound 6F / CX50 / USX00F0002"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(1, index["location_count"])
        location = index["location_directory"][0]
        self.assertEqual("PYNEH", location["canonical_hospital"])
        self.assertEqual(2, location["device_count"])
        self.assertIn("3F-Xray", location["department_rooms"])
        self.assertIn("Ultrasound 6F", location["department_rooms"])
        self.assertIn("PYN", location["confirmed_aliases"])

    def test_unknown_short_location_is_visible_but_never_match_enabled(self):
        index = asana_index.build_index([
            task("1", "KWM / CX50 / USX00F0001"),
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
            task("1", "(Cancel) QMH A7 / CX50 / USX00F0001"),
            task("2", "(Office)QMH K3 / CX50 / USX00F0002"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(1, index["location_count"])
        location = index["location_directory"][0]
        self.assertEqual("QMH", location["canonical_hospital"])
        self.assertIn("A7", location["department_rooms"])
        self.assertIn("K3", location["department_rooms"])

    def test_unclosed_status_text_is_visible_but_not_matchable(self):
        index = asana_index.build_index([
            task("1", "(**Before 14 / CX50 / USX00F0001"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        location = index["location_directory"][0]
        self.assertFalse(location["match_enabled"])
        self.assertEqual("", location["canonical_hospital"])
        self.assertEqual(["(**Before 14"], location["unconfirmed_aliases"])

    def test_relocated_serial_keeps_two_distinct_full_hospitals(self):
        index = asana_index.build_index([
            task("1", "Alpha Hospital / CX50 / USX00F0001"),
            task("2", "Beta Hospital / CX50 / USX00F0001"),
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
            task("1", "SGH / CX50 / USX00F0002"),
            task("2", "Starlight General Hospital / CX50 / USX00F0002"),
            task("3", "SGH / CX50 / USX00F0003"),
            task("4", "Starlight General Hospital / CX50 / USX00F0003"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        asana_client.set_device_index(index)
        expanded = asana_client._index_hospital_aliases("SGH")
        self.assertIn("Starlight General Hospital", expanded)

    def test_location_directory_is_expanded_once_per_sheet_not_once_per_device(self):
        asana_client.set_device_index({
            "schema_version": 3,
            "devices": [row("USX00F0001"), row("USX00F0002")],
            "location_directory": [],
        })
        original = asana_client._index_hospital_aliases
        with patch.object(
            asana_client, "_index_hospital_aliases", wraps=original
        ) as expand:
            asana_client._rank_index_devices(ocr(), "PM")
        self.assertEqual(1, expand.call_count)

    def test_serial_fifty_percent_gate(self):
        device = row("USX00F0001")
        passing = asana_client._score_index_device(device, ocr("USX00GGGGG"))
        failing = asana_client._score_index_device(device, ocr("AAA99BBBBB"))
        self.assertGreaterEqual(passing["serial_similarity"], .50)
        self.assertTrue(passing["eligible"])
        self.assertFalse(failing["eligible"])

    def test_close_candidates_are_capped_at_ten_and_contain_no_task_order(self):
        rows = [row(f"USX00F00{i:02d}") for i in range(12)]
        asana_client.set_device_index({"schema_version": 3, "devices": rows})
        candidates = asana_client.get_close_index_candidates(ocr("USX00F00XX"), "PM")
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
        asana_client.set_device_index({"schema_version": 3, "devices": [row("USX00F0001")]})
        ranked = asana_client._rank_index_devices(ocr(None), "PM")
        self.assertEqual(1, len(ranked))
        two = [row("USX00F0001"), row("USX00F0002")]
        asana_client.set_device_index({"schema_version": 3, "devices": two})
        self.assertEqual([], asana_client._rank_index_devices(ocr(None), "PM"))
        with patch.object(asana_client, "_fetch_task") as fetch:
            pool, index_had_candidates, repeated = asana_client._gather_index_pool(
                ocr(None), "PM"
            )
        self.assertEqual([], pool)
        self.assertTrue(index_had_candidates)
        self.assertFalse(repeated)
        fetch.assert_not_called()

    def test_repeated_visits_remain_visible_after_date_prefilter(self):
        device = row("USX00F0001")
        recent = dict(device["task_refs"][0], gid="recent",
                      due_on="2026-09-18", work_dates=["2026-09-18"])
        older = dict(device["task_refs"][0], gid="older",
                     due_on="2026-07-01", work_dates=["2026-07-01"])
        device["task_refs"] = [recent, older]
        asana_client.set_device_index({"schema_version": 3, "devices": [device]})
        with patch.object(asana_client, "_fetch_task", return_value=task(
            "recent", "PYN / Affiniti 70 / USX00F0001", due_on="2026-09-18"
        )):
            pool, had_candidates, repeated = asana_client._gather_index_pool(
                ocr("USX00F0001", service_date_raw="18/09/2026"), "PM"
            )
        self.assertTrue(had_candidates)
        self.assertEqual(1, len(pool))
        self.assertTrue(repeated)

        device["task_refs"][1] = dict(older, due_on="2026-05-01",
                                      work_dates=["2026-05-01"])
        asana_client.set_device_index({"schema_version": 3, "devices": [device]})
        with patch.object(asana_client, "_fetch_task", return_value=task(
            "recent", "PYN / Affiniti 70 / USX00F0001", due_on="2026-09-18"
        )):
            _, _, distant_repeat = asana_client._gather_index_pool(
                ocr("USX00F0001", service_date_raw="18/09/2026"), "PM"
            )
        self.assertFalse(distant_repeat)

    def test_action_date_over_one_month_rejects_historical_task(self):
        ref = row("USX00F0001")["task_refs"][0]
        self.assertGreaterEqual(asana_client._score_index_task_ref(ref, ocr(), "PM"), 0)
        ref = dict(ref, work_dates=["2026-07-01"])
        self.assertEqual(-1000, asana_client._score_index_task_ref(ref, ocr(), "PM"))

    def _date_joint_choice(self, devices, **changes):
        values = ocr(
            "USV00B0009", product_raw="EPIQ Elite", hospital_raw="PYN",
            phone_candidates=["99990002"], service_date_raw="19/08/2026",
            date_source="ACTION_DATE",
        )
        values.update(changes)
        asana_client.set_device_index({"schema_version": 3, "devices": devices})
        prepared = asana_client._prepare_index_query(values)
        scored = [
            asana_client._score_index_device(device, prepared, "PM")
            for device in devices
        ]
        return asana_client._select_date_joint_device(scored, prepared, "PM")

    def test_date_joint_evidence_corrects_one_serial_character(self):
        wrong_exact = dated_row(
            "USV00B0009", hospital="TMH", phone="99990020",
            due_on="2026-08-12", completed_at="2026-08-12T07:35:59Z",
        )
        correct = dated_row("USV00B0006")
        selected, ambiguous = self._date_joint_choice([wrong_exact, correct])
        self.assertFalse(ambiguous)
        self.assertEqual("USV00B0006", selected["row"]["serial"])

    def test_date_joint_evidence_requires_matching_hospital(self):
        selected, ambiguous = self._date_joint_choice([
            dated_row("USV00B0006", hospital="TMH"),
        ])
        self.assertIsNone(selected)
        self.assertFalse(ambiguous)

    def test_date_joint_evidence_requires_exact_phone(self):
        selected, ambiguous = self._date_joint_choice([
            dated_row("USV00B0006", phone="99990021"),
        ])
        self.assertIsNone(selected)
        self.assertFalse(ambiguous)

    def test_date_joint_evidence_requires_date_within_one_day(self):
        selected, ambiguous = self._date_joint_choice([
            dated_row(
                "USV00B0006", due_on="2026-08-17",
                completed_at="2026-08-17T05:28:07Z",
            ),
        ])
        self.assertIsNone(selected)
        self.assertFalse(ambiguous)

    def test_date_joint_evidence_never_corrects_two_serial_characters(self):
        selected, ambiguous = self._date_joint_choice([
            dated_row("USV00B0066"),
        ])
        self.assertIsNone(selected)
        self.assertFalse(ambiguous)

    def test_date_joint_evidence_keeps_multiple_devices_pending(self):
        selected, ambiguous = self._date_joint_choice([
            dated_row("USV00B0006"), dated_row("USV00B0008"),
        ])
        self.assertIsNone(selected)
        self.assertTrue(ambiguous)

    def test_structured_dates_take_precedence_over_old_note_dates(self):
        ref = dated_row("USV00B0006")["task_refs"][0]
        ref["work_dates"] = ["2024-01-01", "2026-08-20"]
        self.assertEqual(
            [date(2026, 8, 20), date(2026, 8, 19)],
            asana_client._index_dates(ref),
        )

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

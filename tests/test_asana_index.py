"""Asana 設備索引的純本機回歸測試。"""
import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import asana_client, asana_index


def task(gid, name, *, modified="2026-09-10T00:00:00Z", notes="", project_type="PM",
         due_on="2026-09-08"):
    return {
        "gid": gid,
        "name": name,
        "notes": notes,
        "modified_at": modified,
        "created_at": modified,
        "completed_at": modified,
        "due_on": due_on,
        "start_on": "",
        "permalink_url": f"https://app.asana.com/0/0/{gid}",
        "completed": True,
        "_project_job_type": project_type,
    }


class AsanaIndexTests(unittest.TestCase):
    def test_transient_asana_error_is_retried(self):
        limited = MagicMock(status_code=429, headers={"Retry-After": "1"})
        success = MagicMock(status_code=200, headers={})
        success.json.return_value = {"data": []}
        with patch.object(asana_index.config, "ASANA_TOKEN", "token"), \
                patch.object(asana_index.config, "ASANA_WORKSPACE_GID", "workspace"), \
                patch.object(
                    asana_index.requests, "get", side_effect=[limited, success]
                ) as request, \
                patch.object(asana_index.time, "sleep") as sleep:
            payload = asana_index._request("https://example.invalid/tasks", {})
        self.assertEqual({"data": []}, payload)
        self.assertEqual(2, request.call_count)
        sleep.assert_called_once_with(1.0)

    def test_same_device_merges_history_but_different_serials_stay_separate(self):
        rows = asana_index.build_index([
            task("1", "PYNEH / Affiniti 70 / US123F4567", notes="Phone 99990011 Contact: Alice Asset# 88880001"),
            task("2", "PYN / Affiniti 70G / US123F4567", notes="Phone 99990013 Contact: Bob Asset# 88880001", project_type="CM"),
            task("3", "PYNEH / Affiniti 70 / US123F4568", notes="Phone 99990012 Asset# 88880002"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(rows["device_count"], 2)
        merged = next(row for row in rows["devices"] if row["serial"] == "US123F4567")
        self.assertEqual({ref["gid"] for ref in merged["task_refs"]}, {"1", "2"})
        self.assertEqual(set(merged["job_types"]), {"PM", "CM"})
        self.assertIn("PYNEH", merged["hospitals"])
        self.assertEqual(set(merged["product_variants"]), {"Affiniti 70", "Affiniti 70G"})
        self.assertEqual(set(merged["contacts"]), {"Alice", "Bob"})
        refs = {ref["gid"]: ref for ref in merged["task_refs"]}
        self.assertEqual(refs["1"]["contacts"], ["Alice"])
        self.assertEqual(refs["2"]["contacts"], ["Bob"])
        self.assertEqual(refs["1"]["phones"], ["99990011"])
        self.assertEqual(refs["2"]["phones"], ["99990013"])
        self.assertEqual(rows["merged_task_count"], 1)

    def test_unlabelled_asset_number_is_not_indexed_as_phone(self):
        record = asana_index.task_to_record(task(
            "1",
            "PYN / Affiniti 70 / US123F4567",
            notes="Asset# 88880001\nSecondary asset 8888002",
        ), "PM")
        self.assertEqual(["88880001", "8888002"], record["assets"])
        self.assertEqual([], record["phones"])

    def test_labelled_phone_is_kept_while_asset_is_excluded(self):
        record = asana_index.task_to_record(task(
            "1",
            "PYN / Affiniti 70 / US123F4567",
            notes="Phone: 99990001\nAsset# 88880001",
        ), "PM")
        self.assertEqual(["99990001"], record["phones"])
        self.assertEqual(["88880001"], record["assets"])

    def test_month_named_projects_are_pm(self):
        self.assertEqual("PM", asana_index._project_is_pm_cm("2026 Jun"))
        self.assertEqual("PM", asana_index._project_is_pm_cm("2025 September"))
        self.assertIsNone(asana_index._project_is_pm_cm("2026 rollout"))

    def test_unlabelled_contact_after_phone_and_wo_are_indexed(self):
        record = asana_index.task_to_record(task(
            "1",
            "PYN / Affiniti 70 / US123F4567 / 60000011",
            notes="1 / 2 PMS\n99990001 Ms.Sample\nwo: 88880001",
        ), "PM")
        self.assertEqual(["99990001"], record["phones"])
        self.assertEqual(["Ms.Sample"], record["contacts"])
        self.assertEqual(["88880001"], record["assets"])

    def test_one_extra_phone_digit_is_kept_only_as_fuzzy_evidence(self):
        record = asana_index.task_to_record(task(
            "1",
            "PYNEH / EPIQ Elite / SZY00B0007 / 60000013",
            notes="Ben 99990015\nworkshop 222200001\nasset: 8888003",
        ), "PM")
        self.assertIn("222200001", record["phones"])
        self.assertNotIn("60000013", record["phones"])
        self.assertNotIn("8888003", record["phones"])

    def test_old_task_is_outside_two_year_window_and_order_is_not_indexed(self):
        rows = asana_index.build_index([
            task("old", "QEH / CX50 / US999F9999", modified="2022-01-01T00:00:00Z",
                 due_on="2022-01-01"),
            task("new", "QEH / CX50 / US999F9999", notes="Order 60000011"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(rows["task_count_in_window"], 1)
        row = rows["devices"][0]
        self.assertNotIn("order_no", row)
        self.assertNotIn("60000011", json.dumps(row))

    def test_missing_serial_is_weak_row(self):
        rows = asana_index.build_index([
            task("weak", "Sample Regional Hospital / Affiniti 70", notes="Room ICU Phone 99990011")
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertTrue(rows["devices"][0]["weak_identity"])
        self.assertEqual(rows["devices"][0]["serial"], "")
        self.assertTrue(rows["devices"][0]["department_rooms"])

    def test_write_and_manifest_round_trip(self):
        index = asana_index.build_index([
            task("1", "QMH / CX50 / US123F4567")
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31),
        generated_at="2026-09-15T00:00:00Z")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            asana_index.write_outputs(index, path)
            loaded = asana_index.load_index(
                path / "asana-device-index.json", path / "asana-device-index-manifest.json"
            )
            self.assertEqual(loaded["device_count"], 1)
            with (path / "asana-device-index.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["task_gids"], "1")
            self.assertNotIn("order_no", rows[0])
            with (path / "asana-location-index.csv").open(
                encoding="utf-8-sig", newline=""
            ) as stream:
                locations = list(csv.DictReader(stream))
            # Code-only Asana data cannot establish an official full name.
            self.assertEqual("QMH", locations[0]["canonical_hospital"])
            self.assertEqual("true", locations[0]["match_enabled"])

    def test_incremental_build_reprocesses_only_changed_task(self):
        initial = asana_index.build_index([
            task("1", "QMH / CX50 / US123F4567"),
            task("2", "QEH / CX50 / US123F4568"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31),
        generated_at="2026-09-10T00:00:00Z")
        changed = task("1", "QMH / CX50 / US123F4567", modified="2026-09-15T00:00:00Z",
                       project_type="CM")
        updated = asana_index.build_index(
            [changed], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31),
            existing_index=initial, generated_at="2026-09-15T00:00:00Z"
        )
        self.assertEqual(updated["device_count"], 2)
        row = next(row for row in updated["devices"] if row["serial"] == "US123F4567")
        self.assertEqual(row["job_types"], ["CM"])
        self.assertEqual({ref["gid"] for ref in row["task_refs"]}, {"1"})
        self.assertEqual(updated["max_task_modified_at"], "2026-09-15T00:00:00Z")

    def test_incremental_change_is_compared_with_that_tasks_own_timestamp(self):
        initial = asana_index.build_index([
            task("older", "QMH / CX50 / US123F4567", modified="2026-09-01T00:00:00Z"),
            task("newest", "QEH / CX50 / US123F4568", modified="2026-09-15T00:00:00Z"),
        ], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        changed = task(
            "older", "QMH / CX50 / US123F4567",
            modified="2026-09-10T00:00:00Z", project_type="CM",
        )
        updated = asana_index.build_index(
            [changed], window_start=date(2025, 1, 1), window_end=date(2026, 12, 31),
            existing_index=initial,
        )
        row = next(row for row in updated["devices"] if row["serial"] == "US123F4567")
        self.assertEqual(["CM"], row["job_types"])

    def test_project_and_task_pagination(self):
        project_payloads = [
            {"data": [{"gid": "p1", "name": "PM Jobs", "modified_at": ""}],
             "next_page": {"offset": "next"}},
            {"data": [{"gid": "p2", "name": "PCMS-CM", "modified_at": ""}],
             "next_page": None},
        ]
        task_payloads = [
            {"data": [{"gid": "t1", "name": "QMH / CX50 / US123F4567"}],
             "next_page": {"offset": "next"}},
            {"data": [{"gid": "t2", "name": "QEH / CX50 / US123F4568"}],
             "next_page": None},
        ]
        with patch.object(asana_index, "_request", side_effect=project_payloads) as request:
            projects = list(asana_index.iter_projects())
        self.assertEqual([p["job_type"] for p in projects], ["PM", "CM"])
        self.assertEqual(request.call_count, 2)
        with patch.object(asana_index, "_request", side_effect=task_payloads) as request:
            tasks = list(asana_index.iter_project_tasks("p1"))
        self.assertEqual([t["gid"] for t in tasks], ["t1", "t2"])
        self.assertEqual(request.call_count, 2)


class AsanaIndexClientTests(unittest.TestCase):
    def setUp(self):
        asana_client.clear_device_index()
        asana_client._task_cache.clear()

    def tearDown(self):
        asana_client.clear_device_index()

    @staticmethod
    def _ref(gid, *, date_value="2026-09-08", job_type="PM",
             phone="99990011", contact="Alice", room="3F"):
        return {
            "gid": gid, "work_dates": [date_value], "job_type": job_type,
            "location": "PYNEH", "hospital": "PYNEH",
            "hospital_aliases": ["PYN", "PYNEH"],
            "department_rooms": [room], "product": "Affiniti 70",
            "product_family": "AFFINITI",
            "product_variant": "Affiniti 70", "serial": "USX00F0001",
            "phones": [phone], "contacts": [contact], "assets": ["88880001"],
        }

    @classmethod
    def _device(cls, serial="USX00F0001", *, gid="task-1", phone="99990011",
                contact="Alice", date_value="2026-09-08"):
        ref = cls._ref(gid, date_value=date_value, phone=phone, contact=contact)
        ref["serial"] = serial
        return {
            "device_key": serial, "weak_identity": False,
            "serial": serial, "product": "AFFINITI",
            "product_families": ["AFFINITI"],
            "product_variants": ["Affiniti 70", "Affiniti 70G"],
            "hospitals": ["PYNEH"], "locations": ["PYN", "PYNEH-3F"],
            "hospital_aliases": ["PYN", "PYNEH"],
            "department_rooms": ["3F"], "phones": [phone],
            "contacts": [contact], "assets": ["88880001"],
            "work_dates": [date_value], "job_types": ["PM"],
            "task_refs": [ref],
        }

    @staticmethod
    def _ocr(serial="USX00F0001", **overrides):
        data = {
            "order_no": None, "serial_candidates": [serial],
            "serial_no": serial, "product_raw": "Affiniti 70G",
            "hospital_raw": "PYN", "department_room_raw": "3F",
            "phone_candidates": ["99990011"], "contact_person_raw": "Alice",
            "asset_candidates": ["88880001"], "service_date_raw": "08/09/2026",
            "date_source": "ACTION_DATE",
        }
        data.update(overrides)
        return data

    @staticmethod
    def _live(gid, *, serial="USX00F0001", date_value="2026-09-08",
              phone="99990011", contact="Alice"):
        return {
            "gid": gid,
            "name": f"PYNEH / Affiniti 70 / {serial}",
            "notes": f"Phone: {phone}\nContact: {contact}\nAsset# 88880001",
            "memberships": [{"project": {"name": "PM Jobs"}}],
            "due_on": date_value, "start_on": "",
        }

    def test_index_hydrates_live_task_and_order_is_read_only_at_end(self):
        index = {
            "schema_version": 3,
            "devices": [{
                "device_key": "US123F4567", "weak_identity": False,
                "serial": "US123F4567", "product": "CX",
                "product_families": ["CX"],
                "product_variants": ["CX50"],
                "hospitals": ["QMH"], "hospital_aliases": ["QMH"],
                "locations": ["QMH"], "phones": [],
                "contacts": [], "assets": [], "job_types": ["PM"],
                "task_refs": [{"gid": "123", "work_dates": ["2026-09-08"],
                               "job_type": "PM", "location": "QMH",
                               "hospital": "QMH", "hospital_aliases": ["QMH"],
                               "product": "CX", "product_family": "CX",
                               "product_variant": "CX50", "serial": "US123F4567",
                               "phones": [], "contacts": [], "assets": [],
                               "department_rooms": []}],
            }],
        }
        asana_client.set_device_index(index)
        live = {"gid": "123", "name": "QMH / CX50 / US123F4567 / 60000011",
                "notes": "", "memberships": [{"project": {"name": "PM Jobs"}}],
                "due_on": "2026-09-08", "start_on": ""}
        with patch.object(asana_client, "_fetch_task", return_value=live) as fetch, \
                patch.object(asana_client, "_gather_pool") as live_search:
            found, tier = asana_client.find_task({
                "order_no": "60000011",
                "serial_candidates": ["US123F4567"], "product_raw": "CX50",
                "hospital_raw": "QMH", "date_source": "ACTION_DATE",
                "service_date_raw": "08/09/2026",
            }, job_type="PM")
        self.assertEqual(found["gid"], "123")
        self.assertEqual(tier, 1)
        fetch.assert_called_once_with("123")
        live_search.assert_not_called()

    def test_index_miss_uses_live_fallback(self):
        asana_client.set_device_index({"schema_version": 3, "devices": []})
        with patch.object(asana_client, "_gather_pool", return_value=[]) as live_search:
            found, tier = asana_client.find_task({"order_no": "60000011"})
        self.assertIsNone(found)
        self.assertEqual(tier, 0)
        live_search.assert_called_once()

    def test_affiniti_70_and_70g_share_one_product_family(self):
        self.assertEqual("Affiniti 70", asana_client.product_family("Affiniti 70"))
        self.assertEqual("Affiniti 70", asana_client.product_family("Affiniti 70G"))
        self.assertEqual("CX50", asana_client.product_family("CX50"))

    def test_epiq_7_plus_and_plus_symbol_share_one_product_family(self):
        self.assertEqual("EPIQ 7+", asana_client.product_family("EPIQ 7 Plus"))
        self.assertEqual("EPIQ 7+", asana_client.product_family("EPIQ 7+"))

    def test_live_month_project_contact_and_typo_phone_match_index_rules(self):
        live = {
            "gid": "task",
            "name": "PYNEH / EPIQ Elite / SZY00B0007 / 60000013",
            "notes": "workshop 222200001\n99990001 Ms.Sample\nasset: 8888003",
            "memberships": [{"project": {"name": "2026 Jul"}}],
        }
        self.assertEqual("PM", asana_client._task_job_type(live))
        self.assertIn("Ms.Sample", asana_client._task_contacts(live))
        self.assertIn("222200001", asana_client._task_phones(live))
        scored = asana_client._candidate_score(
            live,
            {
                "phone_candidates": ["99990002"],
                "asset_candidates": ["8888003"],
            },
            serials=[], hosp=None, product=None, job_type="PM",
        )
        self.assertIn("phone_fuzzy", scored["support"])
        self.assertIn("asset_exact", scored["support"])

    def test_serial_one_two_or_three_errors_can_match_with_multiple_fields(self):
        for observed in ("USX00F000G", "USX00F00GG", "USX00F0GGG"):
            with self.subTest(observed=observed):
                asana_client.set_device_index({
                    "schema_version": 3, "devices": [self._device()],
                })
                with patch.object(
                    asana_client, "_fetch_task", return_value=self._live("task-1")
                ) as fetch, patch.object(asana_client, "_gather_pool") as fallback:
                    found, tier = asana_client.find_task(self._ocr(observed), job_type="PM")
                self.assertEqual("task-1", found["gid"])
                self.assertEqual(2, tier)
                fetch.assert_called_once_with("task-1")
                fallback.assert_not_called()

    def test_serial_at_fifty_percent_can_match_after_other_gates(self):
        asana_client.set_device_index({
            "schema_version": 3, "devices": [self._device()],
        })
        with patch.object(asana_client, "_fetch_task", return_value=self._live("task-1")) as fetch, \
                patch.object(asana_client, "_gather_pool") as fallback:
            found, tier = asana_client.find_task(
                self._ocr("USX00FGGGG"), job_type="PM"
            )
        self.assertEqual("task-1", found["gid"])
        self.assertEqual(2, tier)
        fetch.assert_called_once()
        fallback.assert_not_called()

    def test_missing_product_or_hospital_gate_uses_live_fallback(self):
        asana_client.set_device_index({
            "schema_version": 3, "devices": [self._device()],
        })
        sparse = self._ocr(
            "USX00F000G", hospital_raw=None, department_room_raw=None,
            phone_candidates=[], contact_person_raw=None, asset_candidates=[],
            service_date_raw=None, date_source=None,
        )
        with patch.object(asana_client, "_fetch_task") as fetch, \
                patch.object(asana_client, "_gather_pool", return_value=[]) as fallback:
            found, tier = asana_client.find_task(sparse, job_type=None)
        self.assertIsNone(found)
        self.assertEqual(0, tier)
        fetch.assert_not_called()
        fallback.assert_called_once()

    def test_weak_index_row_never_auto_names_even_with_phone_asset_and_date(self):
        weak = self._device()
        weak.update({
            "device_key": "WEAK|PYNEH|AFFINITI70", "weak_identity": True,
            "serial": "",
        })
        weak["task_refs"][0]["serial"] = ""
        asana_client.set_device_index({"schema_version": 3, "devices": [weak]})
        ocr = self._ocr(serial_candidates=[], serial_no=None)
        with patch.object(asana_client, "_fetch_task") as fetch, \
                patch.object(asana_client, "_gather_pool") as fallback:
            found, tier = asana_client.find_task(ocr, job_type="PM")
        self.assertIsNone(found)
        self.assertEqual(0, tier)
        fetch.assert_not_called()
        fallback.assert_not_called()

    def test_two_close_devices_with_small_score_gap_stay_pending(self):
        other = self._device("USX00F0002", gid="task-2")
        asana_client.set_device_index({
            "schema_version": 3, "devices": [self._device(), other],
        })
        with patch.object(asana_client, "_fetch_task") as fetch:
            found, tier = asana_client.find_task(
                self._ocr("USX00F000X"), job_type="PM"
            )
        self.assertIsNone(found)
        self.assertEqual(0, tier)
        fetch.assert_not_called()

    def test_historical_task_is_selected_by_its_date_and_phone_not_recency(self):
        device = self._device(gid="older", phone="99990011", date_value="2026-09-08")
        newer = self._ref(
            "newer", date_value="2026-09-14", phone="99990014", contact="Bob"
        )
        device["task_refs"].append(newer)
        device["phones"].append("99990014")
        device["contacts"].append("Bob")
        device["work_dates"].append("2026-09-14")
        asana_client.set_device_index({"schema_version": 3, "devices": [device]})
        live = {
            "older": self._live("older"),
            "newer": self._live(
                "newer", date_value="2026-09-14", phone="99990014", contact="Bob"
            ),
        }
        with patch.object(asana_client, "_fetch_task", side_effect=lambda gid: live[gid]):
            found, tier = asana_client.find_task(self._ocr(), job_type="PM")
        self.assertEqual("older", found["gid"])
        self.assertEqual(2, tier)

    def test_tied_history_is_not_resolved_by_newest_task(self):
        device = self._device(gid="older")
        device["task_refs"].append(self._ref("newer"))
        asana_client.set_device_index({"schema_version": 3, "devices": [device]})
        live = {gid: self._live(gid) for gid in ("older", "newer")}
        with patch.object(asana_client, "_fetch_task", side_effect=lambda gid: live[gid]):
            found, tier = asana_client.find_task(self._ocr(), job_type="PM")
        self.assertIsNone(found)
        self.assertEqual(0, tier)

    def test_candidate_logs_do_not_reveal_indexed_customer_values(self):
        asana_client.set_device_index({
            "schema_version": 3, "devices": [self._device()],
        })
        with patch.object(
            asana_client, "_fetch_task", return_value=self._live("task-1")
        ), self.assertLogs(asana_client.log, level="INFO") as captured:
            asana_client.find_task(self._ocr("USX00F000G"), job_type="PM")
        output = "\n".join(captured.output)
        for private_value in ("USX00F0001", "99990011", "Alice", "88880001"):
            self.assertNotIn(private_value, output)


if __name__ == "__main__":
    unittest.main()

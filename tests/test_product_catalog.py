"""Only synthetic product/identifier data, no cloud or model calls."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src import asana_index, product_catalog


def task(name, gid="1"):
    return {"gid": gid, "name": name, "due_on": "2026-09-01", "notes": "",
            "modified_at": "2026-09-01T00:00:00Z", "_project_job_type": "PM"}


class ProductCatalogTests(unittest.TestCase):
    def test_metadata_and_serials_never_become_products(self):
        for raw in ("CM", "PM", "HAWO 12345678", "WO12345678", "SR#61234567",
                    "US123B4567", "12345678", "*Example", "Phone 61234567"):
            info = product_catalog.inspect_product(raw)
            self.assertEqual("rejected", info["status"], raw)
            self.assertEqual("", info["variant"])

    def test_real_model_codes_starting_cm_are_not_removed_as_job_type(self):
        for raw in ("CM10", "CM12"):
            info = product_catalog.inspect_product(raw)
            self.assertEqual("unconfirmed", info["status"])
            self.assertTrue(product_catalog.indexable(info))

    def test_typo_or_new_model_is_not_certified_by_fuzzy_normalizer(self):
        for raw in ("EPLQ 5G", "EPIQ 7C", "EPIQ 5W", "Lumify Pro"):
            self.assertEqual("unconfirmed", product_catalog.inspect_product(raw)["status"])
        self.assertEqual("group_only", product_catalog.inspect_product("EPIQ")["status"])
        self.assertEqual("EPIQ 7+", product_catalog.inspect_product("EPIQ 7Plus")["canonical"])

    def test_canonical_product_is_found_before_metadata_not_tail(self):
        for name in ("QMH / CX50 / CM / US123B4567 / HAWO 12345678",
                     "QMH / HAWO 12345678 / CX50 / US123B4567 / PM",
                     "QMH / CX50 US123B4567 / CM",
                     "QMH / CX50 / US123B4567 / Affiniti 70"):
            record = asana_index.task_to_record(task(name))
            self.assertEqual(["CX50"], record["product_variants"])
            self.assertEqual(["QMH"], record["locations"])
            self.assertEqual("confirmed", record["task_refs"][0]["product_parse_status"])

    def test_missing_product_is_not_replaced_with_serial_contact_or_work_type(self):
        for name in ("QMH / US123B4567 / CM", "QMH / CM / US123B4567",
                     "QMH / *Example / US123B4567", "QMH / Alex / US123B4567",
                     "QMH / Example Person / US123B4567"):
            record = asana_index.task_to_record(task(name))
            self.assertEqual("US123B4567", record["serial"])
            self.assertEqual([], record["product_variants"])
            self.assertEqual("rejected", record["task_refs"][0]["product_parse_status"])

    def test_unknown_positional_model_is_retained_but_marked_for_review(self):
        record = asana_index.task_to_record(task("QMH / Lumify Pro / US123B4567"))
        self.assertEqual(["Lumify Pro"], record["product_variants"])
        self.assertEqual("unconfirmed", record["task_refs"][0]["product_parse_status"])

    def test_location_detail_remains_separate_and_conflicting_models_are_not_guessed(self):
        record = asana_index.task_to_record(task("QMH / Ultrasound 6F / CX50 / US123B4567"))
        self.assertEqual(["QMH / Ultrasound 6F"], record["locations"])
        conflict = asana_index.task_to_record(task("QMH / CX50 / Affiniti 70 / US123B4567"))
        self.assertEqual([], conflict["product_variants"])
        self.assertEqual("conflicting_product_segments", conflict["task_refs"][0]["product_parse_reason"])

    def test_catalog_counts_distinct_devices_and_does_not_promote_frequent_unknowns(self):
        rows = [{"serial": "US123B4567", "product_variants": ["EPIQ Elite", "EPIQ ELITE", "CM10"],
                 "task_refs": [{"gid": str(i)} for i in range(20)]},
                {"serial": "US123B4567", "product_variants": ["EPIQ Elite", "CM10"]},
                {"serial": "US123B4568", "product_variants": ["EPIQ Elite", "CM10"]}]
        result = product_catalog.build_catalog({"devices": rows})
        self.assertEqual(2, result["confirmed_models"][0]["device_count"])
        self.assertEqual(["CM10"], [r["name"] for r in result["needs_review"]])
        self.assertEqual(2, result["needs_review"][0]["device_count"])
        self.assertNotIn("US123B4567", json.dumps(result))
        self.assertNotIn("task_refs", json.dumps(result))

    def test_identifiers_and_weak_rows_do_not_enter_reference_table(self):
        result = product_catalog.build_catalog({"devices": [
            {"serial": "US123B4567", "product_variants": ["61234567", "HAWO 12345678", "US123B4567"]},
            {"serial": "", "weak_identity": True, "product_variants": ["EPIQ Elite"]}]})
        self.assertEqual([], result["confirmed_models"])
        self.assertEqual(1, result["weak_rows_excluded"])
        for value in ("61234567", "12345678", "US123B4567"):
            self.assertNotIn(value, json.dumps(result))

    def test_private_outputs_round_trip_with_no_cloud_calls(self):
        result = product_catalog.build_catalog({"devices": [
            {"serial": "US123B4567", "product_variants": ["EPIQ", "Affiniti 70G", "CM10"]}]})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            product_catalog.write_catalog(result, root)
            self.assertEqual(result, json.loads((root/"product-catalog.json").read_text(encoding="utf-8")))
            md = (root/"PRODUCT_CATALOG.md").read_text(encoding="utf-8")
            self.assertIn("Affiniti 70G", md)
            self.assertNotIn("US123B4567", md)

    def test_legacy_incremental_index_requires_full_rebuild_before_network(self):
        legacy = {"devices": [], "schema_version": 3}
        with patch.object(asana_index, "iter_projects") as projects:
            with self.assertRaisesRegex(asana_index.AsanaIndexError, "完整重建"):
                asana_index.build_from_asana(existing_index=legacy)
            projects.assert_not_called()

    def test_new_index_tracks_parser_version_and_keeps_orders_out(self):
        result = asana_index.build_index([task("QMH / CX50 / US123B4567 / 61234567")],
                                        window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(product_catalog.PARSER_VERSION, result["product_parser_version"])
        self.assertNotIn("61234567", json.dumps(result))
        updated = asana_index.build_index([], existing_index=result,
                                         window_start=date(2025, 1, 1), window_end=date(2026, 12, 31))
        self.assertEqual(1, updated["device_count"])


if __name__ == "__main__":
    unittest.main()

"""Synthetic tests: no private index row or real jobsheet is committed."""
import unittest

from src import private_ocr_context


class PrivateOcrContextTests(unittest.TestCase):
    def test_only_confirmed_short_vocabulary_is_projected(self):
        index = {
            "schema_version": 3,
            "devices": [
                {"serial": "TEST0001", "weak_identity": False,
                 "product_variants": ["EPIQ 5G", "Unverified Widget"],
                 "phones": ["12345678"], "contacts": ["PRIVATE PERSON"],
                 "assets": ["99999999"]},
                {"serial": "TEST0002", "weak_identity": False,
                 "product_variants": ["EPIQ 5G"]},
                {"serial": None, "weak_identity": True,
                 "product_variants": ["Affiniti 70"]},
            ],
            "location_directory": [
                {"match_enabled": True, "alias_sources": ["confirmed"],
                 "device_count": 2, "confirmed_aliases": ["AA", "AB", "Example Hospital"],
                 "historical_locations": ["PRIVATE FLOOR"]},
                {"match_enabled": False, "alias_sources": ["unconfirmed"],
                 "device_count": 9, "confirmed_aliases": ["BAD"]},
            ],
        }
        vocabulary = private_ocr_context.build_vocabulary(index)
        self.assertEqual(["EPIQ 5G"], vocabulary["product_models"])
        self.assertEqual(["EPIQ"], vocabulary["product_families"])
        self.assertEqual(["AA", "AB"], vocabulary["hospital_codes"])
        self.assertEqual([["AA", "AB"]], vocabulary["same_hospital_codes"])
        text = str(vocabulary)
        for forbidden in ("PRIVATE", "12345678", "99999999", "TEST0001", "BAD"):
            self.assertNotIn(forbidden, text)

    def test_missing_or_unconfirmed_index_cannot_create_prompt(self):
        for index in ({}, {"schema_version": 3, "devices": [], "location_directory": []}):
            with self.assertRaises(ValueError):
                private_ocr_context.build_vocabulary(index)


if __name__ == "__main__":
    unittest.main()

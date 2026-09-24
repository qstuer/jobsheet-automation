"""Synthetic checks for the private product/location OCR experiment."""
import unittest

from src.field_ocr_experiment import _score


class FieldOcrExperimentTests(unittest.TestCase):
    def test_product_variant_is_not_silently_changed(self):
        result = _score("product_raw", "Affiniti 70", "Affiniti 70G")
        self.assertFalse(result["exact"])
        self.assertTrue(result["core"])

    def test_confirmed_product_spelling_variant(self):
        result = _score("product_raw", "EPIQ 7Plus", "EPIQ 7+")
        self.assertTrue(result["exact"])

    def test_hospital_alias_and_detail_are_scored_separately(self):
        result = _score("hospital_raw", "PYN-K6", "PYNEH-K6")
        self.assertFalse(result["exact"])
        self.assertTrue(result["core"])

    def test_unknown_hospital_code_is_not_a_match(self):
        result = _score("hospital_raw", "PN", "PYN")
        self.assertFalse(result["core"])

    def test_blank_field_does_not_pass(self):
        result = _score("product_raw", None, "CX50")
        self.assertTrue(result["blank"])
        self.assertFalse(result["exact"])
        self.assertFalse(result["core"])


if __name__ == "__main__":
    unittest.main()

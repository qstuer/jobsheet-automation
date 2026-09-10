import unittest

from src import asana_audit


class AsanaAuditScoreTests(unittest.TestCase):
    def test_scores_serial_phone_hospital_product_and_recent_date(self):
        task = {
            "name": "PYNEH/ CX50/ SG41700123/ 61904259",
            "notes": "EDU 2595 6612 Asset 1329285",
            "modified_at": "2026-08-24T00:00:00Z",
        }
        case = {
            "serial_candidates": ["SG41700123"],
            "phone_candidates": ["25956612"],
            "asset_candidates": ["1329285"],
            "hospital_candidates": ["PYN", "PYNEH"],
            "product": "CX50",
        }
        score, reasons = asana_audit._task_score(task, case, "2026-06-10")
        self.assertEqual(220, score)
        self.assertIn("serial:SG41700123", reasons)
        self.assertIn("phone:25956612", reasons)

    def test_extracts_only_valid_eight_digit_order(self):
        self.assertEqual("61904259", asana_audit.ORDER_RE.search("CX50 / 61904259").group(1))
        self.assertIsNone(asana_audit.ORDER_RE.search("CX50"))


if __name__ == "__main__":
    unittest.main()

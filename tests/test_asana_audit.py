import unittest
from unittest.mock import Mock, patch

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

    def test_paid_search_falls_back_to_typeahead_on_402(self):
        response = Mock(status_code=402)
        with patch.object(asana_audit.config, "ASANA_TOKEN", "token"), \
                patch.object(asana_audit.config, "ASANA_WORKSPACE_GID", "workspace"), \
                patch.object(asana_audit.requests, "get", return_value=response), \
                patch.object(asana_audit, "_request_typeahead", return_value=[{"gid": "1"}]) as fallback:
            result = asana_audit._request_search("US123")
        self.assertEqual([{"gid": "1"}], result)
        fallback.assert_called_once_with("US123")


if __name__ == "__main__":
    unittest.main()

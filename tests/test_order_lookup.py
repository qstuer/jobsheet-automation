import unittest
from unittest.mock import patch

from src import order_lookup


class OrderLookupTests(unittest.TestCase):
    def test_keeps_only_exact_order_matches(self):
        tasks = [
            {"gid": "1", "name": "QEH / EPIQ / US123456 / 61877079"},
            {"gid": "2", "name": "unrelated 618770790"},
        ]
        with patch.object(order_lookup.asana_client, "_typeahead", return_value=tasks):
            result = order_lookup.lookup_orders(["61877079"])
        self.assertEqual(["1"], [m["gid"] for m in result[0]["matches"]])
        self.assertEqual("US123456", result[0]["matches"][0]["serial_no"])

    def test_rejects_invalid_input_without_querying_asana(self):
        with patch.object(order_lookup.asana_client, "_typeahead") as typeahead:
            result = order_lookup.lookup_orders(["not-an-order"])
        typeahead.assert_not_called()
        self.assertEqual("invalid order number", result[0]["error"])


if __name__ == "__main__":
    unittest.main()

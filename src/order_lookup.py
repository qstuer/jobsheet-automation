"""緊急補單工具：用 Order Number 從 Asana 找回 task 標題與機器序號。

訂單清單由私人 Google Drive 控制檔提供，不放在 workflow input 或程式碼，
避免公開 repository 的 Actions 頁面顯示客戶訂單號。
"""
import json
import os
import re
from pathlib import Path

from . import asana_client


ORDER_RE = re.compile(r"^[56]\d{7}$")


def lookup_orders(order_numbers: list[str]) -> list[dict]:
    """回傳每張訂單的精確 Asana task；零個或多個結果都保留供人工核對。"""
    results = []
    for order_no in dict.fromkeys(order_numbers):
        if not ORDER_RE.fullmatch(order_no):
            results.append({"order_no": order_no, "error": "invalid order number", "matches": []})
            continue

        tasks = asana_client._typeahead(order_no)
        exact = [
            {
                "gid": task.get("gid"),
                "name": task.get("name"),
                "completed": task.get("completed"),
                "created_at": task.get("created_at"),
                "serial_no": asana_client.extract_serial(task.get("name", "")),
            }
            for task in tasks
            if re.search(rf"(?<!\d){re.escape(order_no)}(?!\d)", task.get("name", ""))
        ]
        results.append({"order_no": order_no, "matches": exact})
    return results


def main() -> None:
    input_path = Path(os.environ.get("ORDER_LOOKUP_FILE", "/tmp/jobsheet-orders.txt"))
    output_path = Path(os.environ.get("ORDER_LOOKUP_RESULT", "/tmp/jobsheet-order-results.json"))
    orders = [line.strip() for line in input_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not orders:
        raise RuntimeError("訂單控制檔是空的")
    output_path.write_text(
        json.dumps(lookup_orders(orders), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已查詢 {len(orders)} 個 Order Number；詳細結果只存入私有 artifact。")


if __name__ == "__main__":
    main()

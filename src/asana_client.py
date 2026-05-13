"""Asana REST API 搜尋模組
4 層搜尋策略（對應更新後的 CLAUDE.md Section 5 邏輯）：
  第 1 層：Order Number（有填才搜）
  第 2 層：Serial Number
  第 3 層：Customer + Product 組合
  第 4 層：Customer 模糊搜尋 fallback（CHOW 實務）

命名邏輯（2026-05-13 更新）：
  - 找到任務 + 任務名含 Order No → SR#OrderNo.pdf
  - 找到任務 + 任務名無 Order No → 直接抄整個 Asana 任務標題作為檔名
  - 4 層全失敗 → 人工審核（存 _PENDING/待人工審查_*.pdf）
"""
import re
from typing import Optional, List, Tuple

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from . import config


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def search_tasks(query: str, limit: int = 10) -> List[dict]:
    """Asana 全文搜尋 task"""
    url = f"{config.ASANA_BASE_URL}/workspaces/{config.ASANA_WORKSPACE_GID}/tasks/search"
    headers = {"Authorization": f"Bearer {config.ASANA_TOKEN}"}
    params = {"text": query, "limit": limit, "opt_fields": "name,notes,completed"}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("data", [])


def _name_contains(task: dict, *substrings: str) -> bool:
    """task name 是否包含任一字串（不分大小寫）"""
    name = task.get("name", "").upper()
    return any(s and s.upper() in name for s in substrings if s)


def find_task(ocr_data: dict) -> Tuple[Optional[dict], int]:
    """
    4 層 Asana 搜尋。
    回傳 (matched_task_or_None, tier_used)
      tier: 1=OrderNo, 2=Serial, 3=Customer+Product, 4=Customer模糊, 0=未找到
    """
    order_no = ocr_data.get("order_no")
    serial_no = ocr_data.get("serial_no")
    product   = ocr_data.get("product")
    customer  = ocr_data.get("customer")

    # 第 1 層：Order Number
    if order_no:
        results = search_tasks(order_no)
        for task in results:
            if _name_contains(task, order_no):
                return task, 1

    # 第 2 層：Serial Number
    if serial_no:
        results = search_tasks(serial_no)
        for task in results:
            if _name_contains(task, serial_no):
                return task, 2

    # 第 3 層：Customer + Product 組合
    if customer and product:
        results = search_tasks(f"{customer} {product}")
        for task in results:
            if _name_contains(task, customer) and _name_contains(task, product):
                return task, 3

    # 第 4 層：Customer 模糊 fallback（CHOW 實務：前 10 筆通常找得到）
    if customer:
        results = search_tasks(customer, limit=10)
        for task in results:
            if serial_no and _name_contains(task, serial_no):
                return task, 4
            if product and _name_contains(task, product):
                return task, 4

    return None, 0


def extract_order_no_from_name(task: dict) -> Optional[str]:
    """從 Asana task name 抓出 8 位 Order Number（5 或 6 開頭）"""
    match = re.search(r"\b[56]\d{7}\b", task.get("name", ""))
    return match.group(0) if match else None


def get_safe_title(task: dict) -> str:
    """
    取得用於 OneDrive 檔名的安全標題。
    把 Windows 不允許的字元（\\ / : * ? \" < > |）替換成 _
    """
    title = task.get("name", "Unknown").strip()
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    return safe

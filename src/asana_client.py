"""Asana REST API 搜尋模組（免費方案版 — 改用 Project Tasks API）

⚠️ /workspaces/tasks/search 需要 Premium，改用 /projects/{gid}/tasks（免費）
策略：同一次 run 只抓一次所有已知 project 的 tasks，快取後本地過濾。

已知 Project GIDs（CLAUDE.md Section 6）：
  2026 Apr        : 1204466136743272  ← 每月更新 GID
  PM Jobs         : 1199584308902029
  Ultrasound - CM : 1111192145849645

命名邏輯（2026-05-13 更新）：
  - 找到任務 + 任務名含 Order No → SR#OrderNo.pdf
  - 找到任務 + 任務名無 Order No → 直接抄整個 Asana 任務標題作為檔名
  - 4 層全失敗 → 人工審核（存 _PENDING/待人工審查_*.pdf）
"""
import re
import logging
from typing import Optional, List, Tuple

import requests

from . import config

log = logging.getLogger(__name__)

# 已知專案 GID（最常用的放最前面）
KNOWN_PROJECT_GIDS = [
    "1204466136743272",  # 2026 Apr（本月進行中任務）
    "1199584308902029",  # PM Jobs
    "1111192145849645",  # Ultrasound - CM
]

_task_cache: List[dict] = []  # 同一次 run 只抓一次


def _fetch_all_tasks() -> List[dict]:
    """從所有已知 project 抓取 tasks（含 pagination，每次 run 快取）"""
    global _task_cache
    if _task_cache:
        return _task_cache

    headers = {"Authorization": f"Bearer {config.ASANA_TOKEN}"}
    all_tasks: List[dict] = []

    for gid in KNOWN_PROJECT_GIDS:
        url = f"{config.ASANA_BASE_URL}/projects/{gid}/tasks"
        params: dict = {"opt_fields": "name,notes,completed", "limit": 100}
        page = 0
        while url:
            try:
                r = requests.get(url, headers=headers, params=params, timeout=20)
                r.raise_for_status()
                data = r.json()
                batch = data.get("data", [])
                all_tasks.extend(batch)
                page += 1
                next_page = data.get("next_page")
                if next_page:
                    url = next_page["uri"]
                    params = {}  # uri 已含所有參數
                else:
                    url = None
            except Exception as e:
                log.warning(f"  抓取 project {gid} (page {page}) 失敗: {e}")
                break

    _task_cache = all_tasks
    log.info(f"  Asana 共載入 {len(all_tasks)} 個 tasks（來自 {len(KNOWN_PROJECT_GIDS)} 個 projects）")
    return _task_cache


def _name_contains(task: dict, *substrings: str) -> bool:
    """task name 是否包含任一字串（不分大小寫）"""
    name = task.get("name", "").upper()
    return any(s and s.upper() in name for s in substrings if s)


def find_task(ocr_data: dict) -> Tuple[Optional[dict], int]:
    """
    4 層 Asana 搜尋（本地過濾版，不需要 Premium）。
    回傳 (matched_task_or_None, tier_used)
      tier: 1=OrderNo, 2=Serial, 3=Customer+Product, 4=Customer模糊, 0=未找到
    """
    order_no = ocr_data.get("order_no")
    serial_no = ocr_data.get("serial_no")
    product   = ocr_data.get("product")
    customer  = ocr_data.get("customer")

    tasks = _fetch_all_tasks()

    # 第 1 層：Order Number
    if order_no:
        for task in tasks:
            if _name_contains(task, order_no):
                return task, 1

    # 第 2 層：Serial Number
    if serial_no:
        for task in tasks:
            if _name_contains(task, serial_no):
                return task, 2

    # 第 3 層：Customer + Product 組合
    if customer and product:
        for task in tasks:
            if _name_contains(task, customer) and _name_contains(task, product):
                return task, 3

    # 第 4 層：Customer 模糊 fallback
    if customer:
        for task in tasks:
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

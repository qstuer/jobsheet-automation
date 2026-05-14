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

# 已知專案 GID 與 Job 類型對應
KNOWN_PROJECT_GIDS = [
    "1204466136743272",  # 2026 Apr（PM）
    "1199584308902029",  # PM Jobs（PM）
    "1111192145849645",  # Ultrasound - CM（CM）
]

# 每個 project 對應的 job type
PROJECT_JOB_TYPE: dict = {
    "1204466136743272": "PM",
    "1199584308902029": "PM",
    "1111192145849645": "CM",
}

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
                # 每個 task 標記所屬的 job type，方便後續過濾
                for t in batch:
                    t["_job_type"] = PROJECT_JOB_TYPE.get(gid, "unknown")
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

    # 未完成的任務排最前面（completed=False 優先）
    all_tasks.sort(key=lambda t: t.get("completed", False))
    _task_cache = all_tasks
    log.info(f"  Asana 共載入 {len(all_tasks)} 個 tasks（來自 {len(KNOWN_PROJECT_GIDS)} 個 projects）")
    return _task_cache


def _name_contains(task: dict, *substrings: str) -> bool:
    """task name 是否包含任一字串（不分大小寫）"""
    name = task.get("name", "").upper()
    return any(s and s.upper() in name for s in substrings if s)


def _verify_match(task: dict, ocr_data: dict) -> bool:
    """
    三重核對：任何層配對成功後，都必須通過此驗證才算真正命中。

    驗證邏輯：
      1. Serial 核對（最關鍵）：每台機器 serial 唯一，task 必須含 serial 前 8 碼
      2. Product 核對：型號必須吻合，防止同醫院不同機器誤判

    例：OCR 讀到 HKCH + EPIQ 5G + US51680818
        找到 QEH, EPIQ 5G / USN18C0835  → serial 前8碼 US51680 ≠ USN18C08 → ❌ 拒絕
        找到 HKCH, EPIQ 5G / US51680818 → serial + product 全符            → ✅ 接受
    """
    serial_no = ocr_data.get("serial_no")
    product   = ocr_data.get("product")
    name = task.get("name", "").upper()

    # 1. Serial 核對（最重要）：task 必須含 serial 前 8 碼
    if serial_no and len(serial_no) >= 6:
        if serial_no[:8].upper() not in name:
            log.warning(f"    ⚠ 三重核對失敗 Serial：OCR={serial_no} ≠ task='{task.get('name')}'")
            return False

    # 2. Product 核對：task 必須含 product 型號
    if product:
        if product.upper() not in name:
            log.warning(f"    ⚠ 三重核對失敗 Product：OCR={product} ≠ task='{task.get('name')}'")
            return False

    return True


def find_task(ocr_data: dict, job_type: str = None) -> Tuple[Optional[dict], int]:
    """
    4 層 Asana 搜尋（本地過濾版，不需要 Premium）。
    每層命中後均須通過 _verify_match() 三重核對（serial + product）。

    job_type: "CM" 或 "PM"（來自 Jobsheet 本身的判斷）
      → CM 單優先搜 Ultrasound-CM project；PM 單優先搜 PM Jobs/2026 Apr
      → 同一台機器有 CM + PM 兩個 task 時，正確配對各自的任務

    回傳 (matched_task_or_None, tier_used)
      tier: 1=OrderNo, 2=Serial, 3=Customer+Product, 4=Serial模糊, 0=未找到
    """
    order_no = ocr_data.get("order_no")
    serial_no = ocr_data.get("serial_no")
    product   = ocr_data.get("product")
    customer  = ocr_data.get("customer")

    all_tasks = _fetch_all_tasks()

    # 排序：同類型 job 的 task 排最前，然後未完成優先
    # CM 單 → Ultrasound-CM tasks 先；PM 單 → PM tasks 先
    def sort_key(t: dict):
        type_match = 0 if (job_type and t.get("_job_type") == job_type) else 1
        completed  = 1 if t.get("completed", False) else 0
        return (type_match, completed)

    tasks = sorted(all_tasks, key=sort_key)

    # 第 1 層：Order Number（order_no 唯一，仍做 serial + product 核對）
    if order_no:
        for task in tasks:
            if _name_contains(task, order_no) and _verify_match(task, ocr_data):
                return task, 1

    # 第 2 層：Serial Number 精確比對（serial 唯一，核對 product）
    if serial_no:
        for task in tasks:
            if _name_contains(task, serial_no) and _verify_match(task, ocr_data):
                return task, 2

    # 第 3 層：Customer + Product 組合（核對 serial）
    if customer and product:
        for task in tasks:
            if _name_contains(task, customer) and _name_contains(task, product):
                if _verify_match(task, ocr_data):
                    return task, 3

    # 第 4 層：Serial 前 8 碼模糊比對 + product 核對
    # ⚠️ product-only 已移除，serial 是唯一識別
    if serial_no and len(serial_no) >= 6:
        serial_prefix = serial_no[:8].upper()
        for task in tasks:
            if serial_prefix in task.get("name", "").upper():
                if _verify_match(task, ocr_data):
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

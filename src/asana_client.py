"""Asana 配對模組（兩層架構：可靠欄位撈池 → 本機 serial 容錯比對）

關鍵觀念（用戶實證）：
  - Asana typeahead 是「子字串比對」，少字/前綴找得到，但「錯字（替換）找不到」。
    → 所以「容錯」絕不能丟給 Asana，要放在本機用編輯距離算。
  - 醫院名、型號是「來來去去那幾個」的可靠欄位（型號還能對照已知清單校正）。
    → 用「醫院核心碼 + 校正後型號」去 typeahead 撈出一小池候選（這層可靠、不需容錯）。
  - serial 是機器唯一身分證（如 0697/0698/0699 只差最後一碼）。
    → 在候選池裡用 serial 編輯距離挑「唯一最近」那台（這層容錯字）。

安全閥：最近的 serial 必須在門檻內、且「唯一」（沒有兩台機器一樣近）才接受，
        否則送 PENDING，絕不亂猜歸到隔壁機器。
"""
import re
import logging
import time
from typing import Optional, List, Tuple

import requests

from . import config

log = logging.getLogger(__name__)

# 已知型號（正規型；比對時忽略空白與大小寫，並容許 1~2 字 OCR 誤讀）
KNOWN_PRODUCTS = [
    "Affiniti 30", "Affiniti 50", "Affiniti 70",
    "EPIQ 5G", "EPIQ 7G", "EPIQ Elite",
    "CX30", "CX50",
]

MAX_SERIAL_DIST = 3   # serial 容許的最大編輯距離
MAX_PRODUCT_DIST = 2  # 型號校正容許的最大編輯距離

_typeahead_cache: dict = {}


class AsanaError(RuntimeError):
    """Asana 連線或權限故障；必須保留 PDF 等下次重試。"""


# ── 小工具 ────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """正規化：去掉非英數字、轉大寫（吃掉空白差異，如 EPIQ5G == EPIQ 5G）"""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _lev(a: str, b: str) -> int:
    """編輯距離（容忍替換/插入/刪除錯字）"""
    a, b = a.upper(), b.upper()
    m, n = len(a), len(b)
    if not m:
        return n
    if not n:
        return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
    return prev[n]


def normalize_product(ocr_product: Optional[str]) -> Optional[str]:
    """把 OCR 型號對照已知清單校正（EPLQ 5G → EPIQ 5G）。太離譜則原樣回傳。"""
    if not ocr_product:
        return None
    target = _norm(ocr_product)
    best, best_d = None, 99
    for p in KNOWN_PRODUCTS:
        d = _lev(target, _norm(p))
        if d < best_d:
            best_d, best = d, p
    return best if best_d <= MAX_PRODUCT_DIST else ocr_product


def hospital_core(customer: Optional[str]) -> Optional[str]:
    """取醫院核心碼：切掉地址尾段（HKCH-02-Xray → HKCH；保留 'Trinity Medical'）"""
    if not customer:
        return None
    core = re.split(r"[,/\-]", customer.strip(), 1)[0].strip()
    return core or customer.strip()


def extract_serial(name: str) -> Optional[str]:
    """從 task name 抓 serial token（US 開頭 + 6 碼以上），抓不到則回 None"""
    cands = re.findall(r"\b([A-Z]{2}[A-Z0-9]{6,})\b", (name or "").upper())
    for c in cands:
        if c.startswith("US"):
            return c
    return cands[0] if cands else None


def _name_has_product(name: str, product_norm: str) -> bool:
    return product_norm in _norm(name)


# ── Asana typeahead（免費全域；只負責撈池，子字串比對）────────────

def _typeahead(query: str, count: int = 60) -> List[dict]:
    query = (query or "").strip()
    if not query:
        return []
    if query in _typeahead_cache:
        return _typeahead_cache[query]
    if not config.ASANA_TOKEN:
        raise AsanaError("ASANA_TOKEN 未設定")

    headers = {"Authorization": f"Bearer {config.ASANA_TOKEN}"}
    params = {
        "resource_type": "task",
        "query": query,
        "count": count,
        "opt_fields": "name,completed,created_at",
    }
    url = f"{config.ASANA_BASE_URL}/workspaces/{config.ASANA_WORKSPACE_GID}/typeahead"
    response = None
    for attempt in range(1, 5):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.RequestException as exc:
            if attempt == 4:
                raise AsanaError(f"Asana 查詢 '{query}' 連線失敗") from exc
            delay = min(2 ** attempt, 30)
            log.warning(f"  Asana 連線失敗，{delay} 秒後重試（{attempt}/4）")
            time.sleep(delay)
            continue

        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 4:
                raise AsanaError(
                    f"Asana 查詢 '{query}' 失敗：HTTP {response.status_code}"
                )
            retry_after = response.headers.get("Retry-After")
            try:
                delay = max(1.0, min(float(retry_after), 120.0))
            except (TypeError, ValueError):
                delay = min(2 ** attempt, 30)
            log.warning(
                f"  Asana 暫時無法服務（HTTP {response.status_code}），"
                f"{delay:g} 秒後重試（{attempt}/4）"
            )
            time.sleep(delay)
            continue

        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AsanaError(
                f"Asana 查詢 '{query}' 失敗：HTTP {response.status_code}"
            ) from exc

        tasks = payload.get("data")
        if not isinstance(tasks, list):
            raise AsanaError(f"Asana 查詢 '{query}' 回傳格式不正確")
        break
    else:  # pragma: no cover - 迴圈只會 break 或 raise
        raise AsanaError(f"Asana 查詢 '{query}' 失敗")

    # 只快取成功結果。故障不能快取成空清單，否則同一輪會把服務故障
    # 誤認成「真的沒有符合任務」。
    _typeahead_cache[query] = tasks
    return tasks


def _gather_pool(order_no, serial, hosp, product) -> List[dict]:
    """用可靠欄位撈候選池：醫院+型號（主力）、醫院、serial、order"""
    queries: List[str] = []
    if hosp and product:
        queries.append(f"{hosp} {product}")
    if hosp:
        queries.append(hosp)
    if order_no:
        queries.append(order_no)
    if serial:
        queries.append(serial)
        if len(serial) >= 8:
            queries.append(serial[:8])
    pool: dict = {}
    for q in queries:
        for t in _typeahead(q):
            gid = t.get("gid")
            if gid:
                pool[gid] = t
    log.info(f"  候選池：{len(pool)} 個 task（查詢：{queries}）")
    return list(pool.values())


# ── 主配對 ────────────────────────────────────────────────────

def find_task(ocr_data: dict, job_type: str = None) -> Tuple[Optional[dict], int]:
    """
    回傳 (matched_task_or_None, tier_used)
      tier: 1=OrderNo精確, 2=醫院+型號撈池→serial唯一最近, 0=未找到
    """
    order_no = (ocr_data.get("order_no") or "").strip()
    serial   = (ocr_data.get("serial_no") or "").strip().upper()
    product  = normalize_product(ocr_data.get("product"))
    hosp     = hospital_core(ocr_data.get("customer"))

    pool = _gather_pool(order_no, serial, hosp, product)
    if not pool:
        return None, 0

    # 第 1 層：order_no 精確命中（最強）
    if order_no:
        for t in pool:
            if order_no in (t.get("name") or "").upper():
                return t, 1

    # 第 2 層：型號過濾（可靠）→ serial 本機容錯比對挑唯一最近
    cands = pool
    if product:
        pnorm = _norm(product)
        filtered = [t for t in cands if _name_has_product(t.get("name", ""), pnorm)]
        if filtered:
            cands = filtered

    if not serial:
        # 沒 serial 可比：只有當池內剛好唯一一台才敢接受
        serials = {extract_serial(t.get("name", "")) for t in cands}
        serials.discard(None)
        if len(cands) == 1:
            return cands[0], 2
        return None, 0

    # 算每個候選的 serial 編輯距離
    scored = []
    for t in cands:
        ts = extract_serial(t.get("name", ""))
        d = _lev(serial, ts) if ts else 99
        scored.append({"d": d, "task": t, "serial": ts})

    # 先依「未完成優先 + 時間最近」穩定排序，再依距離排序（距離相同時保留前述偏好）
    scored.sort(key=lambda x: (x["task"].get("created_at") or ""), reverse=True)
    scored.sort(key=lambda x: (x["d"], 1 if x["task"].get("completed") else 0))

    best_d = scored[0]["d"]
    # 安全閥：最近距離必須在門檻內，且「唯一一個 serial」並列最近（不會誤配隔壁機器）
    best_serials = {s["serial"] for s in scored if s["d"] == best_d}
    if best_d <= MAX_SERIAL_DIST and len(best_serials) == 1:
        match = scored[0]
        log.info(f"  ✅ serial 比對命中：OCR={serial} → {match['serial']}"
                 f"（dist={best_d}） task='{match['task'].get('name')}'")
        return match["task"], 2

    log.info(f"  ⚠ serial 無唯一最近（best_d={best_d}, 並列={best_serials}）→ 不敢猜")
    return None, 0


# ── 命名輔助（沿用舊版）────────────────────────────────────────

def extract_order_no_from_name(task: dict) -> Optional[str]:
    """從 Asana task name 抓出 8 位 Order Number（5 或 6 開頭）"""
    match = re.search(r"\b[56]\d{7}\b", task.get("name", ""))
    return match.group(0) if match else None


def get_safe_title(task: dict) -> str:
    """OneDrive 檔名安全標題：Windows 不允許的字元換成 _"""
    title = task.get("name", "Unknown").strip()
    return re.sub(r'[\\/:*?"<>|]', "_", title)

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
import logging
import re
import time
from datetime import date
from typing import Optional, List, Tuple

import requests

from . import config

log = logging.getLogger(__name__)

# 已知型號（正規型；比對時忽略空白與大小寫，並容許 1~2 字 OCR 誤讀）
KNOWN_PRODUCTS = [
    "Affiniti 30", "Affiniti 50", "Affiniti 70",
    "EPIQ 5G", "EPIQ 7G", "EPIQ 7+", "EPIQ Elite", "EPIQ CVx",
    "CX30", "CX50",
]

MAX_SERIAL_DIST = 1   # serial 錯一字才可考慮，而且仍須其他欄位交叉支持
MAX_PRODUCT_DIST = 2  # 型號校正容許的最大編輯距離

_typeahead_cache: dict = {}
_task_cache: dict = {}

# 短大寫字串很容易由手寫 OCR 幻覺產生。只有已核對的醫院簡寫才可作搜尋
# 與配對證據；完整的私人機構名稱仍可保留使用。
HOSPITAL_ALIASES = {
    **config.HOSPITAL_SHORT_ALIASES,
    "QUEENMARYHOSPITAL": "QMH",
    "QUEENELIZABETHHOSPITAL": "QEH",
    "KWONGWAHHOSPITAL": "KWH",
    "KOWLOONHOSPITAL": "KH",
    "PAMELAYOUDENETHERSOLEEASTERNHOSPITAL": "PYNEH",
    "PRINCESSMARGARETHOSPITAL": "PMH",
    "HONGKONGCHILDRENSHOSPITAL": "HKCH",
    "PRINCEOFWALESHOSPITAL": "PWH",
    "UNITEDCHRISTIANHOSPITAL": "UCH",
    "TUENMUNHOSPITAL": "TMH",
    "NORTHDISTRICTHOSPITAL": "NDH",
    "GRANTHAMHOSPITAL": "GH",
    # 保留空格供 Asana typeahead 作真正的子字串搜尋；比較時仍會經 _norm。
    "TUNGWAHHOSPITAL": "Tung Wah Hospital",
}


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
    canonical = HOSPITAL_ALIASES.get(_norm(core))
    if canonical:
        return canonical
    # KWM / PYTV 這類未知短碼不得成為候選搜尋或加分依據。
    if re.fullmatch(r"[A-Za-z]{2,6}", core):
        return None
    return core if len(_norm(core)) >= 5 else None


def hospital_search_terms(canonical: Optional[str]) -> List[str]:
    """同院不同短寫都用來撈池；不把這份清單交給圖片模型。"""
    if not canonical:
        return []
    aliases = [
        raw for raw, value in config.HOSPITAL_SHORT_ALIASES.items()
        if value == canonical
    ]
    return list(dict.fromkeys([canonical, *aliases]))


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
        # 官方 typeahead 只保證 compact task；完整欄位會在候選縮小後逐一讀取。
        "opt_fields": "name",
    }
    url = f"{config.ASANA_BASE_URL}/workspaces/{config.ASANA_WORKSPACE_GID}/typeahead"
    response = None
    for attempt in range(1, 5):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.RequestException as exc:
            if attempt == 4:
                raise AsanaError("Asana 候選查詢連線失敗") from exc
            delay = min(2 ** attempt, 30)
            log.warning(f"  Asana 連線失敗，{delay} 秒後重試（{attempt}/4）")
            time.sleep(delay)
            continue

        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 4:
                raise AsanaError(
                    f"Asana 候選查詢失敗：HTTP {response.status_code}"
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
                f"Asana 候選查詢失敗：HTTP {response.status_code}"
            ) from exc

        tasks = payload.get("data")
        if not isinstance(tasks, list):
            raise AsanaError("Asana 候選查詢回傳格式不正確")
        break
    else:  # pragma: no cover - 迴圈只會 break 或 raise
        raise AsanaError("Asana 候選查詢失敗")

    # 只快取成功結果。故障不能快取成空清單，否則同一輪會把服務故障
    # 誤認成「真的沒有符合任務」。
    _typeahead_cache[query] = tasks
    return tasks


def _fetch_task(gid: str) -> dict:
    """讀一個候選的完整證據，包含描述、日期及所屬 project。"""
    if gid in _task_cache:
        return _task_cache[gid]
    if not config.ASANA_TOKEN:
        raise AsanaError("ASANA_TOKEN 未設定")
    headers = {"Authorization": f"Bearer {config.ASANA_TOKEN}"}
    params = {"opt_fields": (
        "name,notes,completed,created_at,completed_at,due_on,start_on,"
        "memberships.project.name,memberships.section.name,"
        "effective_memberships.project.name,effective_memberships.section.name"
    )}
    url = f"{config.ASANA_BASE_URL}/tasks/{gid}"
    response = None
    for attempt in range(1, 5):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.RequestException as exc:
            if attempt == 4:
                raise AsanaError("Asana 工作詳情查詢連線失敗") from exc
            time.sleep(min(2 ** attempt, 30))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 4:
                raise AsanaError(f"Asana 工作詳情查詢失敗：HTTP {response.status_code}")
            try:
                delay = max(1.0, min(float(response.headers.get("Retry-After")), 120.0))
            except (TypeError, ValueError):
                delay = min(2 ** attempt, 30)
            time.sleep(delay)
            continue
        try:
            response.raise_for_status()
            task = response.json().get("data")
        except (requests.RequestException, ValueError) as exc:
            raise AsanaError(f"Asana 工作詳情查詢失敗：HTTP {response.status_code}") from exc
        if not isinstance(task, dict):
            raise AsanaError("Asana 工作詳情回傳格式不正確")
        _task_cache[gid] = task
        return task
    raise AsanaError("Asana 工作詳情查詢失敗")  # pragma: no cover


def _gather_pool(order_no, serials, hosp, product,
                 phones=None, assets=None, work_orders=None) -> List[dict]:
    """用可見欄位撈候選池；真正的取捨在本機評分，不交給 Asana 猜。"""
    queries: List[str] = []
    hospital_terms = hospital_search_terms(hosp)
    for term in hospital_terms:
        if product:
            queries.append(f"{term} {product}")
        queries.append(term)
    if order_no:
        queries.append(order_no)
    for serial in serials or []:
        queries.append(serial)
        if len(serial) >= 8:
            queries.append(serial[:8])
    # HAWO/WO 是 Philips 服務工作編號，通常會同時寫在工作單和 Asana
    # 標題/描述。它比手寫醫院名更不易混淆，應直接用來撈候選。
    for work_order in work_orders or []:
        if len(re.sub(r"\D", "", work_order)) >= 6:
            queries.append(work_order)
    # 電話及較長的 asset/WO 有足夠辨識力，也可能存在 Asana 標題或其搜尋
    # 索引。每類最多查兩個，避免一疊單據造成過多 API 請求；候選回來後
    # 仍須在完整 task 的標題/描述中精確核對，搜尋結果本身不算命中。
    for phone in phones or []:
        digits = re.sub(r"\D", "", phone)
        if len(digits) == 8:
            queries.append(digits)
    for asset in assets or []:
        digits = re.sub(r"\D", "", asset)
        if len(digits) >= 6:
            queries.append(digits)
    queries = list(dict.fromkeys(q for q in queries if q))
    pool: dict = {}
    for q in queries:
        for t in _typeahead(q):
            gid = t.get("gid")
            if gid:
                pool[gid] = t
    compact = list(pool.values())
    # typeahead 排序不是準確度排序。先用候選標題中的可靠 token 排序，再只讀
    # 最相關的一小批完整 task，避免一份單據打數十至數百次 API。
    tokens = [
        order_no, *hospital_terms, product, *(serials or []), *(work_orders or []),
        *(phones or []), *(assets or []),
    ]
    tokens = [_norm(token) for token in tokens if token]
    compact.sort(
        key=lambda task: sum(token in _norm(task.get("name") or "") for token in tokens),
        reverse=True,
    )
    compact = compact[:config.ASANA_MAX_HYDRATED_CANDIDATES]
    hydrated = [_fetch_task(task["gid"]) for task in compact]
    log.info(
        f"  候選池：{len(pool)} 個，讀取最相關 {len(hydrated)} 個完整 task"
        f"（使用 {len(queries)} 組可見欄位查詢）"
    )
    return hydrated


def _clean_candidates(values, fallback=None) -> List[str]:
    items = []
    for value in list(values or []) + ([fallback] if fallback else []):
        value = (value or "").strip().upper()
        if value and value not in items:
            items.append(value)
    return items


def _task_serials(task: dict) -> List[str]:
    """由標題與描述找像 serial 的 token；必須同時含字母及數字。"""
    text = f"{task.get('name') or ''}\n{task.get('notes') or ''}".upper()
    tokens = re.findall(r"\b[A-Z]{1,4}[A-Z0-9]{5,}\b", text)
    return list(dict.fromkeys(
        token for token in tokens
        if any(ch.isalpha() for ch in token) and any(ch.isdigit() for ch in token)
    ))


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    text = value.strip()
    iso = re.search(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", text)
    if iso:
        parts = [int(part) for part in iso.groups()]
        try:
            return date(parts[0], parts[1], parts[2])
        except ValueError:
            return None
    local = re.search(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})(?!\d)", text)
    if local:
        day, month, year = (int(part) for part in local.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _task_dates(task: dict) -> List[date]:
    """只回傳可能代表實際服務日的日期。

    modified_at 會因改名、留言或欄位更新而變，不可用來證明服務日期；
    created/completed 只在另一個 helper 作很弱的排序，不算交叉證據。
    """
    values = [task.get("due_on"), task.get("start_on")]
    notes = task.get("notes") or ""
    values.extend(re.findall(r"\b(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|"
                             r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b", notes))
    parsed = [_parse_date(value) for value in values]
    return list(dict.fromkeys(value for value in parsed if value))


def _task_activity_dates(task: dict) -> List[date]:
    values = [task.get("created_at"), task.get("completed_at")]
    parsed = [_parse_date(value) for value in values]
    return list(dict.fromkeys(value for value in parsed if value))


def _today() -> date:
    """獨立 helper 讓近期日期規則可測試，不把執行日期寫死。"""
    return date.today()


def _service_date_delta(service_date: date, task_dates: List[date]) -> tuple:
    """回傳 (最小日差, 是否只校正了明顯誤讀的年份)。

    手寫 6 常被看成 0/5。只在 OCR 日期不在最近三個月、而 Asana 日期在
    最近三個月時，才用 Asana 年份重算月日差；仍需 serial/電話等證據才會
    真正配對，所以這不是單靠日期猜工作。
    """
    direct = min(abs((candidate - service_date).days) for candidate in task_dates)
    today = _today()
    if abs((service_date - today).days) <= 93:
        return direct, False
    recent_candidates = [
        candidate for candidate in task_dates
        if abs((candidate - today).days) <= 93
    ]
    corrected = []
    for candidate in recent_candidates:
        try:
            same_year = service_date.replace(year=candidate.year)
        except ValueError:
            continue
        corrected.append(abs((candidate - same_year).days))
    if corrected and min(corrected) <= 14 and min(corrected) < direct:
        return min(corrected), True
    return direct, False


def _task_container_text(task: dict) -> str:
    parts = []
    for field in ("memberships", "effective_memberships"):
        for membership in task.get(field) or []:
            for kind in ("project", "section"):
                value = membership.get(kind) or {}
                if value.get("name"):
                    parts.append(value["name"])
    return " ".join(parts)


def _task_job_type(task: dict) -> Optional[str]:
    """只由 Asana project/section 判斷工作種類；不從客戶標題猜。"""
    text = _task_container_text(task).upper()
    if not text:
        return None
    pm = bool(re.search(r"\bPM\b|PREVENTI(?:VE|ATIVE)|PLANNED\s+MAINT", text))
    cm = bool(re.search(r"\bCM\b|CORRECTIVE|REPAIR|SERVICE\s+REQUEST", text))
    if pm == cm:
        return None
    return "PM" if pm else "CM"


def _candidate_score(task: dict, ocr_data: dict, serials: List[str],
                     hosp: Optional[str], product: Optional[str],
                     job_type: Optional[str] = None) -> dict:
    name = task.get("name") or ""
    notes = task.get("notes") or ""
    haystack_norm = _norm(f"{name}\n{notes}")
    digits = re.sub(r"\D", "", f"{name}\n{notes}")
    task_serials = _task_serials(task)

    best_dist = 99
    if serials and task_serials:
        best_dist = min(_lev(_norm(left), _norm(right))
                        for left in serials for right in task_serials)

    score = 0
    support = set()
    reasons = []
    candidate_type = _task_job_type(task)
    if job_type and candidate_type == job_type:
        score += 35
        support.add("job_type")
        reasons.append("job type")
    if best_dist == 0:
        score += 100
        reasons.append("serial exact")
    elif best_dist == 1:
        score += 55
        reasons.append("serial differs by 1")

    phones = [re.sub(r"\D", "", value)
              for value in ocr_data.get("phone_candidates", [])]
    if any(len(value) >= 6 and value in digits for value in phones):
        score += 45
        support.add("phone")
        reasons.append("phone")

    assets = [re.sub(r"\D", "", value)
              for value in ocr_data.get("asset_candidates", [])]
    if any(len(value) >= 4 and value in digits for value in assets):
        score += 35
        support.add("asset")
        reasons.append("asset")

    work_orders = [re.sub(r"\D", "", value)
                   for value in ocr_data.get("work_order_candidates", [])]
    if any(len(value) >= 6 and value in digits for value in work_orders):
        score += 60
        support.add("work_order")
        reasons.append("HAWO/WO")

    hospital_ok = bool(
        hosp and _norm(hospital_core(name)) == _norm(hosp)
    )
    product_ok = bool(product and _norm(product) in _norm(name))
    if hospital_ok:
        score += 20
        reasons.append("hospital prefix")
    if product_ok:
        score += 15
        support.add("product")
        reasons.append("product")
    if hospital_ok and product_ok:
        support.add("hospital+product")

    # 新 OCR 已把 Dept./Room 中純 Asset 內容移除；不可再把整段 Asset#
    # 當作地點加分。只有舊資料完全沒有新欄位時才回退 location_raw。
    location = ocr_data.get("location_raw") or ""
    if len(_norm(location)) >= 3 and _norm(location) in haystack_norm:
        score += 5
        reasons.append("location")

    service_date = None
    if ocr_data.get("date_source") == "ACTION_DATE":
        service_date = _parse_date(ocr_data.get("service_date_raw"))
    task_dates = _task_dates(task)
    date_delta = None
    date_year_corrected = False
    if service_date and task_dates:
        date_delta, date_year_corrected = _service_date_delta(service_date, task_dates)
        if date_delta <= 3:
            score += 50
            support.add("date")
            reasons.append(
                "date within 3 days (year corrected)"
                if date_year_corrected else "date within 3 days"
            )
        elif date_delta <= 14:
            score += 40
            support.add("date")
            reasons.append(
                "date within 14 days (year corrected)"
                if date_year_corrected else "date within 14 days"
            )
        elif date_delta <= 31:
            score += 25
            support.add("date")
            reasons.append("date within 31 days")
        elif date_delta <= 93:
            score += 10
            reasons.append("date within 3 months")
    elif service_date:
        activity_dates = _task_activity_dates(task)
        if activity_dates and min(abs((candidate - service_date).days)
                                  for candidate in activity_dates) <= 93:
            score += 5
            reasons.append("recent task activity")

    return {
        "task": task,
        "score": score,
        "serial_dist": best_dist,
        "support": support,
        "reasons": reasons,
        "date_delta": date_delta,
        "job_type": candidate_type,
    }


# ── 主配對 ────────────────────────────────────────────────────

def find_task(ocr_data: dict, job_type: str = None) -> Tuple[Optional[dict], int]:
    """
    回傳 (matched_task_or_None, tier_used)
      tier: 1=OrderNo精確, 2=醫院+型號撈池→serial唯一最近, 0=未找到
    """
    order_raw = (ocr_data.get("order_no") or "").strip()
    order_digits = re.sub(r"\D", "", order_raw)
    order_no = order_digits if re.fullmatch(config.ORDER_NO_REGEX, order_digits) else ""
    serials = _clean_candidates(
        ocr_data.get("serial_candidates"), ocr_data.get("serial_no")
    )
    visual_serials = _clean_candidates(
        ocr_data.get("serial_visual_candidates")
    )
    product  = normalize_product(
        ocr_data.get("product") or ocr_data.get("product_raw")
    )
    hosp     = hospital_core(
        ocr_data.get("hospital_raw")
        or ocr_data.get("customer")
        or ocr_data.get("customer_raw")
    )
    phones = _clean_candidates(ocr_data.get("phone_candidates"))
    assets = _clean_candidates(ocr_data.get("asset_candidates"))
    work_orders = _clean_candidates(ocr_data.get("work_order_candidates"))

    pool = _gather_pool(
        order_no, serials, hosp, product, phones, assets, work_orders
    )
    if job_type:
        before = len(pool)
        pool = [task for task in pool if _task_job_type(task) in (None, job_type)]
        if len(pool) != before:
            log.info(f"  已排除 {before - len(pool)} 個與 {job_type} 類型衝突的候選")
    if not pool:
        return None, 0

    # 第 1 層：order_no 精確命中（最強）
    if order_no:
        exact_orders = [
            task for task in pool
            if re.search(rf"(?<!\d){re.escape(order_no)}(?!\d)", task.get("name") or "")
        ]
        if len(exact_orders) == 1:
            return exact_orders[0], 1
        if len(exact_orders) > 1:
            log.warning("  同一 order number 命中多個 Asana 工作，不自動選擇")
            return None, 0

    scoring_serials = serials or visual_serials
    scored = [
        _candidate_score(task, ocr_data, scoring_serials, hosp, product, job_type)
        for task in pool
    ]
    scored.sort(key=lambda row: (row["score"], -row["serial_dist"]), reverse=True)
    best = scored[0]
    runner_score = scored[1]["score"] if len(scored) > 1 else -1
    gap = best["score"] - runner_score

    if not serials:
        # serial 仍是首選設備身分證；但手寫 serial 可能每輪都讀得不同。
        # 此時只接受唯一候選，而且完整 task 必須同時精確包含電話、asset
        # 及最近 ACTION DATE。三項來自不同欄位，不能只靠醫院/型號猜。
        required = {"phone", "asset", "date"}
        if len(scored) == 1 and required.issubset(best["support"]):
            log.info(
                f"  ✅ serial 未形成共識，但電話、asset、日期唯一命中"
                f"（{', '.join(best['reasons'])}）"
            )
            return best["task"], 2
        # 未通過格式或只出現一輪的 serial 絕不拿去全域搜尋。只有電話等
        # 可靠欄位已把 Asana 候選縮到唯一一筆後，才容許它作一字距離核對；
        # 仍須至少兩項來自其他欄位的獨立證據。
        visual_support = best["support"] & {
            "phone", "asset", "date", "hospital+product", "product",
        }
        if (
            len(scored) == 1
            and visual_serials
            and best["serial_dist"] <= MAX_SERIAL_DIST
            and len(visual_support) >= 2
        ):
            log.info(
                "  ✅ serial 原始抄錄只差一字，且唯一候選有多欄支持"
                f"（{', '.join(best['reasons'])}）"
            )
            return best["task"], 2
        return None, 0

    # 同一設備在 Asana 會有很多歷史工作；完成狀態不是新舊依據。
    # serial 完全一致仍須日期/電話/asset，或醫院+型號一起支持；若有並列歷史
    # 工作，分數亦必須拉開。serial 錯一字時要求至少兩組額外證據。
    strong = best["support"]
    serial_ambiguous = bool(ocr_data.get("serial_ambiguous"))
    if best["serial_dist"] == 0 and not serial_ambiguous:
        supported = bool(strong & {
            "date", "phone", "asset", "work_order", "hospital+product", "job_type"
        })
        unambiguous = len(scored) == 1 or gap >= 10
        if supported and unambiguous:
            log.info(f"  ✅ Asana 多欄核對命中（{', '.join(best['reasons'])}）")
            return best["task"], 2
    elif best["serial_dist"] <= MAX_SERIAL_DIST:
        # 兩輪 OCR 只差一字時，即使其中一個剛好與 Asana 完全相同，也仍是
        # 有爭議的讀數；必須按一字模糊規則要求兩組額外證據。
        supported = len(strong & {
            "date", "phone", "asset", "work_order", "hospital+product", "job_type"
        }) >= 2
        unambiguous = len(scored) == 1 or gap >= 15
        if supported and unambiguous:
            log.info(f"  ✅ serial 一字模糊但多欄核對命中（{', '.join(best['reasons'])}）")
            return best["task"], 2

    log.info(
        f"  ⚠ 候選證據不足或仍有並列（serial距離={best['serial_dist']}, "
        f"額外證據={sorted(strong)}, 分差={gap}）→ 不敢猜"
    )
    return None, 0


# ── 命名輔助（沿用舊版）────────────────────────────────────────

def extract_order_no_from_name(task: dict) -> Optional[str]:
    """從 Asana task name 抓出 8 位 Order Number（5 或 6 開頭）"""
    match = re.search(r"\b[56]\d{7}\b", task.get("name", ""))
    return match.group(0) if match else None


def get_safe_title(task: dict) -> str:
    """保留 Asana 原文，只整理 Windows 禁用字元及多餘尾端符號。"""
    title = task.get("name", "Unknown").strip()
    title = re.sub(r"\s*[\\/:*?\"<>|]+\s*", " - ", title)
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"(?:\s+-\s*)+$", "", title).strip(" ._-")
    return title or "Unknown"

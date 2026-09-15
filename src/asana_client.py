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

MAX_PRODUCT_DIST = 2  # 型號校正容許的最大編輯距離
MAX_HOSPITAL_NAME_DIST = 2  # 完整醫院名只容許很小的手寫/OCR 誤差

_typeahead_cache: dict = {}
_task_cache: dict = {}
# Optional private index loaded by Stage B.  ``None`` means this process is
# running in the legacy/live-search mode (keeps small unit tests and emergency
# fallback behaviour compatible).
_device_index: Optional[dict] = None

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

HOSPITAL_OFFICIAL_NAMES = {
    "QMH": "Queen Mary Hospital",
    "QEH": "Queen Elizabeth Hospital",
    "KWH": "Kwong Wah Hospital",
    "KH": "Kowloon Hospital",
    "PYNEH": "Pamela Youde Nethersole Eastern Hospital",
    "PMH": "Princess Margaret Hospital",
    "HKCH": "Hong Kong Children's Hospital",
    "PWH": "Prince of Wales Hospital",
    "UCH": "United Christian Hospital",
    "TMH": "Tuen Mun Hospital",
    "NDH": "North District Hospital",
    "GH": "Grantham Hospital",
    "TUNGWAHHOSPITAL": "Tung Wah Hospital",
}


class AsanaError(RuntimeError):
    """Asana 連線或權限故障；必須保留 PDF 等下次重試。"""


def set_device_index(index: dict) -> None:
    """Install a validated device index for this process only."""
    global _device_index
    if not isinstance(index, dict) or not isinstance(index.get("devices"), list):
        raise AsanaError("Asana 設備索引格式不正確")
    _device_index = index


def clear_device_index() -> None:
    """Clear the optional index (used by tests and one-shot local runs)."""
    global _device_index
    _device_index = None


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


def product_family(value: Optional[str]) -> Optional[str]:
    """Return the stable equipment family while preserving variants elsewhere.

    Field engineers use both ``Affiniti 70`` and ``Affiniti 70G`` for the same
    family.  The index groups them together but keeps every raw spelling for
    audit and matching.
    """
    if not value:
        return None
    def family_key(raw: str) -> str:
        normalized = _norm(raw)
        if normalized.startswith("AFFINITI"):
            normalized = re.sub(r"G$", "", normalized)
        # Engineers use both the printed ``EPIQ 7+`` and the written
        # ``EPIQ 7 Plus``.  They are one product family; raw spellings remain
        # in the private index for audit.
        if normalized.startswith("EPIQ"):
            normalized = re.sub(r"PLUS$", "", normalized)
        return normalized

    normalized = family_key(value)
    best = None
    best_distance = 99
    for known in KNOWN_PRODUCTS:
        family_norm = family_key(known)
        distance = _lev(normalized, family_norm)
        if distance < best_distance:
            best, best_distance = known, distance
    return best if best_distance <= MAX_PRODUCT_DIST else normalize_product(value)


def product_group(value: Optional[str]) -> Optional[str]:
    """Return the coarse model-independent product group used by the index.

    The raw spelling is still retained separately.  Digits and suffixes are
    deliberately ignored, so Affiniti 50/70/70G and EPIQ 7G/CVx/Elite form
    their respective broad families.  Unknown Asana products use their first
    meaningful alphabetic token, allowing the private table to learn products
    without a new code release.
    """
    if not value:
        return None
    words = re.findall(r"[A-Z]+", str(value).upper())
    words = [word for word in words if word not in {"PHILIPS", "SYSTEM", "ULTRASOUND"}]
    if not words:
        return None
    joined = "".join(words)
    for known in ("AFFINITI", "EPIQ"):
        if joined.startswith(known):
            return known
    if joined.startswith("CX"):
        return "CX"
    return words[0] if len(words[0]) >= 2 else None


def hospital_acronym(value: Optional[str]) -> Optional[str]:
    """Build a conservative acronym from a full hospital name."""
    if not value:
        return None
    words = re.findall(r"[A-Za-z]+", str(value))
    if len(words) < 2:
        return None
    ignored = {"THE", "OF", "AND"}
    acronym = "".join(word[0].upper() for word in words if word.upper() not in ignored)
    return acronym if 2 <= len(acronym) <= 8 else None


def hospital_aliases(value: Optional[str]) -> List[str]:
    """Return safe comparison forms without turning room detail into a hospital."""
    if not value:
        return []
    raw = str(value).strip()
    # Slash/comma conventionally starts department/detail.  A hyphen is treated
    # as detail only for a leading hospital code (e.g. GH-3F), not in a full name.
    core = re.split(r"[,/]", raw, 1)[0].strip()
    if re.match(r"^[A-Za-z]{2,8}\s*-", core):
        core = core.split("-", 1)[0].strip()
    canonical = hospital_core(core)
    # Unknown short codes are the most common OCR hallucination (PN/KWM/PYTV).
    # They must not pass the 33% fuzzy hospital gate.
    if re.fullmatch(r"[A-Za-z]{2,6}", core) and not canonical:
        return []
    result = [core]
    if canonical:
        result.append(canonical)
    acronym = hospital_acronym(core)
    if acronym:
        result.append(acronym)
    return list(dict.fromkeys(item for item in result if item))


def split_hospital_location(value: Optional[str]) -> tuple[str, str]:
    """Separate hospital identity from a short-code floor/room suffix."""
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    core = re.split(r"[,/]", raw, 1)[0].strip()
    detail = raw[len(core):].lstrip(" ,/").strip()
    match = re.match(r"^(?P<hospital>[A-Za-z]{2,8})\s*-\s*(?P<detail>.+)$", core)
    if match:
        detail = " - ".join(filter(None, [match.group("detail").strip(), detail]))
        core = match.group("hospital").strip()
    return core, detail


def _index_hospital_aliases(value: Optional[str]) -> List[str]:
    """Expand an observed name through the embedded private location directory."""
    aliases = hospital_aliases(value)
    core, _ = split_hospital_location(value)
    observed = _norm(core)
    if not observed or _device_index is None:
        return aliases
    for group in _device_index.get("location_directory") or []:
        if not group.get("match_enabled"):
            continue
        known = [
            group.get("canonical_hospital") or "",
            *(group.get("confirmed_aliases") or []),
            *(group.get("learned_aliases") or []),
        ]
        if observed in {_norm(item) for item in known if item}:
            return list(dict.fromkeys([*aliases, *known]))
    return aliases


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

    # 完整醫院名可能有極少量抄寫誤差，例如 Tong Nah Hospital。只在它與
    # 已確認清單中的某一個完整名稱相差最多兩字、而且最近答案唯一時校正。
    # 這一步只在程式內做；候選名不會交給視覺模型，避免模型迎合答案。
    normalized = _norm(core)
    full_names = {
        raw: value for raw, value in HOSPITAL_ALIASES.items()
        if len(raw) >= 10
    }
    distances = sorted(
        (_lev(normalized, raw), raw, value)
        for raw, value in full_names.items()
    )
    if distances and distances[0][0] <= MAX_HOSPITAL_NAME_DIST:
        nearest = distances[0]
        if len(distances) == 1 or distances[1][0] > nearest[0]:
            log.info("  醫院完整名稱有輕微 OCR 誤差，已由已確認清單唯一校正")
            return nearest[2]
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


def _index_dates(ref: dict) -> List[date]:
    values = []
    for value in ref.get("work_dates") or []:
        parsed = _parse_date(str(value))
        if parsed:
            values.append(parsed)
    return values


def _text_norm(value: Optional[str]) -> str:
    """Unicode-safe comparison key for people and room names."""
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def _similarity(left: Optional[str], right: Optional[str]) -> float:
    left_key, right_key = _text_norm(left), _text_norm(right)
    if not left_key or not right_key:
        return 0.0
    return 1.0 - (_lev(left_key, right_key) / max(len(left_key), len(right_key)))


def _digit_distance(left: str, right: str) -> int:
    return _lev(re.sub(r"\D", "", left), re.sub(r"\D", "", right))


def _key_similarity(left: Optional[str], right: Optional[str]) -> float:
    left_key, right_key = _norm(left), _norm(right)
    if not left_key or not right_key:
        return 0.0
    return max(0.0, 1.0 - _lev(left_key, right_key) / max(len(left_key), len(right_key)))


def _family_similarity(left: Optional[str], right: Optional[str]) -> float:
    """Short families are exact-only; longer names use edit similarity."""
    left_key, right_key = _norm(left), _norm(right)
    if not left_key or not right_key:
        return 0.0
    if min(len(left_key), len(right_key)) < 4:
        return 1.0 if left_key == right_key else 0.0
    return _key_similarity(left_key, right_key)


def _score_index_device(row: dict, ocr_data: dict,
                        job_type: Optional[str] = None) -> dict:
    """Apply product→hospital→serial gates; ACTION DATE is deliberately absent."""
    serials = _clean_candidates(
        ocr_data.get("serial_candidates"), ocr_data.get("serial_no")
    )
    serials += [value for value in _clean_candidates(
        ocr_data.get("serial_visual_candidates")
    ) if value not in serials]
    wanted_product = product_group(ocr_data.get("product") or ocr_data.get("product_raw"))
    row_products = row.get("product_families") or [
        product_group(value) for value in row.get("product_variants") or []
    ]
    product_similarity = max(
        (_family_similarity(wanted_product, value) for value in row_products if value),
        default=0.0,
    )
    wanted_hospitals = _index_hospital_aliases(
        ocr_data.get("hospital_raw") or ocr_data.get("customer")
        or ocr_data.get("customer_raw")
    )
    row_hospitals = row.get("hospital_aliases") or [
        alias for value in [*(row.get("hospitals") or []), *(row.get("locations") or [])]
        for alias in hospital_aliases(value)
    ]
    hospital_similarity = max(
        (_key_similarity(left, right) for left in wanted_hospitals for right in row_hospitals),
        default=0.0,
    )
    row_serial = _norm(row.get("serial"))
    serial_similarity = max(
        (_key_similarity(value, row_serial) for value in serials), default=0.0
    )
    serial_dist = min(
        (_lev(_norm(value), row_serial) for value in serials if row_serial), default=99
    )

    wanted_phones = [re.sub(r"\D", "", value) for value in ocr_data.get("phone_candidates") or []]
    row_phones = [re.sub(r"\D", "", value) for value in row.get("phones") or []]
    phone_dist = min(
        (_digit_distance(left, right) for left in wanted_phones for right in row_phones
         if 7 <= len(left) <= 9 and 7 <= len(right) <= 9), default=99,
    )
    wanted_assets = [re.sub(r"\D", "", value) for value in ocr_data.get("asset_candidates") or []]
    row_assets = [re.sub(r"\D", "", value) for value in row.get("assets") or []]
    asset_dist = min(
        (_digit_distance(left, right) for left in wanted_assets for right in row_assets
         if len(left) >= 4 and len(right) >= 4), default=99,
    )
    contact_similarity = max(
        (_similarity(ocr_data.get("contact_person_raw"), value)
         for value in row.get("contacts") or []), default=0.0,
    )
    support = set()
    if product_similarity >= config.INDEX_PRODUCT_MIN_SIMILARITY:
        support.add("product")
    if hospital_similarity >= config.INDEX_HOSPITAL_MIN_SIMILARITY:
        support.add("hospital")
    if serials and serial_similarity >= config.INDEX_SERIAL_MIN_SIMILARITY:
        support.add("serial")
    if phone_dist == 0:
        support.add("phone_exact")
    elif phone_dist == 1:
        support.add("phone_fuzzy")
    if asset_dist == 0:
        support.add("asset_exact")
    elif asset_dist == 1:
        support.add("asset_fuzzy")
    if contact_similarity >= 0.75:
        support.add("contact")
    eligible = (
        not row.get("weak_identity")
        and product_similarity >= config.INDEX_PRODUCT_MIN_SIMILARITY
        and hospital_similarity >= config.INDEX_HOSPITAL_MIN_SIMILARITY
        and (not serials or serial_similarity >= config.INDEX_SERIAL_MIN_SIMILARITY)
    )
    auxiliary = (
        (1 if phone_dist == 0 else 0), (1 if asset_dist == 0 else 0),
        hospital_similarity, product_similarity, contact_similarity,
    )
    return {
        "row": row, "eligible": eligible, "serial_similarity": serial_similarity,
        "serial_dist": serial_dist, "product_similarity": product_similarity,
        "hospital_similarity": hospital_similarity, "support": support,
        "auxiliary": auxiliary,
    }


def _score_index_task_ref(ref: dict, ocr_data: dict,
                          job_type: Optional[str]) -> int:
    """Rank historical jobs inside an accepted device; never use recency alone."""
    score = 0
    ref_type = ref.get("job_type") or ""
    if job_type and ref_type and ref_type != job_type:
        return -1000
    if job_type and ref_type == job_type:
        score += 20
    service_date = (
        _parse_date(ocr_data.get("service_date_raw"))
        if ocr_data.get("date_source") == "ACTION_DATE" else None
    )
    dates = _index_dates(ref)
    if service_date and dates:
        delta = min(abs((candidate - service_date).days) for candidate in dates)
        if delta > config.INDEX_TASK_DATE_MAX_DAYS:
            return -1000
        score += 30 if delta <= 3 else 20 if delta <= 14 else 10
    for field, points in (("phones", 25), ("assets", 20)):
        wanted_key = "phone_candidates" if field == "phones" else "asset_candidates"
        wanted = [re.sub(r"\D", "", value) for value in ocr_data.get(wanted_key) or []]
        stored = [re.sub(r"\D", "", value) for value in ref.get(field) or []]
        if any(left and left == right for left in wanted for right in stored):
            score += points
        elif any(left and right and _digit_distance(left, right) == 1
                 for left in wanted for right in stored):
            score += points // 2
    contact = ocr_data.get("contact_person_raw")
    if max((_similarity(contact, value) for value in ref.get("contacts") or []), default=0.0) >= 0.75:
        score += 8
    room = ocr_data.get("department_room_raw") or ocr_data.get("location_raw")
    if max((_similarity(room, value) for value in ref.get("department_rooms") or []), default=0.0) >= 0.75:
        score += 8
    wanted_hospital = hospital_core(
        ocr_data.get("hospital_raw") or ocr_data.get("customer")
        or ocr_data.get("customer_raw")
    )
    ref_hospital = hospital_core(ref.get("hospital") or ref.get("location"))
    if wanted_hospital and ref_hospital and _norm(wanted_hospital) == _norm(ref_hospital):
        score += 8
    wanted_product = product_family(
        ocr_data.get("product") or ocr_data.get("product_raw")
    )
    ref_product = product_family(ref.get("product") or ref.get("product_variant"))
    if wanted_product and ref_product and _norm(wanted_product) == _norm(ref_product):
        score += 5
    return score


def _rank_index_devices(ocr_data: dict, job_type: Optional[str] = None) -> List[dict]:
    if _device_index is None:
        return []
    scored = [
        _score_index_device(row, ocr_data, job_type)
        for row in _device_index.get("devices", [])
    ]
    scored = [item for item in scored if item["eligible"]]
    serials_visible = bool(
        ocr_data.get("serial_candidates") or ocr_data.get("serial_visual_candidates")
        or ocr_data.get("serial_no")
    )
    if not serials_visible:
        # Without serial, all four independent facts must be exact and unique.
        scored = [item for item in scored if (
            item["product_similarity"] == 1.0
            and item["hospital_similarity"] == 1.0
            and "phone_exact" in item["support"]
            and "asset_exact" in item["support"]
        )]
        if len(scored) != 1:
            return []
    scored.sort(
        key=lambda item: (item["serial_similarity"], *item["auxiliary"]),
        reverse=True,
    )
    return scored


def get_close_index_candidates(ocr_data: dict, job_type: Optional[str] = None) -> List[dict]:
    """Return at most ten private rows only when deterministic ranking is close."""
    ranked = _rank_index_devices(ocr_data, job_type)
    serials_visible = bool(
        ocr_data.get("serial_candidates") or ocr_data.get("serial_visual_candidates")
        or ocr_data.get("serial_no")
    )
    if not serials_visible or len(ranked) < 2:
        return []
    gap = ranked[0]["serial_similarity"] - ranked[1]["serial_similarity"]
    if gap > config.INDEX_SERIAL_CLOSE_GAP:
        return []
    close_ranked = [
        item for item in ranked
        if ranked[0]["serial_similarity"] - item["serial_similarity"]
        <= config.INDEX_SERIAL_CLOSE_GAP
    ][:config.INDEX_VISION_CANDIDATE_LIMIT]
    result = []
    for number, item in enumerate(close_ranked, 1):
        row = item["row"]
        result.append({
            "candidate_id": f"C{number}", "device_key": row.get("device_key"),
            "serial": row.get("serial") or "",
            "product_families": row.get("product_families") or [],
            "product_variants": row.get("product_variants") or [],
            "hospital_aliases": row.get("hospital_aliases") or [],
            "locations": row.get("locations") or [],
            "phones": row.get("phones") or [], "contacts": row.get("contacts") or [],
            "assets": row.get("assets") or [],
        })
    return result


def _gather_index_pool(ocr_data: dict, job_type: Optional[str],
                       selected_device_key: Optional[str] = None) -> tuple[List[dict], bool]:
    """Choose one device, then hydrate its historical tasks from live Asana.

    The boolean distinguishes a genuine index miss (safe to use typeahead) from
    ambiguous indexed candidates (must stay pending instead of broadening).
    """
    if _device_index is None:
        return [], False
    raw_scores = [
        _score_index_device(row, ocr_data, job_type)
        for row in _device_index.get("devices", [])
    ]
    had_prefilter = any(
        item["product_similarity"] >= config.INDEX_PRODUCT_MIN_SIMILARITY
        and item["hospital_similarity"] >= config.INDEX_HOSPITAL_MIN_SIMILARITY
        for item in raw_scores
    )
    # Reuse the public ranking helper so the serial-missing four-exact-fields
    # rule and uniqueness check cannot diverge from candidate inspection.
    scored = _rank_index_devices(ocr_data, job_type)
    if not scored:
        return [], had_prefilter
    for rank, item in enumerate(scored[:3], 1):
        log.info(
            "  設備候選 #%s：serial相似=%s%%、產品=%s%%、醫院=%s%%、證據=%s",
            rank, round(item["serial_similarity"] * 100),
            round(item["product_similarity"] * 100),
            round(item["hospital_similarity"] * 100), sorted(item["support"]),
        )
    if selected_device_key:
        allowed = [
            item for item in scored
            if scored[0]["serial_similarity"] - item["serial_similarity"]
            <= config.INDEX_SERIAL_CLOSE_GAP
        ][:config.INDEX_VISION_CANDIDATE_LIMIT]
        selected = next(
            (item for item in allowed if item["row"].get("device_key") == selected_device_key),
            None,
        )
        if selected is None:
            log.warning("  視覺複核選擇不在安全候選內，停止配對")
            return [], True
        best = selected
    else:
        best = scored[0]
    serials_visible = bool(
        ocr_data.get("serial_candidates") or ocr_data.get("serial_visual_candidates")
        or ocr_data.get("serial_no")
    )
    gap = (
        best["serial_similarity"] - scored[1]["serial_similarity"]
        if best is scored[0] and len(scored) > 1 else 1.0
    )
    accepted = bool(selected_device_key) or not serials_visible or len(scored) == 1 \
        or gap > config.INDEX_SERIAL_CLOSE_GAP
    if not accepted:
        log.info(
            "  設備索引前兩名只相差 %s 個百分點，需要一次候選複核",
            round(gap * 100),
        )
        return [], True

    refs = list(best["row"].get("task_refs") or [])
    refs.sort(key=lambda ref: _score_index_task_ref(ref, ocr_data, job_type), reverse=True)
    gids = []
    for ref in refs:
        if _score_index_task_ref(ref, ocr_data, job_type) < 0:
            continue
        gid = str(ref.get("gid") or "")
        if gid and gid not in gids:
            gids.append(gid)
    gids = gids[:config.ASANA_MAX_HYDRATED_CANDIDATES]
    hydrated = [_fetch_task(gid) for gid in gids]
    log.info("  已鎖定一部設備，並即時讀取 %s 個歷史 Asana task", len(hydrated))
    return hydrated, True


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
    # 現行例行 PM project 也會只叫 ``2026 Jun``。這項判斷必須與建立
    # 索引時一致，否則索引找到正確工作，最後即時核對卻會丟失 PM 證據。
    monthly_pm = bool(re.search(
        r"(?:^|\s)20\d{2}\s+(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|"
        r"APR(?:IL)?|MAY|JUN(?:E)?|JUL(?:Y)?|AUG(?:UST)?|SEP(?:TEMBER)?|"
        r"OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)(?:\s|$)",
        text,
    ))
    pm = monthly_pm or bool(re.search(
        r"\bPM\b|PREVENTI(?:VE|ATIVE)|PLANNED\s+MAINT", text
    ))
    cm = bool(re.search(r"\bCM\b|CORRECTIVE|REPAIR|SERVICE\s+REQUEST", text))
    if pm == cm:
        return None
    return "PM" if pm else "CM"


def _task_contacts(task: dict) -> List[str]:
    text = f"{task.get('name') or ''}\n{task.get('notes') or ''}"
    matches = re.findall(
        r"(?im)\b(?:contact(?:\s+person)?|attn\.?|attention)\s*[:#-]?\s*"
        r"([^\r\n,;|/]{2,50})",
        text,
    )
    contacts = []
    for value in matches:
        value = re.split(r"\b(?:phone|tel|mobile|asset)\b", value, 1,
                         flags=re.IGNORECASE)[0].strip(" .,:;-")
        if sum(char.isalpha() for char in value) >= 2:
            contacts.append(value)
    # PM 工作常直接寫成 ``25956917 Ms.Yan``，沒有 Contact 標籤。
    # 電話只用來定位同一行；人名仍獨立保存和低權重比較。
    for value in re.findall(
        r"(?im)(?:\+?852[ -]?)?\d{4}[ -]?\d{4}\s+"
        r"([A-Z][A-Z .'-]{1,39})\s*$",
        text,
        flags=re.IGNORECASE,
    ):
        value = value.strip(" .,:;-")
        if sum(char.isalpha() for char in value) >= 2:
            contacts.append(value)
    return list(dict.fromkeys(contacts))


def _task_digit_tokens(task: dict) -> List[str]:
    text = f"{task.get('name') or ''}\n{task.get('notes') or ''}"
    values = []
    for value in re.findall(r"(?<!\d)\d(?:[\d -]{5,}\d)(?!\d)", text):
        digits = re.sub(r"\D", "", value)
        if digits and digits not in values:
            values.append(digits)
    return values


def _task_phones(task: dict) -> List[str]:
    """Read phone evidence by label; avoid treating an Order Number as a phone."""
    text = f"{task.get('name') or ''}\n{task.get('notes') or ''}"
    labeled = re.findall(
        r"(?i)\b(?:phone|telephone|tel\.?|mobile)\s*(?:no\.?)?\s*[#.:\-]?\s*"
        r"((?:\+?852[ -]?)?\d{4}[ -]?\d{4})",
        text,
    )
    values = []
    for value in labeled:
        digits = re.sub(r"\D", "", value)
        if len(digits) == 11 and digits.startswith("852"):
            digits = digits[3:]
        if len(digits) == 8 and digits not in values:
            values.append(digits)
    if values:
        return values

    excluded = set(re.findall(r"(?<!\d)[56]\d{7}(?!\d)", task.get("name") or ""))
    excluded.update(_task_assets(task))
    excluded.update(_task_work_orders(task))
    # Asana 人手輸入偶爾在八位電話前後多打一位；保留 7–9 位只供
    # 一字編輯距離比對。Order、asset、WO 仍先排除，不能成為電話證據。
    return [value for value in _task_digit_tokens(task)
            if 7 <= len(value) <= 9 and value not in excluded]


def _task_assets(task: dict) -> List[str]:
    text = f"{task.get('name') or ''}\n{task.get('notes') or ''}"
    values = re.findall(
        r"(?i)\basset(?:\s*(?:no\.?))?\s*[#.\-:]?\s*(\d[\d -]{2,}\d)",
        text,
    )
    return list(dict.fromkeys(
        digits for value in values
        if len(digits := re.sub(r"\D", "", value)) >= 4
    ))


def _task_work_orders(task: dict) -> List[str]:
    text = f"{task.get('name') or ''}\n{task.get('notes') or ''}"
    values = re.findall(r"(?i)\b(?:HAWO|WO)\s*[#.:\-]?\s*(\d{6,})\b", text)
    return list(dict.fromkeys(values))


def _candidate_score(task: dict, ocr_data: dict, serials: List[str],
                     hosp: Optional[str], product: Optional[str],
                     job_type: Optional[str] = None) -> dict:
    name = task.get("name") or ""
    notes = task.get("notes") or ""
    haystack_norm = _norm(f"{name}\n{notes}")
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
        score += 70
        reasons.append("serial differs by 1")
    elif best_dist == 2:
        score += 50
        reasons.append("serial differs by 2")
    elif best_dist == 3:
        score += 30
        reasons.append("serial differs by 3")

    phones = [re.sub(r"\D", "", value)
              for value in ocr_data.get("phone_candidates", [])]
    task_phones = _task_phones(task)
    if any(len(value) >= 6 and value == candidate
           for value in phones for candidate in task_phones):
        score += 45
        support.add("phone_exact")
        reasons.append("phone")
    elif any(
        7 <= len(value) <= 9 and 7 <= len(candidate) <= 9
        and _lev(value, candidate) == 1
        for value in phones for candidate in task_phones
    ):
        score += 20
        support.add("phone_fuzzy")
        reasons.append("phone differs by 1")

    assets = [re.sub(r"\D", "", value)
              for value in ocr_data.get("asset_candidates", [])]
    task_assets = _task_assets(task)
    if any(len(value) >= 4 and value == candidate
           for value in assets for candidate in task_assets):
        score += 35
        support.add("asset_exact")
        reasons.append("asset")
    elif any(
        len(value) >= 4 and len(candidate) >= 4 and _lev(value, candidate) == 1
        for value in assets for candidate in task_assets
    ):
        score += 15
        support.add("asset_fuzzy")
        reasons.append("asset differs by 1")

    work_orders = [re.sub(r"\D", "", value)
                   for value in ocr_data.get("work_order_candidates", [])]
    task_work_orders = _task_work_orders(task)
    if any(len(value) >= 6 and value == candidate
           for value in work_orders for candidate in task_work_orders):
        score += 60
        support.add("work_order")
        reasons.append("HAWO/WO")

    hospital_ok = bool(
        hosp and _norm(hospital_core(name)) == _norm(hosp)
    )
    product_ok = bool(product and _norm(product) in _norm(name))
    if hospital_ok:
        score += 20
        support.add("hospital")
        reasons.append("hospital prefix")
    if product_ok:
        score += 15
        support.add("product")
        reasons.append("product")
    if hospital_ok and product_ok:
        support.add("hospital+product")

    contact = ocr_data.get("contact_person_raw")
    contact_similarity = max(
        (_similarity(contact, candidate) for candidate in _task_contacts(task)),
        default=0.0,
    )
    if contact_similarity >= 0.90:
        score += 8
        support.add("contact")
        reasons.append("contact")
    elif contact_similarity >= 0.75:
        score += 5
        support.add("contact")
        reasons.append("contact similar")

    # 新 OCR 已把 Dept./Room 中純 Asset 內容移除；不可再把整段 Asset#
    # 當作地點加分。只有舊資料完全沒有新欄位時才回退 location_raw。
    location = ocr_data.get("location_raw") or ""
    if len(_norm(location)) >= 3 and _norm(location) in haystack_norm:
        score += 5
        reasons.append("location")
    room = ocr_data.get("department_room_raw") or ""
    if len(_norm(room)) >= 2 and _norm(room) in haystack_norm:
        score += 8
        support.add("room")
        reasons.append("department/room")

    service_date = None
    if ocr_data.get("date_source") == "ACTION_DATE":
        service_date = _parse_date(ocr_data.get("service_date_raw"))
    task_dates = _task_dates(task)
    date_delta = None
    date_year_corrected = False
    if service_date and task_dates:
        date_delta, date_year_corrected = _service_date_delta(service_date, task_dates)
        if date_delta > config.INDEX_TASK_DATE_MAX_DAYS:
            # Once ACTION DATE is readable, a task more than one month away is
            # a different visit on the same device, not a weak candidate.
            return {
                "task": task, "score": -1000, "serial_dist": best_dist,
                "support": support, "reasons": [*reasons, "date conflict"],
                "date_delta": date_delta, "job_type": candidate_type,
            }
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

def find_task(ocr_data: dict, job_type: str = None,
              selected_device_key: Optional[str] = None) -> Tuple[Optional[dict], int]:
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

    # The private index is only a local narrowing aid.  If it has no usable
    # row, retain the existing live typeahead search for newly-created tasks;
    # once rows are found, do not broaden the search and risk a false match.
    used_index = False
    if _device_index is not None:
        pool, index_had_candidates = _gather_index_pool(
            ocr_data, job_type, selected_device_key=selected_device_key
        )
        used_index = bool(pool)
        if not pool and not index_had_candidates:
            log.info("  設備索引沒有候選，改用即時 Asana 搜尋後備")
            pool = _gather_pool(
                order_no, serials, hosp, product, phones, assets, work_orders
            )
        elif not pool:
            return None, 0
    else:
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
    scored = [row for row in scored if row["score"] > -1000]
    if not scored:
        log.info("  所有歷史工作都與 ACTION DATE 相差超過一個月")
        return None, 0
    scored.sort(key=lambda row: (row["score"], -row["serial_dist"]), reverse=True)
    for rank, row in enumerate(scored[:3], 1):
        log.info(
            "  工作候選 #%s：分數=%s、serial距離=%s、證據=%s",
            rank, row["score"], row["serial_dist"], sorted(row["support"]),
        )
    best = scored[0]
    runner_score = scored[1]["score"] if len(scored) > 1 else -1
    gap = best["score"] - runner_score

    if not serials:
        # serial 仍是首選設備身分證；但手寫 serial 可能每輪都讀得不同。
        # 此時只接受唯一候選，而且完整 task 必須同時精確包含電話、asset
        # 及最近 ACTION DATE。三項來自不同欄位，不能只靠醫院/型號猜。
        required = {"phone_exact", "asset_exact", "hospital", "product"}
        if len(scored) == 1 and required.issubset(best["support"]):
            log.info(
                f"  ✅ serial 未形成共識，但電話、asset、日期唯一命中"
                f"（{', '.join(best['reasons'])}）"
            )
            return best["task"], 2
        # 未通過格式或只出現一輪的 serial 絕不拿去全域搜尋。只有電話等
        # 可靠欄位已把 Asana 候選縮到唯一一筆後，才容許原始讀數作距離核對；
        # 仍須至少兩項來自其他欄位的獨立證據。
        visual_support = best["support"] & {
            "phone_exact", "phone_fuzzy", "asset_exact", "asset_fuzzy",
            "contact", "date", "hospital", "product", "room", "job_type",
        }
        visual_serial_ok = (
            any(
                _key_similarity(value, task_serial) >= config.INDEX_SERIAL_MIN_SIMILARITY
                for value in visual_serials for task_serial in _task_serials(best["task"])
            )
            if used_index else best["serial_dist"] <= 1
        )
        if (
            len(scored) == 1
            and visual_serials
            and visual_serial_ok
            and len(visual_support) >= 2
        ):
            log.info(
                "  ✅ serial 原始抄錄接近固定 serial，且唯一候選有多欄支持"
                f"（{', '.join(best['reasons'])}）"
            )
            return best["task"], 2
        return None, 0

    # 同一設備在 Asana 會有很多歷史工作；完成狀態不是新舊依據。
    # serial 完全一致仍須日期/電話/asset，或醫院+型號一起支持；若有並列歷史
    # 工作，分數亦必須拉開。serial 有誤差時要求至少兩組額外證據。
    strong = best["support"]
    serial_ambiguous = bool(ocr_data.get("serial_ambiguous"))
    if best["serial_dist"] == 0 and not serial_ambiguous:
        supported = bool(strong & {
            "date", "phone_exact", "phone_fuzzy", "asset_exact", "asset_fuzzy",
            "contact", "room", "work_order", "hospital", "product", "job_type"
        })
        unambiguous = len(scored) == 1 or gap >= 10
        if supported and unambiguous:
            log.info(f"  ✅ Asana 多欄核對命中（{', '.join(best['reasons'])}）")
            return best["task"], 2
    elif best["serial_dist"] <= (
        max(1, int(max((len(_norm(value)) for value in scoring_serials), default=0) * 0.5))
        if used_index else 1
    ):
        # 索引已先鎖定設備；task 層仍須用獨立欄位分辨同一設備的歷史工作。
        # 任何 1–3 字誤差都至少要兩項額外證據及一項強設備證據。
        additional = strong & {
            "date", "phone_exact", "phone_fuzzy", "asset_exact", "asset_fuzzy",
            "contact", "room", "work_order", "hospital", "product", "job_type",
        }
        strong_device = additional & {"phone_exact", "asset_exact", "hospital", "product"}
        needed = 2
        supported = len(additional) >= needed and bool(strong_device)
        unambiguous = len(scored) == 1 or gap >= 15
        if supported and unambiguous:
            log.info(f"  ✅ serial 模糊但多欄核對命中（{', '.join(best['reasons'])}）")
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

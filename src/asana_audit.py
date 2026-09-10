"""私人急件核對：以 serial、電話及 asset 在 Asana 名稱與描述全文搜尋。

輸入與輸出只經私人 Google Drive 控制檔傳遞；Actions log 只顯示筆數，
不印客戶電話、Asana 描述或查詢結果。
"""
import json
import os
import re
import time
from pathlib import Path

import requests

from . import config


ORDER_RE = re.compile(r"(?<!\d)([56]\d{7})(?!\d)")
TASK_FIELDS = (
    "gid,name,notes,completed,created_at,modified_at,completed_at,"
    "due_on,start_on,permalink_url"
)


def _norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _request_search(query: str) -> list[dict]:
    if not config.ASANA_TOKEN or not config.ASANA_WORKSPACE_GID:
        raise RuntimeError("Asana 設定未提供")
    url = f"{config.ASANA_BASE_URL}/workspaces/{config.ASANA_WORKSPACE_GID}/tasks/search"
    headers = {"Authorization": f"Bearer {config.ASANA_TOKEN}"}
    params = {
        "text": query,
        "sort_by": "modified_at",
        "sort_ascending": "false",
        "limit": 100,
        "opt_fields": TASK_FIELDS,
    }
    response = None
    for attempt in range(1, 5):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as exc:
            if attempt == 4:
                raise RuntimeError("Asana 全文搜尋連線失敗") from exc
            time.sleep(min(2 ** attempt, 30))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 4:
                raise RuntimeError(f"Asana 全文搜尋失敗：HTTP {response.status_code}")
            retry_after = response.headers.get("Retry-After")
            try:
                delay = max(1.0, min(float(retry_after), 120.0))
            except (TypeError, ValueError):
                delay = min(2 ** attempt, 30)
            time.sleep(delay)
            continue
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError("Asana 全文搜尋回傳格式不正確")
        return data
    raise RuntimeError("Asana 全文搜尋失敗")


def _task_score(task: dict, case: dict, recent_after: str) -> tuple[int, list[str]]:
    name = task.get("name") or ""
    notes = task.get("notes") or ""
    haystack = _norm(f"{name}\n{notes}")
    digit_haystack = _digits(f"{name}\n{notes}")
    score = 0
    reasons = []

    serial_hits = [v for v in case.get("serial_candidates", []) if _norm(v) in haystack]
    if serial_hits:
        score += 100
        reasons.append(f"serial:{serial_hits[0]}")

    phone_hits = [v for v in case.get("phone_candidates", []) if _digits(v) in digit_haystack]
    if phone_hits:
        score += 45
        reasons.append(f"phone:{phone_hits[0]}")

    asset_hits = [v for v in case.get("asset_candidates", []) if _digits(v) in digit_haystack]
    if asset_hits:
        score += 30
        reasons.append(f"asset:{asset_hits[0]}")

    name_norm = _norm(name)
    hospital_hits = [v for v in case.get("hospital_candidates", []) if name_norm.startswith(_norm(v))]
    if hospital_hits:
        score += 20
        reasons.append(f"hospital:{hospital_hits[0]}")

    product = case.get("product") or ""
    if product and _norm(product) in name_norm:
        score += 15
        reasons.append(f"product:{product}")

    date_fields = ("created_at", "modified_at", "completed_at", "due_on", "start_on")
    if any((task.get(field) or "")[:10] >= recent_after for field in date_fields):
        score += 10
        reasons.append("recent")

    return score, reasons


def audit_cases(cases: list[dict], recent_after: str) -> list[dict]:
    search_cache: dict[str, list[dict]] = {}
    results = []
    for case in cases:
        queries = list(dict.fromkeys(
            case.get("serial_candidates", [])
            + case.get("phone_candidates", [])
            + case.get("asset_candidates", [])
        ))
        tasks: dict[str, dict] = {}
        for query in queries:
            if query not in search_cache:
                search_cache[query] = _request_search(query)
            for task in search_cache[query]:
                if task.get("gid"):
                    tasks[task["gid"]] = task

        candidates = []
        for task in tasks.values():
            score, reasons = _task_score(task, case, recent_after)
            order_match = ORDER_RE.search(task.get("name") or "")
            candidates.append({
                "score": score,
                "reasons": reasons,
                "order_no": order_match.group(1) if order_match else None,
                **task,
            })
        candidates.sort(key=lambda item: (item["score"], item.get("modified_at") or ""), reverse=True)
        results.append({
            "id": case.get("id"),
            "source_file": case.get("source_file"),
            "sheet": case,
            "candidates": candidates,
        })
    return results


def main() -> None:
    input_path = Path(os.environ.get("ASANA_AUDIT_FILE", "/tmp/jobsheet-asana-audit.json"))
    output_path = Path(os.environ.get("ASANA_AUDIT_RESULT", "/tmp/jobsheet-asana-audit-results.json"))
    control = json.loads(input_path.read_text(encoding="utf-8"))
    cases = control.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RuntimeError("核對控制檔沒有 cases")
    results = audit_cases(cases, control.get("recent_after", "2026-06-10"))
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已核對 {len(cases)} 份 jobsheet；詳細資料只回傳私人 Google Drive。")


if __name__ == "__main__":
    main()

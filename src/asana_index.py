"""建立及讀取 Asana 設備索引。

索引是私有的加速資料，不是 Asana 的最終真相：每次真正命名前，
``asana_client`` 仍會用 task GID 讀取最新 Asana task。索引不保存 Order
Number，只保存能把同一部機器及其歷史工作縮小出來的資料。
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import os
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests

from . import asana_client, config

log = logging.getLogger(__name__)

INDEX_SCHEMA_VERSION = 2
DEFAULT_WINDOW_DAYS = 365 * 2
TASK_PAGE_SIZE = 100
PROJECT_PAGE_SIZE = 100
_DATE_RE = re.compile(
    r"(?<!\d)(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?!\d)"
)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?852[ -]?)?\d{4}[ -]?\d{4}(?!\d)")
_LABELED_PHONE_RE = re.compile(
    r"(?i)\b(?:phone|telephone|tel\.?|mobile)\s*(?:no\.?)?\s*[#.:\-]?\s*"
    r"(?P<value>(?:\+?852[ -]?)?\d{4}[ -]?\d{4})"
)
_ASSET_RE = re.compile(
    r"\bASSET(?:\s*(?:NO\.?))?\s*[#.:\-]?\s*"
    r"(?P<value>(?:\d[\d\- ]{3,}\d|\d{4,}))\b",
    re.IGNORECASE,
)
_ROOM_RE = re.compile(
    r"(?im)\b(?:dept\.?|department|room|ward|unit|floor|病房|樓|座)\s*[:#-]?\s*"
    r"([A-Z0-9][A-Z0-9 _./-]{0,30})"
)
_CONTACT_RE = re.compile(
    r"(?im)\b(?:contact(?:\s+person)?|attn\.?|attention)\s*[:#-]?\s*"
    r"(?P<value>[^\r\n,;|/]{2,50})"
)


class AsanaIndexError(RuntimeError):
    """索引無法建立或通過格式檢查。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value or "")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _norm(value: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _unique(values: Iterable[str]) -> list[str]:
    seen = OrderedDict()
    for value in values:
        value = str(value or "").strip()
        if value:
            seen.setdefault(value, None)
    return list(seen)


def _request(url: str, params: dict, *, timeout: int = 30) -> dict:
    if not config.ASANA_TOKEN or not config.ASANA_WORKSPACE_GID:
        raise AsanaIndexError("ASANA_TOKEN 或 ASANA_WORKSPACE_GID 未設定")
    headers = {"Authorization": f"Bearer {config.ASANA_TOKEN}"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AsanaIndexError(f"Asana 索引讀取失敗：{url}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise AsanaIndexError("Asana 索引回傳格式不正確")
    return payload


def _project_is_pm_cm(name: Optional[str]) -> Optional[str]:
    text = str(name or "").upper()
    pm = bool(re.search(r"\bPM\b|PREVENTI(?:VE|ATIVE)|PLANNED\s+MAINT", text))
    cm = bool(re.search(r"\bCM\b|CORRECTIVE|REPAIR|SERVICE\s+REQUEST", text))
    if pm == cm:
        return None
    return "PM" if pm else "CM"


def iter_projects() -> Iterable[dict]:
    """分頁列出 workspace project，只保留明確的 PM/CM project。"""
    offset = None
    while True:
        params = {
            "workspace": config.ASANA_WORKSPACE_GID,
            "archived": "false",
            "limit": PROJECT_PAGE_SIZE,
            "opt_fields": "gid,name,archived,modified_at",
        }
        if offset:
            params["offset"] = offset
        payload = _request(f"{config.ASANA_BASE_URL}/projects", params)
        for project in payload["data"]:
            job_type = _project_is_pm_cm(project.get("name"))
            if job_type:
                yield {**project, "job_type": job_type}
        offset = (payload.get("next_page") or {}).get("offset")
        if not offset:
            return


def iter_project_tasks(project_gid: str) -> Iterable[dict]:
    """分頁讀取一個 project 的 task 索引所需欄位。"""
    offset = None
    fields = (
        "gid,name,notes,completed,created_at,modified_at,completed_at,due_on,start_on,"
        "permalink_url,memberships.project.name,memberships.section.name"
    )
    while True:
        params = {"project": project_gid, "limit": TASK_PAGE_SIZE, "opt_fields": fields}
        if offset:
            params["offset"] = offset
        payload = _request(f"{config.ASANA_BASE_URL}/tasks", params)
        yield from payload["data"]
        offset = (payload.get("next_page") or {}).get("offset")
        if not offset:
            return


def _task_dates(task: dict) -> list[str]:
    values = [task.get("due_on"), task.get("start_on")]
    values += _DATE_RE.findall(task.get("notes") or "")
    return _unique(values)


def _task_job_type(task: dict, project_type: Optional[str]) -> Optional[str]:
    if project_type:
        return project_type
    text = " ".join(
        str(item.get(kind, {}).get("name") or "")
        for item in task.get("memberships") or []
        for kind in ("project", "section")
    )
    return _project_is_pm_cm(text)


def _name_parts(name: str) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    parts = [part.strip() for part in re.split(r"\s*/\s*|\s+\|\s+", name or "")]
    location = parts[0] if parts else ""
    product = None
    product_variant = None
    for part in parts[1:]:
        family = asana_client.product_family(part)
        if family in asana_client.KNOWN_PRODUCTS:
            product = family
            product_variant = part
            break
    if product is None:
        for known in asana_client.KNOWN_PRODUCTS:
            if _norm(known) in _norm(name):
                product = asana_client.product_family(known)
                product_variant = known
                break
    serial = None
    # Asana titles occasionally contain ordinary words that satisfy the old
    # broad ``extract_serial`` regex.  The index applies the same business
    # gate as OCR: 2–3 leading letters, 8–12 total characters, and digits.
    for candidate in re.findall(r"\b[A-Z]{2,3}[A-Z0-9]{5,9}\b", name.upper()):
        if 8 <= len(candidate) <= 12 and any(ch.isdigit() for ch in candidate) \
                and sum(ch.isdigit() for ch in candidate) >= 4:
            serial = candidate
            break
    return location, product, product_variant, serial


def _phones(text: str, *, excluded_numbers: Iterable[str] = ()) -> list[str]:
    """Extract phone evidence without mistaking an order number for a phone.

    Hong Kong mobile numbers and Order Numbers can both be eight digits beginning
    with 6, so shape alone cannot distinguish them.  A phone label wins; for
    unlabelled numbers we omit values already seen as an Order Number in the task
    title.  The excluded values are not stored anywhere in the resulting index.
    """
    excluded = {re.sub(r"\D", "", value) for value in excluded_numbers}
    result = []
    labeled = [match.group("value") for match in _LABELED_PHONE_RE.finditer(text or "")]
    candidates = labeled or _PHONE_RE.findall(text or "")
    for value in candidates:
        digits = re.sub(r"\D", "", value)
        if len(digits) == 11 and digits.startswith("852"):
            digits = digits[3:]
        if len(digits) == 8:
            if digits in excluded and not labeled:
                continue
            result.append(digits)
    return _unique(result)


def _assets(text: str) -> list[str]:
    result = []
    for match in _ASSET_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group("value"))
        if len(digits) >= 4:
            result.append(digits)
    return _unique(result)


def _department_rooms(text: str) -> list[str]:
    """保留 Dept./Room 的原文；它只作核對欄位，不作設備主鍵。"""
    values = []
    for match in _ROOM_RE.finditer(text or ""):
        value = re.split(r"\s{2,}|\b(?:phone|tel|asset)\b", match.group(1), 1,
                         flags=re.IGNORECASE)[0].strip(" .,:;-")
        if value:
            values.append(value)
    return _unique(values)


def _contacts(text: str) -> list[str]:
    values = []
    for match in _CONTACT_RE.finditer(text or ""):
        value = re.sub(r"\s+", " ", match.group("value")).strip(" .,:;-")
        value = re.split(r"\b(?:phone|tel|mobile|asset)\b", value, 1,
                         flags=re.IGNORECASE)[0].strip(" .,:;-")
        if sum(char.isalpha() for char in value) >= 2:
            values.append(value)
    return _unique(values)


def _within_window(task: dict, start: date, end: date) -> bool:
    values = []
    for key in ("created_at", "modified_at", "completed_at"):
        parsed = _parse_iso(task.get(key))
        if parsed:
            values.append(parsed.date())
    for key in ("due_on", "start_on"):
        try:
            if task.get(key):
                values.append(date.fromisoformat(str(task[key])[:10]))
        except ValueError:
            pass
    # 無任何可比較日期的 task 仍保留，避免因 Asana 欄位缺失漏掉設備。
    return not values or any(start <= value <= end for value in values)


def _device_key(location: str, product: Optional[str], serial: Optional[str]) -> tuple[str, bool]:
    product_key = _norm(asana_client.product_family(product))
    serial_key = _norm(serial)
    if serial_key and product_key:
        return f"{serial_key}|{product_key}", False
    # 沒有 serial 只能建立弱索引；processor 絕不只靠這列自動命名。
    fallback = f"{_norm(location)}|{product_key}"
    return f"WEAK|{fallback}", True


def task_to_record(task: dict, project_type: Optional[str] = None) -> Optional[dict]:
    name = str(task.get("name") or "").strip()
    location, product, product_variant, serial = _name_parts(name)
    notes = str(task.get("notes") or "")
    if not location and not product and not serial:
        return None
    job_type = _task_job_type(task, project_type)
    key, weak = _device_key(location, product, serial)
    hospital = asana_client.hospital_core(location) or location or None
    # Order Numbers deliberately never enter the index.  They are only used to
    # remove title numbers from the generic-phone fallback; labelled phones such
    # as ``Phone: 61234567`` remain valid evidence.
    combined_text = f"{name}\n{notes}"
    indexed_order_numbers = re.findall(
        r"(?i)\b(?:order(?:\s*(?:no\.?|number))?|sr\s*#?)\s*[:#\-]?\s*"
        r"([56]\d{7})\b",
        combined_text,
    )
    # A bare eight-digit number in a task title is also conventionally an
    # Order Number.  It is excluded only from the unlabelled-phone fallback.
    indexed_order_numbers += re.findall(r"(?<!\d)[56]\d{7}(?!\d)", name)
    phones = _phones(combined_text, excluded_numbers=indexed_order_numbers)
    contacts = _contacts(f"{name}\n{notes}")
    assets = _assets(f"{name}\n{notes}")
    department_rooms = _department_rooms(f"{name}\n{notes}")
    work_dates = _task_dates(task)
    task_ref = {
        "gid": str(task.get("gid") or ""),
        "permalink_url": task.get("permalink_url") or "",
        "created_at": task.get("created_at") or "",
        "modified_at": task.get("modified_at") or "",
        "completed_at": task.get("completed_at") or "",
        "due_on": task.get("due_on") or "",
        "start_on": task.get("start_on") or "",
        "work_dates": work_dates,
        "job_type": job_type or "",
        "completed": bool(task.get("completed")),
        "location": location,
        "hospital": hospital or "",
        "department_rooms": department_rooms,
        "product": product or "",
        "product_variant": product_variant or "",
        "serial": serial or "",
        "phones": phones,
        "contacts": contacts,
        "assets": assets,
    }
    return {
        "device_key": key,
        "weak_identity": weak,
        "serial": serial or "",
        "product": product or "",
        "product_variants": _unique([product_variant] if product_variant else []),
        "locations": _unique([location]),
        "hospitals": _unique([hospital] if hospital else []),
        "department_rooms": department_rooms,
        "phones": phones,
        "contacts": contacts,
        "assets": assets,
        "work_dates": work_dates,
        "job_types": _unique([job_type] if job_type else []),
        "task_refs": [task_ref],
    }


def _merge_record(target: dict, incoming: dict) -> None:
    for key in (
        "product_variants", "locations", "hospitals", "department_rooms",
        "phones", "contacts", "assets", "work_dates", "job_types",
    ):
        target[key] = _unique([*(target.get(key) or []), *(incoming.get(key) or [])])
    existing = {ref.get("gid"): ref for ref in target.get("task_refs") or [] if ref.get("gid")}
    for ref in incoming.get("task_refs") or []:
        if ref.get("gid"):
            existing[ref["gid"]] = ref
    target["task_refs"] = sorted(existing.values(), key=lambda ref: (
        ref.get("due_on") or ref.get("start_on") or ref.get("modified_at") or "",
        ref.get("gid") or "",
    ), reverse=True)


def build_index(tasks: Iterable[dict], *, generated_at: Optional[str] = None,
                window_start: Optional[date] = None,
                window_end: Optional[date] = None,
                project_count: int = 0,
                existing_index: Optional[dict] = None) -> dict:
    end = window_end or _now().date()
    start = window_start or (end - timedelta(days=DEFAULT_WINDOW_DAYS))
    devices: OrderedDict[str, dict] = OrderedDict()
    existing_max = ""
    known_gids: set[str] = set()
    known_modified: dict[str, str] = {}
    changed_gids: set[str] = set()
    old_unparsed = 0
    if existing_index:
        validate_index(existing_index)
        existing_max = str(existing_index.get("max_task_modified_at") or "")
        old_unparsed = int(existing_index.get("unparsed_task_count") or 0)
        for original in existing_index.get("devices") or []:
            row = copy.deepcopy(original)
            devices[row["device_key"]] = row
            for ref in row.get("task_refs") or []:
                ref_gid = str(ref.get("gid") or "")
                if ref_gid:
                    known_gids.add(ref_gid)
                    known_modified[ref_gid] = str(ref.get("modified_at") or "")
    seen_tasks: set[str] = set()
    in_window = 0
    unparsed = 0
    max_modified = ""
    for task in tasks:
        gid = str(task.get("gid") or "")
        if not gid or gid in seen_tasks:
            continue
        seen_tasks.add(gid)
        modified = str(task.get("modified_at") or "")
        # Compare with this task's own indexed timestamp, not the global newest
        # timestamp.  Otherwise a task changed on Sep-10 could be skipped merely
        # because another unrelated task was already modified on Sep-15.
        if (
            existing_index and gid in known_gids and modified
            and modified <= known_modified.get(gid, "")
        ):
            continue
        if existing_index:
            changed_gids.add(gid)
            # A changed task may have moved device, project type or title.  Drop
            # its old reference before adding the freshly read version.
            for key in list(devices):
                refs = [ref for ref in devices[key].get("task_refs") or [] if ref.get("gid") != gid]
                devices[key]["task_refs"] = refs
                if not refs:
                    devices.pop(key)
        
        if not _within_window(task, start, end):
            continue
        in_window += 1
        if modified > max_modified:
            max_modified = modified
        record = task_to_record(task, task.get("_project_job_type"))
        if record is None:
            unparsed += 1
            continue
        if record["device_key"] not in devices:
            devices[record["device_key"]] = record
        else:
            _merge_record(devices[record["device_key"]], record)
    stamp = generated_at or _iso(_now())
    rows = list(devices.values())
    if existing_index:
        # Keep the old high-water mark when this scan found no newer task.
        max_modified = max(max_modified, existing_max)
        task_count = max(int(existing_index.get("task_count") or 0), len(known_gids))
        task_count += len(changed_gids - known_gids)
        task_count_in_window = max(
            int(existing_index.get("task_count_in_window") or 0), in_window
        )
        unparsed = max(0, old_unparsed + unparsed)
    else:
        task_count = len(seen_tasks)
        task_count_in_window = in_window
    merged_count = max(0, sum(len(row.get("task_refs") or []) for row in rows) - len(rows))
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": stamp,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "max_task_modified_at": max_modified,
        "project_count": project_count,
        "task_count": task_count,
        "task_count_in_window": task_count_in_window,
        "unparsed_task_count": unparsed,
        "device_count": len(rows),
        "merged_task_count": merged_count,
        "devices": rows,
    }


def build_from_asana(*, window_start: Optional[date] = None,
                     window_end: Optional[date] = None,
                     existing_index: Optional[dict] = None) -> dict:
    projects = list(iter_projects())
    tasks: OrderedDict[str, dict] = OrderedDict()
    for project in projects:
        for task in iter_project_tasks(project["gid"]):
            task = dict(task)
            task["_project_job_type"] = project["job_type"]
            tasks.setdefault(str(task.get("gid") or ""), task)
    index = build_index(
        tasks.values(), generated_at=_iso(_now()), window_start=window_start,
        window_end=window_end, project_count=len(projects), existing_index=existing_index,
    )
    log.info(
        "Asana 索引：%s 個 project，%s 個 task，%s 部設備，%s 個未解析 task",
        index["project_count"], index["task_count_in_window"],
        index["device_count"], index["unparsed_task_count"],
    )
    return index


def validate_index(index: dict) -> dict:
    if not isinstance(index, dict) or index.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise AsanaIndexError("Asana 索引版本不相容")
    if not isinstance(index.get("devices"), list):
        raise AsanaIndexError("Asana 索引沒有 devices 清單")
    for row in index["devices"]:
        if not isinstance(row, dict) or not row.get("device_key"):
            raise AsanaIndexError("Asana 索引包含無效設備列")
        if not isinstance(row.get("task_refs"), list):
            raise AsanaIndexError("Asana 索引 task_refs 格式不正確")
        for field in (
            "product_variants", "locations", "hospitals", "department_rooms",
            "phones", "contacts", "assets", "work_dates", "job_types",
        ):
            if not isinstance(row.get(field), list):
                raise AsanaIndexError(f"Asana 索引欄位格式不正確：{field}")
        for ref in row["task_refs"]:
            if not isinstance(ref, dict) or not ref.get("gid"):
                raise AsanaIndexError("Asana 索引包含無效 task GID")
            for field in ("department_rooms", "phones", "contacts", "assets", "work_dates"):
                if not isinstance(ref.get(field), list):
                    raise AsanaIndexError(f"Asana 索引 task 欄位格式不正確：{field}")
    return index


def write_outputs(index: dict, output_dir: Path) -> None:
    validate_index(index)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "asana-device-index.json"
    csv_path = output_dir / "asana-device-index.csv"
    manifest_path = output_dir / "asana-device-index-manifest.json"
    json_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "device_key", "weak_identity", "serial", "product", "product_variants",
        "locations", "hospitals", "department_rooms", "phones", "contacts",
        "assets", "work_dates", "job_types",
        "task_count", "task_gids", "task_links",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in index["devices"]:
            refs = row.get("task_refs") or []
            writer.writerow({
                "device_key": row["device_key"],
                "weak_identity": str(bool(row.get("weak_identity"))).lower(),
                "serial": row.get("serial") or "",
                "product": row.get("product") or "",
                "product_variants": " ; ".join(row.get("product_variants") or []),
                "locations": " ; ".join(row.get("locations") or []),
                "hospitals": " ; ".join(row.get("hospitals") or []),
                "department_rooms": " ; ".join(row.get("department_rooms") or []),
                "phones": " ; ".join(row.get("phones") or []),
                "contacts": " ; ".join(row.get("contacts") or []),
                "assets": " ; ".join(row.get("assets") or []),
                "work_dates": " ; ".join(row.get("work_dates") or []),
                "job_types": " ; ".join(row.get("job_types") or []),
                "task_count": len(refs),
                "task_gids": " ; ".join(ref["gid"] for ref in refs),
                "task_links": " ; ".join(ref.get("permalink_url") or "" for ref in refs),
            })
    manifest = {key: index.get(key) for key in (
        "schema_version", "generated_at", "window_start", "window_end",
        "max_task_modified_at", "project_count", "task_count",
        "task_count_in_window", "unparsed_task_count", "device_count",
        "merged_task_count",
    )}
    manifest.update({
        "json_file": json_path.name,
        "csv_file": csv_path.name,
        "manifest_file": manifest_path.name,
    })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def load_index(path: Path, manifest_path: Optional[Path] = None) -> dict:
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AsanaIndexError(f"無法讀取 Asana 索引：{path}") from exc
    index = validate_index(index)
    if manifest_path is not None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AsanaIndexError(f"無法讀取 Asana 索引 manifest：{manifest_path}") from exc
        if not isinstance(manifest, dict):
            raise AsanaIndexError("Asana 索引 manifest 格式不正確")
        for key in ("schema_version", "generated_at", "device_count"):
            if str(manifest.get(key, "")) != str(index.get(key, "")):
                raise AsanaIndexError(f"Asana 索引與 manifest 不一致：{key}")
    return index


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=os.environ.get("ASANA_INDEX_OUTPUT_DIR", "/tmp/asana-index"))
    parser.add_argument("--window-start", default=os.environ.get("ASANA_INDEX_WINDOW_START", ""))
    parser.add_argument("--window-end", default=os.environ.get("ASANA_INDEX_WINDOW_END", ""))
    parser.add_argument("--existing-index", default=os.environ.get("ASANA_INDEX_EXISTING_PATH", ""))
    args = parser.parse_args()
    try:
        start = date.fromisoformat(args.window_start) if args.window_start else None
        end = date.fromisoformat(args.window_end) if args.window_end else None
        existing = None
        if args.existing_index:
            existing = load_index(Path(args.existing_index))
        index = build_from_asana(window_start=start, window_end=end, existing_index=existing)
        write_outputs(index, Path(args.output_dir))
    except (AsanaIndexError, ValueError) as exc:
        log.error("Asana 索引建立失敗：%s", exc)
        return 1
    print(json.dumps({key: index.get(key) for key in (
        "generated_at", "window_start", "window_end", "project_count",
        "task_count_in_window", "device_count", "unparsed_task_count",
        "merged_task_count",
    )}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

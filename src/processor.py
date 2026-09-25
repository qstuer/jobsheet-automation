#!/usr/bin/env python3
"""
階段 B — 處理員（Processor）

對 _SPLIT/ 裡每一份「單一 job PDF」：
    → 多輪 OCR（不同清晰度先取得欄位共識）
    → Asana 多欄配對（serial + 日期/電話/asset/醫院/型號）
    → 命名後上傳 onedrive:.../JOBSHEETS/ → 刪 _SPLIT 那份

命名：
  找到任務 + 任務名含 Order No → SR#OrderNo.pdf
  找到任務 + 任務名無 Order No → Asana 任務標題.pdf
  多輪全失敗            → 留在 Google Drive _PENDING，不碰 OneDrive

手動模式可指定 _SPLIT 或 _PENDING 的一份 PDF；dry-run 全程不寫雲端。
"""
import logging
import os
import re
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

import fitz

from . import asana_client, asana_index, batch_state, config, nvidia_client, private_ocr_context, rclone_helper

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("processor")

TARGET_FILE_ENV = "JOBSHEET_TARGET_FILE"
DRY_RUN_ENV = config.JOBSHEET_DRY_RUN_ENV
SOURCE_QUEUE_ENV = config.JOBSHEET_SOURCE_QUEUE_ENV
CONFIRMED_FILENAME_ENV = "JOBSHEET_CONFIRMED_FILENAME"
ASANA_INDEX_FILE_ENV = "ASANA_INDEX_LOCAL_FILE"
ASANA_INDEX_MANIFEST_ENV = "ASANA_INDEX_MANIFEST_LOCAL_FILE"
CONTEXT_FIELD_OCR_ENV = "JOBSHEET_CONTEXT_FIELD_OCR"

# 本輪已上傳到 JOBSHEETS 的檔名集合，避免同一次執行內兩份 job 撞名互蓋。
# main() 開頭會清空。
_USED_NAMES: set = set()


# ── 上傳 helpers ──────────────────────────────────────────────

def _upload_with_order_no(local_pdf: Path, order_no: str,
                          source_name: str = None) -> dict:
    result = rclone_helper.upload_unique(
        local_pdf, config.ONEDRIVE_OUTPUT, f"SR#{order_no}.pdf", _USED_NAMES,
        source_name=source_name, return_details=True,
    )
    log.info("  ↑ OneDrive 上傳完成（已用 Asana 訂單號命名）")
    return result


def _upload_with_task_title(local_pdf: Path, task: dict,
                            source_name: str = None) -> dict:
    result = rclone_helper.upload_unique(
        local_pdf, config.ONEDRIVE_OUTPUT,
        f"{asana_client.get_safe_title(task)}.pdf", _USED_NAMES,
        source_name=source_name, return_details=True,
    )
    log.info("  ↑ OneDrive 上傳完成（已用 Asana 任務標題命名）")
    return result


def _planned_filename(task: dict) -> tuple[str, str]:
    order_no = asana_client.extract_order_no_from_name(task)
    if order_no:
        return f"SR#{order_no}.pdf", order_no
    return f"{asana_client.get_safe_title(task)}.pdf", ""


def _dry_run_ocr_preview(ocr: dict) -> str:
    """Actions 只列出成功讀到哪些欄位，絕不輸出客戶資料原文。"""
    labels = (
        ("serial", "serial_candidates"),
        ("product", "product_raw"),
        ("hospital", "hospital_raw"),
        ("department_room", "department_room_raw"),
        ("phone", "phone_candidates"),
        ("contact", "contact_person_raw"),
        ("asset", "asset_candidates"),
        ("date", "service_date_raw"),
    )
    readable = []
    for label, key in labels:
        value = ocr.get(key)
        if isinstance(value, list):
            value = any(item for item in value)
        if value:
            readable.append(label)
    return (
        "已讀欄位=" + ",".join(readable)
        if readable else "未讀到可用欄位"
    )


def _public_planned_filename(row: dict, dry_run: bool) -> str:
    """dry-run 的真正檔名只留在記憶體，不寫入公開 Actions 紀錄。"""
    planned = row.get("planned")
    if not planned:
        return ""
    if dry_run:
        return "已產生（隱藏客戶資料）"
    return str(planned)


def _confirmed_pdf_name(value: str) -> str:
    """把人工逐頁核對的名稱轉成 OneDrive 可接受的單一 PDF 檔名。"""
    name = (value or "").strip()
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    safe = asana_client.get_safe_title({"name": name})
    if not safe or safe == "Unknown":
        raise ValueError("人工確認檔名不可為空")
    return f"{safe}.pdf"


def _finalize_confirmed(local_pdf: Path, confirmed_filename: str,
                        source_name: str) -> dict:
    """人工已看過原檔時的單檔救援；不經 OCR/Asana 猜名。"""
    planned = _confirmed_pdf_name(confirmed_filename)
    uploaded = rclone_helper.upload_unique(
        local_pdf, config.ONEDRIVE_OUTPUT, planned, _USED_NAMES,
        source_name=source_name, return_details=True,
    )
    log.info("  ↑ OneDrive 上傳完成（已用人工確認名稱）")
    disposition = uploaded["disposition"]
    return {
        "status": {
            "uploaded": "完成（人工確認）",
            "already_exists": "已存在（人工確認，沒有重複上傳）",
            "versioned": "完成（人工確認，保留重掃版本）",
        }[disposition],
        "state": disposition,
        "onedrive": uploaded["filename"],
        "planned": planned,
        "confirmed": True,
    }


def _finalize_match(local_pdf: Path, task: dict, tier: int,
                    source_name: str) -> dict:
    planned, order_no = _planned_filename(task)
    uploaded = (
        _upload_with_order_no(local_pdf, order_no, source_name)
        if order_no
        else _upload_with_task_title(local_pdf, task, source_name)
    )
    disposition = uploaded["disposition"]
    status = {
        "uploaded": "完成",
        "already_exists": "已存在（沒有重複上傳）",
        "versioned": "完成（保留重掃版本）",
    }[disposition]
    result = {
        "status": status,
        "state": disposition,
        "tier": tier,
        "onedrive": uploaded["filename"],
        "planned": planned,
        "asana_task_gid": task.get("gid"),
    }
    if order_no:
        result["order_no"] = order_no
    else:
        result["task_name"] = task.get("name")
    return result


# ── 多輪 OCR + 配對 ───────────────────────────────────────────

def _norm_evidence(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _edit_distance(left: str, right: str) -> int:
    """小型 Levenshtein；只用於比較不同 OCR 輪次的 serial。"""
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row, right_char in enumerate(right, 1):
        current = [row]
        for column, left_char in enumerate(left, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def _near_serial_consensus(readings: list) -> list:
    """保留跨輪只差一字的 serial 候選，仍交給 Asana 多欄規則裁決。

    手寫的 O/0、6/G 常令兩次抄錄只差一個字。這裡不自行挑其中一個，
    而是保留兩個原始候選；後續仍須電話、asset、日期等至少兩項支持。
    同一輪模型列出的相似候選不算兩次獨立證據。
    """
    entries = []
    for round_index, reading in enumerate(readings):
        seen_this_round = set()
        for value in reading.get("serial_candidates") or []:
            normalized = _norm_evidence(value)
            plausible = nvidia_client._valid_serial_token(value)
            if plausible and normalized not in seen_this_round:
                entries.append((round_index, value, normalized))
                seen_this_round.add(normalized)

    accepted = []
    for round_index, value, normalized in entries:
        if any(
            other_round != round_index
            and _edit_distance(normalized, other_normalized) <= 1
            for other_round, _, other_normalized in entries
        ):
            if normalized not in {_norm_evidence(item) for item in accepted}:
                accepted.append(value)
    return accepted[:3]


def _consensus_ocr(readings: list) -> dict:
    """只保留至少兩次獨立抄錄一致的欄位。"""
    if not readings:
        return {}
    result = {}
    consensus_rejections = {}
    list_fields = {
        "serial_candidates", "phone_candidates", "asset_candidates",
        "work_order_candidates", "unreadable_fields",
    }
    scalar_fields = (
        "order_no", "product_raw", "hospital_raw", "contact_person_raw", "department_room_raw",
        "service_date_raw", "date_source",
    )
    for field in scalar_fields:
        buckets = {}
        for reading in readings:
            value = reading.get(field)
            if field == "hospital_raw":
                key = _norm_evidence(asana_client.hospital_core(value))
            elif field == "product_raw":
                key = _norm_evidence(asana_client.normalize_product(value))
            elif field == "service_date_raw":
                parsed = nvidia_client._parse_action_date(value)
                key = parsed.isoformat() if parsed else ""
            else:
                key = _norm_evidence(value)
            if key:
                buckets.setdefault(key, []).append(value)
        winners = [values for values in buckets.values() if len(values) >= 2]
        result[field] = winners[0][0] if len(winners) == 1 else None
        if not result[field] and any(reading.get(field) for reading in readings):
            consensus_rejections[field] = "conflicting_readings" if len(buckets) > 1 else "insufficient_valid_readings"

    for field in list_fields:
        buckets = {}
        for reading in readings:
            seen_this_round = set()
            for value in reading.get(field) or []:
                key = _norm_evidence(value)
                if key and key not in seen_this_round:
                    buckets.setdefault(key, []).append(value)
                    seen_this_round.add(key)
        result[field] = [values[0] for values in buckets.values() if len(values) >= 2][:3]
        if field != "unreadable_fields" and buckets and not result[field]:
            consensus_rejections[field] = "no_repeated_candidate"

    # 完全一致仍是首選；但只要任何獨立輪次曾抄出另一個有效 serial，便保留
    # 「有爭議」標記。即使第三輪令其中一個讀數取得多數，也不能因此把曾見的
    # 一字差異藏起來，Asana 端仍須要求至少兩組額外證據。
    observed_serials = {
        _norm_evidence(value)
        for reading in readings
        for value in reading.get("serial_candidates") or []
        if nvidia_client._valid_serial_token(value)
    }
    result["serial_visual_candidates"] = list(dict.fromkeys(
        value
        for reading in readings
        for value in reading.get("serial_visual_candidates") or []
        if nvidia_client._visible_serial_token(value)
    ))[:6]
    result["serial_ambiguous"] = (
        len(observed_serials) > 1
        or len(result.get("serial_candidates") or []) > 1
    )
    if not result.get("serial_candidates"):
        result["serial_candidates"] = _near_serial_consensus(readings)
        result["serial_ambiguous"] = bool(result["serial_candidates"])
        if result["serial_candidates"]:
            consensus_rejections.pop("serial_candidates", None)

    # prompt 已限定 service_date_raw 只能抄 ACTION DATE。若兩輪對日期本身有
    # 共識、但模型漏填可選的 date_source，不應因此丟掉最能區分同一設備
    # 不同月份 PM 的證據。單輪日期仍不會通過上方共識。
    if result.get("service_date_raw") and not result.get("date_source"):
        result["date_source"] = "ACTION_DATE"
        consensus_rejections.pop("date_source", None)
    parsed = nvidia_client._parse_action_date(result.get("service_date_raw"))
    result["service_date_iso"] = parsed.isoformat() if parsed else None

    result["serial_no"] = next(iter(result.get("serial_candidates", [])), None)
    result["product"] = asana_client.normalize_product(result.get("product_raw"))
    result["customer_raw"] = result.get("hospital_raw")
    result["location_raw"] = nvidia_client._department_without_asset(
        result.get("department_room_raw")
    )
    result["customer"] = result.get("hospital_raw")
    # This private trace is never a source of matching evidence or a log entry.
    # Keep every pass, including values subsequently rejected or outvoted.
    result["_ocr_audit"] = {
        "readings": [deepcopy({
            "context": reading.get("_read_context", {}),
            **reading.get("_ocr_audit", {
                "raw": {key: reading[key] for key in nvidia_client._OCR_MODEL_FIELDS if key in reading},
                "normalized": {key: value for key, value in reading.items() if not key.startswith("_")},
                "rejections": {},
            }),
        }) for reading in readings],
        "consensus_rejections": consensus_rejections,
    }
    return result


def _apply_focused_action_date(consensus: dict, focused_readings: list) -> bool:
    """In the isolated backtest, only two agreeing date-panel reads may replace a broad-card date.

    Broad cards can independently make the same month error. Their votes remain
    in the private audit, but cannot outvote this pair. If either focused read
    fails or they disagree, discard the date as matching evidence altogether.
    """
    dates = [nvidia_client._parse_action_date(row.get("service_date_raw"))
             for row in focused_readings]
    agreed = len(dates) == 2 and dates[0] is not None and dates[0] == dates[1]
    audit = consensus.setdefault("_ocr_audit", {})
    audit["date_recheck"] = "agreed" if agreed else "unresolved"
    if agreed:
        consensus["service_date_raw"] = focused_readings[0]["service_date_raw"]
        consensus["service_date_iso"] = dates[0].isoformat()
        consensus["date_source"] = "ACTION_DATE"
        return True
    consensus["service_date_raw"] = None
    consensus["service_date_iso"] = None
    consensus["date_source"] = None
    return False


def _ocr_and_match(doc, job_type):
    """分格首讀、身分欄複核、必要時單格精讀；模型永不看 Asana 候選。"""
    nvidia_client.reset_ocr_metrics()
    primary = nvidia_client.ocr_jobsheet_fields(
        doc, 0, zoom=config.OCR_ZOOM_DEFAULT
    )
    identity = nvidia_client.ocr_jobsheet_identity_fields(
        doc, 0, zoom=config.OCR_IDENTITY_ZOOM
    )
    context_fields = os.environ.get(CONTEXT_FIELD_OCR_ENV) == "1"
    def contextual(reading, stage, zoom, field=None):
        return {**reading, "_read_context": {"stage": stage, "zoom": zoom, "field": field}}

    def visible_fields(reading):
        return [
            key for key in (
                "order_no", "serial_candidates", "product_raw", "hospital_raw",
                "contact_person_raw", "department_room_raw", "phone_candidates", "asset_candidates",
                "service_date_raw", "work_order_candidates",
            ) if reading.get(key)
        ]

    log.info(f"  OCR 分格首讀：已讀到 {visible_fields(primary)}")
    log.info(f"  OCR 身分欄複核：已讀到 {visible_fields(identity)}")
    if context_fields:
        # The scored single-field card is authoritative for these two fields.
        # Keep the earlier raw readings in their private audit snapshots, but
        # do not let two matching mistakes on broad cards become OCR votes.
        primary = {**primary, "product_raw": None, "hospital_raw": None}
        identity = {**identity, "product_raw": None, "hospital_raw": None}
    readings = [contextual(primary, "primary", config.OCR_ZOOM_DEFAULT),
                contextual(identity, "identity", config.OCR_IDENTITY_ZOOM)]
    if context_fields:
        vocabulary = private_ocr_context.build_vocabulary(asana_client._device_index)
        for field in ("product_raw", "hospital_raw"):
            for _ in range(2):
                reading = nvidia_client.ocr_jobsheet_context_field(doc, 0, field, vocabulary)
                readings.append(contextual(reading, "context_field", 5.0, field))
    consensus = _consensus_ocr(readings)
    if context_fields and not all(
            consensus.get(field) for field in ("product_raw", "hospital_raw")):
        # One model response cannot decide the hospital or product, and old
        # broad-card readings cannot rescue a disagreement. No Asana match is
        # attempted, so an uncertain card cannot produce a OneDrive upload.
        consensus["ocr_metrics"] = nvidia_client.get_ocr_metrics()
        log.warning("  產品或醫院單格兩輪未一致，保留待核對")
        return None, 0, consensus

    # Order No. 可以合法留白；只有模型曾看見卻未通過格式時才精讀。
    order_needs_focus = any(
        "order_no" in (reading.get("unreadable_fields") or [])
        or reading.get("order_no")
        for reading in readings
    ) and not consensus.get("order_no")
    focus_fields = [
        field for field in ("product_raw", "serial_candidates", "hospital_raw")
        if not consensus.get(field) and (not context_fields or field == "serial_candidates")
    ]
    if order_needs_focus:
        focus_fields.insert(0, "order_no")

    for field in focus_fields:
        log.info(f"  {field} 尚未形成可靠共識，只重讀該格")
        for zoom in config.OCR_FOCUSED_RETRY_ZOOMS:
            try:
                reading = nvidia_client.ocr_jobsheet_focused_field(
                    doc, 0, field, zoom=zoom
                )
            except nvidia_client.NvidiaResponseError:
                log.warning(f"  {field} 單格 {zoom}x 暫時無法完成")
                continue
            readings.append(contextual(reading, "focused_identity", zoom, field))
            consensus = _consensus_ocr(readings)
            if consensus.get(field):
                break

    task, tier = asana_client.find_task(consensus, job_type=job_type)
    if task is None and "serial_candidates" not in focus_fields:
        # 兩張卡可能穩定地看錯同一個字；Asana 完全配不到時，再用只含 serial
        # 的小格做兩次獨立精讀。新舊候選同時保留並標為有爭議，不能降低門檻。
        log.info("  現有身分欄配不到 Asana，再以 SERIAL NO. 單格獨立複核")
        serial_focus = []
        for zoom in config.OCR_FOCUSED_RETRY_ZOOMS:
            try:
                reading = nvidia_client.ocr_jobsheet_serial_candidates(
                    doc, 0, zoom=zoom, with_audit=True
                )
            except nvidia_client.NvidiaResponseError:
                log.warning(f"  serial 單格 {zoom}x 暫時無法完成")
                continue
            # Compatibility for older callers/test fixtures returning a list.
            if isinstance(reading, list):
                reading = nvidia_client._normalize_ocr_data({"serial_candidates": reading})
            serial_focus.append(contextual(reading, "serial_recheck", zoom, "serial_candidates"))
        readings.extend(serial_focus)
        consensus = _consensus_ocr(readings)
        task, tier = asana_client.find_task(consensus, job_type=job_type)
    if task is None:
        # 電話、asset、日期只在確實需要時高倍複核；與首輪一致後才進 Asana。
        log.info("  身分欄仍不足以唯一配對，讀取輔助欄位複核卡")
        try:
            support = nvidia_client.ocr_jobsheet_support_fields(
                doc, 0, zoom=config.OCR_SUPPORT_ZOOM
            )
            readings.append(contextual(support, "support", config.OCR_SUPPORT_ZOOM))
            consensus = _consensus_ocr(readings)
            log.info(f"  輔助欄共識：{visible_fields(consensus)}")
            task, tier = asana_client.find_task(consensus, job_type=job_type)
        except nvidia_client.NvidiaResponseError:
            log.warning("  輔助欄複核暫時無法完成，保留現有安全證據")

    if task is None:
        # 電話與 ACTION DATE 是同一設備不同月份工作的關鍵。完整卡兩輪
        # 不一致時只重讀相應小格；普通/加強兩種影像避免重複確定性誤讀。
        labels = {
            "contact_person_raw": "CONTACT PERSON",
            "phone_candidates": "TELEPHONE NO.",
            "service_date_raw": "ACTION DATE",
        }
        for field in ("contact_person_raw", "phone_candidates", "service_date_raw"):
            if consensus.get(field) or not any(
                reading.get(field)
                or field in (reading.get("unreadable_fields") or [])
                for reading in readings
            ):
                continue
            log.info(f"  {labels[field]} 兩輪不一致，只重讀該格")
            for zoom in config.OCR_FOCUSED_RETRY_ZOOMS:
                try:
                    reading = nvidia_client.ocr_jobsheet_focused_field(
                        doc, 0, field, zoom=zoom
                    )
                except nvidia_client.NvidiaResponseError:
                    log.warning(f"  {labels[field]} 單格 {zoom}x 暫時無法完成")
                    continue
                readings.append(contextual(reading, "focused_support", zoom, field))
                consensus = _consensus_ocr(readings)
                if consensus.get(field):
                    break
        task, tier = asana_client.find_task(consensus, job_type=job_type)

    date_recheck_blocked = False
    if task is None and context_fields and consensus.get("service_date_iso"):
        # Test branch only: when task selection remains unresolved, re-read the
        # ACTION DATE panel twice even if the broad cards agreed. Never show
        # the model candidate dates or use one focused reading to override.
        log.info("  歷史工作未能確定，ACTION DATE 單格兩輪獨立複核")
        date_focus = []
        for zoom in config.OCR_FOCUSED_RETRY_ZOOMS:
            try:
                reading = nvidia_client.ocr_jobsheet_focused_field(
                    doc, 0, "service_date_raw", zoom=zoom
                )
            except nvidia_client.NvidiaResponseError:
                log.warning("  ACTION DATE 單格 %.1fx 暫時無法完成", zoom)
                continue
            date_focus.append(contextual(reading, "date_recheck", zoom, "service_date_raw"))
        readings.extend(date_focus)
        consensus = _consensus_ocr(readings)
        if _apply_focused_action_date(consensus, date_focus):
            task, tier = asana_client.find_task(consensus, job_type=job_type)
        else:
            date_recheck_blocked = True
            log.info("  ACTION DATE 單格兩輪未一致，不憑原先日期自動配對")

    if (task is None and context_fields and asana_client._device_index is not None
            and all(consensus.get(field) for field in
                    ("serial_candidates", "product_raw", "hospital_raw"))):
        # Isolated backtest only. A repeated PM may have an ambiguous handwritten
        # month; independently transcribed probe/part IDs can distinguish its
        # live Asana task. Never show the model Asana descriptions or candidate
        # identifiers. One pass or one matching code is not sufficient.
        log.info("  歷史工作仍未確定，ACTION TAKEN 識別碼做兩輪獨立抄錄")
        action_reads = []
        for zoom in config.OCR_FOCUSED_RETRY_ZOOMS:
            try:
                action_reads.append(nvidia_client.ocr_jobsheet_action_identifiers(
                    doc, 0, zoom=zoom
                ))
            except nvidia_client.NvidiaResponseError:
                log.warning("  ACTION TAKEN 單格 %.1fx 暫時無法完成", zoom)
        action_ids = [value for value in action_reads[0]
                      if value in set(action_reads[1])] if len(action_reads) == 2 else []
        consensus.setdefault("_ocr_audit", {})["action_identifier_recheck"] = {
            "readings": deepcopy(action_reads), "agreed": list(action_ids),
        }
        if len(action_ids) >= 2:
            consensus["action_identifiers"] = action_ids
            task, tier = asana_client.find_task(consensus, job_type=job_type)
        else:
            log.info("  ACTION TAKEN 沒有兩個獨立一致的識別碼，不作工作證據")

    if task is None and not date_recheck_blocked:
        # The fixed program has already applied product, hospital and serial
        # gates.  Only a close top-two tie reaches the vision model, with at
        # most ten rows and no freedom to invent a table-external answer.
        close_candidates = asana_client.get_close_index_candidates(
            consensus, job_type=job_type
        )
        if close_candidates:
            log.info(
                "  固定搜尋剩下 %s 個接近設備，進行一次受限候選複核",
                len(close_candidates),
            )
            try:
                candidate_id = nvidia_client.choose_device_candidate(
                    doc, 0, close_candidates
                )
            except nvidia_client.NvidiaResponseError:
                candidate_id = None
                log.warning("  候選複核暫時無法完成；不猜答案")
            chosen = next(
                (item for item in close_candidates
                 if item.get("candidate_id") == candidate_id),
                None,
            )
            if chosen:
                task, tier = asana_client.find_task(
                    consensus, job_type=job_type,
                    selected_device_key=chosen.get("device_key"),
                )
            else:
                log.info("  候選複核未能明確選擇設備，保留待核對")

    metrics = nvidia_client.get_ocr_metrics()
    consensus["ocr_metrics"] = metrics
    cost = metrics.get("estimated_cost_cny_upper")
    cost_text = f"，費用上限約 RMB {cost:.4f}" if cost is not None else ""
    log.info(
        f"  圖片辨認共 {metrics['calls']} 次，{metrics['seconds']:.1f} 秒，"
        f"{metrics['total_tokens']} tokens{cost_text}"
    )
    if task is not None:
        return task, tier, consensus
    log.warning("  分格複核後仍沒有唯一可靠的 Asana 工作")
    return None, 0, consensus


def _parse_job_type(filename: str):
    m = re.search(r"_(CM|PM)\.pdf$", filename)
    return m.group(1) if m else None


def _move_to_pending_unique(local_pdf: Path, source_remote: str,
                            filename: str) -> str:
    """不覆蓋既有待核對檔；內容相同則只清掉重複的 _SPLIT 來源。"""
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    number = 0
    while True:
        candidate = filename if number == 0 else f"{stem} ({number}){suffix}"
        destination = f"{config.GDRIVE_PENDING}/{candidate}"
        stat = rclone_helper.remote_stat(destination)
        if stat is None:
            rclone_helper.moveto(source_remote, destination)
            return candidate
        if rclone_helper.remote_matches(local_pdf, destination, stat=stat):
            rclone_helper.delete(source_remote)
            return candidate
        number += 1


def _manifest_for_job(work_dir: Path, filename: str) -> dict:
    source_stem = batch_state.source_stem_from_job(filename) or Path(filename).stem
    return (
        batch_state.load(work_dir, source_stem)
        or batch_state.ensure_legacy_manifest(filename)
    )


def _save_result(work_dir: Path, manifest: dict, filename: str,
                 state: str, **details) -> None:
    batch_state.record_result(manifest, filename, state, **details)
    batch_state.save(work_dir, manifest)


def _process_split_file(filename: str, work_dir: Path,
                        source_folder: str = None,
                        dry_run: bool = False,
                        confirmed_filename: str = "") -> dict:
    source_folder = source_folder or config.GDRIVE_SPLIT
    remote = f"{source_folder}/{filename}"
    local = work_dir / filename
    rclone_helper.download(remote, local)

    if confirmed_filename:
        planned = _confirmed_pdf_name(confirmed_filename)
        if dry_run:
            local.unlink(missing_ok=True)
            return {
                "status": "預覽：使用人工確認名稱",
                "planned": planned,
                "confirmed": True,
            }
        result = _finalize_confirmed(local, confirmed_filename, filename)
        manifest = _manifest_for_job(work_dir, filename)
        _save_result(
            work_dir, manifest, filename, result["state"],
            onedrive=result.get("onedrive"), confirmed=True,
        )
        # 與自動配對相同：先記錄成功，最後才刪 Google Drive 來源。
        rclone_helper.delete(remote)
        local.unlink(missing_ok=True)
        return result

    job_type = _parse_job_type(filename)
    try:
        with fitz.open(local) as doc:
            task, tier, ocr = _ocr_and_match(doc, job_type)
    except nvidia_client.NvidiaResponseError as exc:
        if dry_run:
            local.unlink(missing_ok=True)
            return {"status": "預覽：圖片服務暫時失敗", "error": str(exc)}

        manifest = _manifest_for_job(work_dir, filename)
        job = batch_state.find_job(manifest, filename)
        attempts = int((job or {}).get("attempts") or 0) + 1
        if attempts >= config.OCR_MAX_BATCH_ATTEMPTS:
            pending_name = (
                filename if source_folder == config.GDRIVE_PENDING
                else _move_to_pending_unique(local, remote, filename)
            )
            _save_result(
                work_dir, manifest, filename, "pending", attempts=attempts,
                reason="圖片服務連續無法完成辨認", pending=pending_name,
            )
            local.unlink(missing_ok=True)
            return {
                "status": "等待人工核對（辨認重試已用完）",
                "state": "pending", "attempts": attempts, "pending": pending_name,
            }
        _save_result(
            work_dir, manifest, filename, "retryable", attempts=attempts,
            reason="圖片服務暫時無法完成辨認",
        )
        local.unlink(missing_ok=True)
        return {
            "status": "等待自動重試", "state": "retryable", "attempts": attempts,
        }

    if task is not None:
        log.info(f"  Asana 第 {tier} 層命中")
        planned, order_no = _planned_filename(task)
        if dry_run:
            local.unlink(missing_ok=True)
            return {
                "status": "預覽：可以可靠配對",
                "planned": planned,
                "order_no": order_no or None,
                "asana_task_gid": task.get("gid"),
                "tier": tier,
                "ocr_preview": _dry_run_ocr_preview(ocr),
                "ocr_metrics": ocr.get("ocr_metrics", {}),
            }
        result = _finalize_match(local, task, tier, filename)
        result["ocr_metrics"] = ocr.get("ocr_metrics", {})
    else:
        # 名稱未確認時絕不把猜測結果送到正式 OneDrive。保留完整 PDF 在私人
        # Google Drive，之後可人工核對或用改良後的 matcher 重試。
        log.warning("  ⚠ 多輪核對仍不確定 → 留在 Google Drive _PENDING")
        if dry_run:
            local.unlink(missing_ok=True)
            return {
                "status": "預覽：證據不足，會留待人工核對",
                "ocr_preview": _dry_run_ocr_preview(ocr),
                "ocr_metrics": ocr.get("ocr_metrics", {}),
            }
        if source_folder == config.GDRIVE_PENDING:
            pending_name = filename
        else:
            pending_name = _move_to_pending_unique(local, remote, filename)
        result = {
            "status": "等待人工核對", "state": "pending", "pending": pending_name,
        }
        manifest = _manifest_for_job(work_dir, filename)
        _save_result(
            work_dir, manifest, filename, "pending", reason="Asana 配對證據不足",
            pending=pending_name, ocr_metrics=ocr.get("ocr_metrics", {}),
        )
        local.unlink(missing_ok=True)
        return result

    manifest = _manifest_for_job(work_dir, filename)
    _save_result(
        work_dir, manifest, filename, result["state"],
        onedrive=result.get("onedrive"), order_no=result.get("order_no"),
        asana_task_gid=result.get("asana_task_gid"),
        ocr_metrics=result.get("ocr_metrics", {}),
    )
    # OneDrive 已接收檔案後，先把結果寫進耐久批次狀態，最後才刪來源。
    # 若狀態寫入失敗，_SPLIT 仍在，下輪會以內容比對認出既有檔而不重複上傳；
    # 反過來先刪來源，runner 此刻中斷便會留下永遠未完成的批次報告。
    rclone_helper.delete(remote)
    local.unlink(missing_ok=True)
    return result


# ── 主程式 ────────────────────────────────────────────────────

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="processor_") as tmpdir:
        work_dir = Path(tmpdir)
        log.info(f"工作目錄：{work_dir}")
        _USED_NAMES.clear()   # 防撞名集合，每次執行重置
        asana_client._task_cache.clear()
        asana_client.clear_device_index()
        index_path = os.environ.get(ASANA_INDEX_FILE_ENV, "").strip()
        manifest_path = os.environ.get(ASANA_INDEX_MANIFEST_ENV, "").strip()
        if index_path:
            try:
                index = asana_index.load_index(
                    Path(index_path), Path(manifest_path) if manifest_path else None
                )
                asana_client.set_device_index(index)
                log.info(
                    "已載入 Asana 設備索引：%s 部設備（不在公開輸出列出客戶資料）",
                    index.get("device_count", 0),
                )
            except asana_index.AsanaIndexError as exc:
                # 索引遺失、損毀或與 manifest 不一致時必須停在來源端，
                # 不能把「沒有候選」誤當成查無資料而移動/命名 PDF。
                log.error("Asana 設備索引無法驗證，保留來源檔：%s", exc)
                return 1

        source_queue = os.environ.get(SOURCE_QUEUE_ENV, "split").strip().lower() or "split"
        if source_queue not in {"split", "pending"}:
            log.error("JOBSHEET_SOURCE_QUEUE 只接受 split 或 pending")
            return 1
        source_folder = (
            config.GDRIVE_PENDING if source_queue == "pending" else config.GDRIVE_SPLIT
        )
        splits = rclone_helper.list_pdfs(source_folder, exclude_subdirs=True)
        log.info(f"{source_queue.upper()} 待處理 job 數：{len(splits)}")
        target = os.environ.get(TARGET_FILE_ENV, "").strip()
        dry_run = os.environ.get(DRY_RUN_ENV, "").strip().lower() in {"1", "true", "yes"}
        confirmed_filename = os.environ.get(CONFIRMED_FILENAME_ENV, "").strip()
        if dry_run and not target:
            log.error("dry-run 必須精確指定一份 jobsheet_file，沒有處理任何檔案")
            return 1
        if confirmed_filename and not target:
            log.error("人工確認檔名必須精確指定一份 jobsheet_file")
            return 1
        if target:
            # 手動測試時必須精確指定 _SPLIT 根目錄內的一個 PDF；不接受
            # 路徑或模糊名稱，避免誤處理同一批其他工作單。
            if Path(target).name != target or Path(target).suffix.lower() != ".pdf":
                log.error("指定的測試檔名不安全，只接受 _SPLIT 內的單一 PDF 檔名")
                return 1
            if target not in splits:
                log.error("指定的測試工作單目前不在 _SPLIT，沒有處理任何檔案")
                return 1
            splits = [target]
            log.info(f"單檔安全模式：本次只處理 {target}")
        main_report = []
        had_processing_error = False
        for filename in splits:
            log.info(f"=== 處理 {filename} ===")
            try:
                main_report.append({
                    "file": filename,
                    **_process_split_file(
                        filename, work_dir, source_folder=source_folder, dry_run=dry_run,
                        confirmed_filename=confirmed_filename,
                    ),
                })
            except Exception as e:
                log.exception(f"  處理 {filename} 失敗（保留 _SPLIT 等重試）：{e}")
                main_report.append({"file": filename, "status": "處理錯誤", "error": str(e)})
                had_processing_error = True

        if not dry_run:
            finalized = batch_state.finalize_ready_manifests(work_dir)
            if finalized:
                log.info(f"已完成 {finalized} 份批次狀態報告")

    log.info("=" * 60)
    log.info("處理階段完成報告" + ("（只讀預覽）" if dry_run else ""))
    log.info("=" * 60)
    for row in main_report:
        details = [row.get("status")]
        public_planned = _public_planned_filename(row, dry_run)
        if public_planned:
            details.append(f"預計檔名={public_planned}")
        if row.get("ocr_preview"):
            details.append(f"OCR={row['ocr_preview']}")
        if row.get("ocr_metrics"):
            metrics = row["ocr_metrics"]
            metric_text = (
                f"圖片呼叫={metrics.get('calls', 0)}，"
                f"耗時={metrics.get('seconds', 0):.1f}s，"
                f"tokens={metrics.get('total_tokens', 0)}"
            )
            if metrics.get("estimated_cost_cny_upper") is not None:
                metric_text += (
                    f"，估算費用上限=RMB "
                    f"{metrics['estimated_cost_cny_upper']:.4f}"
                )
            details.append(metric_text)
        log.info(f"  {row.get('file')}：{'；'.join(details)}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        lines = [
            "## Jobsheet 處理報告", "",
            "| 檔案 | 結果 | 預計檔名 | OCR 核對欄位 |",
            "|---|---|---|---|",
        ]
        for row in main_report:
            public_planned = _public_planned_filename(row, dry_run)
            cells = (
                row.get("file") or "",
                row.get("status") or "",
                public_planned or "-",
                row.get("ocr_preview") or "-",
            )
            cells = tuple(str(value).replace("|", "\\|").replace("\n", " ")
                          for value in cells)
            lines.append(f"| {' | '.join(cells)} |")
        with open(summary_path, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    # 只有登入、Asana、rclone 或程式等整體故障才令 workflow 失敗；單一圖片
    # 辨認暫時失敗已有耐久重試次數，不再製造重複的 pipeline 故障電郵。
    return 1 if had_processing_error else 0


if __name__ == "__main__":
    sys.exit(main())

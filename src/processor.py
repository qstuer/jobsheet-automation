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
from pathlib import Path

import fitz

from . import asana_client, batch_state, config, nvidia_client, rclone_helper

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("processor")

TARGET_FILE_ENV = "JOBSHEET_TARGET_FILE"
DRY_RUN_ENV = config.JOBSHEET_DRY_RUN_ENV
SOURCE_QUEUE_ENV = config.JOBSHEET_SOURCE_QUEUE_ENV

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


def _consensus_ocr(readings: list) -> dict:
    """只保留至少兩次獨立抄錄一致的欄位。"""
    if not readings:
        return {}
    result = {}
    list_fields = {
        "serial_candidates", "phone_candidates", "asset_candidates",
        "work_order_candidates", "unreadable_fields",
    }
    scalar_fields = (
        "order_no", "product_raw", "customer_raw", "location_raw",
        "service_date_raw", "date_source",
    )
    for field in scalar_fields:
        buckets = {}
        for reading in readings:
            value = reading.get(field)
            key = _norm_evidence(value)
            if key:
                buckets.setdefault(key, []).append(value)
        winners = [values for values in buckets.values() if len(values) >= 2]
        result[field] = winners[0][0] if len(winners) == 1 else None

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

    result["serial_no"] = next(iter(result.get("serial_candidates", [])), None)
    result["product"] = result.get("product_raw")
    result["customer"] = result.get("customer_raw")
    return result


def _ocr_and_match(doc, job_type):
    """先取得欄位共識，再由規則配對一次；模型本身不選 Asana 工作。"""
    readings = []
    last_consensus = {}
    for i, zoom in enumerate(config.OCR_RETRY_ZOOMS, 1):
        ocr = nvidia_client.ocr_jobsheet_fields(doc, 0, zoom=zoom)
        # Actions log 不可印電話、asset、serial 或客戶內容；只記錄哪些欄位看得到。
        visible_fields = [
            key for key in (
                "order_no", "serial_candidates", "product_raw", "customer_raw",
                "location_raw", "phone_candidates", "asset_candidates", "service_date_raw",
                "work_order_candidates",
            )
            if ocr.get(key)
        ]
        log.info(f"  OCR 第{i}輪({zoom}x)：已讀到 {visible_fields}")
        readings.append(ocr)
        if len(readings) < config.OCR_MATCH_CONFIRMATIONS:
            log.info("  尚需另一輪抄錄確認欄位，繼續…")
            continue
        consensus = _consensus_ocr(readings)
        last_consensus = consensus
        visible_consensus = [
            key for key in (
                "order_no", "serial_candidates", "product_raw", "customer_raw",
                "location_raw", "phone_candidates", "asset_candidates",
                "service_date_raw", "work_order_candidates",
            ) if consensus.get(key)
        ]
        log.info(f"  {len(readings)} 輪一致欄位：{visible_consensus}")
        task, tier = asana_client.find_task(consensus, job_type=job_type)
        if task is not None:
            return task, tier, consensus
        if i < len(config.OCR_RETRY_ZOOMS):
            log.info("  一致證據仍不足，針對有爭議欄位再讀一輪…")
    log.warning("  多輪抄錄後仍沒有唯一可靠的 Asana 工作")
    return None, 0, last_consensus


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
                        dry_run: bool = False) -> dict:
    source_folder = source_folder or config.GDRIVE_SPLIT
    remote = f"{source_folder}/{filename}"
    local = work_dir / filename
    rclone_helper.download(remote, local)

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
            }
        result = _finalize_match(local, task, tier, filename)
    else:
        # 名稱未確認時絕不把猜測結果送到正式 OneDrive。保留完整 PDF 在私人
        # Google Drive，之後可人工核對或用改良後的 matcher 重試。
        log.warning("  ⚠ 多輪核對仍不確定 → 留在 Google Drive _PENDING")
        if dry_run:
            local.unlink(missing_ok=True)
            return {"status": "預覽：證據不足，會留待人工核對"}
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
            pending=pending_name,
        )
        local.unlink(missing_ok=True)
        return result

    manifest = _manifest_for_job(work_dir, filename)
    _save_result(
        work_dir, manifest, filename, result["state"],
        onedrive=result.get("onedrive"), order_no=result.get("order_no"),
        asana_task_gid=result.get("asana_task_gid"),
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
        if dry_run and not target:
            log.error("dry-run 必須精確指定一份 jobsheet_file，沒有處理任何檔案")
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
                        filename, work_dir, source_folder=source_folder, dry_run=dry_run
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
        log.info(f"  {row.get('file')}：{row.get('status')}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        lines = ["## Jobsheet 處理報告", "", "| 檔案 | 結果 |", "|---|---|"]
        for row in main_report:
            lines.append(f"| {row.get('file')} | {row.get('status')} |")
        with open(summary_path, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    # 只有登入、Asana、rclone 或程式等整體故障才令 workflow 失敗；單一圖片
    # 辨認暫時失敗已有耐久重試次數，不再製造重複的 pipeline 故障電郵。
    return 1 if had_processing_error else 0


if __name__ == "__main__":
    sys.exit(main())

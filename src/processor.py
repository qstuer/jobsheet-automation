#!/usr/bin/env python3
"""
階段 B — 處理員（Processor）

對 _SPLIT/ 裡每一份「單一 job PDF」：
    → 多輪 OCR（不同清晰度交叉核對，同一個 Asana 工作命中兩次才接受）
    → Asana 多欄配對（serial + 日期/電話/asset/醫院/型號）
    → 命名後上傳 onedrive:.../JOBSHEETS/ → 刪 _SPLIT 那份

命名：
  找到任務 + 任務名含 Order No → SR#OrderNo.pdf
  找到任務 + 任務名無 Order No → Asana 任務標題.pdf
  多輪全失敗            → 留在 Google Drive _PENDING，不碰 OneDrive

最後處理舊格式的 _PENDING 殘檔：能配才正名上傳，配不到繼續保留。
"""
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

import fitz

from . import asana_client, config, nvidia_client, rclone_helper

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("processor")

PENDING_PREFIX = "待人工審查__"   # 舊格式殘檔，retry 用
TARGET_FILE_ENV = "JOBSHEET_TARGET_FILE"

# 本輪已上傳到 JOBSHEETS 的檔名集合，避免同一次執行內兩份 job 撞名互蓋。
# main() 開頭會清空。
_USED_NAMES: set = set()


def _safe(val: str) -> str:
    """檔名欄位淨化：去掉 Windows/OneDrive 不允許字元、空白與 / 換底線"""
    s = re.sub(r'[\\/:*?"<>|]', "", (val or "")).replace(" ", "_")
    return s.replace("__", "_").strip("_") or "Unknown"


# ── 上傳 helpers ──────────────────────────────────────────────

def _upload_with_order_no(local_pdf: Path, order_no: str) -> str:
    fn = rclone_helper.upload_unique(
        local_pdf, config.ONEDRIVE_OUTPUT, f"SR#{order_no}.pdf", _USED_NAMES)
    log.info("  ↑ OneDrive 上傳完成（已用 Asana 訂單號命名）")
    return fn


def _upload_with_task_title(local_pdf: Path, task: dict) -> str:
    fn = rclone_helper.upload_unique(
        local_pdf, config.ONEDRIVE_OUTPUT,
        f"{asana_client.get_safe_title(task)}.pdf", _USED_NAMES)
    log.info("  ↑ OneDrive 上傳完成（已用 Asana 任務標題命名）")
    return fn


def _finalize_match(local_pdf: Path, task: dict, tier: int) -> dict:
    order_no = asana_client.extract_order_no_from_name(task)
    if order_no:
        fn = _upload_with_order_no(local_pdf, order_no)
        return {"status": "完成", "tier": tier, "order_no": order_no, "onedrive": fn}
    fn = _upload_with_task_title(local_pdf, task)
    return {"status": "任務標題命名", "tier": tier,
            "task_name": task.get("name"), "onedrive": fn}


# ── 多輪 OCR + 配對 ───────────────────────────────────────────

def _ocr_and_match(doc, job_type):
    """以不同清晰度重讀；同一個 Asana 工作命中足夠次數才接受。

    單次 OCR 即使剛好命中真實 Asana 工作，也可能只是模型猜中常見字串。
    因此不能像舊版一樣第一次命中便停止；沒有交叉確認便留在 _PENDING。
    回傳 (task, tier, last_ocr)。
    """
    last_ocr = {}
    matches = {}
    for i, zoom in enumerate(config.OCR_RETRY_ZOOMS, 1):
        ocr = nvidia_client.ocr_jobsheet_fields(doc, 0, zoom=zoom)
        # Actions log 不可印電話、asset、serial 或客戶內容；只記錄哪些欄位看得到。
        visible_fields = [
            key for key in (
                "order_no", "serial_candidates", "product_raw", "customer_raw",
                "location_raw", "phone_candidates", "asset_candidates", "service_date_raw",
            )
            if ocr.get(key)
        ]
        log.info(f"  OCR 第{i}輪({zoom}x)：已讀到 {visible_fields}")
        last_ocr = ocr
        task, tier = asana_client.find_task(ocr, job_type=job_type)
        if task is not None:
            gid = task.get("gid")
            if not gid:
                log.warning(f"  第{i}輪候選工作缺少 gid，不接受")
                continue
            record = matches.setdefault(gid, {"count": 0, "task": task, "tier": tier})
            record["count"] += 1
            # 保留最強命中層級（1 比 2 強）。
            record["tier"] = min(record["tier"], tier)
            if record["count"] >= config.OCR_MATCH_CONFIRMATIONS:
                log.info(
                    f"  同一個 Asana 工作已由 {record['count']} 輪 OCR 交叉確認"
                )
                return record["task"], record["tier"], ocr
            log.info(
                f"  第{i}輪命中候選，但仍需另一個清晰度確認，繼續…"
            )
            continue
        log.info(f"  第{i}輪未命中，繼續下一輪…")
    if matches:
        log.warning("  候選只命中一次或不同輪命中不同工作，不敢自動歸檔")
    return None, 0, last_ocr


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


def _process_split_file(filename: str, work_dir: Path) -> dict:
    remote = f"{config.GDRIVE_SPLIT}/{filename}"
    local = work_dir / filename
    rclone_helper.download(remote, local)

    job_type = _parse_job_type(filename)
    with fitz.open(local) as doc:
        task, tier, ocr = _ocr_and_match(doc, job_type)

    if task is not None:
        log.info(f"  Asana 第 {tier} 層命中")
        result = _finalize_match(local, task, tier)
    else:
        # 名稱未確認時絕不把猜測結果送到正式 OneDrive。保留完整 PDF 在私人
        # Google Drive，之後可人工核對或用改良後的 matcher 重試。
        log.warning("  ⚠ 多輪核對仍不確定 → 留在 Google Drive _PENDING")
        pending_name = _move_to_pending_unique(local, remote, filename)
        result = {"status": "等待人工核對", "pending": pending_name}
        local.unlink(missing_ok=True)
        return result

    rclone_helper.delete(remote)
    local.unlink(missing_ok=True)
    return result


# ── 舊 PENDING 殘檔重試（配不到就繼續保留）────────────────────

def _retry_pending(work_dir: Path) -> list:
    log.info("--- 重試舊 _PENDING 殘檔 ---")
    files = rclone_helper.list_pending(config.GDRIVE_PENDING, prefix=PENDING_PREFIX)
    log.info(f"待重試 PENDING 檔數：{len(files)}")
    report = []
    for filename in files:
        body = filename[len(PENDING_PREFIX):].rsplit(".", 1)[0]
        parts = body.split("__")
        if len(parts) < 4:
            report.append({"file": filename, "status": "檔名格式不符，跳過"})
            continue
        customer, product, serial, job_type = parts[0], parts[1], parts[2], parts[-1]
        ocr_synth = {
            "order_no": None,
            "serial_no": None if serial in ("Unknown", "None") else serial,
            "product": None if product == "Unknown" else product.replace("_", " "),
            "customer": None if customer == "Unknown" else customer.replace("_", " "),
        }
        task, tier = asana_client.find_task(ocr_synth, job_type=job_type)

        if task is not None:
            remote = f"{config.GDRIVE_PENDING}/{filename}"
            local = work_dir / filename
            rclone_helper.download(remote, local)
            order_no = asana_client.extract_order_no_from_name(task)
            onedrive = (_upload_with_order_no(local, order_no) if order_no
                        else _upload_with_task_title(local, task))
            report.append({"file": filename, "status": "完成", "tier": tier, "onedrive": onedrive})
            log.info("  ✓ 舊 PENDING 檔已可靠配對並上傳")
            rclone_helper.delete(remote)
            local.unlink(missing_ok=True)
        else:
            report.append({"file": filename, "status": "仍待人工核對"})
            log.warning("  ⚠ 一份舊 PENDING 檔仍無法可靠配對，繼續保留")
    return report


# ── 主程式 ────────────────────────────────────────────────────

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="processor_") as tmpdir:
        work_dir = Path(tmpdir)
        log.info(f"工作目錄：{work_dir}")
        _USED_NAMES.clear()   # 防撞名集合，每次執行重置

        splits = rclone_helper.list_pdfs(config.GDRIVE_SPLIT, exclude_subdirs=True)
        log.info(f"_SPLIT 待處理 job 數：{len(splits)}")
        target = os.environ.get(TARGET_FILE_ENV, "").strip()
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
                main_report.append({"file": filename, **_process_split_file(filename, work_dir)})
            except Exception as e:
                log.exception(f"  處理 {filename} 失敗（保留 _SPLIT 等重試）：{e}")
                main_report.append({"file": filename, "status": "處理錯誤", "error": str(e)})
                had_processing_error = True

        # 單檔安全模式不能順帶重試其他歷史 PENDING；普通自動批次才保留
        # 舊檔重試行為。
        pending_report = [] if target else _retry_pending(work_dir)

    log.info("=" * 60)
    log.info("處理階段完成報告")
    log.info("=" * 60)
    for row in main_report:
        log.info(f"  {row.get('file')}：{row.get('status')}")
    log.info(f"重試 PENDING：{len(pending_report)} 筆")
    for row in pending_report:
        log.info(f"  舊 PENDING：{row.get('status')}")
    # 有 job 因 API / 網路 / rclone 等原因留待重試時，Stage B 必須呈現失敗，
    # 讓 workflow_run 連敗告警看得到；已成功上傳或 _PENDING 分流不受影響。
    return 1 if had_processing_error else 0


if __name__ == "__main__":
    sys.exit(main())

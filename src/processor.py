#!/usr/bin/env python3
"""
階段 B — 處理員（Processor）

對 _SPLIT/ 裡每一份「單一 job PDF」獨立處理：
    googledrive:From_BrotherDevice/_SPLIT/*.pdf
      → OCR 讀 ORDER/SERIAL/PRODUCT/CUSTOMER
      → Asana 全域 typeahead 4 層驗證 + serial/product 三重核對
      → 命名後上傳 onedrive:.../JOBSHEETS/
      → 成功才刪 _SPLIT 那份

命名（與舊版一致）：
  找到任務 + 任務名含 Order No → SR#OrderNo.pdf
  找到任務 + 任務名無 Order No → Asana 任務標題.pdf
  4 層全失敗            → _PENDING/待人工審查__*.pdf

最後跑 PENDING 重試。job type 由 _SPLIT 檔名解析（不再重新視覺判斷）。
"""
import logging
import re
import sys
import tempfile
from pathlib import Path

import fitz

from . import asana_client, config, nvidia_client, rclone_helper

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("processor")

# _PENDING 檔名用 "__" 當分隔，避免欄位內的單底線造成解析錯亂
PENDING_PREFIX = "待人工審查__"


def _safe(val: str) -> str:
    """欄位淨化：空白與 / 換成單底線，移除會撞分隔符的雙底線"""
    s = (val or "Unknown").replace(" ", "_").replace("/", "_")
    return s.replace("__", "_").strip("_") or "Unknown"


# ── 上傳 helpers ──────────────────────────────────────────────

def _upload_with_order_no(local_pdf: Path, order_no: str) -> str:
    filename = f"SR#{order_no}.pdf"
    rclone_helper.upload(local_pdf, f"{config.ONEDRIVE_OUTPUT}/{filename}")
    log.info(f"  ↑ OneDrive: {filename}")
    return filename


def _upload_with_task_title(local_pdf: Path, task: dict) -> str:
    filename = f"{asana_client.get_safe_title(task)}.pdf"
    rclone_helper.upload(local_pdf, f"{config.ONEDRIVE_OUTPUT}/{filename}")
    log.info(f"  ↑ OneDrive (任務標題): {filename}")
    return filename


def _save_manual_review(local_pdf: Path, ocr: dict, job_type: str) -> str:
    name = (f"{PENDING_PREFIX}{_safe(ocr.get('customer'))}__"
            f"{_safe(ocr.get('product'))}__{_safe(ocr.get('serial_no'))}__{job_type}.pdf")
    rclone_helper.upload(local_pdf, f"{config.GDRIVE_PENDING}/{name}")
    log.warning(f"  ⚠ 人工審核: {name}")
    return name


# ── 命中後的命名分流 ──────────────────────────────────────────

def _finalize(local_pdf: Path, task, tier: int, ocr: dict, job_type: str) -> dict:
    if task is not None:
        asana_order_no = asana_client.extract_order_no_from_name(task)
        if asana_order_no:
            fn = _upload_with_order_no(local_pdf, asana_order_no)
            return {"status": "完成", "tier": tier, "order_no": asana_order_no, "onedrive": fn}
        fn = _upload_with_task_title(local_pdf, task)
        return {"status": "任務標題命名", "tier": tier,
                "task_name": task.get("name"), "onedrive": fn}
    fn = _save_manual_review(local_pdf, ocr, job_type)
    return {"status": "待人工審核", "pending": fn}


# ── 單一 split job 處理 ───────────────────────────────────────

def _parse_job_type(filename: str) -> str:
    m = re.search(r"_(CM|PM)\.pdf$", filename)
    return m.group(1) if m else None


def _process_split_file(filename: str, work_dir: Path) -> dict:
    remote = f"{config.GDRIVE_SPLIT}/{filename}"
    local = work_dir / filename
    rclone_helper.download(remote, local)

    job_type = _parse_job_type(filename)

    doc = fitz.open(local)
    ocr = nvidia_client.ocr_jobsheet_fields(doc, 0, zoom=config.OCR_ZOOM_DEFAULT)
    doc.close()
    log.info(f"  OCR(1x): {ocr}")

    task, tier = asana_client.find_task(ocr, job_type=job_type)

    # 4 層全失敗 → 升級 1.5x 重 OCR 再試
    if task is None:
        log.info(f"  ⚙ 4 層失敗，升級 {config.OCR_ZOOM_FALLBACK}x zoom 重 OCR")
        doc = fitz.open(local)
        ocr = nvidia_client.ocr_jobsheet_fields(doc, 0, zoom=config.OCR_ZOOM_FALLBACK)
        doc.close()
        log.info(f"  OCR(1.5x): {ocr}")
        task, tier = asana_client.find_task(ocr, job_type=job_type)

    log.info(f"  Asana 第 {tier} 層 {'命中' if task else '失敗'}")
    result = _finalize(local, task, tier, ocr, job_type or "Unknown")

    # 上傳成功（含轉 PENDING）後，刪掉 _SPLIT 那份
    rclone_helper.delete(remote)
    local.unlink(missing_ok=True)
    return result


# ── PENDING 重試 ──────────────────────────────────────────────

def _retry_pending(work_dir: Path) -> list:
    log.info("--- 重試 _PENDING 暫存檔 ---")
    files = rclone_helper.list_pending(config.GDRIVE_PENDING, prefix=PENDING_PREFIX)
    log.info(f"待重試 PENDING 檔數：{len(files)}")
    report = []
    for filename in files:
        # 格式：待人工審查__{customer}__{product}__{serial}__{type}.pdf
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

        remote = f"{config.GDRIVE_PENDING}/{filename}"
        local = work_dir / filename
        rclone_helper.download(remote, local)
        if task is not None:
            order_no = asana_client.extract_order_no_from_name(task)
            onedrive = (_upload_with_order_no(local, order_no) if order_no
                        else _upload_with_task_title(local, task))
            rclone_helper.delete(remote)
            report.append({"file": filename, "status": "完成", "tier": tier, "onedrive": onedrive})
            log.info(f"  ✓ {filename} → {onedrive}")
        else:
            report.append({"file": filename, "status": "繼續等待"})
        local.unlink(missing_ok=True)
    return report


# ── 主程式 ────────────────────────────────────────────────────

def main() -> int:
    work_dir = Path(tempfile.mkdtemp(prefix="processor_"))
    log.info(f"工作目錄：{work_dir}")

    # Phase 1：處理 _SPLIT 單一 job PDF
    splits = rclone_helper.list_pdfs(config.GDRIVE_SPLIT, exclude_subdirs=True)
    log.info(f"_SPLIT 待處理 job 數：{len(splits)}")
    main_report = []
    for filename in splits:
        log.info(f"=== 處理 {filename} ===")
        try:
            main_report.append({"file": filename, **_process_split_file(filename, work_dir)})
        except Exception as e:
            log.exception(f"  處理 {filename} 失敗（保留 _SPLIT 等重試）：{e}")
            main_report.append({"file": filename, "status": "處理錯誤", "error": str(e)})

    # Phase 2：重試 PENDING
    pending_report = _retry_pending(work_dir)

    log.info("=" * 60)
    log.info("處理階段完成報告")
    log.info("=" * 60)
    for row in main_report:
        log.info(f"  {row}")
    log.info(f"重試 PENDING：{len(pending_report)} 筆")
    for row in pending_report:
        log.info(f"  {row}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

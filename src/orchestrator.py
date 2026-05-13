#!/usr/bin/env python3
"""
Jobsheet 自動化歸檔主程式
Google Drive → OCR (K2.6) → Asana 驗證 → OneDrive 上傳

命名邏輯（2026-05-13 更新）：
  ✅ 找到任務 + 任務名含 Order No → SR#OrderNo.pdf
  ✅ 找到任務 + 任務名無 Order No → 直接用 Asana 任務標題命名（無 SR# 前綴）
  ⚠️ 4 層全失敗 → 待人工審查（存 _PENDING/待人工審查_*.pdf）
"""
import logging
import sys
import tempfile
from pathlib import Path

import fitz

from . import asana_client, config, nvidia_client, pdf_utils, rclone_helper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("orchestrator")


# ─────────────────────────────────────────────
# 上傳 helpers
# ─────────────────────────────────────────────

def upload_with_order_no(local_pdf: Path, order_no: str) -> str:
    """有 Order No → SR#XXXXXXXX.pdf"""
    filename = f"SR#{order_no}.pdf"
    rclone_helper.upload(local_pdf, f"{config.ONEDRIVE_OUTPUT}/{filename}")
    log.info(f"  ↑ OneDrive: {filename}")
    return filename


def upload_with_task_title(local_pdf: Path, task: dict) -> str:
    """找到任務但無 Order No → 直接用 Asana 任務標題命名"""
    safe_title = asana_client.get_safe_title(task)
    filename = f"{safe_title}.pdf"
    rclone_helper.upload(local_pdf, f"{config.ONEDRIVE_OUTPUT}/{filename}")
    log.info(f"  ↑ OneDrive (任務標題): {filename}")
    return filename


def save_manual_review(local_pdf: Path, ocr_data: dict, job_type: str) -> str:
    """4 層全失敗 → 待人工審查"""
    def safe(val):
        return (val or "Unknown").replace(" ", "_").replace("/", "_")
    name = f"待人工審查_{safe(ocr_data.get('customer'))}_{safe(ocr_data.get('product'))}_{safe(ocr_data.get('serial_no'))}_{job_type}.pdf"
    rclone_helper.upload(local_pdf, f"{config.GDRIVE_PENDING}/{name}")
    log.warning(f"  ⚠ 人工審核: {name}")
    return name


# ─────────────────────────────────────────────
# 單一 Job 處理
# ─────────────────────────────────────────────

def process_one_job(source_pdf: Path, job: dict, work_dir: Path) -> dict:
    """切割 → OCR → Asana 4 層搜尋 → 上傳 / 人工審核"""

    # 切割頁面
    label = f"p{job['start']}_{job['type']}"
    job_pdf = work_dir / f"{source_pdf.stem}_{label}.pdf"
    pdf_utils.extract_pages(source_pdf, job["keep_pages"], job_pdf)

    # OCR 1x zoom
    doc = fitz.open(job_pdf)
    ocr = nvidia_client.ocr_jobsheet_fields(doc, 0, zoom=config.OCR_ZOOM_DEFAULT)
    doc.close()
    log.info(f"  OCR(1x): {ocr}")

    # 4 層 Asana 搜尋
    task, tier = asana_client.find_task(ocr)

    # 4 層全失敗 → 升級 1.5x zoom 重 OCR 再試
    if task is None:
        log.info(f"  ⚙ 4 層失敗，升級 {config.OCR_ZOOM_FALLBACK}x zoom 重 OCR")
        doc = fitz.open(job_pdf)
        ocr = nvidia_client.ocr_jobsheet_fields(doc, 0, zoom=config.OCR_ZOOM_FALLBACK)
        doc.close()
        log.info(f"  OCR(1.5x): {ocr}")
        task, tier = asana_client.find_task(ocr)

    log.info(f"  Asana 第 {tier} 層 {'命中' if task else '失敗'}")

    # ── 結果分流 ──────────────────────────────
    if task is not None:
        order_no_in_name = asana_client.extract_order_no_from_name(task)
        if order_no_in_name:
            # ✅ 找到任務 + 有 Order No → SR#OrderNo.pdf
            filename = upload_with_order_no(job_pdf, order_no_in_name)
            result = {"status": "完成", "tier": tier,
                      "order_no": order_no_in_name, "onedrive": filename}
        else:
            # ✅ 找到任務 + 無 Order No → 直接用任務標題
            filename = upload_with_task_title(job_pdf, task)
            result = {"status": "任務標題命名", "tier": tier,
                      "task_name": task.get("name"), "onedrive": filename}
    else:
        # ⚠️ 4 層全失敗 → 人工審核
        filename = save_manual_review(job_pdf, ocr, job["type"])
        result = {"status": "待人工審核", "pending": filename}

    # 刪本機暫存
    job_pdf.unlink(missing_ok=True)
    return result


# ─────────────────────────────────────────────
# PENDING 重試
# ─────────────────────────────────────────────

def retry_pending_files(work_dir: Path) -> list:
    """掃 _PENDING/PENDING_*.pdf，用 Serial 重搜 Asana"""
    log.info("--- 重試 _PENDING 暫存檔 ---")
    pending_files = rclone_helper.list_pending(config.GDRIVE_PENDING, prefix="PENDING_")
    log.info(f"待重試 PENDING 檔數：{len(pending_files)}")
    report = []
    for filename in pending_files:
        # 檔名格式：PENDING_[Customer]_[Product]_[Serial]_[CMPM].pdf
        parts = filename.replace("PENDING_", "").rsplit(".", 1)[0].split("_")
        if len(parts) < 4:
            continue
        customer  = parts[0].replace("_", " ")
        product   = parts[1].replace("_", " ")
        serial    = parts[2] if parts[2] != "NoSerial" else None
        job_type  = parts[-1]

        ocr_synthetic = {
            "order_no": None,
            "serial_no": serial,
            "product": product,
            "customer": customer,
        }

        task, tier = asana_client.find_task(ocr_synthetic)
        local = work_dir / filename
        rclone_helper.download(f"{config.GDRIVE_PENDING}/{filename}", local)

        if task is not None:
            order_no_in_name = asana_client.extract_order_no_from_name(task)
            if order_no_in_name:
                onedrive_name = upload_with_order_no(local, order_no_in_name)
            else:
                onedrive_name = upload_with_task_title(local, task)
            rclone_helper.delete(f"{config.GDRIVE_PENDING}/{filename}")
            report.append({"file": filename, "status": "完成", "onedrive": onedrive_name})
            log.info(f"  ✓ {filename} → {onedrive_name}")
        else:
            report.append({"file": filename, "status": "繼續等待"})

        local.unlink(missing_ok=True)
    return report


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────

def main() -> int:
    work_dir = Path(tempfile.mkdtemp(prefix="jobsheet_"))
    log.info(f"工作目錄：{work_dir}")

    # Phase 1：處理新進 PDF
    pdfs = rclone_helper.list_pdfs(config.GDRIVE_INPUT, exclude_subdirs=True)
    log.info(f"新進 PDF 數：{len(pdfs)}")

    main_report = []
    for filename in pdfs:
        log.info(f"=== 處理 {filename} ===")
        local_pdf = work_dir / filename
        rclone_helper.download(f"{config.GDRIVE_INPUT}/{filename}", local_pdf)
        rclone_helper.delete(f"{config.GDRIVE_INPUT}/{filename}")   # ⚠️ 立刻刪 Drive 原檔

        try:
            jobs = pdf_utils.split_jobs(local_pdf)
            log.info(f"  切出 {len(jobs)} 個 Job")
            for job in jobs:
                result = process_one_job(local_pdf, job, work_dir)
                main_report.append({"file": filename, "job": job["type"], **result})
        except Exception as e:
            log.exception(f"  處理 {filename} 失敗: {e}")
            main_report.append({"file": filename, "status": "處理錯誤", "error": str(e)})
        finally:
            local_pdf.unlink(missing_ok=True)

    # Phase 2：重試 PENDING
    pending_report = retry_pending_files(work_dir)

    # 完成報告
    log.info("=" * 60)
    log.info("完成報告")
    log.info("=" * 60)
    for row in main_report:
        log.info(f"  {row}")
    log.info(f"重試 PENDING：{len(pending_report)} 筆")
    for row in pending_report:
        log.info(f"  {row}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

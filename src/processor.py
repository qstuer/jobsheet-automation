#!/usr/bin/env python3
"""
階段 B — 處理員（Processor）

對 _SPLIT/ 裡每一份「單一 job PDF」：
    → 多輪 OCR（zoom ladder，逐輪重讀重配，配到就停）
    → Asana 兩層配對（醫院+型號撈池 → 本機 serial 容錯）
    → 命名後上傳 onedrive:.../JOBSHEETS/ → 刪 _SPLIT 那份

命名：
  找到任務 + 任務名含 Order No → SR#OrderNo.pdf
  找到任務 + 任務名無 Order No → Asana 任務標題.pdf
  多輪全失敗            → 仍上傳 OneDrive，用 OCR 猜測命名 + [待核對] 前綴標記
                          （不再卡 PENDING；人工在 OneDrive 直接看到並手動改）

最後處理舊的 _PENDING 殘檔：能配就正名上傳，配不到也標記上傳，一律不再 lingering。
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

PENDING_PREFIX = "待人工審查__"   # 舊格式殘檔，retry 用

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
    log.info(f"  ↑ OneDrive: {fn}")
    return fn


def _upload_with_task_title(local_pdf: Path, task: dict) -> str:
    fn = rclone_helper.upload_unique(
        local_pdf, config.ONEDRIVE_OUTPUT,
        f"{asana_client.get_safe_title(task)}.pdf", _USED_NAMES)
    log.info(f"  ↑ OneDrive (任務標題): {fn}")
    return fn


def _flagged_name(ocr: dict) -> str:
    """配不到時，用 OCR 猜測拼出檔名，加 [待核對] 前綴"""
    parts = [ocr.get("customer"), ocr.get("product"),
             ocr.get("serial_no"), ocr.get("order_no")]
    body = "_".join(_safe(p) for p in parts if p) or "Unknown"
    return f"{config.CHECK_PREFIX}{body}.pdf"


def _upload_flagged(local_pdf: Path, ocr: dict) -> str:
    """配對失敗 → 仍上傳 OneDrive 並標記待核對"""
    fn = rclone_helper.upload_unique(
        local_pdf, config.ONEDRIVE_OUTPUT, _flagged_name(ocr), _USED_NAMES)
    log.warning(f"  ⚠ 配對失敗，仍上傳 OneDrive 並標記待核對：{fn}")
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
    """依 zoom ladder 逐輪重讀重配，配到就停。回傳 (task, tier, last_ocr)"""
    last_ocr = {}
    for i, zoom in enumerate(config.OCR_RETRY_ZOOMS, 1):
        ocr = nvidia_client.ocr_jobsheet_fields(doc, 0, zoom=zoom)
        log.info(f"  OCR 第{i}輪({zoom}x): {ocr}")
        last_ocr = ocr
        task, tier = asana_client.find_task(ocr, job_type=job_type)
        if task is not None:
            return task, tier, ocr
        log.info(f"  第{i}輪未命中，繼續下一輪…")
    return None, 0, last_ocr


def _parse_job_type(filename: str):
    m = re.search(r"_(CM|PM)\.pdf$", filename)
    return m.group(1) if m else None


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
        log.info("  多輪 OCR 全部配不到 → 標記上傳")
        name = _upload_flagged(local, ocr)
        result = {"status": "配對失敗(已標記上傳)", "onedrive": name}

    rclone_helper.delete(remote)
    local.unlink(missing_ok=True)
    return result


# ── 舊 PENDING 殘檔重試（一律清空，不再 lingering）──────────────

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

        remote = f"{config.GDRIVE_PENDING}/{filename}"
        local = work_dir / filename
        rclone_helper.download(remote, local)
        if task is not None:
            order_no = asana_client.extract_order_no_from_name(task)
            onedrive = (_upload_with_order_no(local, order_no) if order_no
                        else _upload_with_task_title(local, task))
            report.append({"file": filename, "status": "完成", "tier": tier, "onedrive": onedrive})
            log.info(f"  ✓ {filename} → {onedrive}")
        else:
            onedrive = _upload_flagged(local, ocr_synth)
            report.append({"file": filename, "status": "配對失敗(已標記上傳)", "onedrive": onedrive})
        rclone_helper.delete(remote)   # 一律清掉，不再 lingering
        local.unlink(missing_ok=True)
    return report


# ── 主程式 ────────────────────────────────────────────────────

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="processor_") as tmpdir:
        work_dir = Path(tmpdir)
        log.info(f"工作目錄：{work_dir}")
        _USED_NAMES.clear()   # 防撞名集合，每次執行重置

        splits = rclone_helper.list_pdfs(config.GDRIVE_SPLIT, exclude_subdirs=True)
        log.info(f"_SPLIT 待處理 job 數：{len(splits)}")
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

        pending_report = _retry_pending(work_dir)

    log.info("=" * 60)
    log.info("處理階段完成報告")
    log.info("=" * 60)
    for row in main_report:
        log.info(f"  {row}")
    log.info(f"重試 PENDING：{len(pending_report)} 筆")
    for row in pending_report:
        log.info(f"  {row}")
    # 有 job 因 API / 網路 / rclone 等原因留待重試時，Stage B 必須呈現失敗，
    # 讓 workflow_run 連敗告警看得到；已成功上傳或 [待核對] 分流不受影響。
    return 1 if had_processing_error else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
階段 A — 拆刀手（Splitter）

只做一件事：把掃描器上傳的多 job PDF 切成單一 job PDF，落地到 Google Drive。
    googledrive:From_BrotherDevice/*.pdf
      → 視覺判斷每個 job 的 CM/PM（JOB NATURE 圈選）
      → 按頁數規則切割 + 頁數驗算
      → 單一 job PDF 上傳 googledrive:From_BrotherDevice/_SPLIT/
      → 全部成功才刪原檔

⚠️ 不做 OCR 訂單、不碰 Asana、不上傳 OneDrive —— 那些交給階段 B（processor）。
⚠️ 切割失敗（CM/PM 誤判、頁數對不上）→ 整份原檔搬到 _SPLIT_FAILED/，不刪、不產生垃圾。

切好的檔名格式（讓 processor 解析 job type）：
    {原檔名}__job{序號}_{CM|PM}.pdf
    例：20260530130717_001__job2_PM.pdf
"""
import logging
import sys
import tempfile
from pathlib import Path

from . import config, pdf_utils, rclone_helper
from .pdf_utils import SplitError

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("splitter")


def _split_one(filename: str, work_dir: Path) -> dict:
    """下載一份原始 PDF → 切割 → 上傳各 job → 成功才刪原檔"""
    src_remote = f"{config.GDRIVE_INPUT}/{filename}"
    local_pdf = work_dir / filename
    rclone_helper.download(src_remote, local_pdf)

    # ── 切割（含 CM/PM 視覺判斷 + 頁數驗算）──
    try:
        jobs = pdf_utils.split_jobs(local_pdf)
    except SplitError as e:
        # 只有單據內容/頁數驗算問題會進這裡。API、網路、rclone、檔案 I/O
        # 等基礎設施例外必須繼續向外拋，保留來源 PDF 等下一輪重試。
        rclone_helper.moveto(src_remote, f"{config.GDRIVE_SPLIT_FAILED}/{filename}")
        log.warning(f"  ⚠ 切割失敗，已搬到 _SPLIT_FAILED：{e}")
        local_pdf.unlink(missing_ok=True)
        return {"file": filename, "status": "切割失敗(已轉人工)", "reason": str(e)}

    log.info(f"  切出 {len(jobs)} 個 job：{[j['type'] for j in jobs]}")

    # ── 逐 job 抽頁 → 上傳 _SPLIT ──
    stem = Path(filename).stem
    uploaded = []
    for idx, job in enumerate(jobs, 1):
        out_name = f"{stem}__job{idx}_{job['type']}.pdf"
        out_path = work_dir / out_name
        pdf_utils.extract_pages(local_pdf, job["keep_pages"], out_path)
        rclone_helper.upload(out_path, f"{config.GDRIVE_SPLIT}/{out_name}")
        out_path.unlink(missing_ok=True)
        uploaded.append(out_name)
        log.info(f"    → _SPLIT/{out_name}")

    # ── 全部 job 上傳成功，才刪原檔 ──
    rclone_helper.delete(src_remote)
    local_pdf.unlink(missing_ok=True)
    return {"file": filename, "status": "切割完成",
            "jobs": len(jobs), "uploaded": uploaded}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="splitter_") as tmpdir:
        work_dir = Path(tmpdir)
        log.info(f"工作目錄：{work_dir}")

        pdfs = rclone_helper.list_pdfs(config.GDRIVE_INPUT, exclude_subdirs=True)
        log.info(f"待切割 PDF 數：{len(pdfs)}")

        report = []
        had_unexpected_error = False
        for filename in pdfs:
            log.info(f"=== 切割 {filename} ===")
            try:
                report.append(_split_one(filename, work_dir))
            except Exception as e:
                # 非 SplitError 的意外（下載/上傳/rclone）→ 保留原檔，下次重試
                log.exception(f"  處理 {filename} 發生意外，保留原檔等下次重試：{e}")
                report.append({"file": filename, "status": "意外錯誤(保留重試)", "error": str(e)})
                had_unexpected_error = True

    log.info("=" * 60)
    log.info("切割階段完成報告")
    for row in report:
        log.info(f"  {row}")
    # SplitError 已完成「轉人工」分流，不算 workflow 故障；其他例外必須讓
    # Actions 顯示失敗，否則會出現空跑成功，連續失敗告警也無法生效。
    return 1 if had_unexpected_error else 0


if __name__ == "__main__":
    sys.exit(main())

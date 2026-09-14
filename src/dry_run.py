"""由 Google Drive 原始 PDF 進行全程只讀預覽。"""
import logging
import os
import sys
import tempfile
from pathlib import Path

import fitz

from . import asana_client, config, pdf_utils, processor, rclone_helper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dry-run")

TARGET_ENV = "JOBSHEET_RAW_FILE"


def main() -> int:
    target = os.environ.get(TARGET_ENV, "").strip()
    if not target or Path(target).name != target or Path(target).suffix.lower() != ".pdf":
        log.error("只讀測試必須精確指定入口資料夾內一份 PDF")
        return 1

    available = rclone_helper.list_pdfs(config.GDRIVE_INPUT, exclude_subdirs=True)
    if target not in available:
        log.error("指定 PDF 不在 Google Drive 入口；沒有移動或上傳任何檔案")
        return 1

    with tempfile.TemporaryDirectory(prefix="jobsheet_dry_run_") as tmpdir:
        work_dir = Path(tmpdir)
        source = work_dir / target
        rclone_helper.download(f"{config.GDRIVE_INPUT}/{target}", source)
        jobs = pdf_utils.split_jobs(source)
        report = []
        for index, job in enumerate(jobs, 1):
            if not job["complete"]:
                report.append({
                    "job": index,
                    "type": job["type"],
                    "result": f"掃描不完整：{job['incomplete_reason']}",
                })
                continue
            output = work_dir / f"{Path(target).stem}__job{index}_{job['type']}.pdf"
            pdf_utils.extract_pages(source, job["keep_pages"], output)
            with fitz.open(output) as doc:
                task, tier, _ = processor._ocr_and_match(doc, job["type"])
            if task is None:
                result = "完整，但 Asana 證據不足"
            else:
                planned, _ = processor._planned_filename(task)
                result = f"預計名稱：{planned}（配對層級 {tier}）"
            report.append({"job": index, "type": job["type"], "result": result})

    log.info("=" * 60)
    log.info(f"只讀測試：{target}，共 {len(report)} 份；沒有修改任何雲端檔案")
    for row in report:
        log.info(f"  job {row['job']} {row['type']}：{row['result']}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as stream:
            stream.write("## Jobsheet 全程只讀測試\n\n")
            stream.write(f"來源：`{target}`\n\n")
            stream.write("| 工作單 | 類型 | 結果 |\n|---|---|---|\n")
            for row in report:
                stream.write(f"| {row['job']} | {row['type']} | {row['result']} |\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""PDF 切割與 Job 偵測
對應 CLAUDE.md Section 3 的頁面結構規則
"""
from pathlib import Path
from typing import List

import fitz

from . import config, nvidia_client


def split_jobs(pdf_path: Path) -> List[dict]:
    """
    讀取 PDF，依序判斷每個 Job 是 CM (2頁) 或 PM (6頁)。
    回傳 jobs 列表，每個 job 包含：
      { "start": int, "type": "CM"/"PM", "keep_pages": [int, ...] }

    CM（2頁）：頁N保留，N+1刪 → 輸出 1 頁
    PM（6頁）：頁N保留，N+1刪，N+2~N+4保留，N+5刪 → 輸出 4 頁
    """
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    jobs = []
    cursor = 0
    while cursor < total_pages:
        job_type = nvidia_client.detect_cm_pm(doc, cursor)
        if job_type == "CM":
            jobs.append({
                "start": cursor,
                "type": "CM",
                "keep_pages": [cursor],
            })
            cursor += config.CM_PAGES_PER_JOB
        else:  # PM
            jobs.append({
                "start": cursor,
                "type": "PM",
                "keep_pages": [cursor + offset for offset in config.PM_KEEP_OFFSETS],
            })
            cursor += config.PM_PAGES_PER_JOB
    doc.close()

    # ── 驗算：消耗頁數必須等於 PDF 總頁數 ──────────────────────────
    # 若不吻合，代表 CM/PM 誤判導致分頁錯位，整份 PDF 不可信
    consumed = sum(
        config.CM_PAGES_PER_JOB if j["type"] == "CM" else config.PM_PAGES_PER_JOB
        for j in jobs
    )
    if consumed != total_pages:
        raise ValueError(
            f"分頁驗算失敗：偵測消耗 {consumed} 頁，但 PDF 共 {total_pages} 頁。"
            f"（偵測結果：{[j['type'] for j in jobs]}）"
            f"可能原因：CM/PM 誤判，請人工審查。"
        )

    return jobs


def extract_pages(source_pdf: Path, page_indices: List[int], output_pdf: Path) -> None:
    """從原 PDF 抽出指定頁，存成新 PDF"""
    src = fitz.open(source_pdf)
    out = fitz.open()
    for idx in page_indices:
        out.insert_pdf(src, from_page=idx, to_page=idx)
    out.save(output_pdf)
    out.close()
    src.close()

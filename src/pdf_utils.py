"""PDF 切割與 Job 偵測
對應 CLAUDE.md Section 3 的頁面結構規則
"""
from pathlib import Path
from typing import List

import fitz

from . import config, nvidia_client


class SplitError(ValueError):
    """單據內容令分頁不可信 → 整份應轉人工審查。

    只用於 CM/PM 判讀與頁數驗算；API、網路、rclone、檔案 I/O 等
    基礎設施例外不得包成 SplitError，以免原始 PDF 被誤搬到 _SPLIT_FAILED。
    """


# 每種 job type 消耗的頁數；只有 CM/PM 會走自動流程
_PAGES_PER_TYPE = {
    "CM": config.CM_PAGES_PER_JOB,
    "PM": config.PM_PAGES_PER_JOB,
}


def split_jobs(pdf_path: Path) -> List[dict]:
    """
    讀取 PDF，依序判斷每個 Job 是 CM (2頁) 或 PM (6頁)。
    回傳 jobs 列表，每個 job 包含：
      { "start": int, "type": "CM"/"PM", "keep_pages": [int, ...] }

    CM（2頁）：頁N保留，N+1刪 → 輸出 1 頁
    PM（6頁）：頁N保留，N+1刪，N+2~N+4保留，N+5刪 → 輸出 4 頁

    防呆（任何一條不過 → raise SplitError，整份轉人工，不產生垃圾）：
      1. detect 回 UNKNOWN / FCO / INS → 視為起始頁判讀不可信
      2. 該 job 所需頁數超出剩餘頁數 → 分頁錯位
      3. 全部走完後 cursor 必須剛好等於總頁數
    """
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    try:
        jobs: List[dict] = []
        cursor = 0
        while cursor < total_pages:
            job_type = nvidia_client.detect_cm_pm(doc, cursor)

            if job_type not in _PAGES_PER_TYPE:
                raise SplitError(
                    f"第 {cursor + 1} 頁 JOB NATURE 判讀為 '{job_type}'，"
                    f"非 CM/PM，分頁不可信，請人工審查。"
                    f"（目前偵測：{[j['type'] for j in jobs]}）"
                )

            need = _PAGES_PER_TYPE[job_type]
            if cursor + need > total_pages:
                raise SplitError(
                    f"第 {cursor + 1} 頁判為 {job_type} 需 {need} 頁，"
                    f"但只剩 {total_pages - cursor} 頁 → 分頁錯位，請人工審查。"
                )

            if job_type == "CM":
                keep = [cursor]
            else:  # PM
                keep = [cursor + off for off in config.PM_KEEP_OFFSETS]

            jobs.append({"start": cursor, "type": job_type, "keep_pages": keep})
            cursor += need
    finally:
        doc.close()

    # ── 驗算：消耗頁數必須剛好等於 PDF 總頁數 ──
    if cursor != total_pages:
        raise SplitError(
            f"分頁驗算失敗：偵測消耗 {cursor} 頁，但 PDF 共 {total_pages} 頁。"
            f"（偵測結果：{[j['type'] for j in jobs]}）可能 CM/PM 誤判，請人工審查。"
        )

    return jobs


def extract_pages(source_pdf: Path, page_indices: List[int], output_pdf: Path) -> None:
    """從原 PDF 抽出指定頁，存成新 PDF"""
    src = fitz.open(source_pdf)
    out = fitz.open()
    try:
        for idx in page_indices:
            out.insert_pdf(src, from_page=idx, to_page=idx)
        out.save(output_pdf)
    finally:
        out.close()
        src.close()

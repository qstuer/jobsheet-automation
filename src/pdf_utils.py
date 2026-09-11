"""PDF 切割與 Job 偵測。

掃描器是雙面掃描，所以每份工作單通常佔偶數張掃描頁；PM 標準為 6 張，
但 checklist 缺頁時亦可能只有 2 或 4 張。切割以「下一張有 CM/PM 圈選的
工作單」作真正邊界，再以頁面墨量去掉空白頁與背頁。
"""
from pathlib import Path
from typing import List

import fitz
from PIL import Image

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


def page_has_meaningful_content(pdf_doc: fitz.Document, page_idx: int) -> bool:
    """用低解像度墨量分開表格/checklist 與空白背頁。

    這一步完全在本機計算，不用視覺模型。工作單及 checklist 的深色像素比例
    明顯高於背頁；門檻集中放在 config，方便日後用新掃描樣本校準。
    """
    pix = pdf_doc[page_idx].get_pixmap(matrix=fitz.Matrix(0.5, 0.5), colorspace=fitz.csGRAY)
    samples = memoryview(pix.samples)
    if not samples:
        return False
    dark = sum(value < config.CONTENT_DARK_PIXEL_THRESHOLD for value in samples)
    return (dark / len(samples)) >= config.CONTENT_MIN_DARK_RATIO


def _page_layout_signature(page: fitz.Page) -> tuple[bool, ...]:
    """把頁面縮成粗略黑白版面指紋；忽略手寫細節，保留表格位置。"""
    pix = page.get_pixmap(matrix=fitz.Matrix(0.75, 0.75), colorspace=fitz.csGRAY)
    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    image = image.resize((config.JOBSHEET_LAYOUT_WIDTH, config.JOBSHEET_LAYOUT_HEIGHT))
    return tuple(value < config.JOBSHEET_LAYOUT_DARK_THRESHOLD for value in image.getdata())


def page_looks_like_jobsheet(pdf_doc: fitz.Document, page_idx: int,
                             reference: tuple[bool, ...]) -> bool:
    """確認候選頁使用同一款 Jobsheet 首頁版面，不讓 checklist 冒充邊界。"""
    candidate = _page_layout_signature(pdf_doc[page_idx])
    overlap = sum(left and right for left, right in zip(reference, candidate))
    dark_count = sum(reference) + sum(candidate)
    similarity = (2 * overlap / dark_count) if dark_count else 0.0
    return similarity >= config.JOBSHEET_LAYOUT_MIN_DICE


def _find_job_end(pdf_doc: fitz.Document, cursor: int, job_type: str,
                  detect) -> int:
    """找本 job 的右邊界（不含）。

    先檢查標準長度；若標準位置不是下一張工作單，就按雙面掃描的 2 頁步幅
    往前找。最後一份不足標準長度時，只要剩餘頁數仍為偶數便以 EOF 收尾。
    """
    total_pages = len(pdf_doc)
    nominal = _PAGES_PER_TYPE[job_type]
    nominal_end = cursor + nominal

    if nominal_end == total_pages:
        return nominal_end
    if nominal_end < total_pages and detect(nominal_end) in _PAGES_PER_TYPE:
        return nominal_end

    # PM checklist 可能少一組或兩組；越接近標準長度的邊界優先。
    for offset in range(nominal - 2, 1, -2):
        candidate = cursor + offset
        if candidate < total_pages and detect(candidate) in _PAGES_PER_TYPE:
            return candidate

    remaining = total_pages - cursor
    if 0 < remaining < nominal and remaining % 2 == 0:
        return total_pages

    raise SplitError(
        f"第 {cursor + 1} 頁判為 {job_type}，但在其後 {min(nominal, remaining)} 頁內"
        "找不到可信的下一張工作單邊界，請人工審查。"
    )


def split_jobs(pdf_path: Path) -> List[dict]:
    """
    讀取 PDF，依序判斷每個 Job 是 CM 或 PM。
    回傳 jobs 列表，每個 job 包含：
      { "start": int, "type": "CM"/"PM", "keep_pages": [int, ...] }

    CM：保留工作單正面。
    PM：保留工作單正面及邊界內所有有實際內容的 checklist；空白背頁刪除。

    防呆（任何一條不過 → raise SplitError，整份轉人工，不產生垃圾）：
      1. detect 回 UNKNOWN / FCO / INS → 視為起始頁判讀不可信
      2. 標準邊界或較短的雙面掃描邊界都找不到 → 分頁不可信
      3. 全部走完後 cursor 必須剛好等於總頁數
    """
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    try:
        jobs: List[dict] = []
        detection_cache = {}
        layout_cache = {}
        reference_layout = _page_layout_signature(doc[0])

        def detect(page_idx: int) -> str:
            if page_idx not in detection_cache:
                if page_idx != 0:
                    if page_idx not in layout_cache:
                        layout_cache[page_idx] = page_looks_like_jobsheet(
                            doc, page_idx, reference_layout
                        )
                    if not layout_cache[page_idx]:
                        detection_cache[page_idx] = "NOT_JOBSHEET"
                        return detection_cache[page_idx]
                detection_cache[page_idx] = nvidia_client.detect_cm_pm(doc, page_idx)
            return detection_cache[page_idx]

        cursor = 0
        while cursor < total_pages:
            job_type = detect(cursor)

            if job_type not in _PAGES_PER_TYPE:
                raise SplitError(
                    f"第 {cursor + 1} 頁 JOB NATURE 判讀為 '{job_type}'，"
                    f"非 CM/PM，分頁不可信，請人工審查。"
                    f"（目前偵測：{[j['type'] for j in jobs]}）"
                )

            end = _find_job_end(doc, cursor, job_type, detect)
            if job_type == "CM":
                keep = [cursor]
            else:  # PM
                keep = [cursor]
                keep.extend(
                    page_idx for page_idx in range(cursor + 1, end)
                    if page_has_meaningful_content(doc, page_idx)
                )

            jobs.append({
                "start": cursor,
                "end": end,
                "type": job_type,
                "input_pages": end - cursor,
                "keep_pages": keep,
            })
            cursor = end
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

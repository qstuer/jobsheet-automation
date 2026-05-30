"""K2.6 (NVIDIA NIM) vision API 呼叫
對應 CLAUDE.md Section 5：OCR Tips 與已知陷阱
"""
import base64
import io
import json
import re
from typing import Optional

import fitz
from PIL import Image, ImageEnhance
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from . import config

_client: Optional[OpenAI] = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=config.NVIDIA_BASE_URL,
            api_key=config.NVIDIA_API_KEY,
        )
    return _client


def crop_jobsheet_top(pdf_doc: fitz.Document, page_idx: int, zoom: float = 1.0) -> str:
    """
    裁切第 N 頁的 10%-30% 區域，加強對比，回傳 base64 JPEG。

    頁面高度分佈（CLAUDE.md Section 5）：
      0%-10%   Header（公司 Logo）→ 裁掉
      10%-30%  關鍵資料區（ORDER NO / PRODUCT / SERIAL / CUSTOMER）✅ 保留
      30%-100% 其他維修記錄 → 裁掉
    """
    page = pdf_doc[page_idx]
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    w, h = img.size
    crop = img.crop((0, int(h * config.OCR_CROP_TOP), w, int(h * config.OCR_CROP_BOTTOM)))
    crop = ImageEnhance.Contrast(crop).enhance(config.OCR_CONTRAST)
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=5, max=120))
def _call_k26(prompt: str, image_b64: str, max_tokens: int = 300) -> str:
    """呼叫 K2.6，關閉 thinking 省 tokens"""
    response = get_client().chat.completions.create(
        model=config.K26_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }],
        max_tokens=max_tokens,
        extra_body={"chat_template_kwargs": {"thinking": False}},
    )
    return response.choices[0].message.content.strip()


def detect_cm_pm(pdf_doc: fitz.Document, page_idx: int) -> str:
    """
    讀第一頁的 JOB NATURE 欄位，判斷 CM 或 PM。

    ⚠️ 使用全頁圖（0%-100%）而非裁切版，確保 JOB NATURE 欄完整可見。
    JOB NATURE 欄位在頁面頂部約 5%-15% 位置，裁切版可能切掉部分。
    """
    page = pdf_doc[page_idx]
    mat = fitz.Matrix(1.0, 1.0)
    pix = page.get_pixmap(matrix=mat)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    # 只取上半部（0%-40%），確保 JOB NATURE 行完整顯示
    w, h = img.size
    top_half = img.crop((0, 0, w, int(h * 0.40)))
    buf = io.BytesIO()
    top_half.save(buf, "JPEG", quality=85)
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    answer = _call_k26(
        prompt=(
            "This is the top portion of a Philips medical equipment Job Sheet. "
            "Find the 'JOB NATURE' section which has four checkboxes: CM, PM, FCO, INS. "
            "Look for which box is ticked or circled. "
            "Reply with ONLY the word 'CM' or 'PM'."
        ),
        image_b64=img_b64,
        max_tokens=10,
    ).upper()
    return "PM" if "PM" in answer else "CM"


def ocr_jobsheet_fields(pdf_doc: fitz.Document, page_idx: int, zoom: float = 1.0) -> dict:
    """
    提取 ORDER NO / SERIAL NO / PRODUCT / CUSTOMER

    常見誤讀（CLAUDE.md Section 5）：
      9→G, O→0, 0→D, l→1, S→5, C450→CX50

    回傳格式：
    {
      "order_no": "61248643" 或 None,
      "serial_no": "US622B1115" 或 None,
      "product":   "EPIQ Elite" 或 None,
      "customer":  "PYNEH" 或 None
    }
    """
    img_b64 = crop_jobsheet_top(pdf_doc, page_idx, zoom=zoom)
    prompt = (
        "Extract these fields from the Philips medical equipment jobsheet image. "
        "Return JSON only, no markdown fences:\n"
        "{\n"
        '  "order_no": "8-digit number starting with 5 or 6, or null if blank",\n'
        '  "serial_no": "serial number like US622B1115 or USO16D0865 — '
        "watch for misreads: 9→G, O→0, 0→D, l→1, S→5\",\n"
        '  "product": "product model like EPIQ Elite, EPIQ 5G, Affiniti 50, CX50",\n'
        '  "customer": "hospital or customer name like PYNEH, Trinity CWB, HKCH"\n'
        "}"
    )
    raw = _call_k26(prompt=prompt, image_b64=img_b64, max_tokens=300)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"order_no": None, "serial_no": None, "product": None, "customer": None}

    # 標準化空字串為 None
    for key in list(data.keys()):
        val = data.get(key)
        if isinstance(val, str) and val.strip().lower() in ("", "null", "none", "n/a"):
            data[key] = None
    return data

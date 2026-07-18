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
    裁切第 N 頁的 10%-30% 區域（全寬），加強對比，回傳 base64 JPEG。
    供 ocr_jobsheet_fields 讀 ORDER / PRODUCT / SERIAL / CUSTOMER 用。
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
    """呼叫 NVIDIA 視覺模型做單張圖 OCR"""
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
    )
    return response.choices[0].message.content.strip()


def crop_job_nature(pdf_doc: fitz.Document, page_idx: int,
                    top: float, bottom: float) -> str:
    """
    裁切 JOB NATURE 那一小格（橫向 70%-95% × 指定縱向範圍），3x zoom。
    只佔整頁約 1.7% 面積，字仍清晰且 token 極省。回傳 base64 JPEG。
    """
    page = pdf_doc[page_idx]
    mat = fitz.Matrix(config.CMPM_ZOOM, config.CMPM_ZOOM)
    pix = page.get_pixmap(matrix=mat)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    w, h = img.size
    box = (int(w * config.CMPM_CROP_LEFT), int(h * top),
           int(w * config.CMPM_CROP_RIGHT), int(h * bottom))
    crop = img.crop(box)
    crop = ImageEnhance.Contrast(crop).enhance(config.OCR_CONTRAST)
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


_CMPM_PROMPT = (
    "This image is the JOB NATURE field of a Philips jobsheet. "
    "It lists four options in a row: CM, PM, FCO, INS. "
    "ONE of them is selected by a hand-drawn circle (an oval drawn around the word). "
    "Reply with ONLY the single word that is circled: CM, PM, FCO or INS. "
    "If you truly cannot see any circle, reply UNKNOWN. Do not explain."
)


def _read_job_nature(img_b64: str) -> str:
    raw = _call_k26(prompt=_CMPM_PROMPT, image_b64=img_b64, max_tokens=10).upper()
    for token in ("FCO", "INS", "PM", "CM"):   # 先比長/特殊的，避免 CM 被 PM 誤含
        if token in raw:
            return token
    return "UNKNOWN"


def detect_cm_pm(pdf_doc: fitz.Document, page_idx: int) -> str:
    """
    讀某頁的 JOB NATURE 欄位，回傳 'CM' / 'PM' / 'FCO' / 'INS' / 'UNKNOWN'。

    用戶實測：JOB NATURE 是手畫圈選（不是打勾），位置固定在
    縱向 10%-17%、橫向 70%-95%。先用主框讀，讀到 UNKNOWN 再用放寬框重讀一次。

    不再把無法判讀硬塞成 CM —— 回傳 UNKNOWN 交給 split_jobs 攔截，
    避免一頁誤判造成整份分頁錯位。
    """
    primary = crop_job_nature(pdf_doc, page_idx,
                              config.CMPM_CROP_TOP, config.CMPM_CROP_BOTTOM)
    result = _read_job_nature(primary)
    if result == "UNKNOWN":
        fallback = crop_job_nature(pdf_doc, page_idx,
                                   config.CMPM_FALLBACK_TOP, config.CMPM_FALLBACK_BOTTOM)
        result = _read_job_nature(fallback)
    return result


def ocr_jobsheet_fields(pdf_doc: fitz.Document, page_idx: int, zoom: float = 1.0) -> dict:
    """
    提取 ORDER NO / SERIAL NO / PRODUCT / CUSTOMER
    常見誤讀：9→G, O→0, 0→D, l→1, S→5, C450→CX50
    """
    img_b64 = crop_jobsheet_top(pdf_doc, page_idx, zoom=zoom)
    prompt = (
        "Extract these fields from the Philips medical equipment jobsheet image. "
        "Return JSON only, no markdown fences:\n"
        "{\n"
        '  "order_no": "8-digit number starting with 5 or 6, or null if blank",\n'
        '  "serial_no": "serial number like US622B1115 or USO16D0865",\n'
        '  "product": "product model like EPIQ Elite, EPIQ 5G, Affiniti 50, CX50",\n'
        '  "customer": "hospital or customer name like PYNEH, Trinity CWB, HKCH"\n'
        "}"
    )
    raw = _call_k26(prompt=prompt, image_b64=img_b64, max_tokens=300)
    raw = re.sub(r"^`{3}(?:json)?\s*|\s*`{3}$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"order_no": None, "serial_no": None, "product": None, "customer": None}

    for key in list(data.keys()):
        val = data.get(key)
        if isinstance(val, str) and val.strip().lower() in ("", "null", "none", "n/a"):
            data[key] = None
    return data

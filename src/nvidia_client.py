"""NVIDIA 視覺辨認服務：讀取 CM/PM 圈選及單據欄位。"""
import base64
import io
import json
import re
import time
from typing import Optional

import fitz
from PIL import Image, ImageEnhance
from openai import OpenAI
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from . import config

_client: Optional[OpenAI] = None


class NvidiaResponseError(RuntimeError):
    """辨認服務回覆不完整或格式不正確；不得當成空白單據繼續處理。"""


def _is_retryable_error(exc: BaseException) -> bool:
    """只重試暫時性問題；401/403、缺少 key 等設定錯誤要立即報告。"""
    if isinstance(exc, NvidiaResponseError):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code in (408, 409, 429):
        return True
    if isinstance(status_code, int) and status_code >= 500:
        return True
    return exc.__class__.__name__ in {"APIConnectionError", "APITimeoutError"}


def get_client() -> OpenAI:
    global _client
    if _client is None:
        if not config.NVIDIA_API_KEY:
            raise RuntimeError("NVIDIA_API_KEY 未設定")
        _client = OpenAI(
            base_url=config.NVIDIA_BASE_URL,
            api_key=config.NVIDIA_API_KEY,
            timeout=config.NVIDIA_REQUEST_TIMEOUT_SECONDS,
            # SDK 自己再重試會令實際等待時間失控；下方 tenacity 統一處理。
            max_retries=0,
        )
    return _client


def crop_jobsheet_top(pdf_doc: fitz.Document, page_idx: int, zoom: float = 1.0) -> str:
    """
    裁切第 N 頁的 10%-56% 區域（全寬），加強對比，回傳 base64 JPEG。
    除訂單/設備/醫院外，也涵蓋聯絡電話、資產編號與服務日期。
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


@retry(
    retry=retry_if_exception(_is_retryable_error),
    stop=stop_after_attempt(2),
    wait=wait_exponential(min=2, max=5),
    reraise=True,
)
def _call_vision(prompt: str, image_b64: str, max_tokens: int = 300,
                 expects_json: bool = False) -> str:
    """呼叫 NVIDIA 視覺模型做單張圖 OCR。

    Kimi K3 永遠會先推理，因此不能沿用舊 Llama 的極小輸出上限。詳細欄位
    JSON 工作會預留較多輸出空間，最後仍由本機 parser 嚴格驗證格式。
    """
    is_kimi_k3 = config.NVIDIA_MODEL.strip().lower() == "moonshotai/kimi-k3"
    request = dict(
        model=config.NVIDIA_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }],
        max_tokens=(
            max(max_tokens, config.KIMI_JSON_MAX_TOKENS)
            if is_kimi_k3 and expects_json
            else max(max_tokens, config.KIMI_TEXT_MAX_TOKENS)
            if is_kimi_k3
            else max_tokens
        ),
        temperature=1 if is_kimi_k3 else 0,
        timeout=config.NVIDIA_REQUEST_TIMEOUT_SECONDS,
    )
    if is_kimi_k3:
        # OCR 不需要長篇推理；low 可縮短免費入口的等待，同時保留推理能力。
        # NVIDIA 的 Kimi 範例使用串流；若等待整份答案，免費入口可能在回覆
        # headers 前已超時。只收集最後 content，reasoning_content 不進日誌。
        request.update(reasoning_effort="low", seed=0, stream=True)

    response = get_client().chat.completions.create(**request)
    if is_kimi_k3:
        started = time.monotonic()
        parts = []
        for chunk in response:
            if time.monotonic() - started > config.KIMI_STREAM_MAX_SECONDS:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                raise NvidiaResponseError("NVIDIA Kimi 串流超過時間限制")
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                piece = getattr(delta, "content", None)
                if isinstance(piece, str):
                    parts.append(piece)
        content = "".join(parts)
    else:
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError) as exc:
            raise NvidiaResponseError("NVIDIA 視覺模型回覆格式不完整") from exc
    if not isinstance(content, str) or not content.strip():
        raise NvidiaResponseError("NVIDIA 視覺模型沒有回傳文字")
    return content.strip()


def _parse_json_object(raw: str, required_keys: Optional[set] = None) -> dict:
    """讀取模型回覆中的 JSON 物件。

    NVIDIA 有時會在正確 JSON 前後加入短說明或 markdown fence。
    只接受真正可由 json 解析的物件，不用 eval，也不從散文猜測欄位。
    """
    text = raw.strip().lstrip("\ufeff")

    def usable(value) -> bool:
        return isinstance(value, dict) and (
            required_keys is None or required_keys.issubset(value)
        )

    try:
        candidate = json.loads(text)
        if usable(candidate):
            return candidate
        # 完整回覆本身是合法 JSON，但不是指定物件（例如 list/scalar），
        # 不可再從它的內部挖出一段內容冒充正式回覆。
        raise NvidiaResponseError("NVIDIA OCR 回覆不是指定的 JSON 物件")
    except json.JSONDecodeError:
        pass

    # 外層文字可能自己也含有一個 JSON 範例；逐一掃描，直到找到
    # 真正含齊工作單四個欄位的物件，避免誤收前面的無關物件。
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            candidate, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if usable(candidate):
            return candidate

    raise NvidiaResponseError("NVIDIA OCR 回覆沒有完整的 JSON 物件")


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
    raw = _call_vision(prompt=_CMPM_PROMPT, image_b64=img_b64, max_tokens=10).upper()
    # 只能接受一個明確答案。若模型不守指示，在解釋中同時列出 CM/PM/FCO/INS，
    # 舊寫法會取第一個字而誤切頁；現在一律回 UNKNOWN 轉人工。
    tokens = set(re.findall(r"\b(?:CM|PM|FCO|INS)\b", raw))
    return tokens.pop() if len(tokens) == 1 else "UNKNOWN"


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


def ocr_jobsheet_fields(
        pdf_doc: fitz.Document,
        page_idx: int,
        zoom: float = config.OCR_ZOOM_DEFAULT,
) -> dict:
    """
    忠實抄錄 ORDER / SERIAL / PRODUCT / CUSTOMER / LOCATION / PHONE / ASSET / DATE。

    視覺模型只負責「看字」，不負責挑 Asana 工作或修正常見值。模糊字元以
    candidates 保存，讓後面的 Asana 比對用日期、電話與 asset 交叉確認。
    常見誤讀：9→G, O→0, 0→D, l→1, S→5, C450→CX50
    """
    img_b64 = crop_jobsheet_top(pdf_doc, page_idx, zoom=zoom)
    prompt = (
        "You are a strict transcription reader, not a matching or guessing system. "
        "Read only values visibly written in the labelled boxes of this one jobsheet image. "
        "Treat this image independently: never invent, autocomplete, or reuse values from typical "
        "equipment, hospitals, previous images, or the field labels themselves. Do not normalize "
        "hospital abbreviations or product names. Preserve visible letters, digits and punctuation. "
        "For an ambiguous serial, phone or asset number, list at most three readings that are each "
        "actually supported by the handwriting. Never create alternatives merely to fill the list. "
        "The service date must come from the ACTION DATE / service-date box, not a printed form date. "
        "Use null or [] when blank or unreadable. "
        "Return JSON only, with no markdown fences:\n"
        "{\n"
        '  "order_no": "raw text below ORDER NO., or null",\n'
        '  "serial_candidates": ["raw visible reading"],\n'
        '  "product_raw": "raw text below PRODUCT, or null",\n'
        '  "customer_raw": "raw text in Customer Name, or null",\n'
        '  "location_raw": "raw department, ward, floor or room text, or null",\n'
        '  "phone_candidates": ["raw telephone reading"],\n'
        '  "asset_candidates": ["raw equipment/asset number reading"],\n'
        '  "service_date_raw": "raw ACTION DATE / service date, or null",\n'
        '  "date_source": "ACTION_DATE, OTHER, or null",\n'
        '  "unreadable_fields": ["field label"]\n'
        "}"
    )
    data = None
    last_error = None
    # 服務偶爾會在 JSON 前後加解釋。找出其中真正的 JSON；若仍不合法，
    # 同一張圖再問一次。兩次都錯才讓 Stage B 失敗並保留來源。
    field_order = (
        "order_no", "serial_candidates", "product_raw", "customer_raw",
        "location_raw", "phone_candidates", "asset_candidates",
        "service_date_raw", "date_source", "unreadable_fields",
    )
    # 模型偶爾省略可選的 location/unreadable 欄位；核心四項齊全便可讀，
    # 其餘缺項在本機補空值，避免格式小差異令整條 pipeline 失敗。
    expected = {"order_no", "serial_candidates", "product_raw", "customer_raw"}
    for _ in range(2):
        raw = _call_vision(
            prompt=prompt,
            image_b64=img_b64,
            max_tokens=300,
            expects_json=True,
        )
        try:
            candidate = _parse_json_object(raw, required_keys=expected)
        except (json.JSONDecodeError, NvidiaResponseError) as exc:
            last_error = exc
            continue

        list_fields = {
            "serial_candidates", "phone_candidates", "asset_candidates",
            "unreadable_fields",
        }
        invalid_types = []
        for key in field_order:
            value = candidate.get(key)
            if key in list_fields:
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) for item in value)
                ):
                    invalid_types.append(key)
            elif value is not None and not isinstance(value, str):
                invalid_types.append(key)
        if invalid_types:
            fields = ", ".join(sorted(invalid_types))
            last_error = NvidiaResponseError(f"NVIDIA OCR 欄位不是文字：{fields}")
            continue
        # 只保留預期欄位，避免模型附帶的其他內容進入後續流程。
        data = {
            key: candidate.get(key, [] if key in list_fields else None)
            for key in field_order
        }
        break

    if data is None:
        raise NvidiaResponseError("NVIDIA OCR 連續兩次回覆格式不正確") from last_error

    for key in list(data.keys()):
        val = data.get(key)
        if isinstance(val, str) and val.strip().lower() in ("", "null", "none", "n/a"):
            data[key] = None
        elif val is None and key in {
            "serial_candidates", "phone_candidates", "asset_candidates", "unreadable_fields"
        }:
            data[key] = []
        elif isinstance(val, list):
            cleaned = []
            for item in val:
                item = item.strip()
                if item and item.lower() not in ("null", "none", "n/a") and item not in cleaned:
                    cleaned.append(item)
            data[key] = cleaned[:3] if key != "unreadable_fields" else cleaned

    if data.get("date_source"):
        data["date_source"] = data["date_source"].strip().upper().replace(" ", "_")
        if data["date_source"] not in {"ACTION_DATE", "OTHER"}:
            data["date_source"] = None

    # 相容欄位由原始抄錄派生，不作任何猜測或自動修正。
    data["serial_no"] = next(iter(data["serial_candidates"]), None)
    data["product"] = data["product_raw"]
    data["customer"] = data["customer_raw"]
    return data

"""視覺辨認服務：讀取 CM/PM 圈選及單據欄位。

檔名為歷史相容保留；正式流程預設使用 NVIDIA，Safe Dry Run 亦可明確
選用 DeepSeek 官方付費 API 做隔離測試。
"""
import base64
import io
import json
import logging
import re
import time
from typing import Optional

import fitz
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from openai import OpenAI
from . import config

_client: Optional[OpenAI] = None
_client_identity: Optional[tuple] = None
_unavailable_models: set[str] = set()
log = logging.getLogger(__name__)


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


def _provider_settings() -> tuple[str, str, str, str, float, Optional[str]]:
    """回傳供應商、網址、key、主模型、timeout、後備模型。"""
    provider = config.OCR_PROVIDER.strip().lower()
    if provider == "deepseek":
        return (
            provider,
            config.DEEPSEEK_BASE_URL,
            config.DEEPSEEK_API_KEY,
            config.DEEPSEEK_MODEL,
            config.DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
            None,
        )
    if provider == "nvidia":
        return (
            provider,
            config.NVIDIA_BASE_URL,
            config.NVIDIA_API_KEY,
            config.NVIDIA_MODEL,
            config.NVIDIA_REQUEST_TIMEOUT_SECONDS,
            config.NVIDIA_FALLBACK_MODEL,
        )
    raise RuntimeError(f"不支援的 OCR_PROVIDER：{config.OCR_PROVIDER}")


def current_model_name() -> str:
    """供 smoke test 及日誌顯示目前真正會呼叫的模型。"""
    return _provider_settings()[3]


def get_client() -> OpenAI:
    global _client, _client_identity
    provider, base_url, api_key, _, timeout, _ = _provider_settings()
    identity = (provider, base_url, api_key, timeout)
    if _client is None or _client_identity != identity:
        if not api_key:
            raise RuntimeError(f"{provider.upper()}_API_KEY 未設定")
        _client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            # SDK 自己再重試會令實際等待時間失控；下方程式統一處理。
            max_retries=0,
        )
        _client_identity = identity
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
    # Jobsheet 是黑白表格；自動拉開紙色／墨色並輕微銳化，比單純放大更能
    # 保留手寫字邊緣。只處理指定欄位區，不把簽名與印章等雜訊送給模型。
    crop = ImageOps.autocontrast(crop.convert("L")).convert("RGB")
    crop = ImageEnhance.Contrast(crop).enhance(config.OCR_CONTRAST)
    crop = crop.filter(ImageFilter.SHARPEN)
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def crop_jobsheet_serial(pdf_doc: fitz.Document, page_idx: int,
                         zoom: float = 5.0) -> str:
    """只渲染 SERIAL NO. 標籤及手寫值，供配對失敗後精讀。"""
    page = pdf_doc[page_idx]
    rect = page.rect
    clip = fitz.Rect(
        rect.x0 + rect.width * config.OCR_SERIAL_CROP_LEFT,
        rect.y0 + rect.height * config.OCR_SERIAL_CROP_TOP,
        rect.x0 + rect.width * config.OCR_SERIAL_CROP_RIGHT,
        rect.y0 + rect.height * config.OCR_SERIAL_CROP_BOTTOM,
    )
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    img = ImageOps.autocontrast(img.convert("L")).convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(config.OCR_CONTRAST)
    img = img.filter(ImageFilter.SHARPEN)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def _call_vision_once(prompt: str, image_b64: str, model: str,
                      max_tokens: int, expects_json: bool) -> str:
    """向一個指定模型發出一次請求；重試和後備由外層控制。"""
    provider, _, _, _, timeout, _ = _provider_settings()
    normalized_model = model.strip().lower()
    is_deepseek = provider == "deepseek"
    is_kimi_k3 = normalized_model == "moonshotai/kimi-k3"
    is_nemotron_omni = (
        normalized_model
        == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    )
    request = dict(
        model=model,
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
            else max(max_tokens, config.DEEPSEEK_JSON_MAX_TOKENS)
            if is_deepseek and expects_json
            else max(max_tokens, config.NEMOTRON_INSTRUCT_MAX_TOKENS)
            if is_nemotron_omni
            else max(max_tokens, config.NVIDIA_JSON_MAX_TOKENS)
            if expects_json
            else max_tokens
        ),
        temperature=0.2 if is_nemotron_omni else 1 if is_kimi_k3 else 0,
        timeout=timeout,
    )
    if is_deepseek:
        # OCR 只需忠實抄錄，關閉預設思考可縮短延遲；JSON 工作同時使用
        # 官方 JSON mode，回覆仍會再經本機 parser 及欄位型別驗證。
        request["extra_body"] = {"thinking": {"type": "disabled"}}
        if expects_json:
            request["response_format"] = {"type": "json_object"}
    if is_nemotron_omni:
        # Nemotron 官方的 instruct/OCR 設定：不產生推理文字、固定取最可能
        # 的 token。模型只抄錄畫面內容，最後仍由本機嚴格驗證 JSON。
        request.update(
            seed=0,
            extra_body={
                "top_k": 1,
                "chat_template_kwargs": {"enable_thinking": False},
            },
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


def _call_vision(prompt: str, image_b64: str, max_tokens: int = 300,
                 expects_json: bool = False,
                 allow_fallback: bool = True) -> str:
    """呼叫目前指定的視覺模型做單張圖 OCR。

    Kimi 失聯時只等待一次（避免它再次長時間掛起）；目前使用的 Nemotron
    遇到 503/timeout 會短重試一次，才改用後備模型。失聯的模型會在本批
    工作內停用，避免每張單據都重等。
    """
    provider, _, _, primary, _, fallback = _provider_settings()
    primary = primary.strip()
    models = [primary]
    fallback = (fallback or "").strip()
    if allow_fallback and fallback and primary.lower() != fallback.lower():
        models.append(fallback)

    last_error = None
    for model in models:
        if model in _unavailable_models:
            continue
        # Kimi 曾實測長時間不回覆，因此維持一次即後備；Nemotron 的偶發
        # 503 通常短重試即可恢復，不應因一次暫時故障立刻落到舊 Llama。
        is_primary_kimi = (
            model == primary and model.strip().lower() == "moonshotai/kimi-k3"
        )
        attempts = 1 if is_primary_kimi and len(models) > 1 else 2
        for attempt in range(attempts):
            try:
                return _call_vision_once(
                    prompt, image_b64, model, max_tokens, expects_json
                )
            except Exception as exc:
                if not _is_retryable_error(exc):
                    raise
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(2)
        _unavailable_models.add(model)
        if model == primary and len(models) > 1:
            log.warning("主要圖片模型本次無回應，改用安全後備模型")

    if last_error is not None:
        # 已確認是暫時性圖片服務問題；轉成統一例外，讓 processor 記錄重試
        # 次數，而不是令整批工作單每次都顯示 pipeline 崩潰。
        raise NvidiaResponseError(
            f"{provider} 圖片模型暫時無法完成辨認"
        ) from last_error
    raise NvidiaResponseError(f"沒有可用的 {provider} 圖片模型")


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
    忠實抄錄 ORDER / SERIAL / PRODUCT / CUSTOMER / LOCATION / PHONE / ASSET /
    HAWO(WO) / DATE。

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
        "For an ambiguous serial, phone, asset or HAWO/WO number, list at most three readings that are each "
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
        '  "work_order_candidates": ["raw HAWO or WO service reference visibly written in FAULT SYMPTOM or ACTION TAKEN"],\n'
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
        "work_order_candidates", "service_date_raw", "date_source",
        "unreadable_fields",
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
            "work_order_candidates", "unreadable_fields",
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
            "serial_candidates", "phone_candidates", "asset_candidates",
            "work_order_candidates", "unreadable_fields"
        }:
            data[key] = []
        elif isinstance(val, list):
            cleaned = []
            for item in val:
                item = item.strip()
                if item and item.lower() not in ("null", "none", "n/a") and item not in cleaned:
                    cleaned.append(item)
            data[key] = cleaned[:3] if key != "unreadable_fields" else cleaned

    # 格式安全閘：模型只抄字，但不合業務格式的值不可進 Asana 搜尋。
    order_digits = re.sub(r"\D", "", data.get("order_no") or "")
    data["order_no"] = (
        order_digits if re.fullmatch(config.ORDER_NO_REGEX, order_digits) else None
    )

    def valid_mixed(value: str, minimum: int = 6) -> bool:
        token = re.sub(r"[^A-Z0-9]", "", value.upper())
        return (
            len(token) >= minimum
            and any(ch.isalpha() for ch in token)
            and any(ch.isdigit() for ch in token)
        )

    data["serial_candidates"] = [
        value for value in data["serial_candidates"] if valid_mixed(value)
    ]
    phones = []
    for value in data["phone_candidates"]:
        digits = re.sub(r"\D", "", value)
        if len(digits) == 11 and digits.startswith("852"):
            digits = digits[3:]
        if len(digits) == 8 and digits not in phones:
            phones.append(digits)
    data["phone_candidates"] = phones
    data["asset_candidates"] = [
        value for value in data["asset_candidates"]
        if len(re.sub(r"\D", "", value)) >= 4
    ]
    data["work_order_candidates"] = [
        value for value in data["work_order_candidates"]
        if len(re.sub(r"\D", "", value)) >= 6
    ]

    if data.get("date_source"):
        data["date_source"] = data["date_source"].strip().upper().replace(" ", "_")
        if data["date_source"] not in {"ACTION_DATE", "OTHER"}:
            data["date_source"] = None

    # 相容欄位由原始抄錄派生，不作任何猜測或自動修正。
    data["serial_no"] = next(iter(data["serial_candidates"]), None)
    data["product"] = data["product_raw"]
    data["customer"] = data["customer_raw"]
    return data


def ocr_jobsheet_serial_candidates(
        pdf_doc: fitz.Document,
        page_idx: int,
        zoom: float = 5.0,
) -> list[str]:
    """高倍精讀 serial 小格；只回傳畫面支持的機身編號候選。"""
    img_b64 = crop_jobsheet_serial(pdf_doc, page_idx, zoom=zoom)
    prompt = (
        "This crop contains the printed label SERIAL NO. and its handwritten value box. "
        "Transcribe only the handwritten serial value. Ignore the printed label and any "
        "adjacent job-nature boxes. Never autocomplete from a known device or prior image. "
        "If exactly one character is visually ambiguous, include at most three readings "
        "that are each supported by the strokes. Return JSON only: "
        '{"serial_candidates":["raw visible reading"]}. '
        "Use [] if the handwriting is unreadable."
    )
    raw = _call_vision(
        prompt=prompt,
        image_b64=img_b64,
        max_tokens=120,
        expects_json=True,
    )
    data = _parse_json_object(raw, required_keys={"serial_candidates"})
    values = data.get("serial_candidates")
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise NvidiaResponseError("NVIDIA serial 精讀欄位不是文字清單")

    candidates = []
    for value in values:
        value = value.strip()
        normalized = re.sub(r"[^A-Z0-9]", "", value.upper())
        if (
            len(normalized) >= 8
            and any(char.isalpha() for char in normalized)
            and any(char.isdigit() for char in normalized)
            and normalized not in {
                re.sub(r"[^A-Z0-9]", "", item.upper()) for item in candidates
            }
        ):
            candidates.append(value)
    return candidates[:3]

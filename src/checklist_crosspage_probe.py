"""B10-only, read-only probe of PM checklist identity and signature dates.

This never changes the production OCR or matcher. The private reviewed answer
is used only after image reads to grade anonymous booleans, never in prompts.
"""

import base64
import hashlib
import io
import json
import logging
import os
import re
from pathlib import Path

import fitz
from PIL import Image, ImageOps

from . import asana_client, backtest, nvidia_client

log = logging.getLogger(__name__)

# Normalized page rectangles: first checklist header and last checklist footer.
CHECKLIST_HEADER = (0.055, 0.125, 0.935, 0.225)
CHECKLIST_SIGNATURES = (0.050, 0.680, 0.955, 0.800)
CHECKLIST_PRODUCT = (0.055, 0.175, 0.340, 0.225)
CHECKLIST_SERIAL = (0.345, 0.175, 0.565, 0.225)
ZOOMS = (5.0, 6.0)


def _panel_image(doc, page_index: int, box: tuple[float, ...], zoom: float) -> str:
    page = doc[page_index]
    rect = page.rect
    left, top, right, bottom = box
    clip = fitz.Rect(rect.x0 + rect.width * left,
                     rect.y0 + rect.height * top,
                     rect.x0 + rect.width * right,
                     rect.y0 + rect.height * bottom)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
    image = ImageOps.autocontrast(Image.open(io.BytesIO(pix.tobytes("png"))).convert("L"))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=92)
    return base64.b64encode(output.getvalue()).decode("ascii")


def _read_json(image_b64: str, *, section: str) -> dict:
    if section == "header":
        fields = ("hospital", "product", "serial", "date")
        prompt = (
            "Strictly transcribe the handwritten values on the FIRST PAGE of a PM CHECKLIST. "
            "Read only Customer, System, sn, and Date near the top. "
            "Do not use prior images, typical serial patterns, Asana, or likely answers. "
            "Use ? for a genuinely unclear character, null if a field is blank. "
            'Return JSON only: {"hospital":null,"product":null,"serial":null,"date":null}'
        )
    elif section == "signatures":
        fields = ("engineer_date", "customer_date")
        prompt = (
            "Strictly transcribe only the two handwritten dates at the bottom of the LAST "
            "PAGE of a PM CHECKLIST: Engineer Date and Customer Date. "
            "Ignore any printed calibration date or other text. Do not infer missing digits. "
            'Return JSON only: {"engineer_date":null,"customer_date":null}'
        )
    elif section == "product":
        fields = ("product",)
        prompt = (
            "Transcribe only the handwritten value beside System on this PM checklist. "
            "Do not infer from typical Philips models or previous images. "
            'Return JSON only: {"product":null}'
        )
    elif section == "serial":
        fields = ("serial",)
        prompt = (
            "Transcribe only the handwritten value beside sn on this PM checklist. "
            "Copy each visible character. Write ? for a truly unclear character. "
            "Do not infer from product, typical serial patterns, or previous images. "
            'Return JSON only: {"serial":null}'
        )
    else:
        raise ValueError("unknown checklist section")
    last_error = None
    for _ in range(2):
        raw = nvidia_client._call_vision(
            prompt=prompt, image_b64=image_b64, max_tokens=160, expects_json=True
        )
        try:
            values = nvidia_client._parse_json_object(raw, required_keys=set(fields))
            if any(values[key] is not None and not isinstance(values[key], str)
                   for key in fields):
                raise ValueError("invalid field type")
            return {key: values[key].strip() if values[key] else None for key in fields}
        except (ValueError, json.JSONDecodeError, nvidia_client.NvidiaResponseError) as exc:
            last_error = exc
    raise nvidia_client.NvidiaResponseError("檢查表欄位兩次回覆格式不正確") from last_error


def _serial(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9?]", "", (value or "").upper())


def _compatible_partial(partial: str | None, complete: str | None) -> bool:
    left, right = _serial(partial), _serial(complete)
    return bool(left and right and len(left) == len(right)
                and all(a == "?" or a == b for a, b in zip(left, right)))


def _read_date(value: str | None) -> str | None:
    parsed = nvidia_client._parse_action_date(value)
    return parsed.isoformat() if parsed else None


def _anonymous_report(header_reads: list[dict], signature_reads: list[dict],
                      product_reads: list[dict], serial_reads: list[dict],
                      observed: dict, expected: dict, metrics: dict) -> dict:
    expected_serial = _serial(expected.get("serial"))
    observed_date = _read_date(observed.get("service_date_raw"))
    header_serials = [_serial(read.get("serial")) for read in header_reads]
    header_dates = [_read_date(read.get("date")) for read in header_reads]
    engineer_dates = [_read_date(read.get("engineer_date")) for read in signature_reads]
    customer_dates = [_read_date(read.get("customer_date")) for read in signature_reads]
    products = [asana_client.product_group(read.get("product")) for read in header_reads]
    product_cards = [asana_client.product_group(read.get("product")) for read in product_reads]
    wanted_product = asana_client.product_group(observed.get("product_raw"))
    hospitals = [asana_client.hospital_core(read.get("hospital")) for read in header_reads]
    wanted_hospital = asana_client.hospital_core(observed.get("hospital_raw"))
    serial_agreed = bool(header_serials[0] and header_serials[0] == header_serials[1]
                         and nvidia_client._valid_serial_token(header_serials[0]))
    serial_cards = [_serial(read.get("serial")) for read in serial_reads]
    serial_card_agreed = bool(serial_cards[0] and serial_cards[0] == serial_cards[1]
                              and nvidia_client._valid_serial_token(serial_cards[0]))
    report = {
        "sample_id": "B10",
        "page_count_ok": True,
        "checklist_serial_two_reads_agree": serial_agreed,
        "checklist_serial_matches_reviewed": bool(serial_agreed and header_serials[0] == expected_serial),
        "checklist_serial_compatible_with_sheet": bool(serial_agreed and
            _compatible_partial(observed.get("serial_raw"), header_serials[0])),
        "serial_card_two_reads_agree": serial_card_agreed,
        "serial_card_matches_reviewed": bool(serial_card_agreed and
                                              serial_cards[0] == expected_serial),
        "serial_card_compatible_with_sheet": bool(serial_card_agreed and
            _compatible_partial(observed.get("serial_raw"), serial_cards[0])),
        "checklist_product_two_reads_match_sheet": bool(wanted_product and
            products[0] == products[1] == wanted_product),
        "product_card_two_reads_match_sheet": bool(wanted_product and
            product_cards[0] == product_cards[1] == wanted_product),
        "checklist_hospital_two_reads_match_sheet": bool(wanted_hospital and
            hospitals[0] == hospitals[1] == wanted_hospital),
        "checklist_date_two_reads_match_sheet": bool(observed_date and
            header_dates[0] == header_dates[1] == observed_date),
        "last_page_engineer_date_two_reads_match_sheet": bool(observed_date and
            engineer_dates[0] == engineer_dates[1] == observed_date),
        "last_page_customer_date_two_reads_match_sheet": bool(observed_date and
            customer_dates[0] == customer_dates[1] == observed_date),
        "calls": int(metrics.get("calls") or 0),
        "seconds": round(float(metrics.get("seconds") or 0), 2),
        "tokens": int(metrics.get("total_tokens") or 0),
        "cost_cny_upper": metrics.get("estimated_cost_cny_upper"),
    }
    report["crosspage_evidence_complete"] = all(report[key] for key in (
        "checklist_serial_two_reads_agree", "checklist_serial_compatible_with_sheet",
        "checklist_product_two_reads_match_sheet", "checklist_hospital_two_reads_match_sheet",
        "checklist_date_two_reads_match_sheet",
        "last_page_engineer_date_two_reads_match_sheet",
        "last_page_customer_date_two_reads_match_sheet",
    ))
    report["crosspage_card_evidence_complete"] = all(report[key] for key in (
        "serial_card_two_reads_agree", "serial_card_compatible_with_sheet",
        "serial_card_matches_reviewed", "product_card_two_reads_match_sheet",
        "checklist_hospital_two_reads_match_sheet", "checklist_date_two_reads_match_sheet",
        "last_page_engineer_date_two_reads_match_sheet",
        "last_page_customer_date_two_reads_match_sheet",
    ))
    return report


def run() -> dict:
    fixture_dir = Path(os.environ.get("JOBSHEET_BACKTEST_DIR", "/tmp/jobsheet-backtest"))
    manifest_path = Path(os.environ.get(
        "JOBSHEET_BACKTEST_MANIFEST", str(fixture_dir / "manifest-reviewed-20260925.json")
    ))
    samples = {row["sample_id"]: row for row in backtest._load_manifest(manifest_path)}
    sample = samples["B10"]
    source = fixture_dir / sample["filename"]
    if (sample.get("review_status") != "confirmed" or sample.get("job_type") != "PM"
            or sample.get("expected", {}).get("kind") != "match"
            or sample.get("observed_fields", {}).get("date_source") != "ACTION_DATE"
            or not sample.get("source_sha256") or not source.is_file()):
        raise backtest.BacktestError("B10 私人答案或原件未驗證")
    if hashlib.sha256(source.read_bytes()).hexdigest() != sample["source_sha256"]:
        raise backtest.BacktestError("B10 PDF 指紋變更，停止只讀測試")
    with fitz.open(source) as doc:
        if doc.page_count != 4:
            raise backtest.BacktestError("B10 不是完整四頁 PM，停止只讀測試")
        nvidia_client.reset_ocr_metrics()
        header_reads = [_read_json(_panel_image(doc, 1, CHECKLIST_HEADER, zoom), section="header")
                        for zoom in ZOOMS]
        signature_reads = [_read_json(_panel_image(doc, 3, CHECKLIST_SIGNATURES, zoom),
                                      section="signatures") for zoom in ZOOMS]
        product_reads = [_read_json(_panel_image(doc, 1, CHECKLIST_PRODUCT, zoom),
                                    section="product") for zoom in ZOOMS]
        serial_reads = [_read_json(_panel_image(doc, 1, CHECKLIST_SERIAL, zoom),
                                   section="serial") for zoom in ZOOMS]
    report = _anonymous_report(
        header_reads, signature_reads, product_reads, serial_reads,
        sample["observed_fields"], sample["expected"],
        nvidia_client.get_ocr_metrics(),
    )
    output = Path(os.environ.get("JOBSHEET_CHECKLIST_REPORT", "/tmp/checklist-anonymous.json"))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## B10 跨頁只讀探針\n\n")
            for key, value in report.items():
                stream.write(f"- {key}: {value}\n")
            stream.write("\n僅核對頁面抄錄；沒有選 Asana 工作、沒有上傳或移動原件。\n")
    log.info("B10 跨頁只讀探針完成：完整證據=%s", report["crosspage_evidence_complete"])
    return report


if __name__ == "__main__":
    run()

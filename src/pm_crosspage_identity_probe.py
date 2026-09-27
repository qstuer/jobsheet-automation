"""Read-only PM checklist identity probe for four unresolved backtest sheets.

Only the first PM checklist page repeats Customer/System/sn/Date/Asset No.
The middle checklist page is safety measurements, not an identity source; the
last checklist page has sign-off dates but no repeated serial or product.
Reviewed answers are used only *after* OCR to grade anonymous booleans.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import fitz

from . import asana_client, backtest, nvidia_client
from .checklist_crosspage_probe import _panel_image, _read_date

SAMPLE_IDS = ("B04", "B06", "B07", "B10")

# Normalised rectangles on page 2 of the four-page PM package. Keep the
# printed label inside each card so the model knows which blank to transcribe.
# The serial card must reach the end of its line (x=.61); the older B10-only
# experiment cut it at x=.565 and could clip the final characters.
HEADER = (0.050, 0.125, 0.950, 0.225)
CUSTOMER = (0.050, 0.135, 0.605, 0.177)
SYSTEM = (0.050, 0.169, 0.360, 0.218)
SERIAL = (0.350, 0.169, 0.610, 0.218)
DATE = (0.610, 0.135, 0.930, 0.177)
ASSET = (0.610, 0.169, 0.930, 0.218)

_PROMPTS = {
    "header": (
        "This image is ONLY the top of PAGE 1 OF 3 of a Philips PM CHECKLIST "
        "(PDF page 2), not the jobsheet cover. Transcribe handwriting next to "
        "these printed labels: top-left Customer=hospital/site, top-right "
        "Date=checklist date; second row left System=product, middle sn=machine "
        "serial number, right Asset No.=asset. Follow each printed underline; "
        "do not copy a value from a neighbouring blank. Copy visible characters "
        "exactly, including short hospital codes. Use ? for an unreadable single "
        "character, null for a blank field. Do not use previous images, model "
        "names, serial conventions, Asana or guessed answers. JSON only: "
        '{"hospital":null,"product":null,"serial":null,"date":null,"asset":null}'
    ),
    "hospital": (
        "This is the top-left Customer line on PM CHECKLIST page 1 of 3. "
        "Copy only the handwritten hospital/site beside Customer. A short code "
        "is acceptable; do not expand it to a hospital name. Do not use other "
        'fields or guesses. JSON only: {"value":null}'
    ),
    "product": (
        "This is the second-row System line on PM CHECKLIST page 1 of 3. "
        "Copy only the handwritten product/model beside System. The printed "
        "sn label to the right marks a DIFFERENT field. Do not infer a model "
        'from serial patterns. JSON only: {"value":null}'
    ),
    "serial": (
        "This is the second-row sn line on PM CHECKLIST page 1 of 3. "
        "Copy the ENTIRE handwritten machine serial number after printed sn, "
        "including its final character just before the Asset No. area. Do not "
        "copy the asset number. Inspect each character as written; use ? for a "
        "genuinely unclear character, especially O/0 or Z/2. Never fill gaps "
        'from a likely serial pattern. JSON only: {"value":null}'
    ),
    "date": (
        "This is the top-right Date line on PM CHECKLIST page 1 of 3. "
        "Copy only the handwritten date beside Date, in its written order. "
        "Do not use any sign-off date, calibration date, or today's date. "
        'JSON only: {"value":null}'
    ),
    "asset": (
        "This is the second-row Asset No. line on PM CHECKLIST page 1 of 3. "
        "Copy only the handwritten asset number beside Asset No.; return null "
        'when blank. Do not copy the machine serial. JSON only: {"value":null}'
    ),
}


def _read(image: str, field: str) -> dict:
    keys = {"hospital", "product", "serial", "date", "asset"} if field == "header" else {"value"}
    last_error = None
    for _ in range(2):
        raw = nvidia_client._call_vision(
            prompt=_PROMPTS[field], image_b64=image, max_tokens=180,
            expects_json=True,
        )
        try:
            data = nvidia_client._parse_json_object(raw, required_keys=keys)
            if any(data[key] is not None and not isinstance(data[key], str) for key in keys):
                raise ValueError("invalid OCR field type")
            return {key: data[key].strip() if data[key] else None for key in keys}
        except (ValueError, json.JSONDecodeError, nvidia_client.NvidiaResponseError) as exc:
            last_error = exc
    raise nvidia_client.NvidiaResponseError("PM checklist OCR response invalid") from last_error


def _normal_serial(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9?]", "", (value or "").upper())


def _hospital_matches(value: str | None, expected: str | None) -> bool:
    return bool(value and expected and
                asana_client.hospital_core(value) == asana_client.hospital_core(expected))


def _product_matches(value: str | None, expected: str | None) -> bool:
    return bool(value and expected and
                asana_client.product_group(value) == asana_client.product_group(expected))


def _grade(reads: dict[str, list[dict]], sample: dict, metrics: dict) -> dict:
    observed = sample["observed_fields"]
    expected = sample["expected"]
    headers = reads["header"]
    cards = {field: reads[field][0]["value"] for field in
             ("hospital", "product", "serial", "date", "asset")}
    expected_serial = _normal_serial(expected["serial"])
    expected_date = _read_date(observed.get("service_date_raw"))
    serials = [_normal_serial(row.get("serial")) for row in headers]
    report = {
        "sample_id": sample["sample_id"],
        "header_hospital_both_correct": all(
            _hospital_matches(row.get("hospital"), observed.get("hospital_raw"))
            for row in headers),
        "header_product_both_correct": all(
            _product_matches(row.get("product"), observed.get("product_raw"))
            for row in headers),
        "header_serial_both_correct": all(s == expected_serial for s in serials),
        "header_serial_both_agree": bool(serials[0] and serials[0] == serials[1]),
        "header_date_both_correct": bool(expected_date and all(
            _read_date(row.get("date")) == expected_date for row in headers)),
        "card_hospital_correct": _hospital_matches(cards["hospital"], observed.get("hospital_raw")),
        "card_product_correct": _product_matches(cards["product"], observed.get("product_raw")),
        "card_serial_correct": _normal_serial(cards["serial"]) == expected_serial,
        "card_date_correct": bool(expected_date and _read_date(cards["date"]) == expected_date),
        "card_asset_present": bool(cards["asset"]),
        "card_serial_same_as_header": bool(_normal_serial(cards["serial"]) and
                                           _normal_serial(cards["serial"]) in serials),
        "calls": int(metrics.get("calls") or 0),
        "seconds": round(float(metrics.get("seconds") or 0), 2),
        "cost_cny_upper": metrics.get("estimated_cost_cny_upper"),
    }
    report["all_identity_cards_correct"] = all(report[key] for key in (
        "card_hospital_correct", "card_product_correct", "card_serial_correct"))
    return report


def run() -> dict:
    fixture_dir = Path(os.environ.get("JOBSHEET_BACKTEST_DIR", "/tmp/jobsheet-backtest"))
    manifest = Path(os.environ.get("JOBSHEET_BACKTEST_MANIFEST", str(
        fixture_dir / "manifest-reviewed-20260925.json")))
    samples = {s["sample_id"]: s for s in backtest._load_manifest(manifest)}
    reports = []
    for sample_id in SAMPLE_IDS:
        sample = samples[sample_id]
        source = fixture_dir / sample["filename"]
        if (sample.get("review_status") != "confirmed" or sample.get("job_type") != "PM"
                or sample.get("expected", {}).get("kind") != "match"
                or not sample.get("source_sha256") or not source.is_file()):
            raise backtest.BacktestError(f"{sample_id} reviewed PM source unavailable")
        if hashlib.sha256(source.read_bytes()).hexdigest() != sample["source_sha256"]:
            raise backtest.BacktestError(f"{sample_id} source hash mismatch")
        with fitz.open(source) as doc:
            if doc.page_count != 4:
                raise backtest.BacktestError(f"{sample_id} is not a four-page PM")
            nvidia_client.reset_ocr_metrics()
            reads = {"header": [
                _read(_panel_image(doc, 1, HEADER, zoom), "header")
                for zoom in (5.0, 6.0)
            ]}
            for field, box in (("hospital", CUSTOMER), ("product", SYSTEM),
                               ("serial", SERIAL), ("date", DATE), ("asset", ASSET)):
                reads[field] = [_read(_panel_image(doc, 1, box, 6.0), field)]
        reports.append(_grade(reads, sample, nvidia_client.get_ocr_metrics()))
    result = {"read_only": True, "sample_count": len(reports), "samples": reports}
    output = Path(os.environ.get("JOBSHEET_PM_IDENTITY_REPORT", "/tmp/pm-identity-anonymous.json"))
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## 四張 PM 工作單跨頁身分欄，只讀測試\n\n")
            stream.write("只讀第 2 頁固定欄位；人工答案僅用作匿名評分，未選 Asana 工作，未移動或上傳檔案。\n\n")
            for report in reports:
                stream.write(f"### {report['sample_id']}\n\n")
                for key, value in report.items():
                    if key != "sample_id":
                        stream.write(f"- {key}: {value}\n")
                stream.write("\n")
    return result


if __name__ == "__main__":
    run()

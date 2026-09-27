"""Read-only 20-sample test of independent PM checklist date evidence.

The image model sees only one date panel at a time. Reviewed dates are used
after all reads for anonymous grading, never in prompts or matching.
"""

import hashlib
import json
import logging
import os
from pathlib import Path

import fitz

from . import backtest, nvidia_client
from .checklist_crosspage_probe import _panel_image

log = logging.getLogger(__name__)

# Normalized boxes for the first PM checklist's Date and final checklist's
# Customer Date. Neither includes the equipment calibration date.
CHECKLIST_DATE = (0.580, 0.120, 0.940, 0.175)
CUSTOMER_SIGNATURE_DATE = (0.520, 0.695, 0.940, 0.790)
ZOOMS = (5.0, 6.0)


def _read_date(image_b64: str, *, location: str) -> str | None:
    if location == "checklist":
        prompt = (
            "Read ONLY the handwritten Date at the top of the FIRST PAGE of a PM "
            "checklist. Copy day/month/year exactly; return null if unclear. "
            "Do not infer from any other page or Asana task. "
            'Return JSON only: {"date":null}'
        )
    elif location == "customer_signature":
        prompt = (
            "Read ONLY the handwritten Customer Date below the signature at "
            "the bottom of the LAST PAGE of a PM checklist. Ignore stamps and "
            "printed equipment calibration dates. Copy day/month/year exactly; "
            "return null if unclear. Do not infer from other pages or Asana. "
            'Return JSON only: {"date":null}'
        )
    else:
        raise ValueError("unknown PM date location")
    raw = nvidia_client._call_vision(
        prompt=prompt, image_b64=image_b64, max_tokens=80, expects_json=True
    )
    try:
        parsed = nvidia_client._parse_json_object(raw, required_keys={"date"})
    except (ValueError, json.JSONDecodeError, nvidia_client.NvidiaResponseError):
        return None
    value = parsed["date"]
    if value is not None and not isinstance(value, str):
        return None
    day = nvidia_client._parse_action_date(value)
    return day.isoformat() if day else None


def _grade(checklist: list[str | None], customer: list[str | None],
           reviewed_date: str, metrics: dict) -> dict:
    checklist_agrees = bool(checklist[0] and checklist[0] == checklist[1])
    customer_agrees = bool(customer[0] and customer[0] == customer[1])
    crosspage_agrees = bool(checklist_agrees and customer_agrees
                            and checklist[0] == customer[0])
    if not checklist_agrees or not customer_agrees:
        status = "UNREADABLE_OR_DISAGREED"
    elif not crosspage_agrees:
        status = "CROSSPAGE_CONFLICT"
    elif checklist[0] == reviewed_date:
        status = "CONFIRMED_CORRECT"
    else:
        status = "CONSISTENT_BUT_WRONG"
    return {
        "status": status,
        "checklist_two_reads_agree": checklist_agrees,
        "checklist_matches_reviewed": bool(checklist_agrees and checklist[0] == reviewed_date),
        "customer_two_reads_agree": customer_agrees,
        "customer_matches_reviewed": bool(customer_agrees and customer[0] == reviewed_date),
        "crosspage_agrees": crosspage_agrees,
        "matches_reviewed": bool(crosspage_agrees and checklist[0] == reviewed_date),
        "calls": int(metrics.get("calls") or 0),
        "seconds": round(float(metrics.get("seconds") or 0), 2),
        "cost_cny_upper": metrics.get("estimated_cost_cny_upper"),
    }


def run() -> list[dict]:
    fixture_dir = Path(os.environ.get("JOBSHEET_BACKTEST_DIR", "/tmp/jobsheet-backtest"))
    manifest = Path(os.environ.get(
        "JOBSHEET_BACKTEST_MANIFEST", str(fixture_dir / "manifest-reviewed-20260925.json")
    ))
    output = Path(os.environ.get("JOBSHEET_CHECKLIST_DATE_REPORT",
                                 "/tmp/checklist-dates-anonymous.json"))
    rows = []
    for sample in backtest._load_manifest(manifest):
        source = fixture_dir / sample["filename"]
        if (not sample.get("source_sha256") or not source.is_file() or
                hashlib.sha256(source.read_bytes()).hexdigest() != sample["source_sha256"]):
            raise backtest.BacktestError("私人 PDF 指紋變更，停止只讀測試")
        sample_id = sample["sample_id"]
        with fitz.open(source) as doc:
            if sample["job_type"] != "PM" or doc.page_count != 4:
                result = {"status": "INCOMPLETE_OR_NOT_PM", "calls": 0}
            elif sample["review_status"] != "confirmed":
                result = {"status": "UNVERIFIED_ANSWER", "calls": 0}
            else:
                nvidia_client.reset_model_availability()
                nvidia_client.reset_ocr_metrics()
                try:
                    checklist = [
                        _read_date(_panel_image(doc, 1, CHECKLIST_DATE, zoom),
                                   location="checklist") for zoom in ZOOMS
                    ]
                    customer = [
                        _read_date(_panel_image(doc, 3, CUSTOMER_SIGNATURE_DATE, zoom),
                                   location="customer_signature") for zoom in ZOOMS
                    ]
                except nvidia_client.NvidiaResponseError:
                    result = {"status": "VISION_SERVICE_ERROR", "calls":
                              int(nvidia_client.get_ocr_metrics().get("calls") or 0)}
                else:
                    result = _grade(checklist, customer,
                                    sample["observed_fields"]["service_date_raw"],
                                    nvidia_client.get_ocr_metrics())
        rows.append({"sample_id": sample_id, **result})
        # An interrupted workflow still leaves only anonymous completed rows.
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("[%s] PM checklist date probe: %s", sample_id, result["status"])
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## 20 份 PM 檢查表跨頁日期只讀試驗\n\n")
            stream.write("| 樣本 | 結果 | 呼叫 |\n|---|---|---:|\n")
            for row in rows:
                stream.write(f"| {row['sample_id']} | {row['status']} | {row['calls']} |\n")
            stream.write("\n沒有選 Asana 工作、沒有上傳或移動原件；報告不含原始日期或客戶資料。\n")
    return rows


if __name__ == "__main__":
    run()

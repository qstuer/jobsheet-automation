"""Four private PDFs: assisted, read-only acceptance of the pending-review path.

The private manifest provides the human-confirmed date. Its expected Asana GID
is withheld until the date-only pass explicitly reports multiple visits. No
ground-truth hospital, product, serial or phone is given to the image model.
"""

import hashlib
import json
import logging
import os
from pathlib import Path

import fitz

from . import asana_client, asana_index, backtest, nvidia_client, pending_review, processor

log = logging.getLogger(__name__)
SAMPLE_IDS = ("B01", "B04", "B07", "B10")


def _anonymous_row(sample_id: str, *, status: str, reason: str = "",
                   assisted: bool = False, metrics: dict | None = None) -> dict:
    metrics = metrics or {}
    return {
        "sample_id": sample_id, "status": status, "reason": reason,
        "task_choice_supplied": assisted,
        "calls": int(metrics.get("calls") or 0),
        "seconds": round(float(metrics.get("seconds") or 0), 2),
        "tokens": int(metrics.get("total_tokens") or 0),
        "cost_cny_upper": metrics.get("estimated_cost_cny_upper"),
    }


def _add_metrics(first: dict, second: dict) -> dict:
    result = dict(second)
    for key in ("calls", "seconds", "total_tokens", "estimated_cost_cny_upper"):
        if isinstance(first.get(key), (int, float)):
            result[key] = (result.get(key) or 0) + first[key]
    return result


def run() -> list[dict]:
    fixture_dir = Path(os.environ.get("JOBSHEET_BACKTEST_DIR", "/tmp/jobsheet-backtest"))
    manifest_path = Path(os.environ.get(
        "JOBSHEET_BACKTEST_MANIFEST", str(fixture_dir / "manifest-reviewed-20260925.json")
    ))
    samples = {row["sample_id"]: row for row in backtest._load_manifest(manifest_path)}
    index_path = Path(os.environ.get("ASANA_INDEX_LOCAL_FILE", "/tmp/asana-device-index.json"))
    index_manifest = Path(os.environ.get(
        "ASANA_INDEX_MANIFEST_LOCAL_FILE", "/tmp/asana-device-index-manifest.json"
    ))
    asana_client.set_device_index(asana_index.load_index(index_path, index_manifest))

    # Fail before the first paid image call if any private input is wrong.
    for sample_id in SAMPLE_IDS:
        sample = samples[sample_id]
        source = fixture_dir / sample["filename"]
        if sample.get("review_status") != "confirmed" or sample["expected"]["kind"] != "match":
            raise backtest.BacktestError(f"{sample_id} 尚無已核實的工作答案")
        if sample.get("observed_fields", {}).get("date_source") != "ACTION_DATE":
            raise backtest.BacktestError(f"{sample_id} 日期不是工作單 ACTION DATE")
        pending_review.parse_review_date(
            sample.get("observed_fields", {}).get("service_date_raw")
        )
        if not source.is_file() or not sample.get("source_sha256"):
            raise backtest.BacktestError(f"{sample_id} 原件或指紋缺失")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != sample["source_sha256"]:
            raise backtest.BacktestError(f"{sample_id} 原件已變更，停止驗收")

    rows = []
    for sample_id in SAMPLE_IDS:
        sample = samples[sample_id]
        nvidia_client.reset_model_availability()
        log.info("[%s] 開始單份只讀核對", sample_id)
        with fitz.open(fixture_dir / sample["filename"]) as doc:
            expected_pages = 4 if sample["job_type"] == "PM" else 1
            if doc.page_count != expected_pages:
                row = _anonymous_row(sample_id, status="PENDING", reason="page_count_conflict")
                rows.append(row)
                continue
            try:
                nvidia_client.reset_ocr_metrics()
                detected = nvidia_client.detect_cm_pm(doc, 0)
                circle_metrics = nvidia_client.get_ocr_metrics()
                if detected != sample["job_type"]:
                    row = _anonymous_row(
                        sample_id, status="PENDING", reason="job_type_conflict",
                        metrics=circle_metrics,
                    )
                    rows.append(row)
                    continue
                ocr = processor._ocr_for_pending_review(doc)
                metrics = _add_metrics(circle_metrics, ocr.get("ocr_metrics") or {})
                confirmed_day = sample["observed_fields"]["service_date_raw"]
                first = pending_review.review_ocr(ocr, detected, confirmed_day)
                assisted = False
                result = first
                if first["reason"] == "visit_ambiguous":
                    # Only here is the confirmed *work* revealed to the
                    # reviewer; it still rechecks every identity and date.
                    assisted = True
                    result = pending_review.review_ocr(
                        ocr, detected, confirmed_day, sample["expected"]["task_gid"]
                    )
                if result["status"] == "READY_READ_ONLY":
                    task = result["task"]
                    correct = backtest._matches_expected(task, sample["expected"])
                    row = _anonymous_row(
                        sample_id, status="PASS_ASSISTED" if correct and assisted else
                        "PASS_DATE_ONLY" if correct else "WRONG_MATCH",
                        reason="all_checks_passed" if correct else "answer_conflict",
                        assisted=assisted, metrics=metrics,
                    )
                else:
                    row = _anonymous_row(sample_id, status="PENDING",
                                         reason=result["reason"], assisted=assisted,
                                         metrics=metrics)
            except nvidia_client.NvidiaResponseError:
                # Provider text can contain customer data; never print it.
                row = _anonymous_row(
                    sample_id, status="SERVICE_ERROR", reason="vision_unavailable",
                    metrics=nvidia_client.get_ocr_metrics(),
                )
        rows.append(row)
        log.info("[%s] %s / %s", sample_id, row["status"], row["reason"])

    report_path = Path(os.environ.get("JOBSHEET_REVIEW_REPORT", "/tmp/pending-review-anonymous.json"))
    report_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as stream:
            stream.write("## 四份受控核對（只讀，人工日期不是模型成績）\n\n")
            stream.write("| 樣本 | 結果 | 是否另選工作 | 原因 | 圖片呼叫 |\n")
            stream.write("|---|---|---|---|---:|\n")
            for row in rows:
                stream.write(f"| {row['sample_id']} | {row['status']} | "
                             f"{'是' if row['task_choice_supplied'] else '否'} | "
                             f"{row['reason']} | {row['calls']} |\n")
            stream.write("\n沒有 OneDrive 寫入或 Google Drive 來源搬移；公開報告不含日期、電話、機身編號、醫院、任務或檔名。\n")
    return rows


if __name__ == "__main__":
    run()

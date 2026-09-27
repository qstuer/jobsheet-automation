"""Anonymous, read-only comparison of one-shot versus segmented date OCR.

The private reviewed date is used only to grade both outputs. Neither OCR
prompt sees the answer, any Asana task, or another page of the jobsheet.
"""

import hashlib
import json
import logging
import os
from pathlib import Path

import fitz

from . import backtest, nvidia_client

log = logging.getLogger(__name__)
SAMPLES = ("B01", "B04", "B06", "B07", "B08", "B09", "B10", "B19")
ZOOMS = (5.0, 6.0)


def _grade(readings: list[str | None], reviewed: str) -> dict:
    return {
        "individual": [
            "UNREADABLE" if value is None else
            "CORRECT" if value == reviewed else "OTHER_VALID_DATE"
            for value in readings
        ],
        "two_reads": (
            "CORRECT" if readings[0] == reviewed else "OTHER_VALID_DATE"
        ) if readings[0] is not None and readings[0] == readings[1]
        else "UNRESOLVED",
    }


def run() -> list[dict]:
    fixture_dir = Path(os.environ["JOBSHEET_BACKTEST_DIR"])
    manifest = Path(os.environ["JOBSHEET_BACKTEST_MANIFEST"])
    output = Path(os.environ["JOBSHEET_CUSTOMER_DATE_REPORT"])
    samples = {sample["sample_id"]: sample for sample in backtest._load_manifest(manifest)
               if sample["sample_id"] in SAMPLES}
    if set(samples) != set(SAMPLES):
        raise ValueError("missing private test fixtures")
    rows = []
    for sample_id in SAMPLES:
        sample = samples[sample_id]
        source = fixture_dir / sample["filename"]
        if (sample.get("review_status") != "confirmed"
                or not sample.get("source_sha256") or not source.is_file()
                or hashlib.sha256(source.read_bytes()).hexdigest()
                != sample["source_sha256"]):
            raise ValueError("private fixture verification failed")
        reviewed = sample["observed_fields"]["service_date_raw"]
        if sample["observed_fields"].get("date_source") != "ACTION_DATE":
            raise ValueError("reviewed date is not ACTION DATE")
        nvidia_client.reset_model_availability()
        nvidia_client.reset_ocr_metrics()
        with fitz.open(source) as doc:
            if doc.page_count != 4 or sample["job_type"] != "PM":
                raise ValueError("expected complete PM sample")
            baseline = []
            segmented = []
            for zoom in ZOOMS:
                try:
                    baseline.append(nvidia_client.ocr_jobsheet_signature_date(
                        doc, 0, "customer_signed_date", zoom=zoom
                    ))
                except nvidia_client.NvidiaResponseError:
                    baseline.append(None)
                try:
                    segmented.append(nvidia_client.ocr_jobsheet_customer_date_parts(
                        doc, 0, zoom=zoom
                    ))
                except nvidia_client.NvidiaResponseError:
                    segmented.append(None)
        metrics = nvidia_client.get_ocr_metrics()
        row = {
            "sample_id": sample_id,
            "baseline": _grade(baseline, reviewed),
            "segmented": _grade(segmented, reviewed),
            "calls": int(metrics.get("calls") or 0),
            "seconds": round(float(metrics.get("seconds") or 0), 2),
            "cost_upper_cny": metrics.get("estimated_cost_cny_upper"),
        }
        rows.append(row)
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        log.info("[%s] customer date probe completed; no source values logged", sample_id)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## Customer signature date: whole versus segmented OCR\n\n")
            stream.write("| Sample | Whole date | Day/month/year | Calls |\n")
            stream.write("|---|---|---|---:|\n")
            for row in rows:
                stream.write(f"| {row['sample_id']} | {row['baseline']['two_reads']} "
                             f"| {row['segmented']['two_reads']} | {row['calls']} |\n")
            stream.write("\nRead-only; no Asana match, OneDrive write, or source move.\n")
    return rows


if __name__ == "__main__":
    run()

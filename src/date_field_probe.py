"""Read-only, anonymous comparison of three handwritten date fields.

The reviewed answers and PDFs remain in the private backtest fixture folder.
Only per-field correctness flags and model usage may leave the runner.  This
probe neither searches Asana nor changes a production jobsheet.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

import fitz

from . import nvidia_client
from .backtest import _load_manifest, _validate_files

log = logging.getLogger("date_field_probe")

SAMPLES = ("B01", "B04", "B07", "B10")
FIELDS = (
    "service_date_raw",
    "engineer_signed_date",
    "customer_signed_date",
)
ZOOMS = (5.0, 6.0)


def _reviewed_day(sample: dict) -> date:
    if sample.get("review_status") != "confirmed":
        raise ValueError("日期探針需要已覆核的私人答案")
    observed = sample.get("observed_fields") or {}
    if observed.get("date_source") != "ACTION_DATE":
        raise ValueError("日期探針只接受 ACTION DATE 私人答案")
    return date.fromisoformat(observed["service_date_raw"])


def _read_date(doc: fitz.Document, field: str, zoom: float) -> date | None:
    if field == "service_date_raw":
        result = nvidia_client.ocr_jobsheet_focused_field(
            doc, 0, field, zoom=zoom,
        )
        audit = result.get("_ocr_audit") or {}
        raw = (audit.get("raw") or {}).get(field) or result.get(field)
        return nvidia_client._parse_action_date(raw)
    raw = nvidia_client.ocr_jobsheet_signature_date(
        doc, 0, field, zoom=zoom,
    )
    return nvidia_client._parse_action_date(raw)


def _read_status(read: date | None, expected: date) -> str:
    if read is None:
        return "UNREADABLE"
    return "CORRECT" if read == expected else "OTHER_VALID_DATE"


def probe_sample(sample: dict, fixture_dir: Path) -> dict:
    """Return no dates or private contents, including on model failure."""
    expected = _reviewed_day(sample)
    nvidia_client.reset_ocr_metrics()
    nvidia_client.reset_model_availability()
    fields: dict[str, list[str]] = {}
    with fitz.open(fixture_dir / sample["filename"]) as doc:
        if doc.page_count < 1:
            raise ValueError("日期探針不能讀取空白 PDF")
        for field in FIELDS:
            statuses = []
            for zoom in ZOOMS:
                try:
                    value = _read_date(doc, field, zoom)
                    statuses.append(_read_status(value, expected))
                except nvidia_client.NvidiaResponseError:
                    statuses.append("MODEL_ERROR")
            fields[field] = statuses
    metrics = nvidia_client.get_ocr_metrics()
    return {
        "sample_id": sample["sample_id"],
        "fields": fields,
        "calls": metrics["calls"],
        "seconds": round(metrics["seconds"], 2),
        "tokens": metrics["total_tokens"],
        "cost_upper_cny": metrics.get("estimated_cost_cny_upper"),
    }


def run() -> int:
    fixture_dir = Path(os.environ["JOBSHEET_BACKTEST_DIR"])
    manifest = Path(os.environ["JOBSHEET_BACKTEST_MANIFEST"])
    report = Path(os.environ["JOBSHEET_DATE_PROBE_REPORT"])
    samples = _load_manifest(manifest)
    _validate_files(samples, fixture_dir)
    selected = {sample["sample_id"]: sample for sample in samples
                if sample["sample_id"] in SAMPLES}
    if set(selected) != set(SAMPLES):
        raise ValueError("日期探針缺少指定樣本")
    rows = []
    for sample_id in SAMPLES:
        row = probe_sample(selected[sample_id], fixture_dir)
        rows.append(row)
        log.info("[%s] 日期欄位只讀檢查完成：%s", sample_id, row["fields"])
    temporary = report.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(run())

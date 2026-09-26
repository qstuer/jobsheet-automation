"""Read-only, anonymous comparison of three handwritten date fields.

The reviewed answers and PDFs remain in the private backtest fixture folder.
Only per-field correctness flags and model usage may leave the runner.  This
probe neither searches Asana nor changes a production jobsheet.
"""
from __future__ import annotations

import json
import logging
import os
import hashlib
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import patch

import fitz

from . import config, nvidia_client
from .backtest import _load_manifest

log = logging.getLogger("date_field_probe")

SAMPLES = ("B01", "B04", "B07", "B10")
FIELDS = (
    "service_date_raw",
    "engineer_signed_date",
    "customer_signed_date",
)
ZOOMS = (5.0, 6.0)
_JOINT_KEYS = {
    "action_date": "service_date_raw",
    "engineer_date": "engineer_signed_date",
    "customer_date": "customer_signed_date",
}


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


def _component_flags(read: date | None, expected: date) -> dict[str, bool] | None:
    if read is None:
        return None
    return {
        "day": read.day == expected.day,
        "month": read.month == expected.month,
        "year": read.year == expected.year,
    }


def _validate_selected_files(samples: list[dict], fixture_dir: Path) -> None:
    """Check only the four selected private PDFs, without logging their bytes."""
    for sample in samples:
        path = fixture_dir / sample["filename"]
        expected_hash = sample.get("source_sha256")
        if not path.is_file() or not expected_hash:
            raise ValueError("日期探針缺少經指紋核實的原件")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError("日期探針原件指紋不符")


def _read_joint_dates(doc: fitz.Document, zoom: float) -> dict[str, date | None]:
    """Let OCR compare handwriting while transcribing all three boxes separately.

    No Asana dates or reviewed answers are included in the prompt.  Similar
    handwriting is a visual aid, not a reason to force three equal answers.
    """
    card = nvidia_client.crop_jobsheet_field_card(
        doc, 0, FIELDS, zoom=zoom, strong=zoom >= 6.0,
    )
    prompt = (
        "This image has three separately labelled handwritten date boxes: "
        "ACTION DATE, ENGINEER SIGNATURE DATE, CUSTOMER SIGNATURE DATE. "
        "Transcribe each box independently in DD/MM/YYYY order. The dates "
        "may be different. You may compare the writer's digit shapes across "
        "boxes, but do not copy a value merely to make them agree. Use null "
        "for an unreadable box; do not invent missing digits. Ignore stamps, "
        "printed date-format headings, names and other boxes. Return JSON "
        'only: {"action_date":null,"engineer_date":null,"customer_date":null}. '
        "Replace null with the visible date string for each readable box."
    )
    raw = nvidia_client._call_vision(prompt, card, max_tokens=220, expects_json=True)
    parsed = nvidia_client._parse_json_object(raw, required_keys=set(_JOINT_KEYS))
    if any(value is not None and not isinstance(value, str)
           for key, value in parsed.items() if key in _JOINT_KEYS):
        raise nvidia_client.NvidiaResponseError("日期並排複核格式錯誤")
    return {
        field: nvidia_client._parse_action_date(parsed[key])
        for key, field in _JOINT_KEYS.items()
    }


def _majority_date(reading: dict[str, date | None]) -> date | None:
    valid = [value for value in reading.values() if value is not None]
    counts = Counter(valid)
    if not counts:
        return None
    day, count = counts.most_common(1)[0]
    return day if count >= 2 else None


def probe_sample_joint(sample: dict, fixture_dir: Path) -> dict:
    """Anonymous joint-card experiment; no result can select an Asana task."""
    expected = _reviewed_day(sample)
    nvidia_client.reset_ocr_metrics()
    nvidia_client.reset_model_availability()
    fields = {field: [] for field in FIELDS}
    components = {field: [] for field in FIELDS}
    majority_days = []
    with fitz.open(fixture_dir / sample["filename"]) as doc:
        if doc.page_count < 1:
            raise ValueError("日期探針不能讀取空白 PDF")
        for zoom in ZOOMS:
            try:
                reading = _read_joint_dates(doc, zoom)
            except nvidia_client.NvidiaResponseError:
                reading = {field: None for field in FIELDS}
            for field in FIELDS:
                fields[field].append(_read_status(reading[field], expected))
                components[field].append(_component_flags(reading[field], expected))
            majority_days.append(_majority_date(reading))
    agreed = (len(majority_days) == 2 and majority_days[0] is not None
              and majority_days[0] == majority_days[1])
    majority_result = (
        _read_status(majority_days[0], expected) if agreed else "UNRESOLVED"
    )
    metrics = nvidia_client.get_ocr_metrics()
    return {
        "sample_id": sample["sample_id"],
        "joint_fields": fields,
        "joint_components": components,
        "two_render_majority": majority_result,
        "calls": metrics["calls"],
        "seconds": round(metrics["seconds"], 2),
        "tokens": metrics["total_tokens"],
        "cost_upper_cny": metrics.get("estimated_cost_cny_upper"),
    }


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


def probe_sample_cross_provider(sample: dict, fixture_dir: Path) -> dict:
    """Compare ACTION DATE using the three already configured vision models.

    This is a read-only experiment, not an automatic correction. In particular,
    NVIDIA's fallback is called explicitly as a third reader. Automatic model
    fallback is disabled so an outage cannot silently change any reader's
    identity. No reader sees the private answer or an Asana candidate date.
    """
    expected = _reviewed_day(sample)
    statuses = {}
    components = {}
    agreed_dates = {}
    usage = {}
    llama_model = config.NVIDIA_FALLBACK_MODEL
    if not llama_model or llama_model == config.NVIDIA_MODEL:
        raise ValueError("日期探針需要獨立設定的 NVIDIA 後備模型")
    with fitz.open(fixture_dir / sample["filename"]) as doc:
        if doc.page_count < 1:
            raise ValueError("日期探針不能讀取空白 PDF")
        for reader, provider, model in (
            ("deepseek", "deepseek", config.NVIDIA_MODEL),
            ("nvidia_nemotron", "nvidia", config.NVIDIA_MODEL),
            ("nvidia_llama", "nvidia", llama_model),
        ):
            nvidia_client.reset_ocr_metrics()
            nvidia_client.reset_model_availability()
            values = []
            with patch.object(config, "OCR_PROVIDER", provider), \
                    patch.object(config, "NVIDIA_MODEL", model), \
                    patch.object(config, "NVIDIA_FALLBACK_MODEL", ""):
                for zoom in ZOOMS:
                    try:
                        values.append(_read_date(doc, "service_date_raw", zoom))
                    except nvidia_client.NvidiaResponseError:
                        values.append(None)
                usage[reader] = nvidia_client.get_ocr_metrics()
            statuses[reader] = [_read_status(value, expected) for value in values]
            components[reader] = [_component_flags(value, expected) for value in values]
            agreed_dates[reader] = (
                values[0] if len(values) == 2 and values[0] is not None
                and values[0] == values[1] else None
            )
    cross_agreed = (agreed_dates["deepseek"] is not None
                    and agreed_dates["deepseek"] == agreed_dates["nvidia_nemotron"])
    return {
        "sample_id": sample["sample_id"],
        "reader_statuses": statuses,
        "reader_components": components,
        "cross_provider_agreement": (
            _read_status(agreed_dates["deepseek"], expected)
            if cross_agreed else "UNRESOLVED"
        ),
        "usage": usage,
    }


def run() -> int:
    fixture_dir = Path(os.environ["JOBSHEET_BACKTEST_DIR"])
    manifest = Path(os.environ["JOBSHEET_BACKTEST_MANIFEST"])
    report = Path(os.environ["JOBSHEET_DATE_PROBE_REPORT"])
    samples = _load_manifest(manifest)
    selected = {sample["sample_id"]: sample for sample in samples
                if sample["sample_id"] in SAMPLES}
    if set(selected) != set(SAMPLES):
        raise ValueError("日期探針缺少指定樣本")
    _validate_selected_files(list(selected.values()), fixture_dir)
    mode = os.environ.get("JOBSHEET_DATE_PROBE_MODE", "single")
    if mode not in {"single", "joint", "cross_provider"}:
        raise ValueError("日期探針模式不正確")
    rows = []
    for sample_id in SAMPLES:
        probe = {
            "single": probe_sample,
            "joint": probe_sample_joint,
            "cross_provider": probe_sample_cross_provider,
        }[mode]
        row = probe(selected[sample_id], fixture_dir)
        rows.append(row)
        log.info("[%s] 日期欄位只讀檢查完成：%s", sample_id,
                 row.get("joint_fields") or row.get("fields")
                 or row.get("reader_statuses"))
    temporary = report.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(run())

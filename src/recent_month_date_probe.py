"""Read-only A/B test of a bounded recent-month hint for handwritten PM dates.

The reviewed answers are used only after all image calls, for anonymous scoring.
This probe never calls Asana, moves Drive files, or writes OneDrive. A month
reading is not permission to choose a historical task.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from datetime import date, timedelta
from pathlib import Path

import fitz
from PIL import Image, ImageDraw

from . import backtest, nvidia_client, pm_crosspage_identity_probe


SAMPLES = ("B04", "B06", "B07", "B10", "B03", "B16", "B17")
HARD = frozenset(SAMPLES[:4])
FIELDS = frozenset({"action_month", "customer_month", "checklist_month", "selected_month"})


def _parse_as_of(raw: str) -> date:
    return date.fromisoformat(raw)


def _window(as_of: date) -> tuple[date, set[str]]:
    start = as_of - timedelta(days=70)
    months = set()
    current = start.replace(day=1)
    while current <= as_of:
        months.add(f"{current.month:02d}")
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, months


def _month(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.isdigit() or len(value) > 2:
        return None
    number = int(value)
    return f"{number:02d}" if 1 <= number <= 12 else None


def _image(doc: fitz.Document, zoom: float) -> str:
    """Put three independently written date boxes in one labelled image."""
    panels = (
        ("FIRST PAGE: ACTION DATE", nvidia_client.crop_jobsheet_field_card(
            doc, 0, ("service_date_raw",), zoom=zoom, focused=True,
            strong=zoom >= 6.0)),
        ("FIRST PAGE: CUSTOMER SIGNATURE DATE", nvidia_client.crop_jobsheet_field_card(
            doc, 0, ("customer_signed_date",), zoom=zoom,
            strong=zoom >= 6.0)),
        ("PM CHECKLIST PAGE 1: DATE", pm_crosspage_identity_probe._panel_image(
            doc, 1, pm_crosspage_identity_probe.DATE, zoom)),
    )
    decoded = [(label, Image.open(io.BytesIO(base64.b64decode(value))).convert("RGB"))
               for label, value in panels]
    width = max(image.width for _, image in decoded) + 40
    gap = 16
    height = sum(image.height + 38 for _, image in decoded) + gap * (len(decoded) + 1)
    card = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(card)
    y = gap
    for label, image in decoded:
        draw.rectangle((8, y, width - 8, y + image.height + 38), outline="black", width=2)
        draw.text((20, y + 10), label, fill="black")
        card.paste(image, ((width - image.width) // 2, y + 34))
        y += image.height + 38 + gap
    output = io.BytesIO()
    card.save(output, format="JPEG", quality=92)
    return base64.b64encode(output.getvalue()).decode("ascii")


def _prompt(*, recent: bool, start: date, as_of: date, months: set[str]) -> str:
    text = (
        "The image contains three separately handwritten dates. Read ONLY the "
        "month digits between the date separators in each labelled panel. "
        "Compare the visible pen strokes across panels; a panel may be blank "
        "or obscured. Do not copy the printed field label as an answer. "
        "For selected_month, give your best visually supported month only; "
        "if the handwriting does not distinguish it, use null. Copy each "
        "panel independently even when its reading conflicts with the others. "
    )
    if recent:
        text += (
            f"For this controlled test only, the review date is {as_of.isoformat()} "
            f"and the plausible 70-day window begins {start.isoformat()}. "
            f"Months {', '.join(sorted(months))} are possible. This is only a "
            "weak prior, not an answer. In particular, handwritten 8 and 9 "
            "can look similar: compare their actual strokes in the repeated "
            "boxes. Do not choose a month merely because it is the newest. "
        )
    text += (
        'Return JSON only with strings of one or two digits or null: '
        '{"action_month":null,"customer_month":null,'
        '"checklist_month":null,"selected_month":null}'
    )
    return text


def _read(image_b64: str, prompt: str) -> dict[str, str | None]:
    raw = nvidia_client._call_vision(
        prompt=prompt, image_b64=image_b64, max_tokens=160, expects_json=True)
    values = nvidia_client._parse_json_object(raw, required_keys=FIELDS)
    return {key: _month(values[key]) for key in FIELDS}


def _grade(reading: dict[str, str | None] | None, expected: str,
           allowed_months: set[str]) -> dict:
    if reading is None:
        return {"readable": False, "correct": False, "supported_correct": False,
                "outside_window": False}
    chosen = reading["selected_month"]
    support = sum(reading[key] == chosen for key in
                  ("action_month", "customer_month", "checklist_month")) if chosen else 0
    return {
        "readable": chosen is not None,
        "correct": chosen == expected,
        "supported_correct": chosen == expected and support >= 1,
        "outside_window": chosen is not None and chosen not in allowed_months,
        "panel_support_count": support,
    }


def run() -> dict:
    fixture_dir = Path(os.environ.get("JOBSHEET_BACKTEST_DIR", "/tmp/jobsheet-backtest"))
    manifest_path = Path(os.environ.get(
        "JOBSHEET_BACKTEST_MANIFEST", str(fixture_dir / "manifest-reviewed-20260925.json")))
    as_of = _parse_as_of(os.environ.get("JOBSHEET_RECENCY_AS_OF", "2026-09-27"))
    start, months = _window(as_of)
    samples = {row["sample_id"]: row for row in backtest._load_manifest(manifest_path)}

    # Verify the complete experiment before making a billable model call.
    for sample_id in SAMPLES:
        sample = samples[sample_id]
        source = fixture_dir / sample["filename"]
        if (sample.get("review_status") != "confirmed" or
                sample.get("job_type") != "PM" or
                sample.get("observed_fields", {}).get("date_source") != "ACTION_DATE" or
                not sample.get("source_sha256") or not source.is_file()):
            raise backtest.BacktestError(f"{sample_id} 私人答案或原件未核實")
        if hashlib.sha256(source.read_bytes()).hexdigest() != sample["source_sha256"]:
            raise backtest.BacktestError(f"{sample_id} PDF 指紋不符")
        expected = date.fromisoformat(sample["observed_fields"]["service_date_raw"])
        if not start <= expected <= as_of:
            raise backtest.BacktestError(f"{sample_id} 不在模擬日期視窗內")
        with fitz.open(source) as doc:
            if doc.page_count < 2:
                raise backtest.BacktestError(f"{sample_id} 無 PM 檢查表首頁")

    rows = []
    for sample_id in SAMPLES:
        sample = samples[sample_id]
        expected_month = date.fromisoformat(
            sample["observed_fields"]["service_date_raw"]).strftime("%m")
        nvidia_client.reset_model_availability()
        nvidia_client.reset_ocr_metrics()
        grades = {"neutral": [], "recent": []}
        with fitz.open(fixture_dir / sample["filename"]) as doc:
            for zoom in (5.0, 6.0):
                image_b64 = _image(doc, zoom)
                for kind in ("neutral", "recent"):
                    try:
                        reading = _read(image_b64, _prompt(
                            recent=kind == "recent", start=start, as_of=as_of,
                            months=months))
                    except (nvidia_client.NvidiaResponseError, ValueError, json.JSONDecodeError):
                        reading = None
                    grades[kind].append(_grade(reading, expected_month, months))
        metrics = nvidia_client.get_ocr_metrics()
        rows.append({
            "sample_id": sample_id, "cohort": "hard" if sample_id in HARD else "control",
            "neutral": grades["neutral"], "recent": grades["recent"],
            "calls": int(metrics.get("calls") or 0),
            "cost_cny_upper": metrics.get("estimated_cost_cny_upper"),
        })

    report = {"as_of": as_of.isoformat(), "window_days": 70,
              "original_upload_dates_known": False, "rows": rows}
    output = Path(os.environ.get("JOBSHEET_RECENT_MONTH_REPORT", "/tmp/recent-month-anonymous.json"))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## 最近月份提示：私人日期辨認 A/B 試驗\n\n")
            stream.write("舊樣本原始上傳日未知；2026-09-27 是模擬參考日，不是來源證據。\n\n")
            stream.write("| 樣本 | 無提示兩輪正確 | 最近月份提示兩輪正確 | 模型呼叫 |\n")
            stream.write("|---|---:|---:|---:|\n")
            for row in rows:
                neutral = sum(item["supported_correct"] for item in row["neutral"])
                recent = sum(item["supported_correct"] for item in row["recent"])
                stream.write(f"| {row['sample_id']} | {neutral}/2 | {recent}/2 | {row['calls']} |\n")
            stream.write("\n只測日期辨認，沒有選 Asana 工作或寫入雲端。\n")
    return report


if __name__ == "__main__":
    run()

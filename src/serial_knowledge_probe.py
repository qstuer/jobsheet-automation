"""Read-only A/B check of handwritten serials on two private held-out PDFs.

The index row being graded is excluded from advisory knowledge before either
model call. Answers are used only after each call for anonymous scoring; no
OCR text, contact data or device identifiers enter the public report.
"""

import hashlib
import json
import os
from pathlib import Path

import fitz

from . import asana_client, asana_index, backtest, config, nvidia_client
from . import vision_experiment, vision_knowledge

SAMPLE_IDS = ("B07", "B10")


def _grade_reading(reading: dict | None, expected_row: dict, job_type: str) -> dict:
    if reading is None:
        return {"present": False}
    scored = asana_client._score_index_device(
        expected_row, asana_client._prepare_index_query(reading), job_type
    )
    return {
        "present": True,
        "serial_distance": scored["serial_dist"] if scored["serial_dist"] < 99 else None,
        "serial_exact": scored["serial_dist"] == 0,
        "product_match": scored["product_similarity"] == 1.0,
        "hospital_match": scored["hospital_similarity"] == 1.0,
    }


def _grade_arm(arm: dict, expected_row: dict, job_type: str) -> dict:
    metrics = arm.get("metrics") or {}
    graded = {
        "status": arm.get("status"),
        "calls": int(metrics.get("calls") or 0),
        "tokens": int(metrics.get("total_tokens") or 0),
        "cost_cny_upper": metrics.get("estimated_cost_cny_upper"),
    }
    if arm.get("status") == "read":
        graded["transcription"] = _grade_reading(
            arm["reading"]["transcription"], expected_row, job_type
        )
        graded["assisted"] = _grade_reading(
            arm["reading"]["assisted"], expected_row, job_type
        )
    return graded


def _probe_asset(doc, expected_ref: dict) -> dict:
    """B07 only: does the separate Dept./Room crop recover the visible tag?"""
    results = []
    for zoom in config.OCR_FOCUSED_RETRY_ZOOMS:
        nvidia_client.reset_ocr_metrics()
        try:
            reading = nvidia_client.ocr_jobsheet_focused_field(
                doc, 0, "department_room_raw", zoom=zoom
            )
            exact = asana_client._asset_match_level(
                reading.get("asset_candidates"), expected_ref.get("assets")
            ) == 2
            status = "read"
        except nvidia_client.NvidiaResponseError:
            exact, status = False, "failed"
        metrics = nvidia_client.get_ocr_metrics()
        results.append({
            "status": status, "asset_exact_visit": exact,
            "calls": int(metrics.get("calls") or 0),
            "tokens": int(metrics.get("total_tokens") or 0),
            "cost_cny_upper": metrics.get("estimated_cost_cny_upper"),
        })
    return {"passes": results,
            "independent_exact_pair": all(row["asset_exact_visit"] for row in results)}


def run() -> list[dict]:
    folder = Path(os.environ.get("JOBSHEET_BACKTEST_DIR", "/tmp/jobsheet-backtest"))
    manifest = Path(os.environ.get(
        "JOBSHEET_BACKTEST_MANIFEST", str(folder / "manifest-reviewed-20260925.json")
    ))
    samples = {row["sample_id"]: row for row in backtest._load_manifest(manifest)}
    index = asana_index.load_index(
        Path(os.environ.get("ASANA_INDEX_LOCAL_FILE", "/tmp/asana-device-index.json")),
        Path(os.environ.get("ASANA_INDEX_MANIFEST_LOCAL_FILE",
                            "/tmp/asana-device-index-manifest.json")),
    )
    # All private inputs and holdouts must be valid before the first paid call.
    selected = []
    for sample_id in SAMPLE_IDS:
        sample = samples[sample_id]
        source = folder / sample["filename"]
        if (sample.get("review_status") != "confirmed"
                or sample.get("expected", {}).get("kind") != "match"
                or not source.is_file()
                or hashlib.sha256(source.read_bytes()).hexdigest()
                != sample.get("source_sha256")):
            raise backtest.BacktestError(f"{sample_id} private input mismatch")
        serial = asana_client._norm(sample["expected"]["serial"])
        rows = [row for row in index["devices"]
                if asana_client._norm(row.get("serial")) == serial]
        if len(rows) != 1:
            raise backtest.BacktestError(f"{sample_id} index row is not unique")
        refs = [ref for ref in rows[0].get("task_refs") or []
                if str(ref.get("gid")) == sample["expected"]["task_gid"]]
        if len(refs) != 1:
            raise backtest.BacktestError(f"{sample_id} historical visit is not unique")
        knowledge = vision_knowledge.build_knowledge(index, excluded_serials=(serial,))
        reference = vision_knowledge.prompt_reference(knowledge)
        if serial in json.dumps(reference, ensure_ascii=False).upper():
            raise backtest.BacktestError(f"{sample_id} holdout leaked into advisory prompt")
        selected.append((sample_id, sample, rows[0], refs[0], knowledge))

    results = []
    for sample_id, sample, row, ref, knowledge in selected:
        nvidia_client.reset_model_availability()
        with fitz.open(folder / sample["filename"]) as doc:
            if doc.page_count != (4 if sample["job_type"] == "PM" else 1):
                raise backtest.BacktestError(f"{sample_id} page count mismatch")
            comparison = vision_experiment.compare(
                doc, knowledge, guided_first=(sample_id == "B10")
            )
            result = {
                "sample_id": sample_id,
                "same_image_both_arms": True,
                "guided_first": comparison["guided_first"],
                "baseline": _grade_arm(comparison["arms"]["baseline"], row,
                                       sample["job_type"]),
                "guided": _grade_arm(comparison["arms"]["guided"], row,
                                     sample["job_type"]),
            }
            if sample_id == "B07":
                result["asset_crop"] = _probe_asset(doc, ref)
        results.append(result)

    report = Path(os.environ.get("JOBSHEET_SERIAL_REPORT", "/tmp/serial-probe-anonymous.json"))
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as stream:
            stream.write("## Serial 圖片知識 A/B（只讀）\n\n")
            stream.write("匿名結果在限時 artifact；不顯示任何原文、答案或圖片。\n")
    return results


if __name__ == "__main__":
    run()

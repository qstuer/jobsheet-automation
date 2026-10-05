"""Bounded production-code check: four new PDFs, no cloud mutations or answers."""
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import fitz

from src import asana_client, asana_index, config, nvidia_client, processor, rclone_helper


def main():
    job = int(os.environ["BATCH_JOB"])
    if job not in (1, 2, 3, 4):
        raise ValueError("Only the four explicitly authorized PDFs may be tested")
    filename = f"20261005072226_001__job{job}_PM.pdf"
    trace = []

    class SafeTrace(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            for phrase, category in (
                ("設備索引沒有候選", "index_miss_live_fallback"),
                ("已鎖定一部設備", "device_locked_live_tasks_read"),
                ("所有歷史工作都與 ACTION DATE", "task_date_outside_window"),
                ("serial 模糊但多欄核對命中", "fuzzy_serial_multifield_match"),
                ("Asana 多欄核對命中", "multifield_match"),
                ("類型衝突", "project_type_conflict_filtered"),
                ("保留待核對", "ambiguous_candidate_review"),
                ("分格複核後仍沒有唯一", "no_unique_task"),
            ):
                if phrase in message and category not in trace:
                    trace.append(category)

    logging.disable(logging.NOTSET)
    logging.getLogger().handlers.clear()
    logging.getLogger().addHandler(SafeTrace())
    logging.getLogger().setLevel(logging.INFO)

    # A second guard below the caller: even an accidental finalization must fail.
    original_run_result = rclone_helper.run_result

    def read_only_run(*args):
        if not args or args[0] not in {"lsf", "lsjson", "copyto"}:
            raise RuntimeError("Cloud mutation blocked by read-only test")
        if args[0] == "copyto":
            if not str(args[1]).startswith("googledrive:") or ":" in str(args[2]):
                raise RuntimeError("Only Drive-to-runner downloads are allowed")
        return original_run_result(*args)

    rclone_helper.run_result = read_only_run
    out = Path("anonymous-results")
    out.mkdir(exist_ok=True)
    report = {"job": job, "read_only": True, "cloud_writes": 0,
              "code_base": "858d17de2eb203343c9d08dd2cb5f187a2352a63",
              "provider": config.OCR_PROVIDER, "model": (
                  config.DEEPSEEK_MODEL if config.OCR_PROVIDER == "deepseek"
                  else config.NVIDIA_MODEL)}
    started = time.monotonic()
    print(f"Job {job}: starting blind read-only production-code check", flush=True)
    try:
        index = asana_index.load_index(Path("/tmp/asana-device-index.json"),
                                      Path("/tmp/asana-device-index-manifest.json"))
        asana_client._task_cache.clear()
        asana_client.clear_device_index()
        asana_client.set_device_index(index)
        report["index_generated_at"] = index.get("generated_at")
        report["index_device_count"] = index.get("device_count")
        with tempfile.TemporaryDirectory(prefix="new_batch_readonly_") as folder:
            local = Path(folder) / filename
            rclone_helper.download(f"{config.GDRIVE_SPLIT}/{filename}", local)
            key = hashlib.sha256(local.read_bytes()).digest()

            def receipt(value):
                # Keyed proof; no customer field, serial, filename or task GID is
                # published. The reviewer compares with the independent local
                # answer after the run, never providing that answer to OCR.
                normalized = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
                return hmac.new(key, normalized.encode(), hashlib.sha256).hexdigest()

            with fitz.open(local) as doc:
                report["page_count"] = len(doc)
                if len(doc) != 4:
                    raise ValueError("Expected a complete four-page PM result")
                task, tier, ocr = processor._ocr_and_match(doc, "PM")
            report["matched"] = task is not None
            report["tier"] = tier
            report["field_receipts"] = {}
            # Use the real production OCR schema, not display-column aliases.
            # Lists retain each independently transcribed candidate as a proof.
            for field in ("hospital_raw", "department_room_raw", "product_raw",
                          "serial_candidates", "serial_visual_candidates",
                          "phone_candidates", "contact_person_raw", "asset_candidates",
                          "service_date_raw", "date_source", "order_no"):
                value = ocr.get(field)
                values = value if isinstance(value, list) else [value] if value else []
                report["field_receipts"][field] = {
                    "present": bool(values), "receipts": [receipt(item) for item in values]}
            if task:
                planned, order_no = processor._planned_filename(task)
                report["task_receipt"] = receipt(task.get("gid"))
                report["filename_receipt"] = receipt(planned)
                report["has_order_number"] = bool(order_no)
    except Exception as exc:
        # API exception text can include payloads/URLs; publish only its class.
        report["error_type"] = type(exc).__name__
        report["matched"] = False
    finally:
        metrics = nvidia_client.get_ocr_metrics()
        report["metrics"] = {field: metrics.get(field) for field in (
            "calls", "seconds", "total_tokens", "estimated_cost_cny_upper")}
        report["elapsed_seconds"] = round(time.monotonic() - started, 1)
        report["trace"] = trace
        report["measurement_schema"] = 2
        (out / f"job{job}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    # A green run means the measurement finished, NOT that its match is correct.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

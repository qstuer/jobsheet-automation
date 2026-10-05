"""Three blind repetitions of authorized PDFs; no answer inputs/cloud writes."""
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import tempfile
import time
from datetime import date
from pathlib import Path

import fitz

from src import asana_client, asana_index, config, matching_rules, nvidia_client, processor, rclone_helper


def main():
    job = int(os.environ["BATCH_JOB"])
    if job not in (1, 2, 3, 4):
        raise ValueError("PDF outside authorized batch")
    reference_day = date.fromisoformat(os.environ["JOBSHEET_ORIGINAL_UPLOAD_DATE"])
    filename = f"20261005072226_001__job{job}_PM.pdf"
    # Sources are already safely delivered; DO NOT put them back in _SPLIT.
    archive = "googledrive:From_BrotherDevice/.jobsheet-control/completed-reviewed/20261005072226_001"
    logging.getLogger().handlers = [logging.NullHandler()]
    original_run = rclone_helper.run_result
    def readonly(*args):
        if not args or args[0] not in {"lsf", "lsjson", "copyto"}:
            raise RuntimeError("Cloud mutation blocked")
        if args[0] == "copyto" and (not str(args[1]).startswith("googledrive:") or ":" in str(args[2])):
            raise RuntimeError("Only Drive-to-runner downloads allowed")
        return original_run(*args)
    rclone_helper.run_result = readonly
    os.environ[processor.DRY_RUN_ENV] = "1"
    os.environ["JOBSHEET_EVIDENCE_LIVE_RULES"] = "1"
    output = Path("anonymous-results")
    output.mkdir(exist_ok=True)
    index = asana_index.load_index(Path("/tmp/asana-device-index.json"),
                                  Path("/tmp/asana-device-index-manifest.json"))
    reports = []
    with tempfile.TemporaryDirectory(prefix="evidence_rules_readonly_") as folder:
        local = Path(folder) / filename
        rclone_helper.download(f"{archive}/{filename}", local)
        key = hashlib.sha256(local.read_bytes()).digest()
        def proof(value):
            normalized = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
            return hmac.new(key, normalized.encode(), hashlib.sha256).hexdigest()
        original_call = nvidia_client._call_vision_once
        signature = inspect.signature(original_call)
        attempts = []
        def audited_call(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            model = bound.arguments["model"]
            try:
                result = original_call(*args, **kwargs)
                attempts.append({"model": model, "response": True})
                return result
            except Exception:
                attempts.append({"model": model, "response": False})
                raise
        nvidia_client._call_vision_once = audited_call
        for repetition in range(1, 4):
            report = {"job": job, "round": repetition, "read_only": True,
                "cloud_writes": 0, "ruleset": matching_rules.RULESET_VERSION,
                "commit": os.environ.get("GITHUB_SHA"), "provider": config.OCR_PROVIDER,
                "reference_date_source": "original_batch_upload",
                "reference_date": reference_day.isoformat(),
                "source_sha256": key.hex(), "index_generated_at": index.get("generated_at"),
                "live_product_parser_version": 1}
            attempts.clear()
            asana_client._task_cache.clear()
            asana_client._typeahead_cache.clear()
            asana_client.clear_device_index()
            asana_client.set_device_index(index)
            nvidia_client.reset_model_availability()
            nvidia_client.reset_ocr_metrics()
            started = time.monotonic()
            try:
                with fitz.open(local) as doc:
                    report["page_count"] = len(doc)
                    task, tier, ocr = processor._ocr_and_match(doc, "PM")
                report["matched"] = task is not None
                report["audit"] = ocr.get("rules_audit")
                report["field_receipts"] = {}
                for field in ("hospital_raw", "product_raw", "serial_candidates", "serial_visual_candidates",
                              "phone_candidates", "asset_candidates", "service_date_raw"):
                    value = ocr.get(field)
                    values = value if isinstance(value, list) else [value] if value else []
                    report["field_receipts"][field] = [proof(item) for item in values]
                if task:
                    planned, order = processor._planned_filename(task)
                    record = asana_index.task_to_record(task)
                    report.update({"task_receipt": proof(task.get("gid")),
                        "filename_receipt": proof(planned),
                        "device_receipt": proof(record.get("serial")),
                        "has_order_number": bool(order),
                        "project_type": asana_client._task_job_type(task)})
            except Exception as exc:
                report.update({"matched": False, "error_type": type(exc).__name__})
            finally:
                report["elapsed_seconds"] = round(time.monotonic() - started, 1)
                metrics = nvidia_client.get_ocr_metrics()
                report["metrics"] = {k: metrics.get(k) for k in (
                    "calls", "seconds", "total_tokens", "estimated_cost_cny_upper")}
                report["model_attempts"] = list(attempts)
                reports.append(report)
                (output / f"job{job}-round{repetition}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(json.dumps(report), flush=True)
    return 0  # Measurement completion is not a correctness score.


if __name__ == "__main__":
    raise SystemExit(main())

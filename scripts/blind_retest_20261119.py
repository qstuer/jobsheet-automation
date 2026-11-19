"""One fresh production OCR/matching pass; no expected-answer input or cloud writes.

The production modules are checked out at a fixed SHA in a separate directory.
Only private-source-keyed anonymous receipts leave the ephemeral runner. An
independent local grader compares them AFTER all four samples have finished.
"""
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

sys.path.insert(0, str(Path("production").resolve()))

import fitz
import httpx
import requests

from src import asana_client, asana_index, config, nvidia_client, pdf_utils, processor, rclone_helper
from readonly_guard import ReadOnlyGuard

PRODUCTION_SHA = "7c1b1fa0ae8518da47406a74e3cb6a4efc0377ca"
RAW_NAME = "20261119092353_001.pdf"
logging.getLogger().handlers = [logging.NullHandler()]


def main():
    assert os.environ.get("PRODUCTION_SHA") == PRODUCTION_SHA
    assert os.environ.get(processor.MATCHING_RULES_ENV) == "evidence-live-v2"
    assert os.environ.get(processor.DRY_RUN_ENV) == "true"
    assert os.environ.get("JOBSHEET_ORDER_REVIEW_MODE") == "off"
    assert config.OCR_PROVIDER == "nvidia"
    for variable in (processor.CONFIRMED_FILENAME_ENV, processor.REVIEW_ACTION_DATE_ENV,
                     processor.REVIEW_TASK_ENV, "JOBSHEET_ORIGINAL_UPLOAD_DATE",
                     "JOBSHEET_EVIDENCE_LIVE_RULES"):
        assert not os.environ.get(variable), "Answer/date override is forbidden"

    counters = {"cloud_write_attempts": 0, "input_downloads": 0,
                "asana_gets": 0, "vision_posts": 0}
    results = {"production_sha": PRODUCTION_SHA, "single_pass": True,
               "expected_answers_supplied": False, "jobs": [], "guards": counters}
    out = Path("anonymous-results")
    out.mkdir(exist_ok=True)

    nvidia_host = urlparse(config.NVIDIA_BASE_URL).hostname
    guard = ReadOnlyGuard(counters, vision_host=nvidia_host)
    request_allowed = guard.request_allowed

    # Exercise the policy without issuing a request.
    assert request_allowed("GET", "https://app.asana.com/api/1.0/tasks/example")
    assert not request_allowed("PUT", "https://app.asana.com/api/1.0/tasks/example")
    assert not request_allowed("POST", "https://www.googleapis.com/drive/v3/files")
    assert not request_allowed("PUT", "https://graph.microsoft.com/v1.0/drives/example")

    original_request = requests.sessions.Session.request
    def readonly_request(session, method, url, **kwargs):
        if not request_allowed(method, url):
            return guard.reject_network(method)
        if urlparse(str(url)).hostname == "app.asana.com":
            counters["asana_gets"] += 1
        return original_request(session, method, url, **kwargs)
    requests.sessions.Session.request = readonly_request

    original_send = httpx.Client.send
    def guarded_send(client, request, **kwargs):
        if not request_allowed(request.method, str(request.url)):
            return guard.reject_network(request.method)
        if request.url.host == nvidia_host:
            counters["vision_posts"] += 1
        return original_send(client, request, **kwargs)
    httpx.Client.send = guarded_send

    for name in ("upload", "upload_unique", "delete", "moveto"):
        setattr(rclone_helper, name, guard.reject_cloud_write)
    processor._review_uploaded_job = guard.reject_cloud_write
    processor._save_result = guard.reject_cloud_write
    processor._finalize_match = guard.reject_cloud_write
    processor._finalize_confirmed = guard.reject_cloud_write
    processor._move_to_pending_unique = guard.reject_cloud_write
    subprocess.Popen = guard.popen
    results["guard_policy_self_checks"] = True

    with tempfile.TemporaryDirectory(prefix="jobsheet_blind_20261119_") as folder:
        temp = Path(folder).resolve()
        source = temp / RAW_NAME
        index_file = temp / "index.json"
        manifest_file = temp / "index-manifest.json"
        try:
            # Use the same dedicated Drive credentials as production. Only
            # these three exact Drive-to-runner copies can start a subprocess.
            raw_remote=f'googledrive:{RAW_NAME}'
            raw_options=('--drive-root-folder-id',config.GDRIVE_FROM_BROTHER_FOLDER_ID,
                         '--drive-trashed-only')
            guard.allow_drive_download(raw_remote,source,*raw_options)
            guard.allow_drive_download(config.GDRIVE_ASANA_INDEX_JSON,index_file)
            guard.allow_drive_download(config.GDRIVE_ASANA_INDEX_MANIFEST,manifest_file)
            rclone_helper.run('copyto',raw_remote,str(source),*raw_options)
            rclone_helper.download(config.GDRIVE_ASANA_INDEX_JSON,index_file)
            rclone_helper.download(config.GDRIVE_ASANA_INDEX_MANIFEST,manifest_file)
            index = asana_index.load_index(index_file, manifest_file)
            key = hashlib.sha256(source.read_bytes()).digest()
            def tag(label, value):
                message = f"jobsheet-blind-20261119-v1\0{label}\0{value}".encode()
                return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]

            results["source_identity_receipt"] = tag("source", "original-scan")
            with fitz.open(source) as doc:
                results["source_pages"] = len(doc)
            results["reference_day"] = nvidia_client._today().isoformat()
            jobs = pdf_utils.split_jobs(source)
            results["split_jobs"] = len(jobs)
            # Production Stage B starts with fresh caches after Stage A.
            nvidia_client.reset_model_availability()
            asana_client._task_cache.clear()
            asana_client._typeahead_cache.clear()
            asana_client.clear_device_index()
            asana_client.set_device_index(index)
            for number, job in enumerate(jobs, 1):
                row = {"job": number, "type": job["type"], "complete": job["complete"],
                       "keep_pages": job["keep_pages"], "matched": False}
                started = time.monotonic()
                try:
                    if not job["complete"]:
                        row["reason"] = "incomplete_scan"
                    else:
                        local = temp / f"sample-{number}.pdf"
                        pdf_utils.extract_pages(source, job["keep_pages"], local)
                        with fitz.open(local) as doc:
                            task, tier, ocr = processor._ocr_and_match(
                                doc, job["type"], reference_day=nvidia_client._today())
                        row["reason"] = (ocr.get("rules_audit") or {}).get("reason")
                        row["ocr_calls"] = ocr.get("ocr_metrics", {}).get("calls")
                        if task:
                            filename, _ = processor._planned_filename(task)
                            row.update(matched=True, tier=tier,
                                task_receipt=tag(f"job-{number}-task", task.get("gid")),
                                filename_receipt=tag(f"job-{number}-filename", filename))
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                row["seconds"] = round(time.monotonic() - started, 2)
                results["jobs"].append(row)
                print("BLIND_JOB_RESULT " + json.dumps(row), flush=True)
            results["measurements_complete"] = True
            guard.assert_clean()
        except Exception as exc:
            results["fatal_error_type"] = type(exc).__name__
            return 1
        finally:
            (out / "four-sheet-measurements.json").write_text(
                json.dumps(results, indent=2) + "\n", encoding="utf-8")
            print("BLIND_BATCH_RESULT " + json.dumps(results), flush=True)


if __name__ == "__main__":
    raise SystemExit(main() or 0)

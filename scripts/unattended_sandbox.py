"""One uninterrupted production A/B pass; only file transport is simulated.

The sandbox has no Drive/OneDrive credentials. It uses real NVIDIA inference
and live GET-only Asana calls. No expected answers enter this process.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import fnmatch
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

import fitz
import httpx
import requests

from src import asana_index, config, nvidia_client, pdf_utils, processor, rclone_helper, splitter


class LocalCloud:
    """Implement the existing rclone command boundary on a confined disk tree."""
    def __init__(self, root, source_created_at=None):
        self.root = Path(root).resolve()
        self.source_created_at = source_created_at
        self.events = []
        self.blocked = 0

    def path(self, value):
        text = str(value)
        if text.startswith(("googledrive:", "onedrive:")):
            remote, tail = text.split(":", 1)
            parts = PurePosixPath(tail).parts
            if any(p in {"..", "/"} for p in parts) or "\\" in tail:
                self.blocked += 1
                raise AssertionError("Unsafe virtual remote path")
            candidate = self.root / "cloud" / remote / tail
        else:
            candidate = Path(value)
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            self.blocked += 1
            raise AssertionError("File operation escaped sandbox")
        return resolved

    def command(self, *args):
        cmd = args[0]
        args = list(args)
        rc, output = 0, ""
        if cmd == "lsf":
            folder = self.path(args[1])
            pattern = args[args.index("--include") + 1] if "--include" in args else "*"
            if not folder.is_dir():
                rc = 3
            else:
                output = "".join(p.name + "\n" for p in sorted(folder.iterdir())
                    if p.is_file() and fnmatch.fnmatchcase(p.name, pattern))
        elif cmd == "lsjson":
            value = next(a for a in args[1:] if a.startswith(("googledrive:", "onedrive:")))
            path = self.path(value)
            if not path.exists():
                rc = 3
            else:
                row = {"Path": path.name, "Name": path.name, "IsDir": path.is_dir(),
                       "Size": path.stat().st_size}
                if "--metadata" in args:
                    row["Metadata"] = {"btime": self.source_created_at}
                output = json.dumps(row)
        elif cmd in {"copyto", "moveto"}:
            source, target = self.path(args[1]), self.path(args[2])
            target.parent.mkdir(parents=True, exist_ok=True)
            if cmd == "copyto":
                shutil.copyfile(source, target)
            else:
                shutil.move(str(source), str(target))
        elif cmd == "deletefile":
            self.path(args[1]).unlink()
        elif cmd == "hashsum" and args[1] == "SHA-256":
            value = next(a for a in args[2:] if a.startswith(("googledrive:", "onedrive:")))
            source = self.path(value)
            output = hashlib.sha256(source.read_bytes()).hexdigest() + "  " + source.name + "\n"
        else:
            self.blocked += 1
            raise AssertionError("Unimplemented transport operation; fail closed")
        self.events.append({"operation": cmd, "returncode": rc})
        return subprocess.CompletedProcess(["virtual-rclone", *args], rc, output, "")


class NetworkGuard:
    def __init__(self):
        self.asana_get = 0
        self.nvidia_post = 0
        self.blocked = 0

    def validate(self, method, url):
        parsed = urlsplit(str(url))
        if parsed.scheme == "https" and method.upper() == "GET" \
                and parsed.hostname == "app.asana.com" and parsed.path.startswith("/api/1.0/"):
            self.asana_get += 1
        elif parsed.scheme == "https" and method.upper() == "POST" \
                and parsed.hostname == "integrate.api.nvidia.com" \
                and parsed.path == "/v1/chat/completions":
            self.nvidia_post += 1
        else:
            self.blocked += 1
            raise AssertionError("Unexpected network operation blocked")


def source_tag(source, label, value):
    key = hashlib.sha256(Path(source).read_bytes()).digest()
    message = f"jobsheet-selection-v1\0{label}\0{value}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:24]


def execute(source, index_file, index_manifest, context):
    # No per-paper dates, names, tasks, or single-file selectors may be supplied.
    for name in (processor.TARGET_FILE_ENV, processor.CONFIRMED_FILENAME_ENV,
                 processor.REVIEW_ACTION_DATE_ENV, processor.REVIEW_TASK_ENV,
                 "JOBSHEET_EVIDENCE_LIVE_RULES"):
        assert not os.environ.get(name), "Manual override supplied"
    assert os.environ[processor.MATCHING_RULES_ENV] == "evidence-live-v2"
    assert os.environ.get("OCR_PROVIDER") == "nvidia"
    assert os.environ.get(processor.DRY_RUN_ENV, "false") == "false"
    index = asana_index.load_index(index_file, index_manifest)
    assert index["generated_at"] == "2026-11-06T07:15:42.749143Z"
    with fitz.open(source) as doc:
        assert len(doc) == 16
    original_tempdir = tempfile.tempdir
    captured_jobs, ocr_calls = [], {}
    original_split, original_match = pdf_utils.split_jobs, processor._ocr_and_match

    def observe_split(*args, **kwargs):
        result = original_split(*args, **kwargs)
        captured_jobs.extend(result)
        return result

    def observe_match(doc, kind, *args, **kwargs):
        filename = Path(doc.name).name
        ocr_calls[filename] = ocr_calls.get(filename, 0) + 1
        result = original_match(doc, kind, *args, **kwargs)
        print("UNATTENDED_EVENT " + json.dumps({"event": "job_matching_finished",
            "ordinal": len(ocr_calls), "matched": result[0] is not None}), flush=True)
        return result

    with tempfile.TemporaryDirectory(prefix="unattended_") as folder:
        root = Path(folder).resolve()
        work = root / "work"
        work.mkdir()
        tempfile.tempdir = str(work)
        cloud = LocalCloud(root, context.get("source_created_at"))
        network = NetworkGuard()
        incoming = cloud.path(config.GDRIVE_INPUT)
        incoming.mkdir(parents=True)
        raw_name = "20261116091149_001.pdf"
        shutil.copyfile(source, incoming / raw_name)
        for location in (config.GDRIVE_SPLIT, config.GDRIVE_PENDING, config.GDRIVE_REPORTS,
                         config.GDRIVE_SPLIT_FAILED, config.GDRIVE_INCOMPLETE,
                         config.GDRIVE_INCOMPLETE_RAW, config.ONEDRIVE_OUTPUT):
            cloud.path(location).mkdir(parents=True, exist_ok=True)
        assert not any(cloud.path(config.ONEDRIVE_OUTPUT).iterdir())
        assert not Path.home().joinpath(".config/rclone/rclone.conf").exists()
        assert not any(name.startswith("RCLONE_CONFIG") for name in os.environ)
        os.environ[processor.ASANA_INDEX_FILE_ENV] = str(index_file)
        os.environ[processor.ASANA_INDEX_MANIFEST_ENV] = str(index_manifest)
        os.environ[processor.SOURCE_QUEUE_ENV] = "split"
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
        request_send = requests.sessions.Session.send
        httpx_send = httpx.Client.send

        def requests_readonly(session, request, *args, **kwargs):
            network.validate(request.method, request.url)
            return request_send(session, request, *args, **kwargs)

        def inference_only(client, request, *args, **kwargs):
            network.validate(request.method, request.url)
            return httpx_send(client, request, *args, **kwargs)

        def no_process(*args, **kwargs):
            network.blocked += 1
            raise AssertionError("External subprocess blocked")

        old_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            with ExitStack() as guards:
                guards.enter_context(patch.object(rclone_helper, "run_result", side_effect=cloud.command))
                guards.enter_context(patch.object(subprocess, "Popen", side_effect=no_process))
                guards.enter_context(patch.object(requests.sessions.Session, "send", requests_readonly))
                guards.enter_context(patch.object(httpx.Client, "send", inference_only))
                guards.enter_context(patch("urllib.request.urlopen", side_effect=no_process))
                guards.enter_context(patch.object(pdf_utils, "split_jobs", side_effect=observe_split))
                guards.enter_context(patch.object(processor, "_ocr_and_match", side_effect=observe_match))
                print('UNATTENDED_EVENT {"event":"stage_a_started"}', flush=True)
                stage_a = splitter.main()
                stage_b = None
                split_count = len(list(cloud.path(config.GDRIVE_SPLIT).glob("*.pdf")))
                print("UNATTENDED_EVENT " + json.dumps({"event": "stage_a_finished",
                    "exit": stage_a, "split_count": split_count}), flush=True)
                if stage_a == 0 and split_count:
                    print('UNATTENDED_EVENT {"event":"automatic_stage_b_handoff"}', flush=True)
                    stage_b = processor.main()
        finally:
            logging.disable(old_disable)
            tempfile.tempdir = original_tempdir
        reports = list(cloud.path(config.GDRIVE_REPORTS).rglob("*.json"))
        manifest = json.loads(reports[0].read_text(encoding="utf-8")) if len(reports) == 1 else {}
        rows = []
        for row in manifest.get("jobs", []):
            index = row["index"]
            filename, gid = row.get("onedrive"), row.get("asana_task_gid")
            output = cloud.path(f"{config.ONEDRIVE_OUTPUT}/{filename}") if filename else None
            pixels_equal = None
            pages = None
            kept = captured_jobs[index - 1]["keep_pages"]
            if output and output.is_file():
                with fitz.open(source) as original, fitz.open(output) as archived:
                    pages = len(archived)
                    pixels_equal = pages == len(kept) and all(
                        archived[n].get_pixmap(matrix=fitz.Matrix(.5, .5)).samples
                        == original[page].get_pixmap(matrix=fitz.Matrix(.5, .5)).samples
                        for n, page in enumerate(kept))
            receipt = {"task": source_tag(source, "asana-task-gid", gid),
                       "filename": source_tag(source, "planned-filename", filename)} if gid and filename else None
            rows.append({"sample": f"B{index:02d}", "type": row["type"], "state": row["state"],
                "matching_entry_calls": ocr_calls.get(row["file"], 0),
                "kept_source_pages": [p + 1 for p in kept], "pages": pages,
                "pixels_equal": pixels_equal, "receipt": receipt})
        result = {"stage_a_exit": stage_a, "stage_b_exit": stage_b, "stage_a_runs": 1,
            "stage_b_runs": int(stage_b is not None), "jobs": rows,
            "source_receipt": source_tag(source, "sandbox-source", "raw-16-page-scan"),
            "initial_index_generated_at": index_file and "2026-11-06T07:15:42.749143Z",
            "processing_day_hong_kong": nvidia_client._today().isoformat(),
            "initial_destination_files": 0,
            "final_destination_files": len(list(cloud.path(config.ONEDRIVE_OUTPUT).glob("*.pdf"))),
            "final_input_files": len(list(incoming.glob("*.pdf"))),
            "final_split_files": len(list(cloud.path(config.GDRIVE_SPLIT).glob("*.pdf"))),
            "final_pending_files": len(list(cloud.path(config.GDRIVE_PENDING).glob("*.pdf"))),
            "manifest_final": manifest.get("final"), "action_required": manifest.get("action_required"),
            "real_asana_get_requests": network.asana_get,
            "real_nvidia_post_requests": network.nvidia_post,
            "blocked_network_or_process_attempts": network.blocked,
            "blocked_path_or_transport_attempts": cloud.blocked,
            "virtual_file_transport_calls": len(cloud.events),
            "virtual_file_writes": sum(e["operation"] in {"copyto", "moveto", "deletefile"} for e in cloud.events),
            "real_cloud_file_writes": 0, "manual_overrides": 0,
            "drive_onedrive_credentials_present": False,
            "human_interventions_during_pass": 0,
            "completed_at_utc": datetime.now(timezone.utc).isoformat()}
        print("UNATTENDED_RESULT " + json.dumps(result), flush=True)
        passed = (stage_a == stage_b == 0 and len(rows) == 4
            and result["final_destination_files"] == 4 and result["manifest_final"]
            and not result["action_required"] and not result["final_input_files"]
            and not result["final_split_files"] and not result["final_pending_files"]
            and not network.blocked and not cloud.blocked
            and all(r["state"] == "uploaded" and r["matching_entry_calls"] == 1
                    and r["pixels_equal"] for r in rows))
        return 0 if passed else 1


if __name__ == "__main__":
    try:
        base = Path(os.environ["SANDBOX_PREP"])
        raise SystemExit(execute(base / "source.pdf", base / "index.json",
            base / "manifest.json", json.loads((base / "context.json").read_text(encoding="utf-8"))))
    except Exception as exc:
        print("UNATTENDED_FAILURE " + type(exc).__name__, flush=True)
        raise SystemExit(1) from None

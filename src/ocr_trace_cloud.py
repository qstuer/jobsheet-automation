"""Test-branch-only encrypted diagnosis; three downloads, no cloud mutations."""
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess

from .private_ocr_trace import PRIVATE_ROOT, main as capture_main


def download_id(file_id, destination):
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", file_id):
        raise ValueError("Invalid Drive source ID")
    destination = Path(destination).resolve()
    if not destination.is_relative_to(PRIVATE_ROOT.resolve()) or destination.exists():
        raise ValueError("Download destination must be a new private local file")
    # The destination is an absolute local path, never an rclone remote. This
    # is Drive -> runner only. stdout/stderr may contain names; do not print.
    result = subprocess.run(["rclone", "backend", "copyid", "googledrive:",
        file_id, str(destination)], capture_output=True, timeout=300)
    if result.returncode or not destination.is_file():
        raise RuntimeError("Private source download failed")


def main():
    # No normal matcher/client log reaches a public diagnostic run.
    previous_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if (os.environ.get("GITHUB_REF") != "refs/heads/codex/parser-ingress-fix-20261013"
                or os.environ.get("JOBSHEET_DRY_RUN") != "1"
                or os.environ.get("OCR_PROVIDER") != "nvidia"):
            raise ValueError("Encrypted diagnosis requires the approved read-only test branch")
        expected = os.environ["OCR_TRACE_SOURCE_SHA256"]
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Missing source fingerprint")
        key = base64.b64decode(os.environ["OCR_TRACE_RECIPIENT_KEY"], validate=True)
        from .private_trace_crypto import load_recipient
        load_recipient(key)  # Fail before downloads or model calls for bad keys.
        inputs = PRIVATE_ROOT / "cloud-inputs"
        inputs.mkdir(parents=True, exist_ok=False)
        pdf, index, manifest, public = [inputs / name for name in (
            "source.pdf", "index.json", "manifest.json", "recipient.public.pem")]
        public.write_bytes(key)
        for variable, destination in (("OCR_TRACE_SOURCE_ID", pdf),
                ("OCR_TRACE_INDEX_ID", index), ("OCR_TRACE_MANIFEST_ID", manifest)):
            download_id(os.environ[variable], destination)
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != expected:
            raise ValueError("Source fingerprint mismatch; no OCR call permitted")
        return capture_main(["--pdf", str(pdf), "--index", str(index),
            "--index-manifest", str(manifest), "--job-type", os.environ["OCR_TRACE_JOB_TYPE"],
            "--reference-date", os.environ["OCR_TRACE_REFERENCE_DATE"],
            "--output", str(PRIVATE_ROOT / "cloud"), "--recipient-public-key", str(public),
            "--allow-model-calls", "--live-asana"])
    except Exception as exc:
        print(json.dumps({"status": "DIAGNOSIS_STOPPED", "error_type": type(exc).__name__,
                          "drive_writes": 0, "onedrive_writes": 0}))
        return 3
    finally:
        logging.disable(previous_logging_level)


if __name__ == "__main__":
    raise SystemExit(main())

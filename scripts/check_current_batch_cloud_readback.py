"""Read four exact OneDrive paths against an ephemeral private checksum spec."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import fitz

from src import config, selection_audit


def download(remote, destination):
    result = subprocess.run(["rclone", "copyto", remote, str(destination),
        "--retries", "1", "--low-level-retries", "2", "--contimeout", "15s",
        "--timeout", "30s"], capture_output=True, timeout=60)
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").lower()
        print("CLOUD_DOWNLOAD_FAILURE " + json.dumps({
            "local_label": destination.name, "remote_kind": remote.split(":", 1)[0],
            "exit": result.returncode, "http_codes": sorted(set(re.findall(r"\b[45]\d\d\b", error))),
            "categories": [tag for tag in ("quota", "rate limit", "directory not found",
                "object not found", "invalid_grant", "expired", "access denied", "timeout",
                "connection", "unauthorized", "file not found") if tag in error],
        }), flush=True)
        raise RuntimeError("Read-only cloud download failed")


def main():
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        spec = json.loads(os.environ["JOBSHEET_READBACK_SPEC"])
        assert spec["source_pages"] == 16 and len(spec["jobs"]) == 4
        for index, kind in enumerate(("PM", "CM", "PM", "CM"), 1):
            sample = f"B{index:02d}"
            row = next(r for r in spec["jobs"] if r["sample"] == sample)
            assert row["type"] == kind
            filename = row["filename"]
            assert Path(filename).name == filename and filename.endswith(".pdf")
            archived = work / f"{sample}_archived.pdf"
            download(f"{config.ONEDRIVE_OUTPUT}/{filename}", archived)
            assert hashlib.sha256(archived.read_bytes()).hexdigest() == row["sha256"]
            with fitz.open(archived) as doc:
                assert len(doc) == (4 if kind == "PM" else 1)
                pages = len(doc)
            result = {"sample": sample, "pages": pages,
                "size": archived.stat().st_size, "hash_equal": True, "cloud_writes": 0,
                "receipt": selection_audit.receipt(archived,
                    task_gid=row["task_gid"], filename=filename)}
            print("CLOUD_READBACK_RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("CLOUD_READBACK_FAILURE " + type(exc).__name__, flush=True)
        raise SystemExit(1) from None

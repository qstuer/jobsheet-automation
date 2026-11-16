"""Read only the current batch report, four fixtures and four archived files."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

import fitz

from src import batch_state, config, selection_audit


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
        report = work / "batch.json"
        download(batch_state.report_remote("20261116091149_001.pdf"), report)
        manifest = json.loads(report.read_text(encoding="utf-8"))
        assert manifest["source_file"] == "20261116091149_001.pdf"
        assert manifest["source_pages"] == 16
        assert manifest["final"] and not manifest["action_required"]
        assert len(manifest["jobs"]) == 4
        for index, kind in enumerate(("PM", "CM", "PM", "CM"), 1):
            row = next(r for r in manifest["jobs"] if r["index"] == index)
            assert row["type"] == kind and row["state"] in {"uploaded", "already_exists"}
            filename = row["onedrive"]
            assert Path(filename).name == filename and filename.endswith(".pdf")
            sample = f"B{index:02d}"
            source = work / f"{sample}_source.pdf"
            archived = work / f"{sample}_archived.pdf"
            download(f"{config.GDRIVE_CONTROL}/stale-index-regression-20261116/{sample}_{kind}.pdf", source)
            download(f"{config.ONEDRIVE_OUTPUT}/{filename}", archived)
            assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(archived.read_bytes()).digest()
            with fitz.open(archived) as doc:
                assert len(doc) == (4 if kind == "PM" else 1)
                pages = len(doc)
            result = {"sample": sample, "pages": pages,
                "size": archived.stat().st_size, "hash_equal": True, "cloud_writes": 0,
                "receipt": selection_audit.receipt(source,
                    task_gid=row["asana_task_gid"], filename=filename)}
            print("CLOUD_READBACK_RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("CLOUD_READBACK_FAILURE " + type(exc).__name__, flush=True)
        raise SystemExit(1) from None

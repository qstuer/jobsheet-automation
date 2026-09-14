"""Google Drive 上的耐久批次狀態。

每個原始掃描只有一份 JSON。Stage A 寫入切頁及完整性結果；Stage B 更新
配對／上傳結果。GitHub runner 消失後仍可知道每份工作單停在哪一步。
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, rclone_helper


SCHEMA_VERSION = 1
TERMINAL_STATES = {
    "uploaded", "already_exists", "versioned", "incomplete", "pending",
}
ACTION_STATES = {"versioned", "incomplete", "pending"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def source_stem_from_job(filename: str) -> Optional[str]:
    match = re.match(r"^(.+)__job\d+_(?:CM|PM)\.pdf$", filename, re.IGNORECASE)
    return match.group(1) if match else None


def report_name(source_file_or_stem: str) -> str:
    stem = Path(source_file_or_stem).stem
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)
    return f"{safe}.json"


def report_remote(source_file_or_stem: str) -> str:
    return f"{config.GDRIVE_REPORTS}/{report_name(source_file_or_stem)}"


def new_manifest(source_file: str, source_pages: int, jobs: list) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "source_file": source_file,
        "source_pages": source_pages,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "final": False,
        "action_required": any(not job.get("complete", True) for job in jobs),
        "jobs": [
            {
                "index": index,
                "file": job["output_name"],
                "type": job["type"],
                "input_pages": job["input_pages"],
                "content_pages": len(job["keep_pages"]),
                "expected_content_pages": job["expected_content_pages"],
                "state": "split" if job.get("complete", True) else "incomplete",
                "reason": job.get("incomplete_reason"),
                "attempts": 0,
            }
            for index, job in enumerate(jobs, 1)
        ],
    }


def load(work_dir: Path, source_file_or_stem: str) -> Optional[dict]:
    remote = report_remote(source_file_or_stem)
    if rclone_helper.remote_stat(remote) is None:
        return None
    local = work_dir / report_name(source_file_or_stem)
    rclone_helper.download(remote, local)
    try:
        value = json.loads(local.read_text(encoding="utf-8"))
    finally:
        local.unlink(missing_ok=True)
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
        raise ValueError(f"批次狀態格式不正確：{remote}")
    return value


def save(work_dir: Path, manifest: dict) -> None:
    manifest["updated_at"] = utc_now()
    source = manifest["source_file"]
    local = work_dir / report_name(source)
    local.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        rclone_helper.upload(local, report_remote(source))
    finally:
        local.unlink(missing_ok=True)


def find_job(manifest: dict, filename: str) -> Optional[dict]:
    return next((job for job in manifest.get("jobs", []) if job.get("file") == filename), None)


def record_result(manifest: dict, filename: str, state: str, **details) -> dict:
    job = find_job(manifest, filename)
    if job is None:
        job = {
            "index": len(manifest.get("jobs", [])) + 1,
            "file": filename,
            "type": "PM" if filename.upper().endswith("_PM.PDF") else "CM",
            "attempts": 0,
        }
        manifest.setdefault("jobs", []).append(job)
    job["state"] = state
    job.update({key: value for key, value in details.items() if value is not None})
    manifest["action_required"] = any(
        row.get("state") in ACTION_STATES for row in manifest.get("jobs", [])
    )
    manifest["final"] = bool(manifest.get("jobs")) and all(
        row.get("state") in TERMINAL_STATES for row in manifest.get("jobs", [])
    )
    return job


def ensure_legacy_manifest(filename: str) -> dict:
    stem = source_stem_from_job(filename) or Path(filename).stem
    return {
        "schema": SCHEMA_VERSION,
        "source_file": f"{stem}.pdf",
        "source_pages": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "final": False,
        "action_required": False,
        "jobs": [],
    }


def finalize_ready_manifests(work_dir: Path) -> int:
    """完成只有缺頁、或 Stage B 已全部終結的批次報告。"""
    changed = 0
    for filename in rclone_helper.list_files(config.GDRIVE_REPORTS, "*.json"):
        manifest = load(work_dir, Path(filename).stem)
        if not manifest or manifest.get("final") or not manifest.get("jobs"):
            continue
        if all(job.get("state") in TERMINAL_STATES for job in manifest["jobs"]):
            manifest["final"] = True
            manifest["action_required"] = any(
                job.get("state") in ACTION_STATES for job in manifest["jobs"]
            )
            save(work_dir, manifest)
            changed += 1
    return changed

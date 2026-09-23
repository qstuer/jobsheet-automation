"""Run a private, read-only batch backtest against known jobsheets.

The manifest and PDFs live in Google Drive's private control folder.  Public
Actions output contains only anonymous sample IDs and non-sensitive usage
metrics.  This module deliberately calls the OCR/Asana matching core directly;
it never imports a cloud source queue and never uploads, moves, or deletes a
Google Drive or OneDrive file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

import fitz

from . import asana_client, asana_index, nvidia_client, processor, rclone_helper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

BACKTEST_DIR_ENV = "JOBSHEET_BACKTEST_DIR"
BACKTEST_MANIFEST_ENV = "JOBSHEET_BACKTEST_MANIFEST"
INDEX_FILE_ENV = "ASANA_INDEX_LOCAL_FILE"
INDEX_MANIFEST_ENV = "ASANA_INDEX_MANIFEST_LOCAL_FILE"
_SAMPLE_ID_RE = re.compile(r"B(?:0[1-9]|1[0-9]|20)")


class BacktestError(RuntimeError):
    """The private fixture set is missing or unsafe to execute."""


def _load_manifest(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestError("私人回測清單無法讀取") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") not in (1, 2) or not isinstance(payload.get("samples"), list):
        raise BacktestError("私人回測清單格式不正確")

    samples = payload["samples"]
    if len(samples) != 20:
        raise BacktestError("回測必須剛好包含 20 份工作單")
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise BacktestError("私人回測樣本格式不正確")
        sample_id = str(sample.get("sample_id") or "")
        filename = str(sample.get("filename") or "")
        expected = sample.get("expected")
        if not _SAMPLE_ID_RE.fullmatch(sample_id) or sample_id in seen_ids:
            raise BacktestError("回測 sample_id 必須是唯一的 B01–B20")
        if (not filename or any(c in filename for c in '/\\:')
                or Path(filename).name != filename or Path(filename).suffix.lower() != ".pdf"):
            raise BacktestError(f"{sample_id} 的檔名不安全")
        if filename in seen_files or sample.get("job_type") not in {"PM", "CM"}:
            raise BacktestError(f"{sample_id} 的檔名或工作類型不正確")
        source_hash = sample.get("source_sha256")
        if source_hash is not None and (
            not isinstance(source_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", source_hash)
        ):
            raise BacktestError(f"{sample_id} 的原件指紋格式不正確")
        if not isinstance(expected, dict) or expected.get("kind") not in {
            "order", "serial", "pending", "match", "unreviewed"
        }:
            raise BacktestError(f"{sample_id} 缺少可核對的私人答案")
        if expected["kind"] in {"order", "serial"} and not str(expected.get("value") or "").strip():
            raise BacktestError(f"{sample_id} 的私人答案為空")
        # Old serial/order-only fixtures remain useful diagnostics, never a
        # full pass. Do not turn the matcher's own answer into ground truth.
        sample["_verified_answer"] = False
        if payload["schema_version"] == 2:
            if sample.get("review_status") not in {"confirmed", "unreviewed"}:
                raise BacktestError(f"{sample_id} 缺少答案覆核狀態")
            if "reference_date" not in sample:
                raise BacktestError(f"{sample_id} 缺少原始日期欄位（未知請填 null）")
            ref_date = sample["reference_date"]
            if ref_date is not None:
                try:
                    if date.fromisoformat(ref_date).isoformat() != ref_date:
                        raise ValueError
                except (ValueError, TypeError):
                    raise BacktestError(f"{sample_id} 的原始日期格式不正確") from None
                if sample.get("reference_date_source") not in {"original_upload", "scan_record"}:
                    raise BacktestError(f"{sample_id} 缺少原始日期來源")
            confirmed = sample["review_status"] == "confirmed"
            if confirmed:
                if not isinstance(sample.get("review_note"), str) or not sample["review_note"].strip():
                    raise BacktestError(f"{sample_id} 缺少私人覆核依據")
                if expected["kind"] not in {"match", "pending"}:
                    raise BacktestError(f"{sample_id} 不能只靠 Serial 或訂單號標記已確認")
                if expected["kind"] == "match":
                    if not all(isinstance(expected.get(k), str) and expected[k].strip()
                               for k in ("serial", "task_gid", "filename")):
                        raise BacktestError(f"{sample_id} 缺少設備、工作或檔名答案")
                    if (not expected["task_gid"].isdigit()
                            or not expected["filename"].endswith(".pdf")
                            or any(c in expected["filename"] for c in '/\\:*?"<>|')):
                        raise BacktestError(f"{sample_id} 的工作或檔名答案格式不正確")
            sample["_verified_answer"] = confirmed
        seen_ids.add(sample_id)
        seen_files.add(filename)
    return samples


def _validate_files(samples: list[dict], fixture_dir: Path) -> None:
    """Reject accidental binary duplicates before spending OCR calls."""
    hashes: set[str] = set()
    for sample in samples:
        path = fixture_dir / sample["filename"]
        if not path.is_file():
            raise BacktestError(f"{sample['sample_id']} 的 PDF 不存在")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        # A reused anonymous filename can point to another scan. Bind reviewed
        # answers to the actual PDF, not a cached preview with the same label.
        if sample.get("source_sha256") and sample["source_sha256"] != digest:
            raise BacktestError(f"{sample['sample_id']} 的 PDF 已變更，必須重新覆核答案")
        if digest in hashes:
            raise BacktestError("20 份回測資料含有完全重複的 PDF")
        hashes.add(digest)


def _matches_expected(task: dict | None, expected: dict) -> bool:
    """Exact task and filename are required; serial alone cannot prove the month."""
    kind = expected["kind"]
    if kind == "pending":
        return task is None
    if task is None or kind != "match":
        return False
    expected_serial = asana_client._norm(expected["serial"])
    return (str(task.get("gid")) == expected["task_gid"]
            and processor._planned_filename(task)[0] == expected["filename"]
            and expected_serial in {
        asana_client._norm(serial) for serial in asana_client._task_serials(task)
    })


def _evaluate_result(task: dict | None, sample: dict, page_count: int,
                     detected_type: str | None = None) -> dict:
    """Return only anonymous checks, never task data or expected answers."""
    expected = sample["expected"]
    serial = expected.get("serial") or (
        expected.get("value") if expected["kind"] == "serial" else None
    )
    def check(value: bool) -> str:
        return "PASS" if value else "FAIL"
    device = "NOT_REVIEWED" if not serial else check(bool(task) and asana_client._norm(serial) in {
        asana_client._norm(s) for s in asana_client._task_serials(task)
    })
    exact_task = "NOT_REVIEWED"
    filename = "NOT_REVIEWED"
    task_type = "NOT_TESTED" if task is None else check(
        asana_client._task_job_type(task) == sample["job_type"]
    )
    if expected["kind"] == "match":
        exact_task = check(bool(task) and str(task.get("gid")) == expected.get("task_gid"))
        filename = check(bool(task) and processor._planned_filename(task)[0] == expected.get("filename"))
    count_ok = page_count == (4 if sample["job_type"] == "PM" else 1)
    circle = "NOT_TESTED" if detected_type is None else check(detected_type == sample["job_type"])
    if not sample.get("_verified_answer"):
        status = "UNVERIFIED"
    elif circle == "FAIL" or not _matches_expected(task, expected) or (task is not None and task_type != "PASS"):
        status = "FAIL"
    elif not count_ok:
        status = "DIAGNOSTIC_ONLY"
    else:
        status = "PASS"
    return {
        "status": status, "device_check": device, "task_check": exact_task,
        "filename_check": filename, "task_type_check": task_type,
        "page_count": page_count,
        "page_check": "COUNT_ONLY" if count_ok else "INCOMPLETE_OR_UNEXPECTED",
        # A page count does not validate the checklist or the splitter.
        "circle_check": circle, "field_accuracy": "NOT_TESTED",
        "full_pipeline_check": "NOT_TESTED",
    }


def audit_fixtures(samples: list[dict], fixture_dir: Path) -> list[dict]:
    """Offline readiness inventory. No model, Asana or cloud calls."""
    _validate_files(samples, fixture_dir)
    seen_tasks: dict[str, str] = {}
    rows = []
    for sample in samples:
        try:
            with fitz.open(fixture_dir / sample["filename"]) as doc:
                pages = doc.page_count
        except Exception:
            raise BacktestError(f"{sample['sample_id']} 的 PDF 無法讀取") from None
        if pages < 1:
            raise BacktestError(f"{sample['sample_id']} 是空白 PDF")
        expected = sample["expected"]
        gid = expected.get("task_gid") if sample.get("_verified_answer") else None
        duplicate = seen_tasks.get(gid) if gid else None
        if gid:
            seen_tasks.setdefault(gid, sample["sample_id"])
        rows.append({
            "sample_id": sample["sample_id"], "pages": pages,
            "expected_pages": 4 if sample["job_type"] == "PM" else 1,
            "answer_ready": bool(sample.get("_verified_answer")),
            "reference_date_known": bool(sample.get("reference_date")),
            "same_work_as": duplicate,
            "business_identity_known": bool(gid),
        })
    return rows


def _append_summary(rows: list[dict], path: Path) -> None:
    passed = sum(row["status"] == "PASS" for row in rows)
    pending_expected = sum(row["expected_pending"] for row in rows)
    unverified = sum(row["status"] == "UNVERIFIED" for row in rows)
    repeated_work = sum(bool(row.get("same_work_as")) for row in rows)
    unknown_work = sum(not row.get("business_identity_known") for row in rows)
    calls = sum(row["calls"] for row in rows)
    seconds = sum(row["seconds"] for row in rows)
    tokens = sum(row["tokens"] for row in rows)
    costs = [row["cost"] for row in rows if row["cost"] is not None]
    with path.open("a", encoding="utf-8") as stream:
        stream.write("## Jobsheet 20 份私人只讀回測\n\n")
        stream.write(
            f"严格配对通过：**{passed}/{len(rows)}**；未核验答案：{unverified}；预期安全待确认：{pending_expected}；"
            "OneDrive 写入：**0**。\n\n"
        )
        stream.write("这不是全流程通过率：圈选独立列出；逐栏准确率、checklist 内容与切页尚未验收。页数正确也不代表完整。\n\n")
        stream.write(f"重复工作样本：{repeated_work}；工作身份未确认：{unknown_work}。不同 PDF 不等于不同工作。\n\n")
        stream.write("| 样本 | 结果 | 设备 | 工作 | 名称 | Asana类型 | 圈选 | 页数检查 | OCR 呼叫 | 耗时 | Tokens | 费用上限 |\n")
        stream.write("|---|---|---|---|---|---|---|---|---:|---:|---:|---:|\n")
        for row in rows:
            cost = f"RMB {row['cost']:.4f}" if row["cost"] is not None else "-"
            stream.write(
                f"| {row['sample_id']} | {row['status']} | "
                f"{row.get('device_check', 'NOT_REVIEWED')} | {row.get('task_check', 'NOT_REVIEWED')} | "
                f"{row.get('filename_check', 'NOT_REVIEWED')} | {row.get('task_type_check', 'NOT_TESTED')} | "
                f"{row.get('circle_check', 'NOT_TESTED')} | "
                f"{row.get('page_check', 'NOT_TESTED')} | {row['calls']} | "
                f"{row['seconds']:.1f}s | {row['tokens']} | {cost} |\n"
            )
        total_cost = f"RMB {sum(costs):.4f}" if costs else "-"
        stream.write(
            f"\n合计：{calls} 次图片呼叫、{seconds:.1f} 秒、{tokens} tokens、"
            f"费用上限 {total_cost}。公开报告不含客户、电话、地点、Serial 或任务资料。\n"
        )


def _read_sample(doc, expected_type: str) -> tuple:
    """Expected type is an evaluation gate, never an input to vision/matching."""
    nvidia_client.reset_ocr_metrics()
    detected = nvidia_client.detect_cm_pm(doc, 0)
    metrics = nvidia_client.get_ocr_metrics()
    if detected not in {"CM", "PM"} or detected != expected_type:
        return None, detected, metrics
    task, _tier, ocr = processor._ocr_and_match(doc, detected)
    # The matching core resets its own metrics; include the earlier circle calls.
    for key, value in (ocr.get("ocr_metrics") or {}).items():
        if isinstance(value, (int, float)):
            metrics[key] = metrics.get(key, 0) + value
    return task, detected, metrics


def _deny_cloud_operation(*args, **kwargs):
    # Fixtures/index are already local. There is no reason for this evaluation
    # process to invoke rclone or a production upload/finalization entrypoint.
    raise BacktestError("只讀回測禁止雲端檔案操作")


def _checkpoint(rows: list[dict]) -> None:
    """Persist only evaluation flags/usage; never OCR or task/answer objects."""
    target = os.environ.get("JOBSHEET_BACKTEST_REPORT", "").strip()
    if target:
        path = Path(target)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def _select_samples(samples: list[dict], selection: str) -> list[dict]:
    """Private test branch can run a tiny subset before the full 20-sample retry."""
    if not selection.strip():
        return samples
    requested = [part.strip() for part in selection.split(",")]
    available = {sample["sample_id"] for sample in samples}
    if (not all(requested) or len(requested) != len(set(requested))
            or set(requested) - available):
        raise BacktestError("回測樣本選擇無效")
    return [sample for sample in samples if sample["sample_id"] in requested]


def run() -> list[dict]:
    fixture_dir = Path(os.environ.get(BACKTEST_DIR_ENV, "/tmp/jobsheet-backtest"))
    manifest_path = Path(
        os.environ.get(BACKTEST_MANIFEST_ENV, str(fixture_dir / "manifest.json"))
    )
    samples = _load_manifest(manifest_path)
    inventory = {row["sample_id"]: row for row in audit_fixtures(samples, fixture_dir)}
    samples = _select_samples(samples, os.environ.get("JOBSHEET_BACKTEST_SAMPLE_IDS", ""))

    index_path = Path(os.environ.get(INDEX_FILE_ENV, "/tmp/asana-device-index.json"))
    index_manifest = Path(
        os.environ.get(INDEX_MANIFEST_ENV, "/tmp/asana-device-index-manifest.json")
    )
    asana_client.set_device_index(asana_index.load_index(index_path, index_manifest))

    rows: list[dict] = []
    for sample in samples:
        sample_id = sample["sample_id"]
        log.info("[%s] 开始只读 OCR + Asana 核对", sample_id)
        with fitz.open(fixture_dir / sample["filename"]) as doc:
            if doc.page_count < 1:
                raise BacktestError(f"{sample_id} 是空白 PDF")
            page_count = doc.page_count
            task, detected, metrics = _read_sample(doc, sample["job_type"])
        evaluation = _evaluate_result(task, sample, page_count, detected)
        status = evaluation["status"]
        log.info("[%s] %s（客户资料已隐藏）", sample_id, status)
        rows.append({
            "sample_id": sample_id,
            **evaluation,
            "same_work_as": inventory[sample_id]["same_work_as"],
            "business_identity_known": inventory[sample_id]["business_identity_known"],
            "expected_pending": sample.get("_verified_answer", False) and sample["expected"]["kind"] == "pending",
            "calls": int(metrics.get("calls") or 0),
            "seconds": float(metrics.get("seconds") or 0),
            "tokens": int(metrics.get("total_tokens") or 0),
            "cost": metrics.get("estimated_cost_cny_upper"),
        })
        _checkpoint(rows)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        _append_summary(rows, Path(summary_path))
    passed = sum(row["status"] == "PASS" for row in rows)
    log.info(
        "严格配对通过=%s/%s；答案未核验=%s；不是全流程验收；OneDrive 写入=0",
        passed, len(rows), sum(row["status"] == "UNVERIFIED" for row in rows),
    )
    return rows


def main() -> int:
    try:
        if sys.argv[1:] == ["--audit-fixtures"]:
            fixture_dir = Path(os.environ.get(BACKTEST_DIR_ENV, "/tmp/jobsheet-backtest"))
            manifest = Path(os.environ.get(BACKTEST_MANIFEST_ENV, str(fixture_dir / "manifest.json")))
            print(json.dumps(audit_fixtures(_load_manifest(manifest), fixture_dir)))
            return 0
        if sys.argv[1:]:
            raise BacktestError("只支援 --audit-fixtures 或不帶參數執行回測")
        with ExitStack() as guards:
            guards.enter_context(patch.object(rclone_helper, "run_result", _deny_cloud_operation))
            for name in ("_finalize_match", "_finalize_confirmed", "_process_split_file"):
                guards.enter_context(patch.object(processor, name, _deny_cloud_operation))
            run()
    except BacktestError as exc:
        log.error("回测资料错误：%s", exc)
        return 1
    except Exception as exc:
        # External exceptions can include URLs, OCR text or customer details.
        # Keep completed rows in the anonymous checkpoint; no public traceback.
        log.error("回測中止（%s）；已完成結果保存在匿名紀錄，沒有雲端檔案寫入", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

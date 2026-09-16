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
from pathlib import Path

import fitz

from . import asana_client, asana_index, processor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")

BACKTEST_DIR_ENV = "JOBSHEET_BACKTEST_DIR"
BACKTEST_MANIFEST_ENV = "JOBSHEET_BACKTEST_MANIFEST"
INDEX_FILE_ENV = "ASANA_INDEX_LOCAL_FILE"
INDEX_MANIFEST_ENV = "ASANA_INDEX_MANIFEST_LOCAL_FILE"
_SAMPLE_ID_RE = re.compile(r"B\d{2}")


class BacktestError(RuntimeError):
    """The private fixture set is missing or unsafe to execute."""


def _load_manifest(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestError("私人回測清單無法讀取") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("samples"), list):
        raise BacktestError("私人回測清單格式不正確")

    samples = payload["samples"]
    if len(samples) != 20:
        raise BacktestError("回測必須剛好包含 20 份工作單")
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "")
        filename = str(sample.get("filename") or "")
        expected = sample.get("expected")
        if not _SAMPLE_ID_RE.fullmatch(sample_id) or sample_id in seen_ids:
            raise BacktestError("回測 sample_id 必須是唯一的 B01–B20")
        if Path(filename).name != filename or Path(filename).suffix.lower() != ".pdf":
            raise BacktestError(f"{sample_id} 的檔名不安全")
        if filename in seen_files or sample.get("job_type") not in {"PM", "CM"}:
            raise BacktestError(f"{sample_id} 的檔名或工作類型不正確")
        if not isinstance(expected, dict) or expected.get("kind") not in {
            "order", "serial", "pending"
        }:
            raise BacktestError(f"{sample_id} 缺少可核對的私人答案")
        if expected["kind"] != "pending" and not str(expected.get("value") or "").strip():
            raise BacktestError(f"{sample_id} 的私人答案為空")
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
        if digest in hashes:
            raise BacktestError("20 份回測資料含有完全重複的 PDF")
        hashes.add(digest)


def _matches_expected(task: dict | None, expected: dict) -> bool:
    kind = expected["kind"]
    if kind == "pending":
        return task is None
    if task is None:
        return False
    value = str(expected["value"])
    if kind == "order":
        return asana_client.extract_order_no_from_name(task) == value
    expected_serial = asana_client._norm(value)
    return expected_serial in {
        asana_client._norm(serial) for serial in asana_client._task_serials(task)
    }


def _append_summary(rows: list[dict], path: Path) -> None:
    passed = sum(row["status"] == "PASS" for row in rows)
    pending_expected = sum(row["expected_pending"] for row in rows)
    calls = sum(row["calls"] for row in rows)
    seconds = sum(row["seconds"] for row in rows)
    tokens = sum(row["tokens"] for row in rows)
    costs = [row["cost"] for row in rows if row["cost"] is not None]
    with path.open("a", encoding="utf-8") as stream:
        stream.write("## Jobsheet 20 份私人只讀回測\n\n")
        stream.write(
            f"结果：**{passed}/20**；预期安全待确认：{pending_expected}；"
            "OneDrive 写入：**0**。\n\n"
        )
        stream.write("| 样本 | 结果 | OCR 呼叫 | 耗时 | Tokens | 费用上限 |\n")
        stream.write("|---|---|---:|---:|---:|---:|\n")
        for row in rows:
            cost = f"RMB {row['cost']:.4f}" if row["cost"] is not None else "-"
            stream.write(
                f"| {row['sample_id']} | {row['status']} | {row['calls']} | "
                f"{row['seconds']:.1f}s | {row['tokens']} | {cost} |\n"
            )
        total_cost = f"RMB {sum(costs):.4f}" if costs else "-"
        stream.write(
            f"\n合计：{calls} 次图片呼叫、{seconds:.1f} 秒、{tokens} tokens、"
            f"费用上限 {total_cost}。公开报告不含客户、电话、地点、Serial 或任务资料。\n"
        )


def run() -> list[dict]:
    fixture_dir = Path(os.environ.get(BACKTEST_DIR_ENV, "/tmp/jobsheet-backtest"))
    manifest_path = Path(
        os.environ.get(BACKTEST_MANIFEST_ENV, str(fixture_dir / "manifest.json"))
    )
    samples = _load_manifest(manifest_path)
    _validate_files(samples, fixture_dir)

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
            task, _tier, ocr = processor._ocr_and_match(doc, sample["job_type"])
        metrics = ocr.get("ocr_metrics") or {}
        passed = _matches_expected(task, sample["expected"])
        status = "PASS" if passed else "FAIL"
        log.info("[%s] %s（客户资料已隐藏）", sample_id, status)
        rows.append({
            "sample_id": sample_id,
            "status": status,
            "expected_pending": sample["expected"]["kind"] == "pending",
            "calls": int(metrics.get("calls") or 0),
            "seconds": float(metrics.get("seconds") or 0),
            "tokens": int(metrics.get("total_tokens") or 0),
            "cost": metrics.get("estimated_cost_cny_upper"),
        })

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        _append_summary(rows, Path(summary_path))
    passed = sum(row["status"] == "PASS" for row in rows)
    log.info(
        "20 份回测完成：%s/20；OneDrive 写入=0；客户资料未写入公开摘要",
        passed,
    )
    return rows


def main() -> int:
    try:
        run()
    except BacktestError as exc:
        log.error("回测资料错误：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

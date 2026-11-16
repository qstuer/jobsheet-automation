"""Exercise the production reader against a frozen private index and real PDFs."""
import json
import os
from datetime import date
from pathlib import Path
from unittest.mock import patch

import fitz

from src import asana_client, asana_index, processor, rclone_helper, selection_audit


def main():
    sample = os.environ["STALE_SAMPLE"]
    kind = os.environ["STALE_JOB_TYPE"]
    assert sample in {"B01", "B02", "B03", "B04"}
    assert kind in {"CM", "PM"}
    assert os.environ["JOBSHEET_MATCHING_RULES"] == "evidence-live-v2"
    assert os.environ["JOBSHEET_DRY_RUN"] == "true"
    index = asana_index.load_index(Path("/tmp/asana-device-index.json"),
                                  Path("/tmp/asana-device-index-manifest.json"))
    assert index["generated_at"] == "2026-11-06T07:15:42.749143Z"
    asana_client.set_device_index(index)
    source = Path("/tmp/jobsheet.pdf")
    with fitz.open(source) as doc, \
            patch.object(rclone_helper, "upload_unique", side_effect=AssertionError("Cloud write blocked")), \
            patch.object(rclone_helper, "delete", side_effect=AssertionError("Cloud delete blocked")), \
            patch.object(rclone_helper, "moveto", side_effect=AssertionError("Cloud move blocked")):
        assert len(doc) == (4 if kind == "PM" else 1)
        task, tier, ocr = processor._ocr_and_match(
            doc, kind, date.fromisoformat(os.environ["JOBSHEET_ORIGINAL_UPLOAD_DATE"]))
    metrics = ocr.get("ocr_metrics") or {}
    indexed_gids = {ref["gid"] for row in index["devices"] for ref in row["task_refs"]}
    receipt = None
    if task:
        filename, _ = processor._planned_filename(task)
        receipt = selection_audit.receipt(source, task_gid=task["gid"], filename=filename)
    result = {"sample": sample, "kind": kind, "matched": task is not None,
              "tier": tier, "receipt": receipt,
              "selected_task_was_indexed": task["gid"] in indexed_gids if task else None,
              "index_generated_at": index["generated_at"],
              "calls": metrics.get("calls"), "seconds": metrics.get("seconds"),
              "tokens": metrics.get("total_tokens"), "cloud_writes": 0,
              "trace": (ocr.get("rules_audit") or {}).get("trace")}
    print("STALE_INDEX_RESULT " + json.dumps(result), flush=True)
    if not task or (kind == "CM" and result["selected_task_was_indexed"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

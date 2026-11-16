"""Exercise the production reader against a frozen private index and real PDFs."""
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import fitz

from src import asana_client, asana_index, matching_rules, nvidia_client, processor, rclone_helper, selection_audit


def without_date(reader):
    def read(*args, **kwargs):
        result = reader(*args, **kwargs)
        return {**result, "service_date_raw": None, "service_date_iso": None, "date_source": None}
    return read


def main():
    sample = os.environ["STALE_SAMPLE"]
    kind = os.environ["STALE_JOB_TYPE"]
    date_mode = os.environ.get("DATE_MODE", "normal")
    assert sample in {"B01", "B02", "B03", "B04"}
    assert kind in {"CM", "PM"}
    assert date_mode in {"normal", "missing"}
    assert os.environ["JOBSHEET_MATCHING_RULES"] == "evidence-live-v2"
    assert os.environ["JOBSHEET_DRY_RUN"] == "true"
    index = asana_index.load_index(Path("/tmp/asana-device-index.json"),
                                  Path("/tmp/asana-device-index-manifest.json"))
    assert index["generated_at"] == "2026-11-06T07:15:42.749143Z"
    asana_client.set_device_index(index)
    source = Path("/tmp/jobsheet.pdf")
    reference_day = nvidia_client._today()
    first_day, last_day = matching_rules.month_window(reference_day)
    with fitz.open(source) as doc, ExitStack() as readers, \
            patch.object(rclone_helper, "upload_unique", side_effect=AssertionError("Cloud write blocked")), \
            patch.object(rclone_helper, "delete", side_effect=AssertionError("Cloud delete blocked")), \
            patch.object(rclone_helper, "moveto", side_effect=AssertionError("Cloud move blocked")):
        assert len(doc) == (4 if kind == "PM" else 1)
        if date_mode == "missing":
            for name in ("ocr_jobsheet_fields", "ocr_jobsheet_identity_fields",
                         "ocr_jobsheet_support_fields", "ocr_jobsheet_focused_field"):
                readers.enter_context(patch.object(nvidia_client, name,
                    side_effect=without_date(getattr(nvidia_client, name))))
            readers.enter_context(patch.object(matching_rules, "_checklist_read",
                side_effect=without_date(matching_rules._checklist_read)))
        task, tier, ocr = processor._ocr_and_match(doc, kind, reference_day)
    trace = (ocr.get("rules_audit") or {}).get("trace") or []
    metrics = ocr.get("ocr_metrics") or {}
    indexed_gids = {ref["gid"] for row in index["devices"] for ref in row["task_refs"]}
    receipt = None
    if task:
        filename, _ = processor._planned_filename(task)
        receipt = selection_audit.receipt(source, task_gid=task["gid"], filename=filename)
    result = {"sample": sample, "kind": kind, "date_mode": date_mode,
              "processing_day": reference_day.isoformat(),
              "window": [first_day.isoformat(), last_day.isoformat()], "matched": task is not None,
              "tier": tier, "receipt": receipt,
              "selected_task_was_indexed": task["gid"] in indexed_gids if task else None,
              "index_generated_at": index["generated_at"],
              "calls": metrics.get("calls"), "seconds": metrics.get("seconds"),
              "tokens": metrics.get("total_tokens"), "cloud_writes": 0,
              "trace": trace, "date_present": bool(ocr.get("service_date_raw")),
              "diagnostic": (ocr.get("rules_audit") or {}).get("last_live_diagnostic")}
    print("STALE_INDEX_RESULT " + json.dumps(result), flush=True)
    if date_mode == "missing":
        assert not result["date_present"]
        assert trace[-1] == "upload_prior_live_visit_confirmed"
    if not task or (kind == "CM" and result["selected_task_was_indexed"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

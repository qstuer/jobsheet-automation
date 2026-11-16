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


def with_identity_checks(diagnostic):
    def check(tasks, ev, job_type):
        result = diagnostic(tasks, ev, job_type)
        repeated_serials = (matching_rules._stable(ev, "serial_candidates")
                            | matching_rules._stable(ev, "serial_visual_candidates"))
        checks = []
        for task in tasks:
            facts = matching_rules._live_facts(task)
            if not facts:
                continue
            assets = matching_rules._stable(ev, "asset_candidates")
            checks.append({
                "serial_repeat_count": len(repeated_serials),
                "repeated_serial_distances": sorted(asana_client._lev(s, facts["serial"]) for s in repeated_serials),
                "exact_model_supported": matching_rules._stable(ev, "product_model") == {facts["product_model"]},
                "repeated_phone_exact": bool(matching_rules._stable(ev, "phone_candidates") & facts["phones"]),
                "repeated_asset_exact": len(assets) == 1 and assets.issubset(facts["assets"]),
            })
        return {**result, "fallback_identity_checks": checks[:10]}
    return check


def with_lookup_counts(reader, audit):
    def lookup(rows, ev, reference_day, **kwargs):
        entry = {"valid_serial_count": len([s for s in matching_rules._serials(ev)
            if nvidia_client._valid_serial_token(s)]),
            "indexed_task_count": len({str(ref["gid"]) for row in rows
                for ref in row.get("task_refs") or [] if ref.get("gid")}),
            "typeahead_result_counts": []}
        typeahead = asana_client._typeahead
        def search(*args, **kw):
            result = typeahead(*args, **kw)
            entry["typeahead_result_counts"].append(len(result))
            return result
        with patch.object(asana_client, "_typeahead", side_effect=search):
            result = reader(rows, ev, reference_day, **kwargs)
        entry["live_task_count"] = len(result[0])
        entry["reason"] = result[1]
        audit.append(entry)
        return result
    return lookup


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
    lookup_audit = []
    with fitz.open(source) as doc, ExitStack() as readers, \
            patch.object(rclone_helper, "upload_unique", side_effect=AssertionError("Cloud write blocked")), \
            patch.object(rclone_helper, "delete", side_effect=AssertionError("Cloud delete blocked")), \
            patch.object(rclone_helper, "moveto", side_effect=AssertionError("Cloud move blocked")):
        assert len(doc) == (4 if kind == "PM" else 1)
        readers.enter_context(patch.object(matching_rules, "diagnostic_pool",
            side_effect=with_identity_checks(matching_rules.diagnostic_pool)))
        readers.enter_context(patch.object(matching_rules, "live_pool",
            side_effect=with_lookup_counts(matching_rules.live_pool, lookup_audit)))
        if date_mode == "missing":
            for name in ("ocr_jobsheet_fields", "ocr_jobsheet_identity_fields",
                         "ocr_jobsheet_support_fields", "ocr_jobsheet_focused_field",
                         "ocr_jobsheet_context_field"):
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
              "lookup_audit": lookup_audit,
              "diagnostic": (ocr.get("rules_audit") or {}).get("last_live_diagnostic")}
    print("STALE_INDEX_RESULT " + json.dumps(result), flush=True)
    if date_mode == "missing":
        assert not result["date_present"]
        assert trace[-1] == "upload_prior_live_visit_confirmed"
    if not task or (kind == "CM" and result["selected_task_was_indexed"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

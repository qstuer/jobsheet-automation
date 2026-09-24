"""Read-only comparison of product and hospital OCR on private jobsheets.

Ground truth and PDFs are supplied at runtime from the private Drive control
folder.  Neither raw OCR replies nor customer data are written to the public
Actions report.  This module does not import the cloud queue or upload code.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import fitz

from . import asana_client, nvidia_client


FIELDS = ("product_raw", "hospital_raw")
SAMPLE_IDS = ("B01", "B03", "B04", "B06", "B09", "B13", "B15", "B19", "B20")


def _plain(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9+]", "", (value or "").upper())


def _score(field: str, observed: str | None, truth: str) -> dict:
    if field == "product_raw":
        actual = asana_client.normalize_product(observed)
        expected = asana_client.normalize_product(truth)
        return {
            "exact": bool(actual and _plain(actual) == _plain(expected)),
            "core": bool(actual and asana_client.product_group(actual)
                         == asana_client.product_group(expected)),
            "blank": not bool(observed),
        }
    return {
        "exact": bool(observed and _plain(observed) == _plain(truth)),
        "core": bool(observed and asana_client.hospital_core(observed)
                     and asana_client.hospital_core(observed)
                     == asana_client.hospital_core(truth)),
        "blank": not bool(observed),
    }


def _ask(image: str, prompt: str, required: set[str]) -> dict:
    raw = nvidia_client._call_vision(
        prompt=prompt, image_b64=image, max_tokens=180, expects_json=True,
    )
    value = nvidia_client._parse_json_object(raw, required_keys=required)
    return {field: item.strip() if isinstance(item, str) and item.strip() else None
            for field, item in value.items() if field in FIELDS}


def _paired(doc: fitz.Document) -> dict:
    image = nvidia_client.crop_jobsheet_field_card(
        doc, 0, FIELDS, zoom=4.0,
    )
    prompt = (
        "Two separate boxes are labelled PRODUCT ONLY and CUSTOMER NAME / "
        "HOSPITAL ONLY. Transcribe the handwritten value in each box; include "
        "any hospital location suffix visible in that SAME box. Ignore printed "
        "labels. If a value is crossed out and replaced, use only the uncrossed "
        "replacement. Never infer a missing letter, number, or model suffix. Use null "
        "if unreadable. Return only JSON with keys product_raw and hospital_raw."
    )
    return _ask(image, prompt, set(FIELDS))


def _single(doc: fitz.Document, field: str, context: bool) -> dict:
    image = nvidia_client.crop_jobsheet_field_card(
        doc, 0, (field,), zoom=5.0, focused=True,
    )
    if field == "product_raw":
        guidance = (
            "Known Philips product families include Affiniti, EPIQ, and CX. "
            "Common models include Affiniti 30/50/70/70G, EPIQ 5G/7G/7+/Elite/CVx, "
            "and CX30/CX50. This is spelling context, NOT a multiple-choice test. "
            "Copy only the model variant actually supported by the handwriting; "
            "if only the family is visible, return only the family. "
        ) if context else ""
        label = "PRODUCT"
    else:
        guidance = (
            "Known hospital abbreviations include PYN, PYNEH, GH, KWH, QMH, "
            "QEH, KH, PMH, HKCH, PWH, UCH, and TMH. PYN and PYNEH refer "
            "to the same hospital. This is spelling context, NOT a list to choose "
            "from. Copy the visible spelling and any suffix such as floor/room. "
            "Preserve a visible hyphen or slash between the hospital name and "
            "floor/room code; do not replace it with a space or omit it. "
            "Do not replace it with an assumed hospital name. "
        ) if context else ""
        label = "CUSTOMER NAME / HOSPITAL"
    prompt = (
        f"This image contains one {label} value field. "
        "Read printed or handwritten VALUE only, not the field label. "
        + guidance
        + "If a value is crossed out and replaced, use only the uncrossed "
        "replacement. Return null if no value is visible. Never make up "
        "missing characters. "
        f'Return only JSON: {{"{field}":null}} (replace null with the exact visible string).'
    )
    return _ask(image, prompt, {field})


def _load_samples(folder: Path, manifest_path: Path,
                  sample_ids: tuple[str, ...] = SAMPLE_IDS) -> list[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2 or len(manifest.get("samples", [])) != 20:
        raise ValueError("Private reviewed manifest is missing or invalid")
    selected = []
    for sample in manifest["samples"]:
        if sample.get("sample_id") not in sample_ids:
            continue
        if sample.get("review_status") != "confirmed":
            raise ValueError("Selected sample is not independently reviewed")
        filename = sample.get("filename")
        if not isinstance(filename, str) or not re.fullmatch(r"B\d{2}\.pdf", filename):
            raise ValueError("Unexpected private PDF filename")
        path = folder / filename
        if hashlib.sha256(path.read_bytes()).hexdigest() != sample["source_sha256"]:
            raise ValueError("Private PDF fingerprint mismatch")
        if not all(sample.get("observed_fields", {}).get(field) for field in FIELDS):
            raise ValueError("Selected sample has no independently observed truth")
        selected.append(sample)
    if len(selected) != len(sample_ids):
        raise ValueError("Private sample set is incomplete")
    return selected


def run() -> dict:
    folder = Path(os.environ["JOBSHEET_BACKTEST_DIR"])
    manifest = Path(os.environ["JOBSHEET_BACKTEST_MANIFEST"])
    report_path = Path(os.environ["JOBSHEET_FIELD_REPORT"])
    requested = os.environ.get("JOBSHEET_FIELD_SAMPLE_IDS", "")
    sample_ids = tuple(item.strip() for item in requested.split(",") if item.strip()) \
        if requested else SAMPLE_IDS
    if not sample_ids or len(sample_ids) != len(set(sample_ids)) \
            or any(item not in SAMPLE_IDS for item in sample_ids):
        raise ValueError("Field experiment sample selection is invalid")
    rows = []
    for sample in _load_samples(folder, manifest, sample_ids):
        sid = sample["sample_id"]
        nvidia_client.reset_model_availability()
        nvidia_client.reset_ocr_metrics()
        arms = {}
        with fitz.open(folder / sample["filename"]) as doc:
            for arm in ("paired", "single_strict", "single_context"):
                try:
                    if arm == "paired":
                        values = _paired(doc)
                    else:
                        values = {
                            field: _single(doc, field, context=arm == "single_context").get(field)
                            for field in FIELDS
                        }
                    arms[arm] = {
                        field: _score(field, values.get(field),
                                      sample["observed_fields"][field])
                        for field in FIELDS
                    }
                except Exception as exc:
                    # Exception text may contain provider data. Keep only class.
                    arms[arm] = {"error_class": type(exc).__name__}
        usage = nvidia_client.get_ocr_metrics()
        rows.append({
            "sample_id": sid,
            "arms": arms,
            "usage": {key: usage.get(key) for key in (
                "calls", "seconds", "prompt_tokens", "completion_tokens",
                "total_tokens", "estimated_cost_cny_upper"
            ) if key in usage},
        })
        print(f"{sid}: " + ", ".join(
            f"{arm}=P{int(result.get('product_raw', {}).get('exact', False))}"
            f"/H{int(result.get('hospital_raw', {}).get('exact', False))}"
            if "error_class" not in result else f"{arm}=SERVICE_ERROR"
            for arm, result in arms.items()
        ), flush=True)
    summary = {}
    for arm in ("paired", "single_strict", "single_context"):
        summary[arm] = {
            field: {
                metric: sum(bool(row["arms"].get(arm, {}).get(field, {}).get(metric))
                            for row in rows)
                for metric in ("exact", "core", "blank")
            } for field in FIELDS
        }
        summary[arm]["service_errors"] = sum(
            "error_class" in row["arms"].get(arm, {}) for row in rows
        )
    report = {"samples": len(rows), "summary": summary, "rows": rows}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Anonymous summary: " + json.dumps(summary, ensure_ascii=False), flush=True)
    if any(item["service_errors"] for item in summary.values()):
        raise RuntimeError("The OCR provider failed during the field experiment; see anonymous report")
    return report


if __name__ == "__main__":
    run()

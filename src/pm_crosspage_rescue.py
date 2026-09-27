"""Opt-in, read-only backtest rescue from a complete PM checklist.

This module is called only by ``src.backtest``. It does not change Stage B or
move a source PDF. The checklist can corroborate an ACTION DATE that the first
page OCR saw once; it cannot invent a date or replace an explicitly conflicting
first-page reading. No Asana candidate is ever given to vision.
"""

from __future__ import annotations

import re
from copy import deepcopy

from . import asana_client, config, nvidia_client


def _date(value):
    return nvidia_client._parse_action_date(value)


def _serial(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _first_page_date_evidence(ocr: dict, target) -> tuple[str | None, bool]:
    """Return a matching ACTION DATE raw value and an explicit conflict flag."""
    values = []
    focused = []
    for reading in (ocr.get("_ocr_audit") or {}).get("readings") or []:
        context = reading.get("context") or {}
        raw = (reading.get("raw") or {}).get("service_date_raw")
        normal = (reading.get("normalized") or {}).get("service_date_raw")
        value = raw or normal
        if isinstance(value, str) and value.strip():
            values.append(value)
            if context.get("stage") == "date_recheck":
                focused.append(_date(value))
    current = ocr.get("service_date_raw")
    if isinstance(current, str) and current.strip():
        values.append(current)
    conflict = (len(focused) == 2 and focused[0] is not None
                and focused[0] == focused[1] and focused[0] != target)
    exact = next((value for value in values if _date(value) == target), None)
    return exact, conflict


def _assess(ocr: dict, headers: list[dict], serial_card: str | None,
            date_card: str | None, last_page: list[dict],
            first_page_customer_date: str | None) -> tuple[dict | None, str]:
    """Require independent visual support before trying the normal matcher."""
    if len(headers) != 2 or len(last_page) != 2:
        return None, "incomplete_reads"
    dates = [_date(row.get("date")) for row in headers]
    if not dates[0] or dates[0] != dates[1] or _date(date_card) != dates[0]:
        return None, "checklist_date_disagreement"
    target = dates[0]
    age = (nvidia_client._today() - target).days
    if not (-config.OCR_SERVICE_DATE_FUTURE_TOLERANCE_DAYS <= age
            <= config.OCR_SERVICE_DATE_MAX_AGE_DAYS):
        return None, "date_outside_window"
    if not all(_date(row.get("customer_date")) == target for row in last_page):
        return None, "last_page_date_disagreement"
    signed = _date(first_page_customer_date)
    if signed is not None and signed != target:
        return None, "first_page_customer_conflict"
    action_raw, focus_conflict = _first_page_date_evidence(ocr, target)
    if focus_conflict:
        return None, "first_page_focused_conflict"
    if not action_raw:
        return None, "no_first_page_action_support"

    first_product = asana_client.product_group(ocr.get("product_raw"))
    if not first_product or not all(
            asana_client.product_group(row.get("product")) == first_product
            for row in headers):
        return None, "product_not_corrob"
    first_hospital = asana_client.hospital_core(ocr.get("hospital_raw"))
    if not first_hospital:
        return None, "first_page_hospital_missing"
    second_hospitals = [asana_client.hospital_core(row.get("hospital")) for row in headers]
    if (second_hospitals[0] and second_hospitals[0] == second_hospitals[1]
            and second_hospitals[0] != first_hospital):
        return None, "hospital_conflict"

    first_serials = {_serial(item) for item in ocr.get("serial_candidates") or []
                     if nvidia_client._valid_serial_token(item)}
    second_serials = {_serial(row.get("serial")) for row in headers}
    second_serials.add(_serial(serial_card))
    if not first_serials or not first_serials.intersection(second_serials):
        return None, "serial_not_corrob"
    if not (ocr.get("phone_candidates") or ocr.get("asset_candidates")):
        return None, "no_independent_support"

    candidate = deepcopy(ocr)
    candidate["service_date_raw"] = action_raw  # Came from the first-page ACTION DATE.
    candidate["service_date_iso"] = target.isoformat()
    candidate["date_source"] = "ACTION_DATE"
    candidate["date_corrob"] = True
    candidate.setdefault("_ocr_audit", {})["pm_crosspage_rescue"] = "evidence_accepted"
    return candidate, "evidence_accepted"


def try_rescue(doc, ocr: dict, job_type: str):
    """Try the normal unique-task matcher only after a guarded PM re-read."""
    if job_type != "PM" or getattr(doc, "page_count", 0) != 4 or not ocr:
        return None, 0, ocr
    # Runtime import keeps the experimental read-only probe outside the normal
    # Stage B import path. Both PDFs and the private index are already local.
    from . import checklist_crosspage_probe as old_probe
    from . import pm_crosspage_identity_probe as probe

    try:
        headers = [probe._read(probe._panel_image(doc, 1, probe.HEADER, zoom), "header")
                   for zoom in (5.0, 6.0)]
        serial_card = probe._read(probe._panel_image(doc, 1, probe.SERIAL, 6.0),
                                  "serial")["value"]
        date_card = probe._read(probe._panel_image(doc, 1, probe.DATE, 6.0),
                                "date")["value"]
        last_page = [old_probe._read_json(
            old_probe._panel_image(doc, 3, old_probe.CHECKLIST_SIGNATURES, zoom),
            section="signatures") for zoom in (5.0, 6.0)]
        first_signed = nvidia_client.ocr_jobsheet_signature_date(
            doc, 0, "customer_signed_date", zoom=5.0)
    except nvidia_client.NvidiaResponseError:
        ocr.setdefault("_ocr_audit", {})["pm_crosspage_rescue"] = "vision_unavailable"
        return None, 0, ocr

    candidate, reason = _assess(
        ocr, headers, serial_card, date_card, last_page, first_signed)
    ocr.setdefault("_ocr_audit", {})["pm_crosspage_rescue"] = reason
    if candidate is None:
        return None, 0, ocr
    task, tier = asana_client.find_task(candidate, job_type="PM")
    if task is None:
        ocr["_ocr_audit"]["pm_crosspage_rescue"] = "matcher_no_unique_task"
        return None, 0, ocr
    candidate["_ocr_audit"]["pm_crosspage_rescue"] = "matched"
    return task, tier, candidate

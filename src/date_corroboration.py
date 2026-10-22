"""Bounded signature-date proof, separate from the original OCR fields.

No model, task data, upload date, or networking is used here. Two renderings
of one panel are consistency checks, never two independent physical sources.
"""
from collections import defaultdict
from datetime import date
import os


ENABLED_ENV = "JOBSHEET_DATE_CORROBORATION"
ZOOMS = (5.0, 6.0)
COVER_FIELDS = ("engineer_signed_date", "customer_signed_date")
CHECKLIST_FIELDS = ("checklist_engineer_signed_date", "checklist_customer_signed_date")
SOURCES = {**{(0, field): "cover_signature" for field in COVER_FIELDS},
           **{(3, field): "pm_checklist_signature" for field in CHECKLIST_FIELDS}}


def require_read_only():
    if os.environ.get("JOBSHEET_DRY_RUN") != "1":
        raise ValueError("Signature-date corroboration is read-only only")


def summarize(records):
    """Validate provenance and return a private proof, not a fabricated reading.

    Only two consistent reads of each of two distinct printed date panels can
    corroborate a date. Any disagreeing stable panel prevents certification.
    """
    result = {"status": "not_attempted", "date": None, "read_count": 0,
              "stable_panel_count": 0, "page_count": 0}
    if not records:
        return result
    if not isinstance(records, (list, tuple)) or len(records) > 8:
        return {**result, "status": "invalid"}
    panels = defaultdict(dict)
    for record in records:
        if not isinstance(record, dict):
            return {**result, "status": "invalid"}
        page, field = record.get("page"), record.get("field")
        if type(page) is not int or not isinstance(field, str):
            return {**result, "status": "invalid"}
        source, zoom = record.get("source"), record.get("zoom")
        if (page, field) not in SOURCES or source != SOURCES[page, field] or zoom not in ZOOMS:
            return {**result, "status": "invalid"}
        if zoom in panels[page, field]:
            return {**result, "status": "invalid"}
        value = record.get("date")
        parsed = None
        if value is not None:
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError("Non-canonical date")
            except (TypeError, ValueError):
                return {**result, "status": "invalid"}
        panels[page, field][zoom] = parsed
    stable = {key: next(iter(values.values())) for key, values in panels.items()
              if set(values) == set(ZOOMS) and len(set(values.values())) == 1
              and next(iter(values.values())) is not None}
    days = set(stable.values())
    result.update(read_count=len(records), stable_panel_count=len(stable),
                  page_count=len({key[0] for key in panels}))
    if len(days) > 1:
        return {**result, "status": "conflict"}
    if len(stable) >= 2:
        return {**result, "status": "confirmed", "date": next(iter(days))}
    return {**result, "status": "insufficient"}

"""Read-only upload-date prior for an isolated historical backtest.

The simulated date only narrows visits on a device already found from OCR.
This module is not wired into production Stage B.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable


def two_previous_months(upload_day: date) -> tuple[date, date]:
    this_month = upload_day.replace(day=1)
    previous = (this_month - timedelta(days=1)).replace(day=1)
    first = (previous - timedelta(days=1)).replace(day=1)
    next_month = (this_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, next_month - timedelta(days=1)


def shortlist(
    refs: list[dict], job_type: str, upload_day: date,
    parse_date: Callable[[str], date | None],
) -> list[dict]:
    start, end = two_previous_months(upload_day)
    result = []
    for ref in refs:
        if ref.get("job_type") != job_type:
            continue
        created = parse_date(str(ref.get("created_at") or ""))
        if created and created > upload_day:
            continue
        due = parse_date(str(ref.get("due_on") or ""))
        completed = parse_date(str(ref.get("completed_at") or ""))
        formal_dates = ([due] if due else []) + (
            [completed] if completed and completed <= upload_day else []
        )
        if any(start <= day <= end for day in formal_dates):
            result.append(ref)
    return result


def select_visit(
    refs: list[dict], action_day: date | None, upload_day: date,
    parse_date: Callable[[str], date | None],
) -> str | None:
    """Never decide using upload recency or completion status alone."""
    if not refs:
        return None
    if action_day is not None:
        exactish = []
        close = []
        for ref in refs:
            due = parse_date(str(ref.get("due_on") or ""))
            completed = parse_date(str(ref.get("completed_at") or ""))
            dates = [day for day in (
                due, completed if completed and completed <= upload_day else None,
            ) if day is not None]
            if any(abs((day - action_day).days) <= 3 for day in dates):
                exactish.append(ref)
            if any(abs((day - action_day).days) <= 14 for day in dates):
                close.append(ref)
        if len(exactish) == 1:
            return str(exactish[0]["gid"])
        if len(exactish) > 1:
            return None
        return str(close[0]["gid"]) if len(close) == 1 else None
    return str(refs[0]["gid"]) if len(refs) == 1 else None

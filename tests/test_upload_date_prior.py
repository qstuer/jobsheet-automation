from datetime import date

from src.upload_date_prior import select_visit, shortlist, two_previous_months


def parse(value):
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def ref(gid, due, *, completed_at=None, created_at="2026-06-01",
        job_type="PM", completed=False):
    return {
        "gid": gid, "due_on": due, "completed_at": completed_at,
        "created_at": created_at, "job_type": job_type, "completed": completed,
    }


def test_two_previous_months_cover_forty_five_day_delay():
    assert two_previous_months(date(2026, 9, 2)) == (
        date(2026, 7, 1), date(2026, 9, 30)
    )
    assert [item["gid"] for item in shortlist(
        [ref("correct", "2026-07-19")], "PM", date(2026, 9, 2), parse,
    )] == ["correct"]


def test_completion_status_does_not_choose_visit():
    upload = date(2026, 9, 20)
    pool = shortlist([
        ref("old-open", "2026-08-01"),
        ref("correct-closed", "2026-08-20", completed_at="2026-09-01", completed=True),
    ], "PM", upload, parse)
    assert select_visit(pool, None, upload, parse) is None
    assert select_visit(pool, date(2026, 8, 20), upload, parse) == "correct-closed"


def test_future_created_and_completed_are_not_backdated():
    assert shortlist([
        ref("future", "2026-08-20", created_at="2026-09-10"),
        ref("future-completion", None, completed_at="2026-09-10"),
    ], "PM", date(2026, 9, 2), parse) == []


def test_conflicting_date_and_nearby_visits_remain_pending():
    upload = date(2026, 9, 20)
    one = shortlist([ref("one", "2026-08-18")], "PM", upload, parse)
    assert select_visit(one, date(2026, 9, 20), upload, parse) is None
    two = shortlist([
        ref("first", "2026-08-18"), ref("second", "2026-08-30"),
    ], "PM", upload, parse)
    assert select_visit(two, date(2026, 8, 24), upload, parse) is None
    assert select_visit(two, date(2026, 8, 18), upload, parse) == "first"


def test_pm_cm_never_mix():
    upload = date(2026, 9, 10)
    pool = shortlist([
        ref("pm", "2026-08-18"), ref("cm", "2026-09-09", job_type="CM"),
    ], "PM", upload, parse)
    assert select_visit(pool, None, upload, parse) == "pm"

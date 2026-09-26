"""Read-only, single-jobsheet review of a disputed ACTION DATE / Asana visit.

The reviewer is deliberately separate from automatic matching and from the
``confirmed_filename`` upload shortcut. A human can correct only the disputed
date and, if needed, identify one existing Asana task. They cannot supply a
device, hospital, product, serial or output name through this path.
"""

import re
from datetime import date
from urllib.parse import urlparse

from . import asana_client, asana_index

REVIEW_WINDOW_DAYS = 14
REASON_MESSAGES = {
    "job_type_unknown": "CM／PM 類型未能確認",
    "identity_incomplete": "設備身分欄位不足",
    "device_not_found": "找不到符合設備",
    "device_ambiguous": "有多部相近設備，日期不能代替設備核對",
    "device_identity_conflict": "設備的醫院、產品或機身編號未吻合",
    "serial_needs_independent_evidence": "機身編號有一字差異，當次電話或 Asset 證據不足",
    "no_visit_within_14_days": "同類工作沒有正式日期落在兩星期範圍內",
    "selected_visit_not_eligible": "指定的 Asana 工作不在安全候選內",
    "visit_ambiguous": "同設備有多次可能工作，請填 Asana 工作連結",
    "live_job_type_conflict": "Asana 最新 CM／PM 類型不吻合",
    "live_date_conflict": "Asana 最新正式日期不吻合",
    "live_serial_conflict": "Asana 最新機身編號不吻合",
    "live_support_conflict": "Asana 當次電話或 Asset 證據不吻合",
    "live_identity_conflict": "Asana 最新產品或醫院不吻合",
    "order_number_conflict": "單據訂單號與 Asana 不吻合",
}


def parse_review_date(value: str) -> date:
    """Require an unambiguous date; never reinterpret 8/9 as 9/8."""
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value or ""):
        raise ValueError("確認日期必須使用 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("確認日期不是有效日曆日期") from exc


def parse_task_gid(value: str) -> str:
    """Accept an Asana task GID or its URL, not arbitrary links or text."""
    value = (value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"\d{8,20}", value):
        return value
    parsed = urlparse(value)
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme == "https" and parsed.hostname == "app.asana.com"
            and len(parts) >= 3 and parts[0] == "0"
            and parts[1].isdigit() and re.fullmatch(r"\d{8,20}", parts[2])):
        return parts[2]
    raise ValueError("Asana 工作只接受 GID 或 app.asana.com 工作連結")


def _same_hospital(wanted: list[str], stored: list[str]) -> bool:
    return bool(wanted and stored and any(
        asana_client._key_similarity(left, right) == 1.0
        for left in wanted for right in stored
    ))


def _same_product(wanted: str, stored: str) -> bool:
    return bool(wanted and stored and asana_client._family_similarity(
        wanted, stored
    ) == 1.0)


def _formal_delta(confirmed: date, source: dict) -> int | None:
    days = asana_client._formal_index_ref_dates(source)
    return min((abs((day - confirmed).days) for day in days), default=None)


def _visit_has_independent_evidence(ocr: dict, ref: dict) -> bool:
    """For a one-character Serial typo, corroboration must be this visit's."""
    sheet_phones = {re.sub(r"\D", "", value) for value in
                    ocr.get("phone_candidates") or []}
    ref_phones = {re.sub(r"\D", "", value) for value in ref.get("phones") or []}
    if any(7 <= len(value) <= 9 for value in sheet_phones & ref_phones):
        return True
    return asana_client._asset_match_level(
        ocr.get("asset_candidates"), ref.get("assets")
    ) == 2


def _pending(reason: str, **extra) -> dict:
    return {"status": "PENDING", "reason": reason, **extra}


def review_ocr(ocr: dict, job_type: str, confirmed_date: str,
               selected_task: str = "") -> dict:
    """Check identity, visit and current Asana facts without any cloud write.

    A confirmation is *not* a filename override. The caller may use a READY
    result only as a private preview; this module has no upload or move path.
    """
    day = parse_review_date(confirmed_date)
    selected_gid = parse_task_gid(selected_task)
    if job_type not in {"PM", "CM"}:
        return _pending("job_type_unknown")
    if asana_client._device_index is None:
        raise asana_client.AsanaError("受控核對需要已驗證的私人設備索引")

    prepared = asana_client._prepare_index_query(ocr)
    serials = prepared.get("_index_serials") or []
    wanted_product = prepared.get("_index_wanted_product")
    wanted_hospitals = prepared.get("_index_wanted_hospitals") or []
    if not serials or not wanted_product or not wanted_hospitals:
        return _pending("identity_incomplete")

    ranked = asana_client._rank_index_devices(ocr, job_type)
    if not ranked:
        return _pending("device_not_found")
    best = ranked[0]
    if len(ranked) > 1 and (best["serial_similarity"] - ranked[1]["serial_similarity"]
                            <= asana_client.config.INDEX_SERIAL_CLOSE_GAP):
        return _pending("device_ambiguous")
    if (best["product_similarity"] < 1.0 or best["hospital_similarity"] < 1.0
            or best["serial_dist"] > 1):
        # A date or selected task cannot rescue a poorly identified device.
        return _pending("device_identity_conflict")
    row = best["row"]
    plausible = []
    undated_same_type = False
    for ref in row.get("task_refs") or []:
        if ref.get("job_type") != job_type:
            continue
        ref_product = asana_client.product_group(
            ref.get("product_family") or ref.get("product")
        )
        ref_hospitals = ref.get("hospital_aliases") or asana_client.hospital_aliases(
            ref.get("hospital") or ref.get("location")
        )
        if not (_same_product(wanted_product, ref_product)
                and _same_hospital(wanted_hospitals, ref_hospitals)):
            continue
        if best["serial_dist"] == 1 and not _visit_has_independent_evidence(ocr, ref):
            continue
        delta = _formal_delta(day, ref)
        if delta is None:
            undated_same_type = True
        elif delta <= REVIEW_WINDOW_DAYS:
            plausible.append(ref)
    plausible_gids = {str(ref.get("gid")) for ref in plausible}
    if not plausible_gids:
        if best["serial_dist"] == 1 and not any(
                _visit_has_independent_evidence(ocr, ref)
                for ref in row.get("task_refs") or [] if ref.get("job_type") == job_type):
            return _pending("serial_needs_independent_evidence")
        return _pending("no_visit_within_14_days")
    if selected_gid:
        if selected_gid not in plausible_gids:
            return _pending("selected_visit_not_eligible")
        gid = selected_gid
    elif len(plausible_gids) == 1 and not undated_same_type:
        gid = next(iter(plausible_gids))
    else:
        # A confirmed date alone must not pick between two PMs or an undated
        # historical task. Ask for the specific Asana visit, never its name.
        return _pending("visit_ambiguous", candidate_count=len(plausible_gids))

    task = asana_client._fetch_task(gid)  # connection failure is not "no match"
    if asana_client._task_job_type(task) != job_type:
        return _pending("live_job_type_conflict")
    live_delta = _formal_delta(day, task)
    if live_delta is None or live_delta > REVIEW_WINDOW_DAYS:
        return _pending("live_date_conflict")
    live_record = asana_index.task_to_record(task, job_type)
    if not live_record or asana_client._norm(live_record.get("serial")) != asana_client._norm(row.get("serial")):
        return _pending("live_serial_conflict")
    live_ref = live_record["task_refs"][0]
    if best["serial_dist"] == 1 and not _visit_has_independent_evidence(ocr, live_ref):
        return _pending("live_support_conflict")
    if not (_same_product(wanted_product, asana_client.product_group(
                live_ref.get("product_family") or live_ref.get("product")))
            and _same_hospital(wanted_hospitals, live_ref.get("hospital_aliases") or [])):
        return _pending("live_identity_conflict")
    # A clearly read order number conflicting with the current task is not
    # silently ignored just because the date was confirmed by a human.
    sheet_order = str(ocr.get("order_no") or "").strip()
    task_order = asana_client.extract_order_no_from_name(task)
    if sheet_order and task_order and sheet_order != task_order:
        return _pending("order_number_conflict")
    return {"status": "READY_READ_ONLY", "reason": "all_checks_passed", "task": task}

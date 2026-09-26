"""Read-only, single-jobsheet review of disputed OCR / Asana evidence.

The reviewer is deliberately separate from automatic matching and from the
``confirmed_filename`` upload shortcut. A human can correct only the disputed
date and, if needed, identify one existing Asana task. A separately confirmed
serial is accepted only with an exact same-visit Asset and a selected task;
it never enters the automatic or upload path.
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
    "confirmed_serial_requires_task": "人工確認機身編號時必須指定 Asana 工作",
    "manual_serial_not_visually_supported": "原始讀數與人工確認機身編號差異過大",
    "manual_serial_not_unique": "人工確認機身編號在索引中不唯一",
    "manual_serial_asset_not_confirmed": "當次 Asset 沒有獨立讀出並與 Asana 完全吻合",
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


def parse_review_serial(value: str) -> str:
    """Accept a complete human-read serial, never a partial/pattern guess."""
    serial = (value or "").strip().upper()
    if not (re.fullmatch(r"[A-Z]{2,3}[A-Z0-9]{6,9}", serial)
            and 8 <= len(serial) <= 12
            and sum(char.isdigit() for char in serial) >= 4):
        raise ValueError("確認機身編號必須完整，不能有空格、符號或問號")
    return serial


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


def _supported_close_devices(ranked: list[dict], ocr: dict,
                             job_type: str, day: date) -> list[tuple[dict, set[str]]]:
    """Find nearby rows corroborated by one coherent historical visit.

    The selected Asana task supplied by a human is intentionally not used to
    choose the device. Search every plausible nearby device so that two rows
    with the same corroboration remain ambiguous.
    """
    prepared = asana_client._prepare_index_query(ocr)
    wanted_product = prepared.get("_index_wanted_product")
    wanted_hospitals = prepared.get("_index_wanted_hospitals") or []
    supported = []
    for item in ranked:
        if (item["serial_dist"] > 3 or item["product_similarity"] != 1.0
                or item["hospital_similarity"] != 1.0):
            continue
        matching_gids = {
            str(ref.get("gid")) for ref in item["row"].get("task_refs") or [] if (
            ref.get("job_type") == job_type
            and _same_product(wanted_product, asana_client.product_group(
                ref.get("product_family") or ref.get("product")))
            and _same_hospital(wanted_hospitals, ref.get("hospital_aliases") or
                               asana_client.hospital_aliases(
                                   ref.get("hospital") or ref.get("location")))
            and _visit_has_independent_evidence(ocr, ref)
            and (delta := _formal_delta(day, ref)) is not None
            and delta <= REVIEW_WINDOW_DAYS
            )
        }
        if matching_gids:
            supported.append((item, matching_gids))
    return supported


def _pending(reason: str, **extra) -> dict:
    return {"status": "PENDING", "reason": reason, **extra}


def _confirmed_asset_on_sheet(ocr: dict, asana_assets: list[str]) -> bool:
    """Require one exact Asset in two independent reads, without a rival vote.

    The consensus field alone can hide a competing two-read value. A human
    serial correction must not treat that ambiguous Asset as confirmation.
    """
    assets = {re.sub(r"\D", "", str(value or ""))
              for value in ocr.get("asset_candidates") or []}
    assets = {value for value in assets if len(value) >= 4}
    if len(assets) != 1 or asana_client._asset_match_level(
            list(assets), asana_assets) != 2:
        return False
    confirmed = next(iter(assets))
    counts = {}
    for reading in (ocr.get("_ocr_audit") or {}).get("readings") or []:
        values = (reading.get("normalized") or {}).get("asset_candidates") or []
        for value in {re.sub(r"\D", "", str(item or "")) for item in values}:
            if len(value) >= 4:
                counts[value] = counts.get(value, 0) + 1
    return counts.get(confirmed, 0) >= 2 and not any(
        value != confirmed and count >= 2 for value, count in counts.items()
    )


def _review_with_confirmed_serial(ocr: dict, job_type: str, day: date,
                                  selected_gid: str, serial: str) -> dict:
    """Narrow one private review; never replace the original OCR evidence.

    A person can resolve the handwriting, but cannot supply the hospital,
    product, Asset, visit date or Asana identity. The latter are independently
    checked against both the private index and a fresh Asana fetch.
    """
    if not selected_gid:
        return _pending("confirmed_serial_requires_task")
    prepared = asana_client._prepare_index_query(ocr)
    visual_serials = prepared.get("_index_serials") or []
    if not visual_serials or min(
            asana_client._lev(asana_client._norm(value), serial)
            for value in visual_serials) > 3:
        return _pending("manual_serial_not_visually_supported")
    rows = [row for row in asana_client._device_index["devices"]
            if asana_client._norm(row.get("serial")) == serial
            and not row.get("weak_identity")]
    if len(rows) != 1:
        return _pending("manual_serial_not_unique")
    row = rows[0]
    score = asana_client._score_index_device(row, prepared, job_type)
    if score["product_similarity"] != 1.0 or score["hospital_similarity"] != 1.0:
        return _pending("device_identity_conflict")
    wanted_product = prepared.get("_index_wanted_product")
    wanted_hospitals = prepared.get("_index_wanted_hospitals") or []
    refs = [ref for ref in row.get("task_refs") or []
            if str(ref.get("gid")) == selected_gid and ref.get("job_type") == job_type]
    if len(refs) != 1:
        return _pending("selected_visit_not_eligible")
    ref = refs[0]
    if not (_same_product(wanted_product, asana_client.product_group(
            ref.get("product_family") or ref.get("product")))
            and _same_hospital(wanted_hospitals, ref.get("hospital_aliases") or
                               asana_client.hospital_aliases(
                                   ref.get("hospital") or ref.get("location")))):
        return _pending("selected_visit_not_eligible")
    delta = _formal_delta(day, ref)
    if delta is None or delta > REVIEW_WINDOW_DAYS:
        return _pending("no_visit_within_14_days")
    # Unlike the normal one-character typo path, this two/three-character
    # exception requires an exact Asset on this *visit*, not a historical
    # phone or Asset from another job on the same equipment.
    if not _confirmed_asset_on_sheet(ocr, ref.get("assets") or []):
        return _pending("manual_serial_asset_not_confirmed")

    # Only after every independent gate, run the existing read-only reviewer
    # with the confirmed serial. Keep all other original OCR fields unchanged.
    adjusted = dict(ocr)
    adjusted.pop("_index_query_prepared", None)
    adjusted["serial_candidates"] = [serial]
    adjusted["serial_visual_candidates"] = []
    adjusted["serial_no"] = serial
    result = review_ocr(adjusted, job_type, day.isoformat(), selected_gid)
    if result["status"] != "READY_READ_ONLY":
        return result
    live_record = asana_index.task_to_record(result["task"], job_type)
    if (not live_record or asana_client._norm(live_record.get("serial")) != serial
            or len(live_record.get("task_refs") or []) != 1):
        return _pending("live_serial_conflict")
    if not _confirmed_asset_on_sheet(
            ocr, live_record["task_refs"][0].get("assets") or []):
        return _pending("live_support_conflict")
    result["manual_serial_confirmed"] = True
    return result


def review_ocr(ocr: dict, job_type: str, confirmed_date: str,
               selected_task: str = "", confirmed_serial: str = "") -> dict:
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
    if confirmed_serial:
        return _review_with_confirmed_serial(
            ocr, job_type, day, selected_gid, parse_review_serial(confirmed_serial)
        )

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
        # A unique exact Serial plus exact product/hospital is stronger than a
        # merely similar second Serial. Never let the human-confirmed date or
        # task resolve a device tie, including two different exact OCR values.
        unique_exact_identity = (
            best["serial_dist"] == 0
            and ranked[1]["serial_dist"] > 0
            and best["product_similarity"] == 1.0
            and best["hospital_similarity"] == 1.0
        )
        if not unique_exact_identity:
            supported = _supported_close_devices(ranked, ocr, job_type, day)
            if len(supported) == 1 and supported[0][0] is best \
                    and best["serial_dist"] == 1:
                pass
            elif selected_gid:
                # Human selection may disambiguate *which Asana visit*, but
                # only among independently corroborated one-typo devices.
                # It cannot provide a missing serial/phone or invent a row.
                chosen = [item for item, gids in supported
                          if selected_gid in gids and item["serial_dist"] == 1]
                if len(chosen) != 1:
                    return _pending("device_ambiguous")
                best = chosen[0]
            else:
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

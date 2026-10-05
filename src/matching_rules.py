"""Opt-in integration of evidence-preserving OCR and live visit confirmation.

The private index retrieves devices; it never certifies a visit. No sample
answers or customer identifiers belong here. Public diagnostics are categories
and counts only. This entry point has no cloud mutation or finalization path.
"""
from __future__ import annotations

import base64
import io
from collections import Counter, defaultdict
from datetime import date, timedelta

import fitz
from PIL import Image, ImageOps

from . import asana_client as ac, asana_index, config, nvidia_client as vision

RULESET_VERSION = "evidence-live-v2"
MAX_DEVICE_ROWS = 10
MAX_LIVE_TASKS = 80
VISIT_WINDOW_DAYS = 14
CHECKLIST_HEADER = (0.05, 0.125, 0.95, 0.225)
CHECKLIST_PROMPT = (
    "This is the top of Philips Ultrasound System PM Checklist page 1 of 3, "
    "PDF page 2. Transcribe only the handwritten values on its two header rows. "
    "Top left Customer is hospital/site; top right Date is checklist date. "
    "Second row: System is product, sn is the FULL machine serial, Asset No. "
    "is asset. Keep each value on its own printed underline. Do not copy a "
    "neighbouring value, an old calibration date, or a guessed serial. Use ? "
    "for an unclear character, null for a blank. No Asana answers are supplied. "
    'JSON only: {"hospital":null,"product":null,"serial":null,"date":null,"asset":null}'
)


def _values(reading, field):
    value = reading.get(field)
    return value if isinstance(value, list) else [value] if value else []


def _key(field, value):
    if field == "hospital_raw":
        return ac._norm(ac.hospital_core(value))
    if field == "product_raw":
        return ac._norm(ac.product_group(ac.normalize_product(value)))
    if field == "service_date_raw":
        parsed = vision._parse_action_date(value)
        return parsed.isoformat() if parsed else ""
    if field == "contact_person_raw":
        return ac._text_norm(value)
    return ac._norm(value)


def evidence(readings):
    """Keep alternatives for retrieval, distinguishing them from consensus.

    A value gets at most one vote per API response, even when a response lists
    it twice. Different zooms of a cell are repeated readings, not independent
    physical fields. Page provenance is retained for contradiction checks.
    """
    fields = ("serial_candidates", "serial_visual_candidates", "hospital_raw",
              "product_raw", "phone_candidates", "asset_candidates",
              "contact_person_raw", "service_date_raw", "order_no")
    result = {field: {} for field in fields}
    for reading in readings:
        for field in fields:
            for key in {_key(field, v) for v in _values(reading, field)} - {""}:
                item = result[field].setdefault(key, {"votes": 0, "pages": Counter()})
                item["votes"] += 1
                item["pages"][reading.get("_page", 0)] += 1
    return result


def _stable(ev, field):
    return {key for key, item in ev[field].items() if item["votes"] >= 2}


def _strong_conflict(ev, field, actual):
    """A known repeated disagreement is different from an unknown short code."""
    stable = _stable(ev, field)
    return bool(stable and actual and not stable.intersection(actual))


def _serials(ev):
    return sorted(set(ev["serial_candidates"]) | set(ev["serial_visual_candidates"]))


def retrieve_rows(index, ev):
    """Serial-first retrieval; stale product/date fields cannot veto an exact ID."""
    if not isinstance(index, dict) or not isinstance(index.get("devices"), list):
        raise asana_index.AsanaIndexError("Validated private index is required")
    serials = _serials(ev)
    ranked = []
    for row in index["devices"]:
        serial = ac._norm(row.get("serial"))
        if not serial or row.get("weak_identity"):
            continue
        families = {ac._norm(ac.product_group(v)) for v in (
            *(row.get("product_variants") or []), *(row.get("product_families") or []))} - {""}
        hospitals = {ac._norm(ac.hospital_core(v)) for v in (
            *(row.get("hospitals") or []), *(row.get("hospital_aliases") or []))} - {""}
        product = bool(families & set(ev["product_raw"]))
        hospital = bool(hospitals & set(ev["hospital_raw"]))
        phone = bool({ac._norm(v) for v in row.get("phones") or []} & set(ev["phone_candidates"]))
        if serials:
            distance = min(ac._lev(s, serial) for s in serials)
            similarity = max(ac._key_similarity(s, serial) for s in serials)
            if distance > 3 or similarity < 0.5:
                continue
            # Loose retrieval only. Final acceptance uses the live task below.
            if distance >= 2 and not (product and hospital):
                continue
            rank = (distance, -int(hospital), -int(product), -int(phone))
        else:
            exact_asset = bool({ac._norm(v) for v in row.get("assets") or []} & set(ev["asset_candidates"]))
            if not (product and hospital and phone and exact_asset):
                continue
            rank = (0, -1, -1, -1)
        ranked.append((rank, row))
    ranked.sort(key=lambda item: item[0])
    if not serials and len(ranked) != 1:
        return [], "missing_serial_not_unique"
    if not ranked:
        return [], "index_miss"
    # Retain a near neighbour to allow live corroboration of a handwriting
    # error, even when the erroneous spelling happens to be another device.
    closest = ranked[0][0][0]
    close = [row for rank, row in ranked if rank[0] <= closest + 1]
    if len(close) > MAX_DEVICE_ROWS:
        return [], "device_candidate_limit"  # Never silently exclude a rival.
    return close, "serial_first_retrieval"


def live_pool(rows, ev, reference_day):
    refs = {str(ref["gid"]): ref for row in rows for ref in row.get("task_refs") or []
            if ref.get("gid")}
    if len(refs) > MAX_LIVE_TASKS:
        return [], "task_candidate_limit"
    # NO indexed type/date filtering: both may have changed since the snapshot.
    tasks = [ac._fetch_task(gid) for gid in refs]
    if not rows:
        # New equipment may not be indexed. Only transcribed serials are used;
        # unbounded phone/asset workspace searches are deliberately avoided.
        gids = set()
        for serial in _serials(ev)[:3]:
            if not vision._valid_serial_token(serial):
                continue
            gids.update(str(t["gid"]) for t in ac._typeahead(serial) if t.get("gid"))
        if len(gids) > MAX_LIVE_TASKS:
            return [], "task_candidate_limit"
        tasks = [ac._fetch_task(gid) for gid in sorted(gids)]
    return tasks, "live_tasks_read"


def _live_facts(task):
    record = asana_index.task_to_record(task)
    if not record:
        return None
    ref = record["task_refs"][0]
    return {
        "serial": ac._norm(record.get("serial")),
        "product": {ac._norm(record.get("product"))} - {""},
        "hospital": {ac._norm(ac.hospital_core(v)) for v in record.get("hospital_aliases") or []} - {""},
        "phones": set(record.get("phones") or []),
        "assets": set(record.get("assets") or []),
        "contacts": record.get("contacts") or [],
        "type": ac._task_job_type(task),
        "formal_dates": ac._formal_index_ref_dates(ref),
    }


def assess_task(task, ev, job_type):
    facts = _live_facts(task)
    if not facts or not facts["serial"]:
        return None
    if facts["type"] != job_type:
        return None  # Project/section, never title or completed status.
    if _strong_conflict(ev, "hospital_raw", facts["hospital"]) or _strong_conflict(ev, "product_raw", facts["product"]):
        return None
    support = set()
    if _stable(ev, "product_raw") & facts["product"]:
        support.add("product")
    if _stable(ev, "hospital_raw") & facts["hospital"]:
        support.add("hospital")
    if set(ev["phone_candidates"]) & facts["phones"]:
        support.add("phone")
    asset = ac._asset_match_level(list(ev["asset_candidates"]), list(facts["assets"]))
    if asset == 2:
        support.add("asset_exact")
    if any(ac._similarity(name, other) >= 0.75
           for name in ev["contact_person_raw"] for other in facts["contacts"]):
        support.add("contact")
    serials = _serials(ev)
    distance = min((ac._lev(s, facts["serial"]) for s in serials), default=99)
    serial_repeated = facts["serial"] in _stable(ev, "serial_candidates") | _stable(ev, "serial_visual_candidates")
    missing_hospital_fuzzy = False
    if serials:
        if distance > 3:
            return None
        if distance == 0:
            safe = (serial_repeated and {"hospital", "product"} <= support) or (
                "product" in support and "phone" in support
                and (serial_repeated or "hospital" in support))
        elif distance == 1:
            safe = {"hospital", "product"} <= support and bool(support & {"phone", "asset_exact"})
            # Complete the already agreed missing-hospital fallback. A
            # one-character serial transcription error is not a veto when
            # product and full phone identify the equipment. This branch
            # additionally requires a readable date and a unique live visit;
            # it must never use the upload-date prior to fill that gap.
            missing_hospital_fuzzy = (
                not _stable(ev, "hospital_raw")
                and {"product", "phone"} <= support
            )
            safe = safe or missing_hospital_fuzzy
        else:
            safe = {"hospital", "product"} <= support and bool(support & {"phone", "asset_exact"})
        if not safe:
            return None
    elif not {"hospital", "product", "phone", "asset_exact"} <= support:
        return None
    # Asset never subtracts points or becomes required when Serial is readable.
    score = ({0: 100, 1: 80, 2: 60, 3: 40}.get(distance, 0)
             + 25 * ("hospital" in support) + 20 * ("product" in support)
             + 30 * ("phone" in support) + 5 * ("contact" in support)
             + (10 if asset == 2 else 5 if asset == 1 else 0))
    return {"task": task, "facts": facts, "score": score, "support": support,
            "serial_distance": distance,
            "requires_reliable_date": missing_hospital_fuzzy}


def select_task(tasks, ev, job_type, reference_day, *, allow_upload_prior=True):
    """Confirm device first, then a unique dated visit. No best-of-run selection."""
    candidates = [row for task in tasks if (row := assess_task(task, ev, job_type))]
    if not candidates:
        return None, "live_identity_not_confirmed"
    # Repeated readings of different known hospitals on different physical
    # pages indicate a mixed packet, not merely an uncertain OCR pass.
    for field in ("hospital_raw", "product_raw"):
        per_page = defaultdict(set)
        for key, item in ev[field].items():
            for page, votes in item["pages"].items():
                if votes >= 2:
                    per_page[page].add(key)
        if len(per_page) > 1 and not set.intersection(*per_page.values()):
            return None, "crosspage_identity_conflict"
    orders = _stable(ev, "order_no")
    if len(orders) > 1:
        return None, "repeated_order_conflict"
    if orders:
        candidates = [row for row in candidates
                      if ac.extract_order_no_from_name(row["task"]) in orders]
        if not candidates:
            return None, "live_order_conflict"
    devices = defaultdict(list)
    for row in candidates:
        devices[row["facts"]["serial"]].append(row)
    ranking = sorted(devices, key=lambda s: max(r["score"] for r in devices[s]), reverse=True)
    if len(ranking) > 1 and (max(r["score"] for r in devices[ranking[0]])
                            - max(r["score"] for r in devices[ranking[1]])) < 15:
        return None, "live_devices_tied"
    visits = devices[ranking[0]]
    stable_dates = _stable(ev, "service_date_raw")
    if len(stable_dates) > 1:
        return None, "repeated_dates_conflict"
    action_day = date.fromisoformat(next(iter(stable_dates))) if stable_dates else None
    if action_day:
        dated = []
        for row in visits:
            days = row["facts"]["formal_dates"]
            delta = min((abs((day - action_day).days) for day in days), default=9999)
            if delta <= VISIT_WINDOW_DAYS:
                dated.append((delta, row))
        if not dated:
            return None, "live_visit_date_conflict"
        dated.sort(key=lambda item: (item[0], -item[1]["score"]))
        best_delta, best = dated[0]
        if len(dated) > 1 and dated[1][0] == best_delta and dated[1][1]["score"] >= best["score"] - 15:
            return None, "live_visits_tied"
        return best["task"], "action_date_live_visit_confirmed"
    if any(row.get("requires_reliable_date") for row in visits):
        return None, "missing_hospital_fuzzy_needs_action_date"
    if reference_day is None:
        return None, "upload_date_unknown"
    if not allow_upload_prior:
        return None, "need_crosspage_before_upload_prior"
    # This is an upload-time prior, NOT a fabricated ACTION DATE. Do not use
    # today's run date when replaying an old document; retain completed tasks.
    dated = []
    for row in visits:
        task = row["task"]
        day = ac._parse_date(task.get("due_on") or task.get("completed_at"))
        if day and reference_day - timedelta(days=70) <= day <= reference_day + timedelta(days=30):
            dated.append((abs((day - reference_day).days), row))
    dated.sort(key=lambda item: item[0])
    if not dated or (len(dated) > 1 and dated[0][0] == dated[1][0]):
        return None, "upload_prior_not_unique"
    return dated[0][1]["task"], "upload_prior_live_visit_confirmed"


def diagnostic_pool(tasks, ev, job_type):
    """Anonymous rejection evidence, not raw OCR/Asana data or task IDs."""
    rows = []
    for task in tasks:
        facts = _live_facts(task)
        if not facts:
            continue
        serials = _serials(ev)
        rows.append({
            "serial_distance": min((ac._lev(v, facts["serial"]) for v in serials), default=99),
            "product_supported": bool(_stable(ev, "product_raw") & facts["product"]),
            "product_conflict": _strong_conflict(ev, "product_raw", facts["product"]),
            "hospital_supported": bool(_stable(ev, "hospital_raw") & facts["hospital"]),
            "hospital_conflict": _strong_conflict(ev, "hospital_raw", facts["hospital"]),
            "phone_exact": bool(set(ev["phone_candidates"]) & facts["phones"]),
            "asset_level": ac._asset_match_level(list(ev["asset_candidates"]), list(facts["assets"])),
            "type_matches": facts["type"] == job_type,
            "passes_identity": assess_task(task, ev, job_type) is not None,
        })
    rows.sort(key=lambda r: (r["serial_distance"], not r["product_supported"], not r["phone_exact"]))
    return {"live_task_count": len(tasks), "nearest_live_facts": rows[:10],
            "repeated_product_count": len(_stable(ev, "product_raw")),
            "repeated_hospital_count": len(_stable(ev, "hospital_raw")),
            "repeated_date_count": len(_stable(ev, "service_date_raw"))}


def _checklist_read(doc, zoom):
    rect = doc[1].rect
    l, t, r, b = CHECKLIST_HEADER
    pix = doc[1].get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(
        rect.x0 + rect.width*l, rect.y0 + rect.height*t,
        rect.x0 + rect.width*r, rect.y0 + rect.height*b))
    img = ImageOps.autocontrast(Image.open(io.BytesIO(pix.tobytes("png"))).convert("L"))
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=92)
    raw = vision._call_vision(prompt=CHECKLIST_PROMPT,
        image_b64=base64.b64encode(output.getvalue()).decode("ascii"),
        max_tokens=300, expects_json=True)
    data = vision._parse_json_object(raw, required_keys={"hospital", "product", "serial", "date", "asset"})
    if any(v is not None and not isinstance(v, str) for v in data.values()):
        raise vision.NvidiaResponseError("Invalid checklist field types")
    normalized = vision._normalize_ocr_data({
        "hospital_raw": data["hospital"], "product_raw": data["product"],
        "serial_candidates": [data["serial"]] if data["serial"] else [],
        "service_date_raw": data["date"], "date_source": "ACTION_DATE",
        "asset_candidates": [data["asset"]] if data["asset"] else [],
    })
    # Preserve the distinct physical source: checklist Date corroborates the
    # service date; it is never presented as handwriting in ACTION DATE.
    return {**normalized, "_page": 1, "_source": "pm_checklist_header"}


def read_and_match(doc, job_type, reference_day=None):
    from . import processor, private_ocr_context
    if len(doc) != (4 if job_type == "PM" else 1):
        raise ValueError("Incomplete jobsheet cannot enter matching")
    vision.reset_ocr_metrics()
    readings = []
    trace = []
    last_diagnostic = {}
    def read(call, source):
        try:
            readings.append({**call(), "_page": 0, "_source": source})
        except vision.NvidiaResponseError:
            trace.append("vision_read_unavailable")
    read(lambda: vision.ocr_jobsheet_fields(doc, 0, zoom=config.OCR_ZOOM_DEFAULT), "cover")
    read(lambda: vision.ocr_jobsheet_identity_fields(doc, 0, zoom=config.OCR_IDENTITY_ZOOM), "cover_identity")
    read(lambda: vision.ocr_jobsheet_support_fields(doc, 0, zoom=config.OCR_SUPPORT_ZOOM), "cover_support")
    if not readings:
        # A complete service outage is infrastructure failure, not evidence
        # that the paper is wrong. Let the existing durable retry path handle it.
        raise vision.NvidiaResponseError("No usable vision response; preserve source for retry")
    def attempt(*, allow_upload_prior=False):
        nonlocal last_diagnostic
        ev = evidence(readings)
        rows, reason = retrieve_rows(ac._device_index, ev)
        trace.append(reason)
        if reason in {"device_candidate_limit", "missing_serial_not_unique"}:
            return None
        pool, reason = live_pool(rows, ev, reference_day)
        trace.append(reason)
        last_diagnostic = diagnostic_pool(pool, ev, job_type)
        task, reason = select_task(pool, ev, job_type, reference_day,
                                  allow_upload_prior=allow_upload_prior)
        trace.append(reason)
        return task
    task = attempt()
    if task is None:
        # One bounded focused pass; raw alternatives are retained, not cleared
        # when they disagree. A private vocabulary contains no task or serial.
        vocabulary = private_ocr_context.build_vocabulary(ac._device_index)
        for field in ("product_raw", "hospital_raw", "serial_candidates", "phone_candidates", "service_date_raw"):
            ev = evidence(readings)
            if _stable(ev, field):
                continue
            for zoom in (5.0, 6.0):
                if field in {"product_raw", "hospital_raw"}:
                    read(lambda f=field, z=zoom: vision.ocr_jobsheet_context_field(doc, 0, f, vocabulary, zoom=z), "cover_focus")
                else:
                    read(lambda f=field, z=zoom: vision.ocr_jobsheet_focused_field(doc, 0, f, zoom=z), "cover_focus")
        task = attempt()
    if task is None and job_type == "PM":
        for zoom in (5.0, 6.0):
            try:
                readings.append(_checklist_read(doc, zoom))
            except vision.NvidiaResponseError:
                trace.append("checklist_read_unavailable")
        trace.append("pm_crosspage_read")
        task = attempt()
    if task is None:
        task = attempt(allow_upload_prior=True)
    ocr = processor._consensus_ocr(readings)
    ocr["ocr_metrics"] = vision.get_ocr_metrics()
    ocr["rules_audit"] = {"version": RULESET_VERSION, "trace": trace,
        "reference_date_known": reference_day is not None,
        "read_count": len(readings), "crosspage_used": any(r.get("_page") == 1 for r in readings),
        "last_live_diagnostic": last_diagnostic}
    return task, 2 if task else 0, ocr

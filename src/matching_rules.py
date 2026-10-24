"""Opt-in integration of evidence-preserving OCR and live visit confirmation.

The private index retrieves devices; it never certifies a visit. No sample
answers or customer identifiers belong here. Public diagnostics are categories
and counts only. This entry point has no cloud mutation or finalization path.
"""
from __future__ import annotations

import base64
import io
import os
from collections import Counter, defaultdict
from datetime import date, timedelta

import fitz
from PIL import Image, ImageOps

from . import asana_client as ac, asana_index, checklist_date_focus, config, date_corroboration, device_table_search, nvidia_client as vision, ocr_reference, pm_field_cards, serial_shape_reference

RULESET_VERSION = "evidence-live-v2"
MAX_DEVICE_ROWS = 10
MAX_LIVE_TASKS = 80
# Resource limits are per device, not an invitation to discard other devices.
# All retained competitors are read before the unchanged device/visit ranking.
MAX_SCANNED_TASKS = MAX_DEVICE_ROWS * MAX_LIVE_TASKS
VISIT_WINDOW_DAYS = 14
SEARCH_HARD_STOPS = {"device_candidate_limit", "task_candidate_limit", "live_devices_tied",
                    "live_visits_tied", "compact_serial_not_unique", "crosspage_identity_conflict",
                    "repeated_order_conflict", "live_order_conflict", "repeated_dates_conflict",
                    "ambiguous_order_numbers", "signature_date_conflict", "signature_date_protocol_rejected",
                    "checklist_date_conflict", "checklist_date_protocol_rejected"}
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
            if field in (reading.get("_evidence_exclusions") or {}):
                continue
            for key in {_key(field, v) for v in _values(reading, field)} - {""}:
                item = result[field].setdefault(key, {"votes": 0, "pages": Counter(), "auxiliary_votes": 0})
                if ocr_reference.field_can_vote(reading, field):
                    item["votes"] += 1
                    item["pages"][reading.get("_page", 0)] += 1
                else:
                    item["auxiliary_votes"] += 1
    return result


def _stable(ev, field):
    return {key for key, item in ev[field].items() if item["votes"] >= 2}


def _auxiliary_repeated(ev, field):
    return {key for key, item in ev[field].items() if item.get("auxiliary_votes", 0) >= 2}


def _should_recheck_stable_phone(ev, diagnostic, trace):
    """A stable phone is not verified merely because two readings agree.

    Only schedule the existing bounded focus pass, never weaken acceptance.
    Require repeated product/hospital plus either repeated serials or a bounded
    live identity rival. Do not retry a phone already corroborated
    on a plausible live task, or reinterpret known ties/conflicts as OCR faults.
    """
    if SEARCH_HARD_STOPS.intersection(trace):
        return False
    facts = diagnostic.get("nearest_live_facts", [])
    bounded_identity = bool(_serials(ev)) and any(
        row.get("product_supported") and row.get("hospital_supported")
        and row.get("type_matches") and not row.get("product_conflict")
        and not row.get("hospital_conflict") and row.get("serial_distance", 99) <= 3
        for row in facts)
    if (not _full_phones(_stable(ev, "phone_candidates"))
            or len(_stable(ev, "product_raw")) != 1
            or len(_stable(ev, "hospital_raw")) != 1
            or not (_stable(ev, "serial_candidates") | _stable(ev, "serial_visual_candidates")
                    or bounded_identity)):
        return False
    corroborated = any(row.get("phone_exact") and row.get("product_supported")
        and row.get("hospital_supported") and row.get("type_matches")
        and row.get("serial_distance", 99) <= 3
        for row in facts)
    return not corroborated


def _should_recheck_stable_serial(ev, diagnostic, trace):
    """Repeated handwriting errors may still need the existing focused pass.

    Only plausible live rivals with agreed product/hospital/type can request
    a reread. This schedules readers; it never substitutes a table serial.
    """
    if (SEARCH_HARD_STOPS.intersection(trace)
            or not (_stable(ev, "serial_candidates") | _stable(ev, "serial_visual_candidates"))
            or len(_stable(ev, "product_raw")) != 1 or len(_stable(ev, "hospital_raw")) != 1):
        return False
    relevant = [row for row in diagnostic.get("nearest_live_facts", [])
                if row.get("product_supported") and row.get("hospital_supported")
                and row.get("type_matches") and not row.get("product_conflict")
                and not row.get("hospital_conflict") and row.get("serial_distance", 99) <= 3]
    return bool(relevant) and not any(row["serial_distance"] == 0 for row in relevant)


def _strong_conflict(ev, field, actual):
    """A known repeated disagreement is different from an unknown short code."""
    stable = _stable(ev, field)
    return bool(stable and actual and not stable.intersection(actual))


def _serials(ev):
    return sorted(set(ev["serial_candidates"]) | set(ev["serial_visual_candidates"]))


def _serial_distance(observed, actual):
    if ocr_reference.compact_serial_hint(observed) or ocr_reference.compact_serial_hint(actual):
        return ocr_reference.compact_ocr_distance(observed, actual)
    return ac._lev(observed, actual)


def _full_phones(values):
    return {v for v in values if len(v) == 8 and v.isascii() and v.isdigit()}


def _weak_compact_hints(index, ev):
    """Read old weak-row task references, never treat the row as a device.

    All retrieval fields must co-occur on one historical task. Do not combine
    another visit's phone/product/location. A live parsed compact serial,
    repeated OCR and a reliable service date are still required later.
    """
    observed = _stable(ev, "serial_candidates") | _stable(ev, "serial_visual_candidates")
    if not any(ocr_reference.compact_serial_hint(v) for v in observed):
        return []
    products, hospitals = _stable(ev, "product_raw"), _stable(ev, "hospital_raw")
    phones = _full_phones(_stable(ev, "phone_candidates"))
    assets = _stable(ev, "asset_candidates")
    if not products or not hospitals or not phones:
        return []
    hints = []
    for row in index["devices"]:
        if not row.get("weak_identity"):
            continue
        refs = []
        for ref in row.get("task_refs") or []:
            product = ac._norm(ac.product_group(ref.get("product_variant") or ref.get("product")))
            phone = bool(_full_phones({ac._norm(v) for v in ref.get("phones") or []}) & phones)
            exact_asset = bool({ac._norm(v) for v in ref.get("assets") or []} & assets)
            # A legacy whitelist sometimes erased the entire product field.
            # In that narrow case require both same-task phone and exact Asset
            # for retrieval only. Never borrow product from a different visit.
            product_hint = product in products or (not product and exact_asset)
            if not (ref.get("gid") and phone and product_hint):
                continue
            hospital = {_key("hospital_raw", v) for v in (
                ref.get("hospital"), *(ref.get("hospital_aliases") or [])) if v} - {""}
            if hospital & hospitals:
                refs.append(ref)
        if refs:
            hints.append({**row, "task_refs": refs, "_retrieval_hint_only": True})
    return hints


def retrieve_rows(index, ev, readings=()):
    """Search equipment history first; a retrieved row does not certify a visit."""
    rows, reason = device_table_search.retrieve_table_rows(index, ev, readings)
    if reason == "device_candidate_limit":
        return [], reason
    # An old parser may leave a compact device under a weak row. Retrieve its
    # bounded historical task references only; identity is established live.
    hints = _weak_compact_hints(index, ev)
    if not rows and not hints:
        return [], "index_miss" if _serials(ev) else "missing_serial_not_unique"
    close = rows + hints
    if len(close) > MAX_DEVICE_ROWS:
        return [], "device_candidate_limit"  # Never silently exclude a rival.
    return close, "weak_compact_task_hints" if hints else reason


def live_pool(rows, ev, reference_day):
    groups = defaultdict(set)
    for row in rows:
        serial = (ac._norm(row.get("serial")) if not row.get("weak_identity")
                  and not row.get("_retrieval_hint_only") else "")
        groups[serial].update(str(ref['gid']) for ref in row.get('task_refs') or [] if ref.get('gid'))

    def within_budget():
        return (len([key for key in groups if key]) <= MAX_DEVICE_ROWS
                and all(len(values) <= MAX_LIVE_TASKS for values in groups.values())
                and len(set().union(*groups.values())) <= MAX_SCANNED_TASKS)

    def refresh(serial):
        groups[serial].update(str(task['gid']) for task in ac._typeahead(serial) if task.get('gid'))
        return within_budget()

    if not within_budget():
        return [], "task_candidate_limit"
    # NO indexed type/date filtering: both may have changed since the snapshot.
    if rows:
        # The index narrows equipment, not the set of visits forever. A new
        # service task may have been created after this snapshot. Refresh only
        # exact literal device serials from retrieved non-weak rows, never a
        # workspace-wide phone/asset query or fuzzy serial expansion.
        for serial in sorted({ac._norm(row.get("serial")) for row in rows
                              if not row.get("weak_identity") and not row.get("_retrieval_hint_only")})[:MAX_DEVICE_ROWS]:
            compact_guard = (ocr_reference.compact_serial_token(serial) is not None
                and bool(_stable(ev, "product_raw")) and bool(_stable(ev, "hospital_raw"))
                and any(len(v) == 8 and v.isascii() and v.isdigit() for v in ev["phone_candidates"]))
            if vision._valid_serial_token(serial) or compact_guard:
                if not refresh(serial):
                    return [], "task_candidate_limit"
    else:
        # New equipment may not be indexed. Only transcribed serials are used;
        # unbounded phone/asset workspace searches are deliberately avoided.
        for serial in _serials(ev)[:3]:
            compact_guard = (
                ocr_reference.compact_serial_token(serial) is not None
                and bool(_stable(ev, "product_raw"))
                and bool(_stable(ev, "hospital_raw"))
                and any(len(v) == 8 and v.isascii() and v.isdigit()
                        for v in ev["phone_candidates"])
            )
            if not vision._valid_serial_token(serial) and not compact_guard:
                continue
            if not refresh(serial):
                return [], "task_candidate_limit"
    gids = set().union(*groups.values())
    tasks = [ac._fetch_task(gid) for gid in sorted(gids)]
    if any(row.get("_retrieval_hint_only") for row in rows):
        # A legacy hint must not freeze the visit list at the old snapshot.
        # Only serials actually read from corroborated live task fields can
        # refresh visits. The ambiguous OCR token itself is never expanded.
        repeated = _stable(ev, "serial_candidates") | _stable(ev, "serial_visual_candidates")
        serials = set()
        for task in tasks:
            facts = _live_facts(task)
            if (facts and ocr_reference.compact_serial_token(facts["serial"])
                    and any(ocr_reference.compact_ocr_distance(v, facts["serial"]) <= 1 for v in repeated)
                    and _stable(ev, "product_raw") & facts["product"]
                    and _stable(ev, "hospital_raw") & facts["hospital"]
                    and _full_phones(_stable(ev, "phone_candidates") & facts["phones"])):
                serials.add(facts["serial"])
        if len(serials) > MAX_DEVICE_ROWS:
            return [], "device_candidate_limit"
        for serial in sorted(serials):
            if not refresh(serial):
                return [], "task_candidate_limit"
        gids = set().union(*groups.values())
        loaded = {str(task["gid"]) for task in tasks}
        tasks.extend(ac._fetch_task(gid) for gid in sorted(gids - loaded))
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
    distance = min((_serial_distance(s, facts["serial"]) for s in serials), default=99)
    serial_repeated = facts["serial"] in _stable(ev, "serial_candidates") | _stable(ev, "serial_visual_candidates")
    missing_hospital_fuzzy = False
    compact_ambiguity = False
    format_corroborated = False
    # Recovered prose does not vote itself into certainty. On a normal exact
    # repeated serial ONLY, live fields plus the opposite strong identity
    # field, repeated full phone, type and reliable date can corroborate it.
    # Two auxiliary fields cannot bootstrap each other into an identity.
    if (distance == 0 and serial_repeated and vision._valid_serial_token(facts["serial"])
            and _full_phones(_stable(ev, "phone_candidates") & facts["phones"])):
        if ("hospital" in support and "product" not in support
                and _auxiliary_repeated(ev, "product_raw") & facts["product"]):
            support.add("product")
            format_corroborated = True
        elif ("product" in support and "hospital" not in support
                and _auxiliary_repeated(ev, "hospital_raw") & facts["hospital"]):
            support.add("hospital")
            format_corroborated = True
    if serials:
        if distance > 3:
            return None
        compact_nearest = any(ocr_reference.compact_serial_hint(s)
                              and _serial_distance(s, facts["serial"]) == distance for s in serials)
        if ocr_reference.compact_serial_token(facts["serial"]) or compact_nearest:
            repeated = _stable(ev, "serial_candidates") | _stable(ev, "serial_visual_candidates")
            compact_ambiguity = (distance == 1
                and any(ocr_reference.compact_ocr_distance(v, facts["serial"]) == 1 for v in repeated))
            if ((not (distance == 0 and serial_repeated) and not compact_ambiguity)
                    or not {"hospital", "product", "phone"} <= support
                    or not _full_phones(set(ev["phone_candidates"]) & facts["phones"])):
                return None
            if compact_ambiguity and not _full_phones(_stable(ev, "phone_candidates") & facts["phones"]):
                return None
        if distance == 0:
            safe = (serial_repeated and {"hospital", "product"} <= support) or (
                "product" in support and "phone" in support
                and (serial_repeated or "hospital" in support))
            if not safe and {"hospital", "product", "asset_exact"} <= support:
                # Assess retained alternatives, not just the nearest string.
                # A lone exact read must not veto a fuzzy read that ALREADY
                # qualifies with this task's exact asset. Keep every original
                # read, and score only the supported fuzzy hypothesis; the
                # unsupported exact read must not grant the 100-point tier.
                alternatives = [_serial_distance(s, facts["serial"]) for s in serials
                                if not ocr_reference.compact_serial_hint(s)
                                and 1 <= _serial_distance(s, facts["serial"]) <= 3]
                if alternatives:
                    distance = min(alternatives)
                    safe = True
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
            "requires_reliable_date": missing_hospital_fuzzy or compact_ambiguity or format_corroborated,
            "compact_ambiguity": compact_ambiguity, "format_corroborated": format_corroborated}


def select_task(tasks, ev, job_type, reference_day, *, allow_upload_prior=True, date_support=(),
                checklist_date_support=()):
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
        if any(ac.resolve_order_number(row['task'])['status'] == 'ambiguous'
               and orders.intersection(ac.resolve_order_number(row['task'])['candidates'])
               for row in candidates):
            return None, 'ambiguous_order_numbers'
        candidates = [row for row in candidates
                      if ac.resolve_order_number(row['task'])['order_no'] in orders]
        if not candidates:
            return None, "live_order_conflict"
    devices = defaultdict(list)
    for row in candidates:
        devices[row["facts"]["serial"]].append(row)
    if len(devices) > MAX_DEVICE_ROWS:
        return None, "device_candidate_limit"
    if any(row.get("compact_ambiguity") for row in candidates) and len(devices) != 1:
        return None, "compact_serial_not_unique"
    ranking = sorted(devices, key=lambda s: max(r["score"] for r in devices[s]), reverse=True)
    if len(ranking) > 1 and (max(r["score"] for r in devices[ranking[0]])
                            - max(r["score"] for r in devices[ranking[1]])) < 15:
        return None, "live_devices_tied"
    visits = devices[ranking[0]]
    if len(visits) > MAX_LIVE_TASKS:
        return None, "task_candidate_limit"  # Never truncate a real device's visits.
    stable_dates = _stable(ev, "service_date_raw")
    if len(stable_dates) > 1:
        return None, "repeated_dates_conflict"
    action_day = date.fromisoformat(next(iter(stable_dates))) if stable_dates else None
    checklist_proof = checklist_date_focus.summarize(checklist_date_support)
    if checklist_proof['status'] == 'invalid' or (checklist_date_support and job_type != 'PM'):
        return None, 'checklist_date_protocol_rejected'
    if checklist_proof['status'] == 'conflict' or (action_day and checklist_proof['status'] == 'confirmed'
                                                  and action_day != checklist_proof['date']):
        return None, 'checklist_date_conflict'
    checklist_used = not action_day and checklist_proof['status'] == 'confirmed'
    if checklist_used:
        action_day = checklist_proof['date']
    proof = date_corroboration.summarize(date_support)
    if (proof["status"] == "invalid" or (job_type != "PM" and
            any(r.get("page") == 3 for r in date_support if isinstance(r, dict)))):
        return None, "signature_date_protocol_rejected"
    if proof["status"] == "conflict" or (action_day and proof["status"] == "confirmed"
                                           and action_day != proof["date"]):
        return None, "signature_date_conflict"
    supplemented = not action_day and proof["status"] == "confirmed"
    if supplemented:
        action_day = proof["date"]
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
        if ac.resolve_order_number(best['task'])['status'] == 'ambiguous':
            return None, 'ambiguous_order_numbers'
        return best["task"], ("checklist_date_live_visit_confirmed" if checklist_used else
                             "signature_date_live_visit_confirmed" if supplemented
                              else "action_date_live_visit_confirmed")
    if any(row.get("requires_reliable_date") for row in visits):
        return None, ("compact_serial_needs_action_date" if any(row.get("compact_ambiguity") for row in visits)
                      else "format_recovery_needs_action_date" if any(row.get("format_corroborated") for row in visits)
                      else "missing_hospital_fuzzy_needs_action_date")
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
    if ac.resolve_order_number(dated[0][1]['task'])['status'] == 'ambiguous':
        return None, 'ambiguous_order_numbers'
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
            "serial_distance": min((_serial_distance(v, facts["serial"]) for v in serials), default=99),
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


def public_diagnostic(audit):
    """Project a private audit onto fixed categories, counts and booleans only.

    Never serialize the audit wholesale: future private fields, exception
    messages and task/OCR values must not enter public Actions logs.
    """
    if not isinstance(audit, dict) or audit.get("version") != RULESET_VERSION:
        return {}
    allowed_reasons = {
        "vision_read_unavailable", "checklist_read_unavailable", "pm_crosspage_read",
        "device_candidate_limit", "missing_serial_not_unique", "index_miss",
        "serial_first_retrieval", "task_candidate_limit", "live_tasks_read",
        "live_identity_not_confirmed", "crosspage_identity_conflict",
        "repeated_order_conflict", "live_order_conflict", "live_devices_tied",
        "repeated_dates_conflict", "live_visit_date_conflict", "live_visits_tied",
        "action_date_live_visit_confirmed", "missing_hospital_fuzzy_needs_action_date",
        "upload_date_unknown", "need_crosspage_before_upload_prior",
        "upload_prior_not_unique", "upload_prior_live_visit_confirmed",
        "product_reread_drift",
        "weak_compact_task_hints", "compact_serial_not_unique", "compact_serial_needs_action_date",
        "format_recovery_needs_action_date",
        "stable_phone_reread", "numeric_literal_auxiliary_retained",
        "stable_serial_reread", "vision_service_incomplete", "device_search_unavailable",
        "serial_shape_reference_read",
        "ambiguous_order_numbers",
        "signature_dates_read", "signature_read_unavailable", "signature_date_conflict",
        "signature_date_protocol_rejected", "signature_date_live_visit_confirmed",
        "checklist_date_focus_read", "checklist_date_focus_unavailable", "checklist_date_conflict",
        "checklist_date_protocol_rejected", "checklist_date_live_visit_confirmed",
        "table_context_retrieval", "table_fragment_retrieval",
        "device_search_proposal", "device_search_uncertain", "device_search_protocol_rejected",
        "device_search_disagrees",
    }
    def count(value, ceiling):
        return min(max(value, 0), ceiling) if type(value) is int else 0
    result = {
        "version": RULESET_VERSION,
        "trace": [v for v in (audit.get("trace") or [])
                  if isinstance(v, str) and v in allowed_reasons][:30],
        "reference_date_known": audit.get("reference_date_known") is True,
        "read_count": count(audit.get("read_count"), 100),
        "crosspage_used": audit.get("crosspage_used") is True,
    }
    table = audit.get("equipment_search")
    if isinstance(table, dict):
        result["equipment_search"] = {
            "candidate_count": count(table.get("candidate_count"), MAX_DEVICE_ROWS),
            "legacy_reference_count": count(table.get("legacy_reference_count"), MAX_DEVICE_ROWS),
            "model_invoked": table.get("model_invoked") is True,
        }
    support = audit.get("date_support")
    if isinstance(support, dict):
        result["date_support"] = {key: count(support.get(key), ceiling) for key, ceiling in
            (("read_count", 8), ("stable_panel_count", 4), ("page_count", 2))}
        result["date_support"]["confirmed"] = support.get("status") == "confirmed"
    checklist = audit.get('checklist_date_focus')
    if isinstance(checklist, dict):
        result['checklist_date_focus'] = {'read_count': count(checklist.get('read_count'), 2),
            'page_count': count(checklist.get('page_count'), 1),
            'confirmed': checklist.get('status') == 'confirmed'}
    diagnostic = audit.get("last_live_diagnostic")
    if not isinstance(diagnostic, dict):
        return result
    result["live"] = {k: count(diagnostic.get(k), MAX_SCANNED_TASKS if k == "live_task_count" else MAX_LIVE_TASKS)
                      for k in ("live_task_count", "repeated_product_count",
                                "repeated_hospital_count", "repeated_date_count")}
    rows = []
    source_rows = diagnostic.get("nearest_live_facts")
    for row in (source_rows if isinstance(source_rows, list) else [])[:MAX_DEVICE_ROWS]:
        if not isinstance(row, dict):
            continue
        clean = {k: row.get(k) is True for k in (
            "product_supported", "product_conflict", "hospital_supported",
            "hospital_conflict", "phone_exact", "type_matches", "passes_identity")}
        clean["serial_distance"] = count(row.get("serial_distance"), 99)
        clean["asset_level"] = count(row.get("asset_level"), 2)
        rows.append(clean)
    result["live"]["nearest_live_facts"] = rows
    return result


def _checklist_read(doc, zoom):
    if os.environ.get(pm_field_cards.ENABLED_ENV) == "1":
        image_b64 = pm_field_cards.render(doc, zoom)
        prompt = (
            "These labelled panels are separate handwritten fields from PDF page 2, "
            "Philips Ultrasound System PM Checklist page 1 of 3. Read only each "
            "panel's value, not the new label. Customer is hospital/site, System "
            "is product, sn is the complete MACHINE serial, Date is checklist "
            "date, Asset No. is asset. Inspect each serial character left to right; "
            "letters may be joined to digits. Preserve visible letters and digits, "
            "use ? for unclear characters and null for blank fields. Do not complete "
            "a serial from a product, copy neighbouring values, or infer missing "
            "digits. No device candidates or previous readings are supplied. "
            'JSON only: {"hospital":null,"product":null,"serial":null,"date":null,"asset":null}'
        )
    else:
        image_b64 = _checklist_strip(doc, zoom)
        prompt = CHECKLIST_PROMPT
    raw = vision._call_vision(prompt=prompt, image_b64=image_b64,
                             max_tokens=300, expects_json=True)
    data = vision._parse_json_object(raw, required_keys={"hospital", "product", "serial", "date", "asset"})
    if set(data) != {"hospital", "product", "serial", "date", "asset"} or any(
            v is not None and not isinstance(v, str) for v in data.values()):
        raise vision.NvidiaResponseError("Invalid checklist field types")
    normalized = vision._normalize_ocr_data({
        "hospital_raw": data["hospital"], "product_raw": data["product"],
        "serial_candidates": [data["serial"]] if data["serial"] else [],
        "service_date_raw": data["date"], "date_source": "ACTION_DATE",
        "asset_candidates": [data["asset"]] if data["asset"] else [],
    })
    return {**normalized, "_page": 1, "_source": "pm_checklist_header"}


def _checklist_strip(doc, zoom):
    rect = doc[1].rect
    l, t, r, b = CHECKLIST_HEADER
    pix = doc[1].get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(
        rect.x0 + rect.width*l, rect.y0 + rect.height*t,
        rect.x0 + rect.width*r, rect.y0 + rect.height*b))
    img = ImageOps.autocontrast(Image.open(io.BytesIO(pix.tobytes("png"))).convert("L"))
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=92)
    return base64.b64encode(output.getvalue()).decode("ascii")


def read_and_match(doc, job_type, reference_day=None):
    from . import device_search_agent, processor, private_ocr_context
    if os.environ.get(pm_field_cards.ENABLED_ENV) == "1":
        pm_field_cards.require_read_only()
    serial_shapes_enabled = os.environ.get(serial_shape_reference.ENABLED_ENV) == '1'
    if serial_shapes_enabled and os.environ.get('JOBSHEET_DRY_RUN') != '1':
        raise ValueError('Serial reference requires read-only mode')
    search_agent_enabled = os.environ.get(device_search_agent.ENABLED_ENV) == "1"
    if search_agent_enabled:
        device_search_agent.require_read_only()  # Fail before any OCR/network access.
    signature_dates_enabled = os.environ.get(date_corroboration.ENABLED_ENV) == "1"
    if signature_dates_enabled:
        date_corroboration.require_read_only()
    checklist_focus_enabled = os.environ.get(checklist_date_focus.ENABLED_ENV) == '1'
    if checklist_focus_enabled:
        checklist_date_focus.require_read_only()
    if len(doc) != (4 if job_type == "PM" else 1):
        raise ValueError("Incomplete jobsheet cannot enter matching")
    vision.reset_ocr_metrics()
    readings = []
    trace = []
    last_diagnostic = {}
    last_rows = []
    equipment_audit = {}
    date_support = []  # Separate physical sources, never merged into OCR vote counts.
    checklist_date_support = []  # Separate checklist provenance, never cover ACTION DATE.
    service_incomplete = False
    def read(call, source, field=None):
        nonlocal service_incomplete
        try:
            result = {**call(), "_page": 0, "_source": source,
                      "_read_context": {"stage": source, "field": field}}
            if result.get("_auxiliary_candidates"):
                trace.append("numeric_literal_auxiliary_retained")
            if field == "product_raw" and ocr_reference.product_focus_conflicts(readings, result):
                result["_evidence_exclusions"] = {"product_raw": "product_reread_drift"}
                result.setdefault("_ocr_audit", {})["evidence_exclusions"] = dict(result["_evidence_exclusions"])
                trace.append("product_reread_drift")
            readings.append(result)
        except vision.NvidiaResponseError as exc:
            service_incomplete |= isinstance(exc, vision.NvidiaServiceUnavailable)
            trace.append("vision_read_unavailable")
    read(lambda: vision.ocr_jobsheet_fields(doc, 0, zoom=config.OCR_ZOOM_DEFAULT), "cover")
    read(lambda: vision.ocr_jobsheet_identity_fields(doc, 0, zoom=config.OCR_IDENTITY_ZOOM), "cover_identity")
    read(lambda: vision.ocr_jobsheet_support_fields(doc, 0, zoom=config.OCR_SUPPORT_ZOOM), "cover_support")
    if not readings:
        # A complete service outage is infrastructure failure, not evidence
        # that the paper is wrong. Let the existing durable retry path handle it.
        raise vision.NvidiaResponseError("No usable vision response; preserve source for retry")
    def attempt(*, allow_upload_prior=False, table_rows=None):
        nonlocal last_diagnostic, last_rows
        ev = evidence(readings)
        rows, reason = (retrieve_rows(ac._device_index, ev, readings) if table_rows is None
                        else (table_rows, "device_search_proposal"))
        last_rows = rows
        equipment_audit["candidates"] = [device_table_search.DeviceTable.candidate_view(
            {"candidate_id": f"T{number}", "row": row}) for number, row in enumerate(rows, 1)
            if not row.get("weak_identity")]
        equipment_audit["candidate_count"] = len(equipment_audit["candidates"])
        equipment_audit["legacy_reference_count"] = sum(bool(row.get("weak_identity")) for row in rows)
        trace.append(reason)
        if reason in {"device_candidate_limit", "missing_serial_not_unique"}:
            return None
        pool, reason = live_pool(rows, ev, reference_day)
        trace.append(reason)
        last_diagnostic = diagnostic_pool(pool, ev, job_type)
        task, reason = select_task(pool, ev, job_type, reference_day,
                                  allow_upload_prior=allow_upload_prior, date_support=date_support,
                                  checklist_date_support=checklist_date_support)
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
                if field == "phone_candidates" and _should_recheck_stable_phone(ev, last_diagnostic, trace):
                    trace.append("stable_phone_reread")
                elif field == "serial_candidates" and _should_recheck_stable_serial(ev, last_diagnostic, trace):
                    trace.append("stable_serial_reread")
                else:
                    continue
            for zoom in (5.0, 6.0):
                if field in {"product_raw", "hospital_raw"}:
                    read(lambda f=field, z=zoom: vision.ocr_jobsheet_context_field(doc, 0, f, vocabulary, zoom=z), "cover_focus", field)
                elif field == 'serial_candidates' and zoom == 6.0 and serial_shapes_enabled and (
                        shapes := serial_shape_reference.project(ac._device_index, _stable(ev, 'product_raw'))):
                    trace.append('serial_shape_reference_read')
                    read(lambda: vision.ocr_jobsheet_serial_with_shapes(doc, 0, shapes, zoom=zoom), 'cover_focus', field)
                else:
                    read(lambda f=field, z=zoom: vision.ocr_jobsheet_focused_field(doc, 0, f, zoom=z), "cover_focus", field)
        task = attempt()
    if task is None and job_type == "PM":
        for zoom in (5.0, 6.0):
            try:
                readings.append(_checklist_read(doc, zoom))
            except vision.NvidiaResponseError as exc:
                service_incomplete |= isinstance(exc, vision.NvidiaServiceUnavailable)
                trace.append("checklist_read_unavailable")
        trace.append("pm_crosspage_read")
        task = attempt()
    if (task is None and job_type == 'PM' and checklist_focus_enabled
            and not SEARCH_HARD_STOPS.intersection(trace)
            and not _stable(evidence(readings), 'service_date_raw')
            and any(row.get('passes_identity') for row in last_diagnostic.get('nearest_live_facts', []))):
        for zoom in checklist_date_focus.ZOOMS:
            try:
                value = vision.ocr_pm_checklist_focused_date(doc, page_idx=1, zoom=zoom)
            except vision.NvidiaResponseError as exc:
                service_incomplete |= isinstance(exc, vision.NvidiaServiceUnavailable)
                value = None
                trace.append('checklist_date_focus_unavailable')
            checklist_date_support.append({'page': checklist_date_focus.PAGE,
                'field': checklist_date_focus.FIELD, 'source': checklist_date_focus.SOURCE,
                'zoom': zoom, 'date': value})
        trace.append('checklist_date_focus_read')
        task = attempt()
    if (task is None and signature_dates_enabled and not SEARCH_HARD_STOPS.intersection(trace)
            and checklist_date_focus.summarize(checklist_date_support)['status'] not in {'confirmed', 'conflict', 'invalid'}
            and not _stable(evidence(readings), "service_date_raw")
            and any(row.get("passes_identity") for row in last_diagnostic.get("nearest_live_facts", []))):
        for page, fields, source in ((0, date_corroboration.COVER_FIELDS, "cover_signature"),
                                    (3, date_corroboration.CHECKLIST_FIELDS, "pm_checklist_signature")):
            if page == 3 and job_type != "PM":
                break
            for field in fields:
                for zoom in date_corroboration.ZOOMS:
                    try:
                        value = vision.ocr_jobsheet_signature_date(doc, page, field, zoom=zoom)
                    except vision.NvidiaResponseError as exc:
                        service_incomplete |= isinstance(exc, vision.NvidiaServiceUnavailable)
                        value = None
                        trace.append("signature_read_unavailable")
                    date_support.append({"page": page, "field": field, "source": source,
                                         "zoom": zoom, "date": value})
            trace.append("signature_dates_read")
            proof = date_corroboration.summarize(date_support)
            # A disagreement is a hold, not an invitation to seek a majority.
            if proof["status"] in {"confirmed", "conflict", "invalid"}:
                break
        task = attempt()
    if task is None:
        task = attempt(allow_upload_prior=True)
    if task is None and search_agent_enabled and not SEARCH_HARD_STOPS.intersection(trace):
        equipment_audit["model_invoked"] = True
        try:
            proposal = device_search_agent.run_search(doc, ac._device_index, evidence(readings), readings, last_rows)
        except vision.NvidiaResponseError as exc:
            # Keep all successful readings and diagnostics. A failed final
            # model search must not erase the earlier identity/date evidence.
            service_incomplete |= isinstance(exc, vision.NvidiaServiceUnavailable)
            proposal = {"status": "unavailable", "reason": "device_search_unavailable", "audit": {}}
        equipment_audit["model_search"] = proposal["audit"]
        trace.append(proposal["reason"])
        if proposal["status"] == "proposal":
            # A proposal cannot erase an already retrieved rival or create a
            # second OCR vote. Validate the union with unchanged live gates.
            combined = list(last_rows)
            for row in proposal["rows"]:
                if all(row is not old for old in combined):
                    combined.append(row)
            if len(combined) > MAX_DEVICE_ROWS:
                trace.append("device_candidate_limit")
            else:
                task = attempt(allow_upload_prior=True, table_rows=combined)
                if task is not None:
                    facts = _live_facts(task)
                    if not facts or facts["serial"] != proposal["proposed_serial"]:
                        trace.append("device_search_disagrees")
                        task = None
    if service_incomplete:
        trace.append("vision_service_incomplete")
    ocr = processor._consensus_ocr(readings)
    ocr["_vision_service_incomplete"] = service_incomplete
    ocr["ocr_metrics"] = vision.get_ocr_metrics()
    ocr["rules_audit"] = {"version": RULESET_VERSION, "trace": trace,
        "reference_date_known": reference_day is not None,
        "read_count": len(readings), "crosspage_used": any(r.get("_page") == 1 for r in readings),
        "last_live_diagnostic": last_diagnostic,
        "equipment_search": {key: equipment_audit.get(key) for key in
            ("candidate_count", "legacy_reference_count", "model_invoked")},
        "date_support": {key: value for key, value in date_corroboration.summarize(date_support).items()
                         if key != "date"},
        'checklist_date_focus': {key: value for key, value in checklist_date_focus.summarize(checklist_date_support).items()
                                 if key != 'date'}}
    ocr["_device_table_audit"] = equipment_audit  # Private, never an OCR reading.
    ocr["_signature_date_audit"] = date_support
    ocr['_checklist_date_audit'] = checklist_date_support
    return task, 2 if task else 0, ocr

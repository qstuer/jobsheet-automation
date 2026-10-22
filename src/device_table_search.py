"""Read-only equipment-table search, distinct from OCR votes and service visits.

Queries use document clues, rows aggregate a known Serial's historical aliases.
No network, dates, job type, task selection, index mutation or upload lives here.
The optional model bridge chooses query fields, not arbitrary code or new values.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import re

from . import asana_client as ac, asana_index, ocr_reference

MAX_ROWS = 10
MAX_SEARCHES = 3
FIELDS = ("serial", "product", "hospital", "phone", "contact", "asset")
EVIDENCE_FIELDS = dict(zip(FIELDS, ("serial_candidates", "product_raw", "hospital_raw",
                                  "phone_candidates", "contact_person_raw", "asset_candidates")))


def _items(value):
    return value if isinstance(value, list) else [value] if value else []


def collect_clues(ev, readings=()):
    """A retrieval projection only; never write recovered values into evidence."""
    clues = {field: set(ev.get(key, {})) for field, key in EVIDENCE_FIELDS.items()}
    clues["serial"].update(ev.get("serial_visual_candidates", {}))
    for reading in readings:
        # Validated numeric recovery may help search, but remains zero votes.
        for value in (reading.get("_auxiliary_candidates") or {}).get("phone_candidates", []):
            if isinstance(value, str) and re.fullmatch(r"[0-9]{8}", value):
                clues["phone"].add(value)
        # Keep uncertainty markers intact. These literals may search the
        # PRIVATE table; they cannot initiate global Asana queries or certify ID.
        raw = (reading.get("_ocr_audit") or {}).get("raw", {}).get("serial_candidates")
        for value in _items(raw):
            if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9?]{4,12}", value)
                    and sum(c.isalnum() for c in value) >= 4
                    and any(c.isalpha() for c in value) and any(c.isdigit() for c in value)):
                clues["serial"].add(value.upper())
    return {field: sorted(values) for field, values in clues.items()}


def _facts(row, hospital_key):
    """Identity is device-wide, not restricted to one past PM/CM contact."""
    return {
        "serial": ac._norm(row.get("serial")),
        "product": {ac._norm(ac.product_group(v)) for v in (
            *_items(row.get("product")), *(row.get("product_variants") or []),
            *(row.get("product_families") or []))} - {""},
        "hospital": {hospital_key(v) for v in (
            [*(row.get("hospitals") or []), *(row.get("hospital_aliases") or [])]
            or row.get("locations") or [])} - {""},
        "phone": {ac._norm(v) for v in row.get("phones") or []} - {""},
        "contact": {ac._text_norm(v) for v in row.get("contacts") or []} - {""},
        "asset": {ac._norm(v) for v in row.get("assets") or []} - {""},
    }


def serial_distance(observed, actual):
    if ocr_reference.compact_serial_hint(observed) or ocr_reference.compact_serial_hint(actual):
        return ocr_reference.compact_ocr_distance(observed, actual)
    if "?" in observed:
        return 99  # Do not silently delete unknown characters for edit distance.
    return ac._lev(observed, actual)


def _serial_matches(observed, actual, mode):
    if not isinstance(observed, str) or not re.fullmatch(r"[A-Z0-9?]{4,12}", observed):
        return False
    if mode == "exact":
        return observed == actual
    if mode == "fragment":
        if "?" in observed:
            return (sum(c != "?" for c in observed) >= 4 and len(observed) == len(actual)
                    and all(a == "?" or a == b for a, b in zip(observed, actual)))
        return len(observed) >= 4 and observed in actual
    return serial_distance(observed, actual) <= 3 and ac._key_similarity(observed, actual) >= 0.5


class DeviceTable:
    """A bounded search function over a private immutable equipment snapshot."""
    def __init__(self, index, clues):
        if not isinstance(index, dict) or not isinstance(index.get("devices"), list):
            raise asana_index.AsanaIndexError("Validated private device table required")
        if any(not isinstance(row, dict) for row in index["devices"]):
            raise asana_index.AsanaIndexError("Invalid private device row")
        self.clues = deepcopy(clues)
        self.entries = []
        # Cache only inside this immutable search snapshot, never across index
        # replacements. Many devices repeat the same hospital aliases.
        @lru_cache(maxsize=None)
        def hospital_key(value):
            return ac._norm(ac.hospital_core(value))
        for position, row in enumerate(index["devices"], 1):
            # A missing Serial is a legacy task hint, never a device identity.
            if row.get("weak_identity") or not row.get("serial"):
                continue
            if not isinstance(row["serial"], str) or not re.fullmatch(r"[A-Za-z0-9]{4,16}", row["serial"]):
                continue
            self.entries.append({"candidate_id": f"D{position}", "row": row, "facts": _facts(row, hospital_key)})

    def search_devices(self, fields, serial_mode="fuzzy"):
        """AND selected fields; OR a field's observed alternatives/history.

        A model may choose field combinations/mode, not invent query values.
        All rivals in the result must fit the cap; never return a top-ten slice.
        """
        if (not isinstance(fields, list) or not fields
                or any(not isinstance(field, str) or field not in FIELDS for field in fields)
                or len(fields) != len(set(fields))
                or not isinstance(serial_mode, str) or serial_mode not in {"exact", "fuzzy", "fragment"}):
            raise ValueError("Invalid equipment query")
        if (any(not self.clues.get(field) for field in fields)
                or ("serial" not in fields and len(fields) < 2)):
            return {"status": "insufficient_clues", "count": 0, "entries": []}
        matches = []
        for entry in self.entries:
            facts = entry["facts"]
            supported = {field: (any(_serial_matches(v, facts["serial"], serial_mode)
                for v in self.clues["serial"]) if field == "serial" else
                bool(set(self.clues[field]) & facts[field])) for field in fields}
            if all(supported.values()):
                matches.append(entry)
        if len(matches) > MAX_ROWS:
            return {"status": "candidate_limit", "count": len(matches), "entries": []}
        return {"status": "ok", "count": len(matches), "entries": matches}

    @staticmethod
    def candidate_view(entry):
        """No task GID/order/date/type can leak into equipment decisions."""
        row = entry["row"]
        result = {"candidate_id": entry["candidate_id"], "serial": row["serial"],
                  "identity_source": "private_device_table_not_ocr", "truncated_counts": {}}
        for name in ("product_variants", "product_families", "hospitals", "hospital_aliases",
                     "locations", "department_rooms", "phones", "contacts", "assets"):
            values = list(row.get(name) or [])
            result[name] = values[:12]
            if len(values) > 12:
                result["truncated_counts"][name] = len(values) - 12
        return result


def retrieve_table_rows(index, ev, readings=()):
    """Serial-first automatically, then a bounded table-only context search.

    Dates/type never filter devices. A mismatching phone does not veto a known
    serial. Historical aliases may support equipment search across visits.
    Existing live acceptance still judges the actual document and visit.
    """
    table = DeviceTable(index, collect_clues(ev, readings))
    ranked = []
    for entry in table.entries:
        facts = entry["facts"]
        serials = [v for v in table.clues["serial"] if "?" not in v]
        if not serials:
            continue
        distance = min(serial_distance(v, facts["serial"]) for v in serials)
        if not any(_serial_matches(v, facts["serial"], "fuzzy") for v in serials):
            continue
        product = bool(set(table.clues["product"]) & facts["product"])
        hospital = bool(set(table.clues["hospital"]) & facts["hospital"])
        if distance >= 2 and not (product and hospital):
            continue
        phone = bool(set(table.clues["phone"]) & facts["phone"])
        ranked.append(((distance, -int(hospital), -int(product), -int(phone)), entry))
    ranked.sort(key=lambda pair: pair[0])
    if ranked:
        closest = ranked[0][0][0]
        entries = [entry for rank, entry in ranked if rank[0] <= closest + 1]
        reason = "serial_first_retrieval"
    else:
        # A partial/uncertain raw serial can search the table without becoming
        # a usable OCR serial. Prefer the actual fragment over broad context.
        found = table.search_devices(["serial"], "fragment")
        if found["status"] == "candidate_limit":
            return [], "device_candidate_limit"
        entries, reason = found["entries"], "table_fragment_retrieval"
        if not entries:
            found = table.search_devices(["product", "hospital"])
            if found["status"] == "candidate_limit":
                return [], "device_candidate_limit"
            entries, reason = found["entries"], "table_context_retrieval"
    if len(entries) > MAX_ROWS:
        return [], "device_candidate_limit"
    return [entry["row"] for entry in entries], reason if entries else "index_miss"


class SearchSession:
    """JSON function-call dispatcher: no arbitrary functions, writes or values."""
    def __init__(self, table):
        self.table = table
        self.search_count = 0
        self.seen = {}
        self.history = []
        self.stopped = False

    def call(self, arguments):
        if self.stopped or not isinstance(arguments, dict):
            raise ValueError("Equipment search session stopped or invalid request")
        if (set(arguments) != {"action", "fields", "serial_mode"}
                or arguments["action"] != "search_devices"):
            raise ValueError("Only the equipment search function is available")
        if self.search_count >= MAX_SEARCHES:
            self.stopped = True
            return {"status": "query_limit", "count": 0, "candidates": []}
        self.search_count += 1
        result = self.table.search_devices(arguments["fields"], arguments["serial_mode"])
        new = {entry["candidate_id"]: entry for entry in result["entries"]}
        if result["status"] == "candidate_limit" or len(set(self.seen) | set(new)) > MAX_ROWS:
            self.stopped = True
            response = {"status": "candidate_limit", "count": max(result["count"], len(set(self.seen) | set(new))), "candidates": []}
        else:
            self.seen.update(new)
            response = {"status": result["status"], "count": result["count"],
                        "candidates": [self.table.candidate_view(entry) for entry in result["entries"]]}
        self.history.append({"request": deepcopy(arguments), "response": response})
        return response

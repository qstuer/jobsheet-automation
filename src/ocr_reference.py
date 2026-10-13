"""Private-index-supported transcription, never an equipment/task answer key."""
from __future__ import annotations

import re
from collections import Counter

from . import asana_client, product_catalog


def field_can_vote(reading: dict, field: str) -> bool:
    """Auxiliary/rejected interpretations never become strong OCR consensus."""
    return (field not in (reading.get("_evidence_exclusions") or {})
            and field not in (reading.get("_auxiliary_fields") or []))


def product_focus_conflicts(readings: list[dict], focused: dict) -> bool:
    """Detect a large re-read jump, not decide which spelling is correct.

    A repeated cover transcription, including a rejected but model-shaped
    string, is an ink anchor. It cannot certify a product, but a focus pass
    must not silently replace it with an unrelated familiar family. An
    independent checklist can supply new evidence; it never overwrites raw.
    """
    value = focused.get("product_raw")
    if not value or "product_raw" in (focused.get("_evidence_exclusions") or {}):
        return False
    anchors = Counter()
    for reading in readings:
        if reading.get("_source") not in {"cover", "cover_identity"}:
            continue
        raw = (reading.get("_ocr_audit") or {}).get("raw", {}).get("product_raw")
        raw = raw or reading.get("product_raw")
        if model_like(raw) and field_can_vote(reading, "product_raw"):
            anchors[asana_client._norm(asana_client.normalize_product(raw))] += 1
    repeated = [anchor for anchor, count in anchors.items() if count >= 2]
    if not repeated:
        return False
    target = asana_client._norm(asana_client.normalize_product(value))
    for anchor in repeated:
        if asana_client.product_group(anchor) == asana_client.product_group(target):
            return False  # Broad-family matching intentionally ignores model suffixes.
        if (re.findall(r"\d+", anchor) == re.findall(r"\d+", target)
                and re.split(r"\d+", anchor)[-1] == re.split(r"\d+", target)[-1]
                and (asana_client._lev(anchor, target) <= 2 or asana_client._key_similarity(anchor, target) >= 0.5)):
            # A badly spelled long family can still preserve most characters
            # plus all model digits/suffix. This permits re-reading, not an
            # identity match; live hospital/serial/phone/date remain required.
            return False
    return True


def compact_serial_token(value: str | None) -> str | None:
    """An unverified legacy token, usable only with bounded corroboration.

    Six/seven-character mixed identifiers occur on older equipment. Do not
    manufacture prefixes, accept ordinary words, strip uncertainty markers,
    or promote this shape into permission for unrestricted workspace search.
    """
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{6,7}", text):
        return None
    if re.match(r"^(?:CM|PM|WO|SR|RM|ROOM|ASSET)\d", text, re.I):
        return None
    if sum(c.isalpha() for c in text) < 2 or sum(c.isdigit() for c in text) < 2:
        return None
    return text.upper()


def compact_serial_hint(value: str | None) -> str | None:
    """Keep a literal O/0-ambiguous compact reading; never generate variants.

    The weaker one-digit shape is only a retrieval hint inside a bounded
    index. It cannot initiate a workspace search or certify a serial.
    """
    literal = compact_serial_token(value)
    if literal:
        return literal
    text = str(value or "").strip().upper()
    if (not re.fullmatch(r"[A-Z0-9]{6,7}", text)
            or re.match(r"^(?:CM|PM|WO|SR|RM|ROOM|ASSET)\d", text)
            or sum(c.isdigit() for c in text) != 1 or "O" not in text):
        return None
    return text


def compact_ocr_distance(observed: str, actual: str) -> int:
    """Only literal equality or one O/0 swap against a real compact serial.

    No insertions, deletions, prefixes or arbitrary character correction.
    Return a distance, never an invented serial string.
    """
    left, right = compact_serial_hint(observed), compact_serial_token(actual)
    if not left or not right or len(left) != len(right):
        return 99
    differences = [(a, b) for a, b in zip(left, right) if a != b]
    if not differences:
        return 0
    return 1 if len(differences) == 1 and set(differences[0]) == {"O", "0"} else 99


def model_like(value: str | None) -> bool:
    info = product_catalog.inspect_product(value)
    # Reject reference/serial contamination and multiple independent numbers.
    return (product_catalog.indexable(info)
            and len(re.findall(r"\d+", str(value or ""))) <= 1
            and asana_client.product_group(value) is not None)


def product_support(index: dict | None, value: str | None) -> str | None:
    """Find comparison support, not model truth; preserve digits and suffixes."""
    if not value:
        return None
    canonical = asana_client.normalize_product(value)
    if canonical in asana_client.OCR_PRODUCT_NAMES:
        return "repository_spelling"
    if not model_like(value) or not isinstance(index, dict):
        return None
    variants = {v for row in index.get("devices") or []
                for v in row.get("product_variants") or []
                if isinstance(v, str) and model_like(v)}
    # Weak device rows can supply model vocabulary, never device identity.
    # They are still excluded by the matching engine's device retrieval.
    key = asana_client._norm(value)
    if key in {asana_client._norm(v) for v in variants}:
        return "indexed_spelling"
    group = asana_client.product_group(value)
    if group in {asana_client.product_group(v) for v in variants}:
        return "indexed_family"
    return None

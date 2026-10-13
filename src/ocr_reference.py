"""Private-index-supported transcription, never an equipment/task answer key."""
from __future__ import annotations

import re

from . import asana_client, product_catalog


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

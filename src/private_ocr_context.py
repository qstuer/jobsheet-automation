"""Project a small, validated OCR vocabulary from the private device index.

Only confirmed model spellings and confirmed short hospital aliases leave the
index. Serial numbers, task references, contacts, phones and detailed places
are never returned or logged. The projection is advisory, never an answer key.
"""
from __future__ import annotations

import re
from collections import Counter

from . import asana_client, product_catalog


def build_vocabulary(index: dict) -> dict:
    if (not isinstance(index, dict) or index.get("schema_version") != 3
            or not isinstance(index.get("devices"), list)
            or not isinstance(index.get("location_directory"), list)):
        raise ValueError("A validated private device index is required")

    models: Counter[str] = Counter()
    families: Counter[str] = Counter()
    for device in index["devices"]:
        if not isinstance(device, dict) or device.get("weak_identity") or not device.get("serial"):
            continue
        seen = set()
        for variant in device.get("product_variants") or []:
            assessment = product_catalog.inspect_product(variant)
            if assessment["status"] != "confirmed":
                continue
            model = assessment["canonical"]
            if model not in seen:
                seen.add(model)
                models[model] += 1
        for model in seen:
            family = asana_client.product_group(model)
            if family:
                families[family] += 1

    codes: Counter[str] = Counter()
    groups = []
    for location in index["location_directory"]:
        if (not isinstance(location, dict) or location.get("match_enabled") is not True
                or "confirmed" not in (location.get("alias_sources") or [])):
            continue
        count = location.get("device_count")
        if not isinstance(count, int) or count < 2:
            continue
        aliases = sorted({alias for alias in location.get("confirmed_aliases") or []
                          if isinstance(alias, str) and re.fullmatch(r"[A-Z]{2,6}", alias)})
        for alias in aliases:
            codes[alias] += count
        if len(aliases) > 1:
            groups.append((count, aliases))

    result = {
        "product_families": [name for name, _ in families.most_common(4)],
        "product_models": [name for name, _ in models.most_common(12)],
        "hospital_codes": [name for name, _ in codes.most_common(20)],
        "same_hospital_codes": [aliases for _, aliases in
                                sorted(groups, key=lambda row: (-row[0], row[1]))[:4]],
    }
    if not result["product_models"] or not result["hospital_codes"]:
        raise ValueError("Private index has no confirmed OCR vocabulary")
    return result

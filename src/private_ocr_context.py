"""Project a small, validated OCR vocabulary from the private device index.

Only confirmed model spellings and confirmed short hospital aliases leave the
index. Serial numbers, task references, contacts, phones and detailed places
are never returned or logged. The projection is advisory, never an answer key.
"""
from __future__ import annotations

import re
from collections import Counter

from . import asana_client, product_catalog

MAX_PRODUCT_MODELS = 32


def build_vocabulary(index: dict) -> dict:
    if (not isinstance(index, dict) or index.get("schema_version") != 3
            or not isinstance(index.get("devices"), list)
            or not isinstance(index.get("location_directory"), list)):
        raise ValueError("A validated private device index is required")

    # Device identity quality must not decide whether a verified word exists.
    # The small verified lexicon also retains rare models absent from this
    # snapshot. Frequency never certifies a previously unknown spelling.
    models = {name for name in asana_client.OCR_PRODUCT_NAMES
              if product_catalog.inspect_product(name)["status"] == "confirmed"}
    for device in index["devices"]:
        if not isinstance(device, dict):
            continue
        for variant in device.get("product_variants") or []:
            assessment = product_catalog.inspect_product(variant)
            if assessment["status"] != "confirmed":
                continue
            models.add(assessment["canonical"])
    if len(models) > MAX_PRODUCT_MODELS:
        # Never silently drop the less frequent alternatives from a prompt.
        raise ValueError("Verified product vocabulary exceeds its review limit")
    families = {asana_client.product_group(model) for model in models} - {None}

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
        "product_families": sorted(families),
        "product_models": sorted(models),
        "hospital_codes": [name for name, _ in codes.most_common(20)],
        "same_hospital_codes": [aliases for _, aliases in
                                sorted(groups, key=lambda row: (-row[0], row[1]))[:4]],
    }
    if not result["product_models"] or not result["hospital_codes"]:
        raise ValueError("Private index has no confirmed OCR vocabulary")
    return result

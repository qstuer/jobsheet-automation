"""Advisory product/serial knowledge, from cleaned private device records only.

No device identifiers or task answers are returned to the vision model. A rare
format is not invalid. Hold out the evaluated device, including all its visits.
"""
from collections import Counter, defaultdict
import re

from . import asana_client, product_catalog

MIN_DEVICES = 3
MAX_COMMON_PATTERNS = 40


def build_knowledge(index: dict, *, excluded_serials=()) -> dict:
    if index.get("product_parser_version") != product_catalog.PARSER_VERSION:
        raise ValueError("Knowledge requires a rebuilt product index, not the legacy snapshot")
    excluded = {asana_client._norm(value) for value in excluded_serials}
    # Merge by serial independently of input rows; repeated tasks never count
    # as independent examples, and a split duplicate row cannot hide conflict.
    entries = defaultdict(list)
    rejected = Counter()
    for row in index.get("devices") or []:
        serial = asana_client._norm(row.get("serial"))
        if serial in excluded:
            continue
        if (row.get("weak_identity") or not re.fullmatch(r"[A-Z]{2,3}[A-Z0-9]{6,9}", serial)
                or not 8 <= len(serial) <= 12 or sum(c.isdigit() for c in serial) < 4):
            rejected["weak_or_invalid_serial"] += 1
            continue
        entries[serial].extend(row.get("task_refs") or [])
    buckets = defaultdict(set)
    models = defaultdict(set)
    for serial, refs in entries.items():
        groups, spellings = set(), set()
        valid = bool(refs)
        for ref in refs:
            info = product_catalog.inspect_product(ref.get("product_variant"))
            if (ref.get("product_parse_status") not in {"confirmed", "group_only"}
                    or info["status"] not in {"confirmed", "group_only"}
                    or asana_client._norm(ref.get("serial")) != serial):
                valid = False
                break
            groups.add(asana_client.product_group(info["canonical"]))
            spellings.add(info["canonical"])
        if not valid or len(groups) != 1:
            rejected["unconfirmed_or_conflicting_product"] += 1
            continue
        group = next(iter(groups))
        prefix = re.match(r"[A-Z]+", serial).group()
        layout = "".join("L" if c.isalpha() else "D" for c in serial)
        buckets[(group, len(serial), prefix, layout)].add(serial)
        for spelling in spellings:
            models[spelling].add(serial)
    common, rare = [], []
    for (group, length, prefix, layout), serials in sorted(buckets.items()):
        pattern = {"product_group": group, "length": length, "prefix": prefix,
                   "layout": layout, "device_count": len(serials), "fixed_letters": {}}
        # Learn fixed LETTER positions only. Never output a complete example
        # serial or fix a digit based on the small training sample.
        if len(serials) >= MIN_DEVICES:
            for position in range(length):
                letters = {s[position] for s in serials}
                if len(letters) == 1 and next(iter(letters)).isalpha():
                    pattern["fixed_letters"][str(position + 1)] = next(iter(letters))
            common.append(pattern)
        else:
            rare.append(pattern)
    common.sort(key=lambda item: (-item["device_count"], item["product_group"], item["prefix"], item["layout"]))
    return {"knowledge_version": 1, "source_generated_at": index.get("generated_at"),
            "minimum_distinct_devices": MIN_DEVICES,
            "models": [{"name": name, "device_count": len(serials)} for name, serials in sorted(models.items())],
            "common_patterns": common[:MAX_COMMON_PATTERNS], "rare_patterns": rare,
            "omitted_common_patterns": max(0, len(common)-MAX_COMMON_PATTERNS),
            "excluded_reason_counts": dict(rejected),
            "advisory_only": True}


def prompt_reference(knowledge: dict) -> dict:
    """Explicit projection: no opaque index text, rare templates or identifiers."""
    if knowledge.get("knowledge_version") != 1 or knowledge.get("advisory_only") is not True:
        raise ValueError("Invalid advisory knowledge")
    # Revalidate even a loaded JSON file; ignore untrusted/free-text keys.
    models = sorted({item["name"] for item in knowledge.get("models") or []
                     if item.get("name") in asana_client.OCR_PRODUCT_NAMES})
    patterns = []
    allowed_groups = {asana_client.product_group(model) for model in models}
    for item in knowledge.get("common_patterns") or []:
        length = item.get("length")
        prefix = item.get("prefix", "")
        layout = item.get("layout", "")
        count = item.get("device_count")
        if (not isinstance(length, int) or not 8 <= length <= 12
                or not isinstance(count, int) or count < MIN_DEVICES
                or not isinstance(prefix, str) or not re.fullmatch(r"[A-Z]{2,3}", prefix)
                or not isinstance(layout, str) or not re.fullmatch(r"[LD]{8,12}", layout)
                or len(layout) != length or layout.count("D") < 4
                or item.get("product_group") not in allowed_groups):
            continue
        fixed = {str(pos): letter for pos, letter in (item.get("fixed_letters") or {}).items()
                 if str(pos).isdigit() and 1 <= int(pos) <= length
                 and layout[int(pos)-1] == "L" and isinstance(letter, str)
                 and re.fullmatch(r"[A-Z]", letter)}
        patterns.append({"product_group": item["product_group"], "length": length,
                         "prefix": prefix, "layout": layout, "fixed_letters": fixed})
        if len(patterns) == MAX_COMMON_PATTERNS:
            break
    return {"known_products": models, "common_serial_formats": patterns}

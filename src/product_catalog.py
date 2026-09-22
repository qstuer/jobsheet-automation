"""Conservative product-field parsing and an offline, private reference table.

Frequency describes usage, not correctness. Only previously confirmed spellings
enter the reference table. This module does not call any external service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from . import asana_client

PARSER_VERSION = 1
_REFERENCE = re.compile(r"\b(?:HAWO|WO|SR|ORDER|ASSET|PHONE|TEL|CONTACT)\b", re.I)
_JOB_TYPE = re.compile(r"^(?:CM|PM|FCO|INS|REPAIR|MAINTENANCE)$", re.I)
_SERIAL = re.compile(r"\b[A-Z]{2,3}[A-Z0-9]{5,9}\b", re.I)


def inspect_product(value: str | None) -> dict:
    """Classify one title segment; never return reference numbers as products."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^(?:PRODUCT|MODEL)\s*[:#]\s*", "", text, flags=re.I)
    def result(status, reason, canonical=None):
        return {"status": status, "reason": reason,
                "variant": text if status != "rejected" else "",
                "canonical": canonical}
    if not text:
        return result("rejected", "empty")
    if _JOB_TYPE.fullmatch(text):
        return result("rejected", "job_type_not_product")
    if _REFERENCE.search(text):
        return result("rejected", "reference_not_product")
    if any(sum(c.isdigit() for c in token) >= 4 for token in _SERIAL.findall(text)):
        return result("rejected", "serial_not_product")
    if re.search(r"\d{4,}", text):
        return result("rejected", "identifier_not_product")
    # Exact confirmed spellings only; fuzzy correction must not certify a new
    # entry in the knowledge table (or turn a common Asana typo into truth).
    key = re.sub(r"[^A-Z0-9+]", "", text.upper())
    canonical = next((p for p in asana_client.OCR_PRODUCT_NAMES
                      if re.sub(r"[^A-Z0-9+]", "", p.upper()) == key), None)
    if key == "EPIQ7PLUS":
        canonical = "EPIQ 7+"
    if canonical and re.fullmatch(r"[A-Za-z0-9+ .-]+", text):
        return result("group_only" if canonical in asana_client.PRODUCT_GROUP_NAMES else "confirmed",
                      "confirmed_spelling", canonical)
    # Model-like unknowns may still be indexed positionally, but are NEVER
    # promoted into vision knowledge. CM10/CM12 must not be stripped as 'CM'.
    if (not re.fullmatch(r"[A-Za-z0-9+ .-]{2,50}", text)
            or not re.search(r"[A-Za-z]{2}", text)):
        return result("rejected", "non_product_text")
    if not re.search(r"\d", text) and not re.search(r"\b(?:PRO|PLUS|ELITE|CVX|XT|IX)\s*$", text, re.I):
        return result("unconfirmed", "text_needs_review")
    return result("unconfirmed", "model_needs_review")


def indexable(assessment: dict) -> bool:
    return assessment["status"] in {"confirmed", "group_only"} or (
        assessment["status"] == "unconfirmed" and assessment["reason"] == "model_needs_review"
    )


def build_catalog(index: dict) -> dict:
    """Count distinct serials, not repeated PM visits; no IDs enter output."""
    confirmed, groups, pending = {}, {}, {}
    rejected = Counter()
    missing_serial = 0
    for row in index.get("devices") or []:
        serial = str(row.get("serial") or "").strip().upper()
        if not serial or row.get("weak_identity"):
            missing_serial += 1
            continue
        for raw in set(row.get("product_variants") or []):
            info = inspect_product(raw)
            if info["status"] == "rejected":
                rejected[info["reason"]] += 1
                continue
            bucket = confirmed if info["status"] == "confirmed" else groups if info["status"] == "group_only" else pending
            key = info["canonical"] or info["variant"]
            entry = bucket.setdefault(key, {"name": key, "variants": set(), "serials": set(), "reason": info["reason"]})
            entry["variants"].add(info["variant"])
            entry["serials"].add(serial)
    def finish(bucket):
        return [{"name": entry["name"], "variants": sorted(entry["variants"]),
                 "device_count": len(entry["serials"]), "reason": entry["reason"]}
                for _, entry in sorted(bucket.items())]
    return {"catalog_version": 1, "product_parser_version": PARSER_VERSION,
            "source_generated_at": index.get("generated_at"),
            "source_product_parser_version": index.get("product_parser_version"),
            "confirmation_basis": "Existing repository-confirmed spellings; counts do not verify device/task identity",
            "source_device_count": len(index.get("devices") or []),
            "confirmed_models": finish(confirmed), "group_only": finish(groups),
            "needs_review": finish(pending), "rejected_counts": dict(sorted(rejected.items())),
            "weak_rows_excluded": missing_serial,
            "notice": "Local snapshot only; frequency is not confirmation. No serials, tasks or orders included."}


def write_catalog(catalog: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "product-catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 私人產品小表", "", "只供本機覆核，尚未接入模型提示或正式索引。頻率不等於正確。",
             "已確認指repo原有型號／寫法清單，不代表每部設備都重新核實。來源為本機快照，不是最新Asana。",
             f"來源產品解析版本：{catalog.get('source_product_parser_version') or '舊版，尚未完整重建'}。", ""]
    for key, heading in (("confirmed_models", "已有確認依據的型號"),
                         ("group_only", "只到大類，不補造型號"), ("needs_review", "待核對，不供模型作答案")):
        lines += [f"## {heading}", "", "| 名稱 | 已見寫法 | 不同設備數 |", "|---|---|---:|"]
        for row in catalog[key]:
            lines.append(f"| {row['name']} | {' ; '.join(row['variants'])} | {row['device_count']} |")
        lines.append("")
    lines += ["## 排除統計", "", *[f"- {reason}: {count}" for reason, count in catalog["rejected_counts"].items()], ""]
    (output_dir / "PRODUCT_CATALOG.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a local private product review table; no network calls")
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    private_root = Path(__file__).resolve().parents[1] / "tmp"
    if not args.output_dir.resolve().is_relative_to(private_root.resolve()):
        parser.error("Product review outputs must stay inside the ignored local tmp directory")
    source = args.index.read_bytes()
    payload = json.loads(source)
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        parser.error("Invalid device index")
    catalog = build_catalog(payload)
    catalog["source_sha256"] = hashlib.sha256(source).hexdigest()
    write_catalog(catalog, args.output_dir)
    print(json.dumps({"confirmed_models": len(catalog["confirmed_models"]),
                      "group_only": len(catalog["group_only"]),
                      "needs_review": len(catalog["needs_review"]),
                      "rejected_counts": catalog["rejected_counts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

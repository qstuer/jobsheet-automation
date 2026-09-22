"""Local-only paired OCR experiment; no Asana matching or cloud mutations."""
import argparse
import base64
import hashlib
import json
from pathlib import Path

import fitz

from . import config, nvidia_client, vision_knowledge


def validate_holdout(knowledge: dict, pdf_digest: str) -> None:
    if knowledge.get("held_out_pdf_sha256") != pdf_digest or knowledge.get("device_holdout_applied") is not True:
        raise ValueError("Knowledge must be prepared with this PDF's device and all its visits held out")


def compare(doc, knowledge: dict, *, guided_first=False) -> dict:
    image = nvidia_client.crop_jobsheet_field_card(
        doc, 0, ("product_raw", "serial_candidates", "hospital_raw"),
        zoom=config.OCR_IDENTITY_ZOOM, strong=True)
    result = {"image_sha256": hashlib.sha256(base64.b64decode(image)).hexdigest(),
              "provider": config.OCR_PROVIDER, "model": nvidia_client.current_model_name(),
              "guided_first": guided_first, "arms": {}}
    for name in (("guided", "baseline") if guided_first else ("baseline", "guided")):
        nvidia_client.reset_ocr_metrics()
        try:
            reading = nvidia_client.read_joint_identity_image(image, knowledge if name == "guided" else None)
            result["arms"][name] = {"status": "read", "reading": reading}
        except Exception as exc:
            # An HTTP exception can contain request data. Persist the class,
            # never its free-text message in the report or public console.
            result["arms"][name] = {"status": "failed", "error_type": type(exc).__name__}
        result["arms"][name]["metrics"] = nvidia_client.get_ocr_metrics()
    result["both_read"] = all(arm["status"] == "read" for arm in result["arms"].values())
    result["accuracy"] = "NOT_SCORED"  # successful API calls do not prove OCR correctness
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="One PDF, two OCR calls, local output only")
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--knowledge", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--guided-first", action="store_true")
    args = parser.parse_args()
    private = Path(__file__).resolve().parents[1] / "tmp"
    if not args.output.resolve().is_relative_to(private.resolve()):
        parser.error("Output must remain in ignored local tmp")
    if args.output.exists():
        parser.error("Output already exists; keep earlier experiment evidence")
    knowledge = json.loads(args.knowledge.read_text(encoding="utf-8"))
    vision_knowledge.prompt_reference(knowledge)
    pdf_digest = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    try:
        validate_holdout(knowledge, pdf_digest)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.allow_model_calls:
        print("READY_NOT_RUN: model calls require --allow-model-calls")
        return 0
    if config.OCR_PROVIDER not in {"nvidia", "deepseek"}:
        parser.error("Unsupported configured OCR provider")
    key = config.DEEPSEEK_API_KEY if config.OCR_PROVIDER == "deepseek" else config.NVIDIA_API_KEY
    if not key:
        parser.error("Configured provider key is unavailable locally; no calls made")
    with fitz.open(args.pdf) as doc:
        if doc.page_count < 1:
            parser.error("PDF is empty")
        result = compare(doc, knowledge, guided_first=args.guided_first)
    result["pdf_sha256"] = pdf_digest
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"both_read": result["both_read"], "accuracy": result["accuracy"],
                      "calls": sum(arm["metrics"]["calls"] for arm in result["arms"].values())}))
    return 0 if result["both_read"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

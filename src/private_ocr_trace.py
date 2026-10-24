"""Explicit local-only OCR recording; never imported by production entrypoints.

Records the actual image, exact reply, parser result and normalization rejection
separately. It observes existing readers with temporary patches; it does not
feed recorded/rejected values back into matching or provide sample answers.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import inspect
import json
import os
import re
import time
from contextlib import ExitStack, contextmanager
from datetime import date
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import asana_client, asana_index, config, matching_rules, nvidia_client as vision

PRIVATE_ROOT = Path(__file__).resolve().parents[1] / "tmp" / "ocr-traces"


def private_output(path: Path, root: Path = PRIVATE_ROOT) -> Path:
    """Reject outputs outside ignored tmp, existing runs and symlink escapes."""
    root = root.resolve()
    output = path.resolve()
    if not output.is_relative_to(root) or output == root:
        raise ValueError("OCR trace must be a new run below the private tmp root")
    if output.exists():
        raise FileExistsError("Existing OCR evidence must not be overwritten")
    return output


class Capture:
    """Observe a synchronous diagnostic run without changing OCR decisions."""

    def __init__(self, output: Path, *, root: Path = PRIVATE_ROOT, recipient_public_key=None):
        # Raw replies must never become Actions logs or unencrypted artifacts.
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true" and not recipient_public_key:
            raise ValueError("Unencrypted OCR capture is local-only")
        self.recipient = None
        if recipient_public_key:
            from .private_trace_crypto import load_recipient
            self.recipient = load_recipient(recipient_public_key)
        self.output = private_output(output, root)
        self.context = {}
        self.call_id = 0
        self.events = None
        self.stack = None
        self.event_number = 0

    def emit(self, kind, **data):
        self.event_number += 1
        value = json.dumps({"event": kind, "call_id": self.call_id,
            "context": self.context, **data}, ensure_ascii=False).encode("utf-8")
        if self.recipient:
            from .private_trace_crypto import seal
            value = seal(value, self.recipient, f"event-{self.event_number}")
        self.events.write(value + b"\n")
        self.events.flush()  # Retain earlier evidence even if a later call fails.

    @contextmanager
    def reading(self, context):
        previous = self.context
        self.context = context
        try:
            yield
        finally:
            self.context = previous

    def wrap_reader(self, function):
        signature = inspect.signature(function)
        @wraps(function)
        def observed(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            context = {"reader": function.__name__}
            for field in ("page_idx", "zoom", "field"):
                if field in bound.arguments:
                    context[field] = bound.arguments[field]
            if function.__name__ == "_checklist_read":
                context["page_idx"] = 1
            with self.reading(context):
                try:
                    result = function(*args, **kwargs)
                except Exception as exc:
                    self.emit("reader_error", error_type=type(exc).__name__)
                    raise
                self.emit("reading", value=result)
                return result
        return observed

    def client_proxy(self, client):
        def create(**request):
            if request.get("stream"):
                raise ValueError("Streaming OCR capture is not supported")
            self.call_id += 1
            number = f"call-{self.call_id:03d}"
            # Whitelist request metadata; never serialize SDK clients, headers,
            # API keys, response objects, exception bodies or reasoning content.
            metadata = {key: request[key] for key in (
                "model", "max_tokens", "temperature", "seed", "response_format", "timeout")
                if key in request}
            texts, images = [], []
            for message in request.get("messages", []):
                for part in message.get("content", []):
                    if part.get("type") == "text":
                        texts.append(part["text"])
                    elif part.get("type") == "image_url":
                        url = part["image_url"]["url"]
                        match = re.fullmatch(r"data:image/(jpeg|png);base64,(.+)", url)
                        if not match:
                            raise ValueError("Only inline JPEG/PNG images may be recorded")
                        image = base64.b64decode(match[2], validate=True)
                        filename = f"{number}-image-{len(images)+1}.{match[1]}"
                        if self.recipient:
                            from .private_trace_crypto import seal
                            (self.output / (filename + ".enc")).write_bytes(seal(image, self.recipient, filename))
                        else:
                            (self.output / filename).write_bytes(image)
                        images.append({"file": filename, "sha256": hashlib.sha256(image).hexdigest()})
            self.emit("request", provider=config.OCR_PROVIDER, parameters=metadata,
                      prompts=texts, images=images)
            started = time.monotonic()
            try:
                response = client.chat.completions.create(**request)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                self.emit("request_error", error_type=type(exc).__name__,
                          status_code=status if isinstance(status, int) else None,
                          seconds=time.monotonic()-started)
                raise
            choices = getattr(response, "choices", None) or []
            choice = choices[0] if choices else None
            content = getattr(getattr(choice, "message", None), "content", None)
            usage = getattr(response, "usage", None)
            self.emit("response", content=content if isinstance(content, str) else None,
                      finish_reason=getattr(choice, "finish_reason", None),
                      usage={key: value for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                             if isinstance(value := getattr(usage, key, None), int)},
                      seconds=time.monotonic()-started)
            return response  # The original parser receives the original object.
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def __enter__(self):
        self.output.mkdir(parents=True, exist_ok=False)
        name = "events.encrypted.jsonl" if self.recipient else "events.private.jsonl"
        self.events = (self.output / name).open("xb")
        self.stack = ExitStack()
        transport = vision._transport_event
        def observed_transport(kind, **data):
            self.emit("transport", category=kind, **data)
            return transport(kind, **data)
        self.stack.enter_context(patch.object(vision, "_transport_event", side_effect=observed_transport))
        get_client = vision.get_client
        self.stack.enter_context(patch.object(vision, "get_client", side_effect=lambda: self.client_proxy(get_client())))
        for name in ("_parse_json_object", "_normalize_ocr_data"):
            original = getattr(vision, name)
            def observed(*args, _fn=original, _name=name, **kwargs):
                try:
                    result = _fn(*args, **kwargs)
                except Exception as exc:
                    self.emit(_name + "_error", error_type=type(exc).__name__)
                    raise
                self.emit(_name, value=result)
                return result
            self.stack.enter_context(patch.object(vision, name, side_effect=observed))
        for name in ("ocr_jobsheet_fields", "ocr_jobsheet_identity_fields",
                     "ocr_jobsheet_support_fields", "ocr_jobsheet_context_field",
                     "ocr_jobsheet_focused_field", "ocr_jobsheet_signature_date", "ocr_jobsheet_serial_with_shapes",
                     "ocr_pm_checklist_focused_date"):
            self.stack.enter_context(patch.object(vision, name, self.wrap_reader(getattr(vision, name))))
        self.stack.enter_context(patch.object(matching_rules, "_checklist_read",
                                             self.wrap_reader(matching_rules._checklist_read)))
        evidence = matching_rules.evidence
        def observed_evidence(readings):
            result = evidence(readings)
            self.emit("matching_evidence", value=result)
            return result
        self.stack.enter_context(patch.object(matching_rules, "evidence", side_effect=observed_evidence))
        live_pool = matching_rules.live_pool
        def observed_pool(*args, **kwargs):
            result = live_pool(*args, **kwargs)
            self.emit("live_pool", tasks=result[0], reason=result[1])
            return result
        self.stack.enter_context(patch.object(matching_rules, "live_pool", side_effect=observed_pool))
        typeahead = asana_client._typeahead
        def observed_typeahead(*args, **kwargs):
            result = typeahead(*args, **kwargs)
            self.emit("asana_typeahead", query=args[0] if args else kwargs.get("query"), tasks=result)
            return result  # Private evidence only, never a new OCR vote.
        self.stack.enter_context(patch.object(asana_client, "_typeahead", side_effect=observed_typeahead))
        select_task = matching_rules.select_task
        def observed_selection(*args, **kwargs):
            result = select_task(*args, **kwargs)
            self.emit("selection", task_gid=result[0].get("gid") if result[0] else None,
                      reason=result[1])
            return result
        self.stack.enter_context(patch.object(matching_rules, "select_task", side_effect=observed_selection))
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stack.close()
        self.events.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--index-manifest", required=True, type=Path)
    parser.add_argument("--job-type", required=True, choices=("CM", "PM"))
    parser.add_argument("--reference-date", type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--live-asana", action="store_true")
    parser.add_argument("--recipient-public-key", type=Path)
    args = parser.parse_args(argv)
    private_output(args.output)
    asana_client.set_device_index(asana_index.load_index(args.index, args.index_manifest))
    import fitz
    with fitz.open(args.pdf) as document:
        if len(document) != (4 if args.job_type == "PM" else 1):
            raise ValueError("Incomplete jobsheet cannot enter OCR diagnosis")
        provider, _, key, _, _, _ = vision._provider_settings()
        readiness = {"mode": "live_read_only" if args.live_asana else "ocr_only",
            "provider": provider, "model_key_available": bool(key.strip()),
            "asana_key_available": bool(config.ASANA_TOKEN.strip()),
            "source_sha256": hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
            "page_count": len(document), "historical_replay": False,
            "onedrive_writes": 0, "drive_writes": 0}
        if not args.allow_model_calls:
            print(json.dumps({"status": "PREPARED_NOT_RUN", **readiness}))
            return 0
        if not key.strip() or args.live_asana and not config.ASANA_TOKEN.strip():
            print(json.dumps({"status": "BLOCKED_MISSING_KEY", **readiness}))
            return 2
        vision.reset_model_availability()
        recipient = args.recipient_public_key.read_bytes() if args.recipient_public_key else None
        with Capture(args.output, recipient_public_key=recipient) as capture, ExitStack() as guards:
            if not args.live_asana:
                # This arm measures OCR only, not actual Asana matching accuracy.
                guards.enter_context(patch.object(matching_rules, "live_pool", return_value=([], "live_tasks_read")))
            capture.emit("run", **readiness, reference_date=args.reference_date.isoformat() if args.reference_date else None,
                         index_sha256=hashlib.sha256(args.index.read_bytes()).hexdigest(),
                         code_sha=os.environ.get("GITHUB_SHA"))
            try:
                task, confidence, ocr = matching_rules.read_and_match(document, args.job_type, args.reference_date)
            except Exception as exc:
                capture.emit("run_error", error_type=type(exc).__name__)
                print(json.dumps({"status": "FAILED_EVIDENCE_RETAINED", **readiness,
                                  "error_type": type(exc).__name__}))
                return 3
            from .processor import _planned_filename
            capture.emit("final", selected_task_gid=task.get("gid") if task else None,
                         planned_filename=_planned_filename(task)[0] if task else None,
                         confidence=confidence, ocr=ocr)
        print(json.dumps({"status": "RECORDED_NOT_SCORED", **readiness,
                          "calls": ocr.get("ocr_metrics", {}).get("calls")}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

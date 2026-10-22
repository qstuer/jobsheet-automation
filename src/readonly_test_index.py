"""Build a disposable runner-local index, never publish the official index."""
import base64
import hashlib
import json
import logging
import os
from pathlib import Path

from . import asana_index
from .private_ocr_trace import PRIVATE_ROOT, private_output
from .private_trace_crypto import load_recipient, seal

TEST_REF = "refs/heads/codex/parser-ingress-fix-20261013"


def build(output_dir, public_key):
    if os.environ.get("GITHUB_REF") != TEST_REF or os.environ.get("JOBSHEET_DRY_RUN") != "1":
        raise ValueError("Disposable index requires the read-only test branch")
    recipient = load_recipient(public_key)  # Reject bad keys before any Asana access.
    destination = private_output(Path(output_dir))
    index = asana_index.build_from_asana()  # Read only; no existing parsed index reused.
    asana_index.write_outputs(index, destination)
    index_file = destination / "asana-device-index.json"
    manifest_file = destination / "asana-device-index-manifest.json"
    # A public artifact must never contain the plaintext index or CSVs.
    for path in (index_file, manifest_file):
        with path.with_suffix(path.suffix + ".enc").open("xb") as stream:
            stream.write(seal(path.read_bytes(), recipient, path.name))
    return index_file, manifest_file


def main():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        public_key = base64.b64decode(os.environ["OCR_TRACE_RECIPIENT_KEY"], validate=True)
        index, _ = build(PRIVATE_ROOT / "test-index", public_key)
        print(json.dumps({"status": "DISPOSABLE_TEST_INDEX_READY",
                          "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
                          "drive_writes": 0, "onedrive_writes": 0}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "DISPOSABLE_INDEX_STOPPED", "error_type": type(exc).__name__,
                          "drive_writes": 0, "onedrive_writes": 0}))
        return 3
    finally:
        logging.disable(previous)


if __name__ == "__main__":
    raise SystemExit(main())

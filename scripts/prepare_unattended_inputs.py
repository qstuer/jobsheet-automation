"""Download read-only exports; no business-cloud write credentials are needed."""
import gzip
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import requests

def main():
    spec = json.loads(os.environ["JOBSHEET_SANDBOX_INPUTS"])
    base = Path(os.environ["SANDBOX_PREP"])
    base.mkdir(parents=True, exist_ok=True)
    for key, filename in (("source_url", "source.pdf"), ("index_url", "index.json.gz"),
                          ("manifest_url", "manifest.json")):
        parsed = urlsplit(spec[key])
        assert parsed.scheme == "https" and parsed.hostname.endswith(".oaiusercontent.com")
        response = requests.get(spec[key], timeout=45)
        assert response.status_code == 200, "Read-only input export unavailable"
        (base / filename).write_bytes(response.content)
    assert hashlib.sha256((base / "source.pdf").read_bytes()).hexdigest() == spec["source_sha256"]
    data = gzip.decompress((base / "index.json.gz").read_bytes())
    assert hashlib.sha256(data).hexdigest() == "63cf119a871b2dccdc304d114f9549164a342b44a5dab24e66654410eb2336ba"
    (base / "index.json").write_bytes(data)
    (base / "context.json").write_text(json.dumps({"source_created_at": spec.get("source_created_at")}), encoding="utf-8")
    print("UNATTENDED_INPUTS_READY", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("UNATTENDED_PREPARATION_FAILURE " + type(exc).__name__, flush=True)
        raise SystemExit(1) from None

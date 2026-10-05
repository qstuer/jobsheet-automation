"""Private-PDF-keyed receipts for public, read-only Jobsheet runs.

The repository and Actions logs are public.  A receipt lets an operator compare
the selected Asana task and intended filename with independently verified
answers, without publishing either value.  The PDF itself is never uploaded by
this module; its SHA-256 is only used locally as the HMAC key.
"""

import argparse
import hashlib
import hmac
from pathlib import Path


def receipt(pdf: Path, *, task_gid: str, filename: str) -> dict[str, str]:
    key = hashlib.sha256(pdf.read_bytes()).digest()

    def tag(label: str, value: str) -> str:
        message = f"jobsheet-selection-v1\0{label}\0{value}".encode("utf-8")
        return hmac.new(key, message, hashlib.sha256).hexdigest()[:24]

    return {
        "task": tag("asana-task-gid", task_gid),
        "filename": tag("planned-filename", filename),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare private Jobsheet selection receipts")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--task-gid", required=True)
    parser.add_argument("--filename", required=True)
    args = parser.parse_args()
    result = receipt(args.pdf, task_gid=args.task_gid, filename=args.filename)
    print(f"task={result['task']} filename={result['filename']}")


if __name__ == "__main__":
    main()

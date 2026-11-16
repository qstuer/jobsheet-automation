"""Cold SDK metadata diagnostic: no papers, tokens, or network requests."""
import json
from pathlib import Path
import subprocess
import traceback
from unittest.mock import patch

import httpx
from openai import OpenAI
import requests

probes, network = [], []


def no_process(*args, **kwargs):
    command = args[0] if args else kwargs.get("args", [])
    probes.append({"program": Path(command[0]).name if isinstance(command, (list, tuple)) else "string",
        "arguments": list(command[1:]) if isinstance(command, (list, tuple)) else [],
        "stack": [Path(f.filename).name + ":" + f.name for f in traceback.extract_stack()[-12:-1]]})
    raise AssertionError("External subprocess blocked")


def no_network(*args, **kwargs):
    network.append(True)
    raise AssertionError("Network disabled in metadata diagnostic")


with patch.object(subprocess, "Popen", side_effect=no_process), \
        patch.object(httpx.Client, "send", side_effect=no_network), \
        patch.object(requests.sessions.Session, "send", side_effect=no_network), \
        patch("urllib.request.urlopen", side_effect=no_network):
    with OpenAI(api_key="synthetic-not-a-credential") as client:
        headers = client.default_headers
        sdk_os = headers.get("X-Stainless-OS")

result = {"intercepted_processes": probes, "network_attempts": len(network),
          "sdk_os_header": sdk_os, "customer_papers_used": 0}
print("GUARD_DIAGNOSTIC_RESULT " + json.dumps(result), flush=True)
assert len(probes) == 1 and probes[0]["program"] == "uname" and probes[0]["arguments"] == ["-p"]
assert not network and sdk_os == "Unknown"

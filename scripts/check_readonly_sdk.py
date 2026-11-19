"""Real SDK header/OS metadata smoke check; no credentials or network calls."""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
import openai._base_client as sdk
from scripts.readonly_guard import ReadOnlyGuard

counters={}
guard=ReadOnlyGuard(counters,vision_host='integrate.api.nvidia.com')
with patch('subprocess.Popen',side_effect=guard.popen), \
     patch.object(httpx.Client,'send',side_effect=AssertionError('No network in SDK smoke check')):
    platform=sdk.get_platform()
guard.assert_clean()
assert platform!='Unknown'
print(json.dumps({'platform_detected':True,'network_calls':0,'guard':counters}))

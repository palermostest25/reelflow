"""Non-secret installation fingerprint and authenticated startup smoke test."""
import hashlib
import json
import urllib.request
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config as c

c.validate_runtime()
state = json.loads((c.DATA / '.runtime.json').read_text())
fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
request = urllib.request.Request('http://127.0.0.1:8080/api/login',
    data=json.dumps({'password': c.PASSWORD}).encode(), headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=10) as response:
    assert response.status == 200
print(fingerprint)

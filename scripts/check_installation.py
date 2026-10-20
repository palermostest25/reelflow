"""CI smoke test: readiness, both processes, authenticated login and stable credentials."""
import hashlib
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config as c

c.validate_runtime()
base='http://127.0.0.1:8080'
for attempt in range(90):
    try:
        with urllib.request.urlopen(base+'/healthz',timeout=2) as response:
            ready=json.load(response)
        if ready.get('status')=='ok' and ready.get('worker_alive'):
            break
    except (urllib.error.URLError,ConnectionError,TimeoutError):
        pass
    time.sleep(.5)
else:
    raise SystemExit('The web UI and posting worker did not become ready.')
state=json.loads((c.DATA/'.runtime.json').read_text())
fingerprint=hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()
client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
request=urllib.request.Request(base+'/api/login',data=json.dumps({'password':c.PASSWORD}).encode(),headers={'Content-Type':'application/json'})
with client.open(request,timeout=10) as response:
    assert response.status==200
with client.open(base+'/api/state',timeout=10) as response:
    assert response.status==200 and json.load(response)['csrf']
print(fingerprint)
